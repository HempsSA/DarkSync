#!/usr/bin/env python3
"""DarkSync 2.0 Multi-Job Edition.
Independent Python/PySide6 folder comparison, synchronization, scheduling, history,
results, and undo/recovery utility.

Changes included:
- Removed two-week dashboard calendar/history.
- Generated report files include only Failed, Cancelled, Not selected, and Conflict.
- Skipped entries are excluded from generated report files.
- Removed scrolling/indeterminate blue progress bar during scanning.
- Scanning left/right labels remain visible side by side.
- Normal determinate progress bar still appears for compare, sync, and recovery.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import platform
import queue
import shutil
import smtplib
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QThread,
    Qt,
    Signal,
    QTimer,
    QTime,
    QUrl,
)
from PySide6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QShortcut, QKeySequence, QPalette, QBrush
from PySide6.QtWidgets import *

qApp = QApplication.instance()  # Global reference for exit calls


# Application constants - can be overridden via environment variables or config file
APP = "DarkSync"
VERSION = "2.6.5"

RUN_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent

JOBS_FILE = RUN_DIR / "darksync_jobs.json"
HISTORY_FILE = RUN_DIR / "darksync_history.json"
LOGS = RUN_DIR / "logs"
UNDO_ROOT = RUN_DIR / ".darksync_undo"
GUARD_ROOT = RUN_DIR / ".darksync_guard"

# Configurable constants (can be adjusted via environment variables)
UNDO_RETENTION_DAYS = int(os.environ.get("DARKSYNC_UNDO_RETENTION_DAYS", "5"))
BLOCK_SIZE_BYTES = int(os.environ.get("DARKSYNC_BLOCK_SIZE_BYTES", str(4 * 1024 * 1024)))  # 4MB default
MAX_WORKERS = int(os.environ.get("DARKSYNC_MAX_WORKERS", "32"))
MIN_WORKERS = int(os.environ.get("DARKSYNC_MIN_WORKERS", "1"))
HISTORY_MAX_ENTRIES = int(os.environ.get("DARKSYNC_HISTORY_MAX_ENTRIES", "5000"))
GUARD_DEFAULT_THRESHOLD_PERCENT = float(os.environ.get("DARKSYNC_GUARD_THRESHOLD_PERCENT", "4.0"))

# Generated report files include only these statuses.
# Skipped is intentionally excluded.
REPORT_STATUSES = {
    "Failed",
    "Cancelled",
    "Not selected",
    "Conflict",
}


class Status(str, Enum):
    EQUAL = "Equal"
    LEFT = "Left only"
    RIGHT = "Right only"
    LNEW = "Left newer"
    RNEW = "Right newer"
    DIFF = "Different"
    ERROR = "Error"


@dataclass(frozen=True)
class Info:
    rel: str
    path: str
    size: int
    mtime_ns: int


@dataclass
class Item:
    rel: str
    left: Optional[Info]
    right: Optional[Info]
    status: Status
    action: str = "Skip"
    selected: bool = True
    note: str = ""


@dataclass
class NotifyConfig:
    # Backup-monitoring defaults: no success noise, alert on failures/issues via ntfy.
    enabled: bool = False
    on_success: bool = False
    on_failure: bool = True

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = ""
    mail_to: str = ""

    ntfy_enabled: bool = True
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = "PA_Backups"
    ntfy_token: str = ""
    ntfy_priority: str = "high"


@dataclass
class Job:
    """Represents a synchronization job with validation."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "New Job"
    source: str = ""
    destination: str = ""
    mode: str = "Mirror"
    compare: str = "Time and size"
    workers: int = field(default_factory=lambda: max(2, min(16, os.cpu_count() or 4)))
    tolerance: int = 2
    include: str = "*"
    exclude: str = ".DS_Store;Thumbs.db;.darksync_*;logs/*;$Recycle.Bin;System Volume Information;*.tmp;*.temp;pagefile.sys;hiberfil.sys"
    follow_links: bool = False
    preserve_times: bool = True
    verify: bool = False
    deletion: str = "Recycle bin"
    enabled: bool = True
    scheduler_enabled: bool = False
    scheduler_time: str = "02:00"
    scheduler_action: str = "Compare and synchronize"
    last_schedule_date: str = ""
    template: str = "Custom"
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    last_run: str = ""
    last_result: str = "Never run"
    last_report: str = ""
    guard_enabled: bool = True
    guard_threshold_percent: float = 4.0
    ignore_scan_errors: bool = False
    ignore_permission_errors: bool = False
    exit_on_completion: bool = False  # For automated daily runs
    
    def __post_init__(self):
        """Validate job configuration after initialization."""
        if self.source and self.destination:
            src = Path(self.source).resolve()
            dst = Path(self.destination).resolve()
            
            if src == dst:
                raise ValueError("Source and destination cannot be the same path")
            
            if src in dst.parents:
                raise ValueError("Destination cannot be inside source directory")
            
            if dst in src.parents:
                raise ValueError("Source cannot be inside destination directory")
        
        if self.workers < 1:
            object.__setattr__(self, 'workers', 1)
        elif self.workers > 32:
            object.__setattr__(self, 'workers', 32)
        
        if self.guard_threshold_percent < 0:
            object.__setattr__(self, 'guard_threshold_percent', 0.0)
        elif self.guard_threshold_percent > 100:
            object.__setattr__(self, 'guard_threshold_percent', 100.0)


@dataclass
class AppState:
    active_job_id: str = ""
    max_parallel_jobs: int = 1
    jobs: List[Job] = field(default_factory=list)
    templates: List[Job] = field(default_factory=list)


class EnhancedJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if hasattr(obj, "__dataclass_fields__"):
            return asdict(obj)
        return super().default(obj)


def job_from_dict(d: dict) -> Job:
    nd = dict(d)

    notify = nd.get("notify", {}) or {}
    notify_allowed = set(NotifyConfig.__dataclass_fields__.keys())
    notify = {k: v for k, v in notify.items() if k in notify_allowed}
    nd["notify"] = NotifyConfig(**notify)

    job_allowed = set(Job.__dataclass_fields__.keys())
    nd = {k: v for k, v in nd.items() if k in job_allowed}

    return Job(**nd)


def state_from_disk() -> AppState:
    if not JOBS_FILE.exists():
        state = AppState()
        state.templates = [
            Job(name="Mirror Backup Template", template="Template", mode="Mirror"),
            Job(name="Update Archive Template", template="Template", mode="Update"),
        ]
        save_state(state)
        return state

    data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))

    state = AppState(
        active_job_id=data.get("active_job_id", ""),
        max_parallel_jobs=int(data.get("max_parallel_jobs", 1)),
    )

    state.jobs = [job_from_dict(x) for x in data.get("jobs", [])]
    state.templates = [job_from_dict(x) for x in data.get("templates", [])]

    return state


def save_state(state: AppState):
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_FILE.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")


def history_data():
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def add_history(
    job: Job,
    operation: str,
    result: str,
    items: int,
    duration: float,
    report: str = "",
    details: str = "",
    bytes_changed: int = 0,
    files_changed: int = 0,
    folders_changed: int = 0,
):
    """Add an entry to the history log.
    
    Args:
        job: The job that was executed
        operation: Type of operation (compare, sync, etc.)
        result: Outcome of the operation
        items: Number of items processed
        duration: Duration in seconds
        report: Optional report data
        details: Additional details
        bytes_changed: Total bytes changed
        files_changed: Number of files changed
        folders_changed: Number of folders changed
    """
    rows = history_data()

    rows.append(
        {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "job": job.name,
            "job_id": job.id,
            "operation": operation,
            "result": result,
            "mode": job.mode,
            "source": job.source,
            "destination": job.destination,
            "items": items,
            "duration": round(duration, 1),
            "report": report,
            "details": details,
            "bytes_changed": bytes_changed,
            "files_changed": files_changed,
            "folders_changed": folders_changed,
        }
    )

    # Keep only the most recent entries to prevent unbounded growth
    HISTORY_FILE.write_text(json.dumps(rows[-HISTORY_MAX_ENTRIES:], indent=2), encoding="utf-8")


def patterns(s: str) -> List[str]:
    return [x.strip().replace("\\", "/") for x in s.replace(",", ";").split(";") if x.strip()]


def allowed(rel: str, job: Job) -> bool:
    rel = rel.replace("\\", "/")
    name = Path(rel).name

    inc = patterns(job.include) or ["*"]
    exc = patterns(job.exclude)

    match = lambda p: fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p)

    return any(match(p) for p in inc) and not any(match(p) for p in exc)


def hsize(n: int) -> str:
    v = float(n)

    for u in ["B", "KB", "MB", "GB", "TB"]:
        if v < 1024 or u == "TB":
            return f"{int(v)} {u}" if u == "B" else f"{v:.1f} {u}"
        v /= 1024

    return f"{n} B"


def mb_text(n: int) -> str:
    return f"{int(n or 0) / (1024 * 1024):,.2f} MB"


def folder_count_from_rows(rows):
    folders = set()

    for row in rows:
        rel = row.get("relative", "").replace("\\", "/").strip("/")
        parent = str(Path(rel).parent).replace("\\", "/")

        if parent not in ("", "."):
            folders.add(parent)

    return len(folders)


def wait_if_paused(cancel: threading.Event, pause: Optional[threading.Event] = None):
    while pause is not None and pause.is_set():
        if cancel.is_set():
            raise InterruptedError
        time.sleep(0.1)


def digest(path: Path, cancel: threading.Event) -> str:
    """Calculate SHA256 hash of a file.
    
    Args:
        path: File path to hash
        cancel: Cancellation event
        
    Returns:
        Hexadecimal digest string
        
    Raises:
        InterruptedError: If operation was cancelled
        IOError: If file cannot be read
    """
    h = hashlib.sha256()
    
    try:
        with open(path, "rb") as f:
            while True:
                if cancel.is_set():
                    raise InterruptedError("Digest calculation cancelled")
                
                b = f.read(BLOCK_SIZE_BYTES)
                
                if not b:
                    return h.hexdigest()
                
                h.update(b)
    except FileNotFoundError:
        raise IOError(f"File not found: {path}")
    except PermissionError:
        raise IOError(f"Permission denied: {path}")
    except Exception as e:
        raise IOError(f"Error reading {path}: {e}")


def should_ignore_scan_error(job: Job, ex: BaseException) -> bool:
    if getattr(job, "ignore_scan_errors", False):
        return True

    if getattr(job, "ignore_permission_errors", False) and isinstance(ex, PermissionError):
        return True

    return False


def scan(
    root: str,
    job: Job,
    cancel: threading.Event,
    progress=None,
    side: str = "",
    pause: Optional[threading.Event] = None,
):
    rootp = Path(root).expanduser().resolve()

    out = {}
    folders = set()
    errors = []

    q = queue.Queue()
    q.put(rootp)

    if not rootp.is_dir():
        raise FileNotFoundError(f"{side} folder missing: {rootp}")

    def one(d: Path):
        dirs = []
        files = []
        errs = []

        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if cancel.is_set():
                            break

                        wait_if_paused(cancel, pause)

                        p = Path(e.path)
                        rel = p.relative_to(rootp).as_posix()

                        if e.is_symlink() and not job.follow_links:
                            continue

                        if e.is_dir(follow_symlinks=job.follow_links):
                            dirs.append((p, rel))
                        elif e.is_file(follow_symlinks=job.follow_links) and allowed(rel, job):
                            st = e.stat(follow_symlinks=job.follow_links)
                            files.append(Info(rel, str(p), st.st_size, st.st_mtime_ns))
                    except Exception as ex:
                        if should_ignore_scan_error(job, ex):
                            continue
                        errs.append(f"{e.path}: {ex}")
        except Exception as ex:
            if not should_ignore_scan_error(job, ex):
                errs.append(f"{d}: {ex}")

        return dirs, files, errs

    with ThreadPoolExecutor(max_workers=job.workers) as pool:
        futures = {}

        while (not q.empty() or futures) and not cancel.is_set():
            wait_if_paused(cancel, pause)

            while not q.empty() and len(futures) < job.workers * 3:
                d = q.get()
                futures[pool.submit(one, d)] = d

            done = [f for f in futures if f.done()]

            if not done:
                time.sleep(0.02)
                continue

            for f in done:
                futures.pop(f)
                ds, fs, es = f.result()

                for d, rel in ds:
                    q.put(d)
                    folders.add(rel)

                out.update({x.rel: x for x in fs})
                errors.extend(es)

            if progress:
                progress(len(out), side)

        if cancel.is_set():
            raise InterruptedError

    return out, folders, errors


def guard_db_path(job: Job) -> Path:
    return GUARD_ROOT / f"{job.id}.sqlite3"


def guard_snapshot_from_scan(files: Dict[str, Info], folders):
    return {r: (int(v.size), int(v.mtime_ns)) for r, v in files.items()}, set(folders)


def guard_load_baseline(job: Job):
    db = guard_db_path(job)

    if not db.exists():
        return None

    con = None

    try:
        con = sqlite3.connect(db)

        files = {
            r[0]: (int(r[1]), int(r[2]))
            for r in con.execute("SELECT path, size, mtime_ns FROM files")
        }

        folders = {r[0] for r in con.execute("SELECT path FROM folders")}
        row = con.execute("SELECT value FROM metadata WHERE key='created'").fetchone()

        return files, folders, (row[0] if row else "")
    finally:
        if con is not None:
            con.close()


def guard_save_baseline(job: Job, files, folders):
    GUARD_ROOT.mkdir(parents=True, exist_ok=True)

    db = guard_db_path(job)
    tmp = db.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)

    con = None

    try:
        con = sqlite3.connect(tmp)

        con.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        con.execute("CREATE TABLE files (path TEXT PRIMARY KEY, size TEXT NOT NULL, mtime_ns TEXT NOT NULL)")
        con.execute("CREATE TABLE folders (path TEXT PRIMARY KEY)")

        con.execute(
            "INSERT INTO metadata VALUES ('created', ?)",
            (datetime.now().isoformat(timespec="seconds"),),
        )

        con.executemany(
            "INSERT INTO files VALUES (?, ?, ?)",
            ((r, str(v[0]), str(v[1])) for r, v in files.items()),
        )

        con.executemany(
            "INSERT INTO folders VALUES (?)",
            ((r,) for r in folders),
        )

        con.commit()
    except Exception:
        if con is not None:
            con.rollback()
        raise
    finally:
        if con is not None:
            con.close()

    os.replace(tmp, db)


def guard_evaluate(job: Job, files, folders):
    baseline = guard_load_baseline(job)

    if baseline is None:
        return {
            "first_run": True,
            "blocked": False,
            "percent": 0.0,
            "changed": 0,
            "previous_total": 0,
            "current_total": len(files) + len(folders),
            "files_added": 0,
            "files_deleted": 0,
            "files_modified": 0,
            "folders_added": 0,
            "folders_deleted": 0,
        }

    old_files, old_folders, created = baseline

    old_names = set(old_files)
    new_names = set(files)

    added = new_names - old_names
    deleted = old_names - new_names
    modified = {r for r in old_names & new_names if old_files[r] != files[r]}

    f_added = folders - old_folders
    f_deleted = old_folders - folders

    changed = len(added) + len(deleted) + len(modified) + len(f_added) + len(f_deleted)
    previous_total = len(old_files) + len(old_folders)

    percent = (changed / previous_total * 100.0) if previous_total else (100.0 if changed else 0.0)

    return {
        "first_run": False,
        "blocked": percent > float(job.guard_threshold_percent),
        "percent": percent,
        "changed": changed,
        "previous_total": previous_total,
        "current_total": len(files) + len(folders),
        "baseline_created": created,
        "files_added": len(added),
        "files_deleted": len(deleted),
        "files_modified": len(modified),
        "folders_added": len(f_added),
        "folders_deleted": len(f_deleted),
    }


def guard_details(g):
    return (
        f"Changed {g['changed']:,}/{g['previous_total']:,} baseline objects ({g['percent']:.2f}%): "
        f"files +{g['files_added']:,} / -{g['files_deleted']:,} / modified {g['files_modified']:,}; "
        f"folders +{g['folders_added']:,} / -{g['folders_deleted']:,}"
    )


def assign_actions(items: List[Item], mode: str):
    for x in items:
        if x.status == Status.EQUAL:
            x.action = "Skip"
        elif mode == "Mirror":
            x.action = "Delete right" if x.status == Status.RIGHT else "Copy left -> right"
        elif mode == "Update":
            x.action = "Skip" if x.status == Status.RIGHT else "Copy left -> right"
        else:
            if x.status == Status.LEFT:
                x.action = "Copy left -> right"
            elif x.status == Status.RIGHT:
                x.action = "Copy right -> left"
            elif x.status == Status.LNEW:
                x.action = "Copy left -> right"
            elif x.status == Status.RNEW:
                x.action = "Copy right -> left"
            else:
                x.action = "Conflict"


def compare_maps(
    L: Dict[str, Info],
    R: Dict[str, Info],
    job: Job,
    cancel: threading.Event,
    progress=None,
) -> List[Item]:
    tol = job.tolerance * 1_000_000_000
    rels = sorted(set(L) | set(R), key=str.casefold)
    items = []

    def classify(rel):
        l, r = L.get(rel), R.get(rel)

        if not l:
            return Item(rel, None, r, Status.RIGHT)

        if not r:
            return Item(rel, l, None, Status.LEFT)

        if job.compare == "Size":
            st = Status.EQUAL if l.size == r.size else Status.DIFF
        elif job.compare == "Content":
            st = (
                Status.EQUAL
                if l.size == r.size and digest(Path(l.path), cancel) == digest(Path(r.path), cancel)
                else Status.DIFF
            )
        else:
            dt = l.mtime_ns - r.mtime_ns

            if l.size == r.size and abs(dt) <= tol:
                st = Status.EQUAL
            elif dt > tol:
                st = Status.LNEW
            elif dt < -tol:
                st = Status.RNEW
            else:
                st = Status.DIFF

        return Item(rel, l, r, st)

    if job.compare == "Content":
        with ThreadPoolExecutor(max_workers=job.workers) as pool:
            fs = {pool.submit(classify, r): r for r in rels}

            for i, f in enumerate(as_completed(fs), 1):
                try:
                    items.append(f.result())
                except Exception as ex:
                    items.append(
                        Item(
                            fs[f],
                            L.get(fs[f]),
                            R.get(fs[f]),
                            Status.ERROR,
                            note=str(ex),
                        )
                    )

                if progress and (i % 25 == 0 or i == len(rels)):
                    progress(i, len(rels), "Comparing content")

        items.sort(key=lambda x: x.rel.casefold())
    else:
        for i, r in enumerate(rels, 1):
            items.append(classify(r))

            if progress and (i % 100 == 0 or i == len(rels)):
                progress(i, len(rels), "Classifying differences")

    assign_actions(items, job.mode)

    return items


def inside(root: Path, p: Path) -> bool:
    try:
        root = Path(root).resolve()
        p = Path(p).resolve(strict=False)
        return p == root or root in p.parents
    except Exception:
        return False


def copy_atomic(src: Path, dst: Path, job: Job, cancel: threading.Event) -> int:
    """Atomically copy a file with optional verification.
    
    Args:
        src: Source file path
        dst: Destination file path
        job: Job configuration
        cancel: Cancellation event
        
    Returns:
        Number of bytes copied
        
    Raises:
        InterruptedError: If operation was cancelled
        IOError: If copy or verification fails
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    
    tmp = dst.with_name(dst.name + f".darksync_tmp_{os.getpid()}_{threading.get_ident()}")
    cleaned_up = False
    
    try:
        bytes_written = 0
        
        with open(src, "rb") as a, open(tmp, "wb") as b:
            while True:
                if cancel.is_set():
                    raise InterruptedError("Copy cancelled")
                
                block = a.read(BLOCK_SIZE_BYTES)
                
                if not block:
                    break
                
                b.write(block)
                bytes_written += len(block)
                b.flush()
                os.fsync(b.fileno())
        
        if job.preserve_times:
            shutil.copystat(src, tmp, follow_symlinks=False)
        
        if job.verify:
            src_digest = digest(src, cancel)
            tmp_digest = digest(tmp, cancel)
            if src_digest != tmp_digest:
                raise IOError(f"Post-copy verification failed: source={src_digest[:16]}... tmp={tmp_digest[:16]}...")
        
        os.replace(tmp, dst)
        cleaned_up = True
        return bytes_written
    except Exception:
        raise
    finally:
        if not cleaned_up and tmp.exists():
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass


def job_undo_root(job: Job) -> Path:
    return UNDO_ROOT / job.id


def available_undo_manifests(job: Job):
    cutoff = datetime.now() - timedelta(days=UNDO_RETENTION_DAYS)
    base = job_undo_root(job)
    out = []

    if not base.exists():
        return out

    for mf in base.glob("*/manifest.json"):
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
            created = datetime.fromisoformat(data.get("created", ""))

            if created >= cutoff and data.get("records"):
                out.append((created, mf, data))
        except Exception:
            continue

    return sorted(out, key=lambda x: x[0], reverse=True)


def cleanup_undo(job: Job):
    cutoff = datetime.now() - timedelta(days=UNDO_RETENTION_DAYS)
    base = job_undo_root(job)

    if not base.exists():
        return

    for folder in base.iterdir():
        if not folder.is_dir():
            continue

        mf = folder / "manifest.json"

        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
            created = datetime.fromisoformat(data.get("created", ""))

            if created < cutoff:
                shutil.rmtree(folder)
        except Exception:
            continue


def send_email_notification(n: NotifyConfig, subject: str, body: str, success: bool) -> None:
    """Send email notification via SMTP.
    
    Args:
        n: Notification configuration
        subject: Email subject line
        body: Email body content
        success: Whether the operation was successful
        
    Raises:
        ValueError: If required email configuration is missing
        smtplib.SMTPException: If email sending fails
    """
    if not n.enabled:
        return

    if not n.smtp_host or not n.mail_from or not n.mail_to:
        raise ValueError("Email notifications require SMTP host, From, and To fields.")

    msg = EmailMessage()
    msg["From"] = n.mail_from
    msg["To"] = n.mail_to
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(n.smtp_host, n.smtp_port, timeout=15) as smtp:
        smtp.starttls()

        if n.smtp_user:
            smtp.login(n.smtp_user, n.smtp_password)

        smtp.send_message(msg)


def send_ntfy_notification(n: NotifyConfig, subject: str, body: str, success: bool) -> None:
    """Send notification via ntfy.sh service.
    
    Args:
        n: Notification configuration
        subject: Notification title
        body: Notification body content
        success: Whether the operation was successful
        
    Raises:
        ValueError: If ntfy topic is missing
        RuntimeError: If ntfy request fails
    """
    if not n.ntfy_enabled:
        return

    if not n.ntfy_topic.strip():
        raise ValueError("ntfy topic is required.")

    server = (n.ntfy_server or "https://ntfy.sh").strip().rstrip("/")
    topic = urllib.parse.quote(n.ntfy_topic.strip().strip("/"), safe="")
    url = f"{server}/{topic}"

    priority = (n.ntfy_priority or ("default" if success else "high")).strip().lower()
    tags = "white_check_mark" if success else "warning"

    headers = {
        "Title": subject,
        "Priority": priority,
        "Tags": tags,
        "Content-Type": "text/plain; charset=utf-8",
        "User-Agent": f"{APP}/{VERSION}",
    }

    if n.ntfy_token.strip():
        headers["Authorization"] = "Bearer " + n.ntfy_token.strip()

    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"ntfy returned HTTP {resp.status}")


def send_notification(job: Job, subject: str, body: str, success: bool) -> None:
    """Send notifications through configured channels (email and/or ntfy).
    
    Args:
        job: The job that triggered the notification
        subject: Notification subject/title
        body: Notification body content
        success: Whether the operation was successful
        
    Raises:
        RuntimeError: If any notification channel fails (with combined error messages)
    """
    n = job.notify

    if not (n.on_success if success else n.on_failure):
        return

    errors = []

    try:
        send_email_notification(n, subject, body, success)
    except Exception as ex:
        errors.append(f"Email: {ex}")

    try:
        send_ntfy_notification(n, subject, body, success)
    except Exception as ex:
        errors.append(f"ntfy: {ex}")

    if errors:
        raise RuntimeError("; ".join(errors))


class SyncWorker(QObject):
    progress = Signal(str, int, int, str)
    compared = Signal(str, object, object)
    finished = Signal(str, object)
    failed = Signal(str, str)

    def __init__(self, job: Job, op: str, items=None, undo_payload=None):
        super().__init__()

        self.job = job
        self.op = op
        self.items = items or []
        self.undo_payload = undo_payload

        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()
        self.pause_event.clear()

    def pause(self):
        self.pause_event.set()

    def resume(self):
        self.pause_event.clear()

    def is_paused(self):
        return self.pause_event.is_set()

    def run(self):
        try:
            if self.op == "compare":
                self.run_compare(emit_finished=True, guard_check=False)
            elif self.op == "sync":
                self.run_sync()
            elif self.op in ("compare_sync", "compare_sync_override"):
                if self.run_compare(
                    emit_finished=False,
                    guard_check=self.job.guard_enabled,
                    override=self.op == "compare_sync_override",
                ):
                    self.items = [x for x in getattr(self, "last_items", [])]
                    self.run_sync()
            elif self.op == "undo":
                self.run_undo()
        except InterruptedError:
            self.failed.emit(self.job.id, "Operation cancelled safely.")
        except Exception:
            self.failed.emit(self.job.id, traceback.format_exc())

    def run_compare(self, emit_finished=True, guard_check=False, override=False):
        self.progress.emit(self.job.id, 0, 0, "Scanning source and destination in parallel...")

        with ThreadPoolExecutor(max_workers=2) as pool:
            lf = pool.submit(
                scan,
                self.job.source,
                self.job,
                self.cancel_event,
                lambda n, side: self.progress.emit(
                    self.job.id,
                    n,
                    0,
                    f"Scanning {side}: {n:,} files found",
                ),
                "left",
                self.pause_event,
            )

            rf = pool.submit(
                scan,
                self.job.destination,
                self.job,
                self.cancel_event,
                lambda n, side: self.progress.emit(
                    self.job.id,
                    n,
                    0,
                    f"Scanning {side}: {n:,} files found",
                ),
                "right",
                self.pause_event,
            )

            L, Lfolders, le = lf.result()
            R, Rfolders, re = rf.result()

        if le:
            raise RuntimeError(
                "Source scan errors prevent a trusted ransomware check. "
                "To allow skipped/unreadable entries, enable Ignore all scan errors or "
                "Ignore permission errors in Job settings > Ransomware Guard. Details: "
                + " | ".join(le[:20])
            )

        if guard_check:
            files, folders = guard_snapshot_from_scan(L, Lfolders)

            self.guard_snapshot = (files, folders)
            self.guard_result = guard_evaluate(self.job, files, folders)
            self.guard_result["manually_approved"] = override

            self.progress.emit(
                self.job.id,
                len(L),
                len(L),
                f"Ransomware Guard: {self.guard_result['percent']:.2f}% changed",
            )

            if self.guard_result["blocked"] and not override:
                self.finished.emit(
                    self.job.id,
                    {
                        "operation": "guard_blocked",
                        "guard": self.guard_result,
                    },
                )
                return False

        items = compare_maps(
            L,
            R,
            self.job,
            self.cancel_event,
            lambda n, t, m: self.progress.emit(self.job.id, n, t, f"{m}: {n:,}/{t:,}"),
        )

        self.last_items = items
        self.compared.emit(self.job.id, items, le + re)

        if emit_finished:
            self.finished.emit(
                self.job.id,
                {
                    "operation": "compare",
                    "items": len(items),
                },
            )

        return True

    def run_sync(self):
        L = Path(self.job.source)
        R = Path(self.job.destination)

        todo = [
            x
            for x in self.items
            if x.selected and x.action not in ("Skip", "Conflict")
        ]

        tx = job_undo_root(self.job) / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup = tx / "backup"
        backup.mkdir(parents=True, exist_ok=True)

        errors = []
        records = []
        report = []

        for x in self.items:
            if not x.selected and x.action not in ("Skip", "Conflict"):
                report.append(
                    {
                        "status": "Not selected",
                        "action": x.action,
                        "relative": x.rel,
                        "source": "",
                        "destination": "",
                        "details": "Excluded by user",
                        "bytes": 0,
                    }
                )
            elif x.action == "Conflict":
                report.append(
                    {
                        "status": "Conflict",
                        "action": x.action,
                        "relative": x.rel,
                        "source": "",
                        "destination": "",
                        "details": "No automatic action",
                        "bytes": 0,
                    }
                )
            elif x.action == "Skip" and x.status != Status.EQUAL:
                report.append(
                    {
                        "status": "Skipped",
                        "action": x.action,
                        "relative": x.rel,
                        "source": "",
                        "destination": "",
                        "details": "Rules specified no action",
                        "bytes": 0,
                    }
                )

        def one(x: Item):
            wait_if_paused(self.cancel_event, self.pause_event)

            if self.cancel_event.is_set():
                raise InterruptedError

            if x.action == "Copy left -> right":
                src = L / x.rel
                dst = R / x.rel
                root = R
                side = "right"
                source = str(src)
                destination = str(dst)
            elif x.action == "Copy right -> left":
                src = R / x.rel
                dst = L / x.rel
                root = L
                side = "left"
                source = str(src)
                destination = str(dst)
            else:
                src = None
                dst = R / x.rel if x.action == "Delete right" else L / x.rel
                root = R if x.action == "Delete right" else L
                side = "right" if x.action == "Delete right" else "left"
                source = ""
                destination = str(dst)

            if not inside(root, dst):
                raise ValueError(f"Unsafe destination | Root: {root} | Destination: {dst}")

            existed = dst.exists()
            b = backup / side / x.rel

            if src:
                if existed:
                    b.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dst, b)

                copy_atomic(src, dst, self.job, self.cancel_event)

                st = dst.stat()

                return {
                    "action": "replace" if existed else "create",
                    "source": source,
                    "destination": destination,
                    "backup": str(b) if existed else "",
                    "post_size": st.st_size,
                    "post_mtime_ns": st.st_mtime_ns,
                    "relative": x.rel,
                    "bytes": st.st_size,
                }

            if existed:
                b.parent.mkdir(parents=True, exist_ok=True)

                size = dst.stat().st_size
                os.replace(dst, b)

                return {
                    "action": "delete",
                    "source": source,
                    "destination": destination,
                    "backup": str(b),
                    "relative": x.rel,
                    "bytes": size,
                }

            return {
                "action": "none",
                "source": source,
                "destination": destination,
                "backup": "",
                "relative": x.rel,
                "bytes": 0,
            }

        with ThreadPoolExecutor(max_workers=self.job.workers) as pool:
            fs = {pool.submit(one, x): x for x in todo}

            for i, f in enumerate(as_completed(fs), 1):
                x = fs[f]
                rec = {}

                try:
                    rec = f.result()

                    status = (
                        "Skipped"
                        if rec["action"] == "none"
                        else ("Copied" if x.action.startswith("Copy") else "Removed")
                    )

                    if rec["action"] != "none":
                        records.append(rec)

                    details = (
                        f"Completed successfully ({rec.get('bytes', 0):,} bytes)"
                        if rec["action"] != "none"
                        else "No filesystem change required"
                    )
                except InterruptedError:
                    self.cancel_event.set()
                    status = "Cancelled"
                    details = "Cancelled before completion"
                    errors.append(f"{x.rel}: {details}")
                except Exception as ex:
                    status = "Failed"
                    details = str(ex)
                    errors.append(f"{x.rel}: {details}")

                report.append(
                    {
                        "status": status,
                        "action": x.action,
                        "relative": x.rel,
                        "source": rec.get("source", ""),
                        "destination": rec.get("destination", ""),
                        "details": details,
                        "bytes": rec.get("bytes", 0),
                    }
                )

                self.progress.emit(self.job.id, i, len(todo), f"Synchronizing: {i}/{len(todo)}")

        mf = {
            "version": 2,
            "job_id": self.job.id,
            "job_name": self.job.name,
            "created": datetime.now().isoformat(timespec="seconds"),
            "source": str(L),
            "destination": str(R),
            "mode": self.job.mode,
            "records": records,
            "undone": False,
        }

        mfp = tx / "manifest.json"
        mfp.write_text(json.dumps(mf, indent=2), encoding="utf-8")

        cleanup_undo(self.job)

        LOGS.mkdir(exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log = LOGS / f"{self.job.id}_{stamp}.log"

        summary = {
            k: sum(1 for r in report if r["status"] == k)
            for k in [
                "Copied",
                "Removed",
                "Failed",
                "Cancelled",
                "Skipped",
                "Not selected",
                "Conflict",
            ]
        }

        changed_rows = [r for r in report if r.get("status") in ("Copied", "Removed")]

        summary["Bytes changed"] = sum(r.get("bytes", 0) for r in records)
        summary["Files changed"] = len(changed_rows)
        summary["Folders changed"] = folder_count_from_rows(changed_rows)
        summary["MB changed"] = round(summary["Bytes changed"] / (1024 * 1024), 2)

        # Generated report files include only REPORT_STATUSES.
        # Skipped is excluded.
        problem_rows = [
            r
            for r in report
            if r.get("status") in REPORT_STATUSES
        ]

        problem_lines = [
            f"{r.get('status', '')}\t"
            f"{r.get('action', '')}\t"
            f"{r.get('relative', '')}\t"
            f"{r.get('details', '')}"
            for r in problem_rows
        ]

        if not problem_lines:
            problem_lines = ["No failed or problem entries."]

        log.write_text("\n".join(problem_lines), encoding="utf-8")

        rp = LOGS / f"{self.job.id}_{stamp}_report.json"

        rd = {
            "created": datetime.now().isoformat(timespec="seconds"),
            "job": self.job.name,
            "summary": summary,
            "items": problem_rows,
            "text_log": str(log),
            "undo_manifest": str(mfp),
        }

        rp.write_text(json.dumps(rd, indent=2), encoding="utf-8")

        success = summary["Failed"] == 0 and summary["Cancelled"] == 0

        guard = None

        if success and self.job.guard_enabled and hasattr(self, "guard_snapshot"):
            guard = getattr(self, "guard_result", None)
            guard_save_baseline(self.job, *self.guard_snapshot)

        self.finished.emit(
            self.job.id,
            {
                "operation": "sync",
                "summary": summary,
                "errors": errors,
                "report": str(rp),
                "report_data": rd,
                "undo_manifest": str(mfp),
                "success": success,
                "guard": guard,
            },
        )

    def run_undo(self):
        p = self.undo_payload

        mf = Path(p["manifest"])
        data = json.loads(mf.read_text(encoding="utf-8"))

        folder = p.get("folder", "").replace("\\", "/").strip("/")
        side = p.get("recovery_side", "original")
        overwrite = bool(p.get("overwrite_existing", False))
        root = Path(p.get("recovery_root", "")) if p.get("recovery_root") else None

        if side in ("source", "destination") and root is None:
            root = Path(data["source"] if side == "source" else data["destination"])

        records = [
            r
            for r in data.get("records", [])
            if not folder
            or r.get("relative", "").replace("\\", "/").strip("/") == folder
            or r.get("relative", "").replace("\\", "/").strip("/").startswith(folder + "/")
        ]

        records = list(reversed(records))

        errors = []
        restored = 0

        for i, r in enumerate(records, 1):
            wait_if_paused(self.cancel_event, self.pause_event)

            if self.cancel_event.is_set():
                raise InterruptedError

            dst = (root / r["relative"]) if root is not None else Path(r["destination"])
            bak = Path(r["backup"]) if r.get("backup") else None

            try:
                if root is not None:
                    src = None

                    if bak and bak.is_file():
                        src = bak
                    elif r["action"] == "create":
                        cur = Path(r["destination"])

                        if not cur.is_file():
                            raise RuntimeError("Synchronized copy missing")

                        st = cur.stat()

                        if st.st_size != r.get("post_size") or st.st_mtime_ns != r.get("post_mtime_ns"):
                            raise RuntimeError("Synchronized copy changed after sync; skipped")

                        src = cur

                    if src is None:
                        raise RuntimeError("No recoverable copy exists")

                    if dst.exists():
                        if not overwrite:
                            raise RuntimeError("Recovery path already exists; skipped (overwrite disabled)")

                        if not dst.is_file():
                            raise RuntimeError("Target exists but is not a regular file")

                        ob = mf.parent / "overwrite_backups" / side / r["relative"]
                        ob.parent.mkdir(parents=True, exist_ok=True)

                        shutil.copy2(dst, ob)

                    copy_atomic(src, dst, self.job, self.cancel_event)
                else:
                    if r["action"] in ("create", "replace"):
                        if not dst.exists():
                            raise RuntimeError("Current file missing")

                        st = dst.stat()

                        if st.st_size != r.get("post_size") or st.st_mtime_ns != r.get("post_mtime_ns"):
                            raise RuntimeError("File changed after sync; skipped")

                        if r["action"] == "create":
                            dst.unlink()
                        else:
                            if not bak or not bak.exists():
                                raise RuntimeError("Undo backup missing")

                            copy_atomic(bak, dst, self.job, self.cancel_event)
                    elif r["action"] == "delete":
                        if dst.exists():
                            raise RuntimeError("Destination recreated after sync; skipped")

                        if not bak or not bak.exists():
                            raise RuntimeError("Undo backup missing")

                        dst.parent.mkdir(parents=True, exist_ok=True)
                        copy_atomic(bak, dst, self.job, self.cancel_event)

                restored += 1
            except Exception as ex:
                errors.append(f"{r.get('relative', dst)}: {ex}")

            self.progress.emit(self.job.id, i, len(records), f"Recovering: {i}/{len(records)}")

        data.setdefault("recoveries", []).append(
            {
                "time": datetime.now().isoformat(timespec="seconds"),
                "folder": folder or "<ALL>",
                "side": side,
                "overwrite": overwrite,
                "restored": restored,
                "errors": errors,
            }
        )

        if not folder and side == "original" and not errors:
            data["undone"] = True

        mf.write_text(json.dumps(data, indent=2), encoding="utf-8")

        self.finished.emit(
            self.job.id,
            {
                "operation": "undo",
                "restored": restored,
                "errors": errors,
                "success": not errors,
            },
        )


class ItemModel(QAbstractTableModel):
    headers = ["Sync", "Relative path", "Left size", "Status", "Action", "Right size"]

    def __init__(self):
        super().__init__()
        self.items = []

    def set_items(self, items):
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def rowCount(self, p=QModelIndex()):
        return 0 if p.isValid() else len(self.items)

    def columnCount(self, p=QModelIndex()):
        return len(self.headers)

    def headerData(self, s, o, r=Qt.DisplayRole):
        return self.headers[s] if r == Qt.DisplayRole and o == Qt.Horizontal else None

    def data(self, i, r=Qt.DisplayRole):
        if not i.isValid():
            return None

        x, c = self.items[i.row()], i.column()

        if r == Qt.CheckStateRole and c == 0:
            return Qt.Checked if x.selected else Qt.Unchecked

        if r == Qt.DisplayRole:
            return [
                "",
                x.rel,
                hsize(x.left.size) if x.left else "",
                x.status.value,
                x.action,
                hsize(x.right.size) if x.right else "",
            ][c]

        if r == Qt.ForegroundRole:
            return QColor(
                {
                    Status.LEFT: "#73c7ff",
                    Status.RIGHT: "#f6ad55",
                    Status.LNEW: "#68d391",
                    Status.RNEW: "#b794f4",
                    Status.DIFF: "#f6e05e",
                    Status.ERROR: "#fc8181",
                }.get(x.status, "#a0aec0")
            )

    def flags(self, i):
        return super().flags(i) | (Qt.ItemIsUserCheckable if i.column() == 0 else Qt.NoItemFlags)

    def setData(self, i, v, r=Qt.EditRole):
        if i.column() == 0 and r == Qt.CheckStateRole:
            self.items[i.row()].selected = v == Qt.Checked
            self.dataChanged.emit(i, i)
            return True

        return False


class JobDialog(QDialog):
    def __init__(self, job: Job, templates: List[Job], parent=None):
        super().__init__(parent)

        self.job = job_from_dict(asdict(job))

        self.setWindowTitle("Job settings")
        self.resize(760, 620)

        tabs = QTabWidget(self)
        root = QVBoxLayout(self)
        root.addWidget(tabs)

        main = QWidget()
        f = QFormLayout(main)

        self.template = QComboBox()
        self.template.addItem("Custom", None)

        for t in templates:
            self.template.addItem(t.name, t)

        self.name = QLineEdit(self.job.name)
        self.src = QLineEdit(self.job.source)
        self.dst = QLineEdit(self.job.destination)

        bs = QPushButton("Browse")
        bd = QPushButton("Browse")

        bs.clicked.connect(lambda: self.pick(self.src))
        bd.clicked.connect(lambda: self.pick(self.dst))

        row1 = QHBoxLayout()
        row1.addWidget(self.src)
        row1.addWidget(bs)

        w1 = QWidget()
        w1.setLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(self.dst)
        row2.addWidget(bd)

        w2 = QWidget()
        w2.setLayout(row2)

        self.mode = QComboBox()
        self.mode.addItems(["Two way", "Mirror", "Update"])
        self.mode.setCurrentText(self.job.mode)

        self.comp = QComboBox()
        self.comp.addItems(["Time and size", "Size", "Content"])
        self.comp.setCurrentText(self.job.compare)

        self.work = QSpinBox()
        self.work.setRange(1, 64)
        self.work.setValue(self.job.workers)

        self.tol = QSpinBox()
        self.tol.setRange(0, 3600)
        self.tol.setValue(self.job.tolerance)
        self.tol.setSuffix(" s")

        self.include = QLineEdit(self.job.include)
        self.exclude = QLineEdit(self.job.exclude)

        self.enabled = QCheckBox()
        self.enabled.setChecked(self.job.enabled)

        self.verify = QCheckBox()
        self.verify.setChecked(self.job.verify)

        self.preserve = QCheckBox()
        self.preserve.setChecked(self.job.preserve_times)

        self.links = QCheckBox()
        self.links.setChecked(self.job.follow_links)

        self.delete = QComboBox()
        self.delete.addItems(["Recycle bin", "Permanent"])
        self.delete.setCurrentText(self.job.deletion)

        for a, b in [
            ("Template", self.template),
            ("Name", self.name),
            ("Source", w1),
            ("Destination", w2),
            ("Enabled", self.enabled),
            ("Mode", self.mode),
            ("Compare by", self.comp),
            ("Parallel workers", self.work),
            ("Timestamp tolerance", self.tol),
            ("Include globs", self.include),
            ("Exclude globs", self.exclude),
            ("Verify copies", self.verify),
            ("Preserve timestamps", self.preserve),
            ("Follow symlinks", self.links),
            ("Deletion policy", self.delete),
        ]:
            f.addRow(a, b)

        tabs.addTab(main, "Job")

        sched = QWidget()
        sf = QFormLayout(sched)

        self.sched_en = QCheckBox()
        self.sched_en.setChecked(self.job.scheduler_enabled)

        self.sched_time = QTimeEdit()
        self.sched_time.setDisplayFormat("HH:mm")

        qt = QTime.fromString(self.job.scheduler_time, "HH:mm")
        if not qt.isValid():
            qt = QTime(2, 0)

        self.sched_time.setTime(qt)

        self.sched_action = QComboBox()
        self.sched_action.addItems(["Compare only", "Compare and synchronize"])
        self.sched_action.setCurrentText(self.job.scheduler_action)

        sf.addRow("Scheduled", self.sched_en)
        sf.addRow("24-hour time", self.sched_time)
        sf.addRow("Action", self.sched_action)

        tabs.addTab(sched, "Schedule")

        guard = QWidget()
        gf = QFormLayout(guard)

        self.guard_en = QCheckBox("Use shared source scan before synchronization")
        self.guard_en.setChecked(self.job.guard_enabled)

        self.guard_pct = QDoubleSpinBox()
        self.guard_pct.setRange(0.01, 100)
        self.guard_pct.setDecimals(2)
        self.guard_pct.setSuffix(" %")
        self.guard_pct.setValue(self.job.guard_threshold_percent)

        self.ignore_perm = QCheckBox("Ignore permission errors on files/folders")
        self.ignore_perm.setChecked(self.job.ignore_permission_errors)

        self.ignore_all = QCheckBox("Ignore all scan errors")
        self.ignore_all.setChecked(self.job.ignore_scan_errors)

        self.exit_on_complete = QCheckBox("Exit application after job completion (for automated runs)")
        self.exit_on_complete.setChecked(self.job.exit_on_completion)

        warn = QLabel(
            "Warning: ignored scan errors mean unreadable/skipped items are excluded from compare "
            "and from the Ransomware Guard baseline. Use only when expected."
        )
        warn.setWordWrap(True)

        gf.addRow("Enable protection", self.guard_en)
        gf.addRow("Maximum allowed change", self.guard_pct)
        gf.addRow("", self.ignore_perm)
        gf.addRow("", self.ignore_all)
        gf.addRow("", self.exit_on_complete)
        gf.addRow("Error handling", warn)

        tabs.addTab(guard, "Ransomware Guard")

        notify = QWidget()
        nf = QFormLayout(notify)

        n = self.job.notify

        self.n_en = QCheckBox()
        self.n_en.setChecked(n.enabled)

        self.n_succ = QCheckBox()
        self.n_succ.setChecked(n.on_success)

        self.n_fail = QCheckBox()
        self.n_fail.setChecked(n.on_failure)

        self.smtp = QLineEdit(n.smtp_host)

        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(n.smtp_port)

        self.user = QLineEdit(n.smtp_user)

        self.pw = QLineEdit(n.smtp_password)
        self.pw.setEchoMode(QLineEdit.Password)

        self.mfrom = QLineEdit(n.mail_from)
        self.mto = QLineEdit(n.mail_to)

        self.ntfy_en = QCheckBox()
        self.ntfy_en.setChecked(n.ntfy_enabled)

        self.ntfy_server = QLineEdit(n.ntfy_server or "https://ntfy.sh")
        self.ntfy_topic = QLineEdit(n.ntfy_topic or "PA_Backups")

        self.ntfy_token = QLineEdit(n.ntfy_token)
        self.ntfy_token.setEchoMode(QLineEdit.Password)

        self.ntfy_prio = QComboBox()
        self.ntfy_prio.addItems(["min", "low", "default", "high", "urgent"])
        self.ntfy_prio.setCurrentText(n.ntfy_priority or "high")

        self.defaults_btn = QPushButton("Use Backup Monitoring Defaults")
        self.defaults_btn.clicked.connect(self.apply_backup_monitoring_defaults)

        self.test_email_btn = QPushButton("Test Email")
        self.test_email_btn.clicked.connect(self.test_email_notification)

        self.test_ntfy_btn = QPushButton("Test ntfy")
        self.test_ntfy_btn.clicked.connect(self.test_ntfy_notification)

        self.test_all_btn = QPushButton("Test All Notifications")
        self.test_all_btn.clicked.connect(self.test_all_notifications)

        test_row = QHBoxLayout()
        test_row.addWidget(self.test_email_btn)
        test_row.addWidget(self.test_ntfy_btn)
        test_row.addWidget(self.test_all_btn)

        test_box = QWidget()
        test_box.setLayout(test_row)

        for a, b in [
            ("Enable email notifications", self.n_en),
            ("Notify on success", self.n_succ),
            ("Notify on failure", self.n_fail),
            ("SMTP host", self.smtp),
            ("SMTP port", self.port),
            ("SMTP user", self.user),
            ("SMTP password", self.pw),
            ("From", self.mfrom),
            ("To", self.mto),
            ("Enable ntfy notifications", self.ntfy_en),
            ("ntfy server", self.ntfy_server),
            ("ntfy topic", self.ntfy_topic),
            ("ntfy access token", self.ntfy_token),
            ("ntfy priority", self.ntfy_prio),
            ("Recommended backup monitoring", self.defaults_btn),
            ("Test notifications", test_box),
        ]:
            nf.addRow(a, b)

        tabs.addTab(notify, "Notifications")

        self.template.currentIndexChanged.connect(self.apply_template)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root.addWidget(buttons)

    def apply_backup_monitoring_defaults(self):
        self.n_en.setChecked(False)
        self.n_succ.setChecked(False)
        self.n_fail.setChecked(True)
        self.ntfy_en.setChecked(True)
        self.ntfy_server.setText("https://ntfy.sh")
        self.ntfy_topic.setText("PA_Backups")
        self.ntfy_token.setText("")
        self.ntfy_prio.setCurrentText("high")

    def current_notify_config(self):
        return NotifyConfig(
            self.n_en.isChecked(),
            self.n_succ.isChecked(),
            self.n_fail.isChecked(),
            self.smtp.text(),
            self.port.value(),
            self.user.text(),
            self.pw.text(),
            self.mfrom.text(),
            self.mto.text(),
            self.ntfy_en.isChecked(),
            self.ntfy_server.text().strip() or "https://ntfy.sh",
            self.ntfy_topic.text().strip() or "PA_Backups",
            self.ntfy_token.text().strip(),
            self.ntfy_prio.currentText(),
        )

    def test_email_notification(self):
        try:
            n = self.current_notify_config()
            n.enabled = True

            body = (
                "DarkSync email notification test successful.\n"
                f"Computer: {platform.node()}\n"
                f"Time: {datetime.now().isoformat(timespec='seconds')}\n"
                f"Version: {VERSION}"
            )

            send_email_notification(n, "DarkSync Test Email", body, True)

            QMessageBox.information(self, "Email test", "Test email sent successfully.")
        except Exception as ex:
            QMessageBox.critical(self, "Email test failed", str(ex))

    def test_ntfy_notification(self):
        try:
            n = self.current_notify_config()
            n.ntfy_enabled = True

            body = (
                "DarkSync ntfy notification test successful.\n"
                f"Computer: {platform.node()}\n"
                f"Time: {datetime.now().isoformat(timespec='seconds')}\n"
                f"Version: {VERSION}"
            )

            send_ntfy_notification(n, "DarkSync Test Notification", body, True)

            QMessageBox.information(self, "ntfy test", "Test ntfy notification sent successfully.")
        except Exception as ex:
            QMessageBox.critical(self, "ntfy test failed", str(ex))

    def test_all_notifications(self):
        results = []

        try:
            n = self.current_notify_config()
            n.enabled = True

            body = (
                "DarkSync email notification test successful.\n"
                f"Computer: {platform.node()}\n"
                f"Time: {datetime.now().isoformat(timespec='seconds')}\n"
                f"Version: {VERSION}"
            )

            send_email_notification(n, "DarkSync Test Email", body, True)
            results.append("Email: Success")
        except Exception as ex:
            results.append(f"Email: Failed - {ex}")

        try:
            n = self.current_notify_config()
            n.ntfy_enabled = True

            body = (
                "DarkSync ntfy notification test successful.\n"
                f"Computer: {platform.node()}\n"
                f"Time: {datetime.now().isoformat(timespec='seconds')}\n"
                f"Version: {VERSION}"
            )

            send_ntfy_notification(n, "DarkSync Test Notification", body, True)
            results.append("ntfy: Success")
        except Exception as ex:
            results.append(f"ntfy: Failed - {ex}")

        QMessageBox.information(self, "Notification test results", "\n".join(results))

    def pick(self, e):
        p = QFileDialog.getExistingDirectory(self, "Select folder", e.text() or str(Path.home()))

        if p:
            e.setText(p)

    def apply_template(self):
        t = self.template.currentData()

        if not t:
            return

        t = job_from_dict(asdict(t)) if not isinstance(t.notify, NotifyConfig) else t

        self.mode.setCurrentText(t.mode)
        self.comp.setCurrentText(t.compare)
        self.work.setValue(t.workers)
        self.include.setText(t.include)
        self.exclude.setText(t.exclude)
        self.verify.setChecked(t.verify)

    def result_job(self):
        j = self.job

        j.name = self.name.text().strip() or "Unnamed Job"
        j.source = self.src.text().strip()
        j.destination = self.dst.text().strip()
        j.enabled = self.enabled.isChecked()
        j.mode = self.mode.currentText()
        j.compare = self.comp.currentText()
        j.workers = self.work.value()
        j.tolerance = self.tol.value()
        j.include = self.include.text()
        j.exclude = self.exclude.text()
        j.verify = self.verify.isChecked()
        j.preserve_times = self.preserve.isChecked()
        j.follow_links = self.links.isChecked()
        j.deletion = self.delete.currentText()
        j.scheduler_enabled = self.sched_en.isChecked()
        j.scheduler_time = self.sched_time.time().toString("HH:mm")
        j.scheduler_action = self.sched_action.currentText()
        j.guard_enabled = self.guard_en.isChecked()
        j.guard_threshold_percent = self.guard_pct.value()
        j.ignore_permission_errors = self.ignore_perm.isChecked()
        j.ignore_scan_errors = self.ignore_all.isChecked()
        j.exit_on_completion = self.exit_on_complete.isChecked()
        j.notify = self.current_notify_config()

        return j


class UndoDialog(QDialog):
    def __init__(self, job: Job, parent=None):
        super().__init__(parent)

        self.job = job

        self.setWindowTitle("Undo / recovery")
        self.resize(820, 360)

        v = QVBoxLayout(self)

        self.journals = available_undo_manifests(job)
        self.journal = QComboBox()

        for created, path, data in self.journals:
            self.journal.addItem(
                f"{created:%Y-%m-%d %H:%M:%S} | {len(data.get('records', [])):,} records | {data.get('mode', '')}",
                (str(path), data),
            )

        self.all = QRadioButton("Recover all files and folders")
        self.folder = QRadioButton("Recover a specific folder")

        self.all.setChecked(True)

        self.folder_combo = QComboBox()
        self.folder_combo.setEnabled(False)

        self.side = QComboBox()
        self.side.addItem("Original affected location (true undo)", "original")
        self.side.addItem("Source / left folder", "source")
        self.side.addItem("Destination / right folder", "destination")

        self.overwrite = QCheckBox("Overwrite existing files at selected recovery location")
        self.overwrite.setToolTip("Existing files are copied to overwrite_backups before replacement.")

        f = QFormLayout()

        f.addRow("Synchronization journal", self.journal)
        f.addRow("", self.all)
        f.addRow("", self.folder)
        f.addRow("Folder", self.folder_combo)
        f.addRow("Recover into", self.side)
        f.addRow("Overwrite", self.overwrite)

        v.addLayout(f)

        self.folder.toggled.connect(self.folder_combo.setEnabled)
        self.journal.currentIndexChanged.connect(self.reload_folders)

        self.reload_folders()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Recover")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        v.addWidget(buttons)

    def reload_folders(self):
        self.folder_combo.clear()

        if not self.journals:
            return

        data = self.journal.currentData()[1]
        folders = set()

        for r in data.get("records", []):
            parent = str(Path(r.get("relative", "")).parent).replace("\\", "/")

            if parent not in ("", "."):
                folders.add(parent)

        for f in sorted(folders, key=str.casefold):
            self.folder_combo.addItem(f)

        if not folders:
            self.folder_combo.addItem("(root only)", "")

    def payload(self):
        path, data = self.journal.currentData()

        folder = (
            ""
            if self.all.isChecked()
            else (self.folder_combo.currentData() or self.folder_combo.currentText())
        )

        return {
            "manifest": path,
            "folder": folder,
            "recovery_side": self.side.currentData(),
            "overwrite_existing": self.overwrite.isChecked(),
        }


class Main(QMainWindow):
    def __init__(self):
        super().__init__()

        self.state = state_from_disk()
        self.model = ItemModel()

        self.active_items = []
        self.threads = {}
        self.workers = {}
        self.running_jobs = set()
        self.job_queue = []
        self.latest_report_rows = []

        self.setWindowTitle(f"{APP} {VERSION}")
        self.resize(1600, 900)
        self.setMinimumSize(1200, 720)

        self.ui()
        self.reload_jobs()
        self.reload_history()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check_schedules)
        self.timer.start(15000)

    def ui(self):
        # Create menu bar with Themes menu
        menubar = self.menuBar()
        themes_menu = menubar.addMenu("Themes")
        
        # Create theme selection group
        self.theme_group = QActionGroup(self)
        self.theme_group.setExclusive(True)
        
        for theme_name in THEMES.keys():
            theme_action = QAction(theme_name, self)
            theme_action.setCheckable(True)
            theme_action.setChecked(theme_name == "Dark Blue")
            theme_action.triggered.connect(lambda checked, name=theme_name: self.change_theme(name))
            self.theme_group.addAction(theme_action)
            themes_menu.addAction(theme_action)
        
        tb = QToolBar()
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

        self.addToolBar(tb)

        compact_actions = [
            ("New", self.new_job),
            ("Copy", self.copy_job),
            ("Edit", self.edit_job),
            ("Delete", self.delete_job),
            ("Run", self.run_selected),
            ("Run All", self.run_all),
            ("Pause/Resume", self.pause_resume_jobs),
            ("Cancel", self.cancel_running_jobs),
            ("Undo", self.undo_recover),
            ("Import", self.import_jobs),
            ("Export", self.export_jobs),
            ("Startup Task", self.create_startup_task),
        ]

        for text, slot in compact_actions:
            a = QAction(text, self)
            a.triggered.connect(slot)
            tb.addAction(a)

            if text in ("Delete", "Run All", "Undo", "Export"):
                tb.addSeparator()

        w = QWidget()

        root = QVBoxLayout(w)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        self.tabs = QTabWidget()

        root.addWidget(self.tabs)

        self.setCentralWidget(w)
        self.setStatusBar(QStatusBar())

        # Add keyboard shortcuts
        QShortcut(QKeySequence("Ctrl+N"), self, self.new_job)
        QShortcut(QKeySequence("Ctrl+E"), self, self.edit_job)
        QShortcut(QKeySequence("Delete"), self, self.delete_job)
        QShortcut(QKeySequence("F5"), self, self.reload_history)
        QShortcut(QKeySequence("Ctrl+R"), self, self.run_selected)
        QShortcut(QKeySequence("Ctrl+Shift+R"), self, self.run_all)
        QShortcut(QKeySequence("Ctrl+Z"), self, self.undo_recover)
        QShortcut(QKeySequence("Ctrl+1"), self, lambda: self.tabs.setCurrentIndex(0))  # Dashboard
        QShortcut(QKeySequence("Ctrl+2"), self, lambda: self.tabs.setCurrentIndex(1))  # Jobs
        QShortcut(QKeySequence("Ctrl+3"), self, lambda: self.tabs.setCurrentIndex(2))  # Sync
        QShortcut(QKeySequence("Ctrl+4"), self, lambda: self.tabs.setCurrentIndex(3))  # Results
        QShortcut(QKeySequence("Ctrl+5"), self, lambda: self.tabs.setCurrentIndex(4))  # History

        dash = QWidget()

        dv = QVBoxLayout(dash)
        dv.setContentsMargins(22, 18, 22, 18)
        dv.setSpacing(16)

        header = QHBoxLayout()

        title = QLabel("DarkSync Dashboard")
        title.setObjectName("title")

        subtitle = QLabel("Backup health and scheduled job overview")
        subtitle.setObjectName("muted")
        subtitle.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        header.addWidget(title)
        header.addStretch()
        header.addWidget(subtitle)

        dv.addLayout(header)

        cards = QHBoxLayout()
        cards.setSpacing(14)

        self.card_jobs = self.make_card("Jobs", "0", "blue")
        self.card_success = self.make_card("Success", "0", "green")
        self.card_warning = self.make_card("Warning", "0", "orange")
        self.card_failed = self.make_card("Failed", "0", "red")

        for card in [self.card_jobs, self.card_success, self.card_warning, self.card_failed]:
            cards.addWidget(card, 1)

        dv.addLayout(cards)

        bottom = QHBoxLayout()
        bottom.setSpacing(14)

        recent = QFrame()
        recent.setObjectName("panel")

        rv = QVBoxLayout(recent)
        rv.setContentsMargins(16, 14, 16, 14)

        rtitle = QLabel("Recent Activity")
        rtitle.setObjectName("sectionTitle")

        rv.addWidget(rtitle)

        self.recent_list = QListWidget()
        self.recent_list.setObjectName("dashboardList")

        rv.addWidget(self.recent_list)

        upcoming = QFrame()
        upcoming.setObjectName("panel")

        uv = QVBoxLayout(upcoming)
        uv.setContentsMargins(16, 14, 16, 14)

        utitle = QLabel("Upcoming Scheduled Jobs")
        utitle.setObjectName("sectionTitle")

        uv.addWidget(utitle)

        self.upcoming_list = QListWidget()
        self.upcoming_list.setObjectName("dashboardList")

        uv.addWidget(self.upcoming_list)

        bottom.addWidget(recent, 1)
        bottom.addWidget(upcoming, 1)

        dv.addLayout(bottom, 2)

        self.tabs.addTab(dash, "Dashboard")

        jobs = QWidget()

        jv = QVBoxLayout(jobs)
        jv.setContentsMargins(10, 10, 10, 10)
        jv.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(8)

        self.parallel = QSpinBox()
        self.parallel.setRange(1, 8)
        self.parallel.setValue(self.state.max_parallel_jobs)
        self.parallel.valueChanged.connect(self.set_parallel)

        top.addWidget(QLabel("Max parallel jobs"))
        top.addWidget(self.parallel)
        top.addStretch()
        top.addWidget(QLabel("Use toolbar buttons above to create, edit, run, import, and export jobs."))

        jv.addLayout(top)

        # Add search box for jobs table
        search_layout = QHBoxLayout()
        self.job_search = QLineEdit()
        self.job_search.setPlaceholderText("🔍 Search jobs by name, source, or destination...")
        self.job_search.textChanged.connect(self.filter_jobs_table)

        search_layout.addWidget(QLabel("Filter:"))
        search_layout.addWidget(self.job_search)
        jv.addLayout(search_layout)

        self.jobs_table = QTableWidget(0, 8)
        self.jobs_table.setHorizontalHeaderLabels(
            ["Name", "Mode", "Enabled", "Schedule", "Last run", "Result", "Source", "Destination"]
        )
        self.jobs_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.jobs_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.jobs_table.setAlternatingRowColors(True)
        self.jobs_table.verticalHeader().hide()
        self.jobs_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.jobs_table.horizontalHeader().setStretchLastSection(True)

        widths = [210, 90, 80, 120, 165, 170, 300, 360]

        for i, wid in enumerate(widths):
            self.jobs_table.setColumnWidth(i, wid)

        jv.addWidget(self.jobs_table)

        self.tabs.addTab(jobs, "Jobs")

        sync = QWidget()

        sv = QVBoxLayout(sync)
        sv.setContentsMargins(10, 10, 10, 10)
        sv.setSpacing(10)

        self.current_job = QLabel("No active job")
        self.current_job.setObjectName("sectionTitle")

        sv.addWidget(self.current_job)

        scan = QHBoxLayout()
        scan.setSpacing(12)

        self.left_scan = QLabel("Scanning left: Ready")
        self.left_scan.setObjectName("scanCard")

        self.right_scan = QLabel("Scanning right: Ready")
        self.right_scan.setObjectName("scanCard")

        self.left_scan.setAlignment(Qt.AlignCenter)
        self.right_scan.setAlignment(Qt.AlignCenter)

        self.left_scan.setMinimumHeight(44)
        self.right_scan.setMinimumHeight(44)

        scan.addWidget(self.left_scan, 1)
        scan.addWidget(self.right_scan, 1)

        sv.addLayout(scan)

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)

        sv.addWidget(self.table, 1)

        foot = QHBoxLayout()
        foot.setSpacing(10)

        self.summary = QLabel()
        self.summary.setMinimumWidth(360)

        self.prog = QProgressBar()
        self.prog.setMinimumWidth(420)
        self.prog.setMaximumWidth(560)

        # Start hidden. It will only be shown for determinate operations.
        self.prog.hide()

        foot.addWidget(self.summary)
        foot.addStretch()
        foot.addWidget(self.prog)

        sv.addLayout(foot)

        self.tabs.addTab(sync, "Synchronization")

        results = QWidget()

        rv = QVBoxLayout(results)
        rv.setContentsMargins(10, 10, 10, 10)
        rv.setSpacing(10)

        rt = QHBoxLayout()
        rt.setSpacing(8)

        self.result_summary = QLabel("No report loaded")
        self.result_summary.setMinimumWidth(460)

        self.result_filter = QComboBox()
        self.result_filter.addItems(
            [
                "All results",
                "Problems only",
                "Failed",
                "Cancelled",
                "Skipped",
                "Not selected",
                "Conflict",
                "Copied",
                "Removed",
            ]
        )
        self.result_filter.currentTextChanged.connect(self.filter_results)

        open_logs = QPushButton("Open report folder")
        open_logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOGS))))

        export_csv = QPushButton("Export CSV")
        export_csv.clicked.connect(self.export_results_csv)

        rt.addWidget(self.result_summary)
        rt.addStretch()
        rt.addWidget(QLabel("Show"))
        rt.addWidget(self.result_filter)
        rt.addWidget(export_csv)
        rt.addWidget(open_logs)

        rv.addLayout(rt)

        self.results_table = QTableWidget(0, 6)
        self.results_table.setHorizontalHeaderLabels(
            ["Result", "Action", "Relative path", "Source", "Destination", "Details"]
        )
        self.results_table.setWordWrap(True)
        self.results_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results_table.setAlternatingRowColors(True)
        self.results_table.verticalHeader().hide()
        self.results_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.results_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.results_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)  # Stretch relative path
        self.results_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.results_table.setColumnWidth(3, 400)
        self.results_table.setColumnWidth(4, 400)
        self.results_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)

        rv.addWidget(self.results_table)

        self.tabs.addTab(results, "Results")

        hist = QWidget()

        hv = QVBoxLayout(hist)
        hv.setContentsMargins(10, 10, 10, 10)
        hv.setSpacing(10)

        hh = QHBoxLayout()

        self.hist_label = QLabel()

        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.reload_history)

        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear_history)

        hh.addWidget(self.hist_label)
        hh.addStretch()
        hh.addWidget(refresh)
        hh.addWidget(clear)

        hv.addLayout(hh)

        self.history_table = QTableWidget(0, 9)
        self.history_table.setHorizontalHeaderLabels(
            ["Time", "Job", "Operation", "Result", "Mode", "Items", "Duration", "Source", "Destination"]
        )
        self.history_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.history_table.setAlternatingRowColors(True)
        self.history_table.verticalHeader().hide()
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.history_table.horizontalHeader().setStretchLastSection(True)

        hwidths = [165, 180, 130, 170, 90, 80, 90, 300, 360]

        for i, wid in enumerate(hwidths):
            self.history_table.setColumnWidth(i, wid)

        hv.addWidget(self.history_table)

        self.tabs.addTab(hist, "History")

    def make_card(self, title, value, color="blue"):
        frame = QFrame()
        frame.setObjectName("card")
        frame.setProperty("tone", color)

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(4)

        title_label = QLabel(title)
        title_label.setObjectName("cardTitle")

        value_label = QLabel(value)
        value_label.setObjectName("cardValue")

        layout.addWidget(title_label)
        layout.addWidget(value_label)

        frame.value_label = value_label

        return frame

    def refresh_dashboard(self):
        rows = history_data()

        success = sum(1 for r in rows if r.get("result", "") == "Success")
        failed = sum(1 for r in rows if "Failed" in r.get("result", ""))
        warning = sum(
            1
            for r in rows
            if r.get("result", "") not in ("Success", "") and "Failed" not in r.get("result", "")
        )

        self.card_jobs.value_label.setText(str(len(self.state.jobs)))
        self.card_success.value_label.setText(str(success))
        self.card_warning.value_label.setText(str(warning))
        self.card_failed.value_label.setText(str(failed))

        self.recent_list.clear()

        for r in list(reversed(rows))[:8]:
            result = r.get("result", "")
            icon = "[OK]" if result == "Success" else ("[X]" if "Failed" in result else "[!]")

            meta = ""

            if r.get("operation") == "Synchronize":
                meta = (
                    f"  |  {mb_text(int(r.get('bytes_changed', 0)))}"
                    f"  |  {int(r.get('files_changed', 0)):,} files"
                    f"  |  {int(r.get('folders_changed', 0)):,} folders"
                )

            item = QListWidgetItem(
                f"{icon} {r.get('timestamp', '').replace('T', ' ')}"
                f"  {r.get('job', '')}  -  {result}{meta}"
            )
            # Store job_id for click navigation
            item.setData(Qt.UserRole, r.get('job_id'))
            self.recent_list.addItem(item)

        # Connect double-click to navigate to job
        self.recent_list.itemDoubleClicked.connect(self.on_dashboard_item_clicked)

        self.upcoming_list.clear()

        now = datetime.now()
        upcoming = []

        for j in self.state.jobs:
            if j.enabled and j.scheduler_enabled:
                try:
                    h, m = map(int, j.scheduler_time.split(":"))
                except Exception:
                    continue

                run = now.replace(hour=h, minute=m, second=0, microsecond=0)

                if run < now:
                    run += timedelta(days=1)

                upcoming.append((run, j.name, j.scheduler_action))

        for run, name, action in sorted(upcoming)[:8]:
            item = QListWidgetItem(f"{run:%Y-%m-%d %H:%M}  {name}  -  {action}")
            # Store job name for click navigation
            item.setData(Qt.UserRole, name)
            self.upcoming_list.addItem(item)

        # Connect double-click to navigate to job
        self.upcoming_list.itemDoubleClicked.connect(self.on_dashboard_item_clicked)

        if not upcoming:
            self.upcoming_list.addItem("No scheduled jobs configured")

    def on_dashboard_item_clicked(self, item):
        """Navigate to the Jobs tab and select the clicked job."""
        job_id = item.data(Qt.UserRole)
        job_name = item.data(Qt.UserRole)  # For upcoming items

        if job_id or job_name:
            # Switch to Jobs tab
            self.tabs.setCurrentIndex(1)

            # Find and select the job
            for row in range(self.jobs_table.rowCount()):
                row_job_id = self.jobs_table.item(row, 0).data(Qt.UserRole)
                row_job_name = self.jobs_table.item(row, 0).text()

                if row_job_id == job_id or row_job_name == job_name:
                    self.jobs_table.selectRow(row)
                    self.jobs_table.scrollToItem(self.jobs_table.item(row, 0))
                    break

    def set_parallel(self, n):
        self.state.max_parallel_jobs = n
        save_state(self.state)

    def selected_job(self) -> Optional[Job]:
        rows = self.jobs_table.selectionModel().selectedRows()

        if not rows:
            return None

        jid = self.jobs_table.item(rows[0].row(), 0).data(Qt.UserRole)

        return next((j for j in self.state.jobs if j.id == jid), None)

    def reload_jobs(self):
        self.jobs_table.setRowCount(len(self.state.jobs))

        for r, j in enumerate(self.state.jobs):
            vals = [
                j.name,
                j.mode,
                "Yes" if j.enabled else "No",
                j.scheduler_time if j.scheduler_enabled else "Disabled",
                j.last_run,
                j.last_result,
                j.source,
                j.destination,
            ]

            for c, v in enumerate(vals):
                it = QTableWidgetItem(str(v))

                if c == 0:
                    it.setData(Qt.UserRole, j.id)

                # Add tooltip for long text
                if len(str(v)) > 50:
                    it.setToolTip(str(v))

                self.jobs_table.setItem(r, c, it)

        # Re-apply filter if search text exists
        if hasattr(self, 'job_search') and self.job_search.text():
            self.filter_jobs_table(self.job_search.text())

        if hasattr(self, "card_jobs"):
            self.refresh_dashboard()

    def filter_jobs_table(self, text):
        """Filter jobs table based on search text."""
        text = text.lower()

        for row in range(self.jobs_table.rowCount()):
            match = False

            for col in range(self.jobs_table.columnCount()):
                item = self.jobs_table.item(row, col)
                if item and text in item.text().lower():
                    match = True
                    break

            self.jobs_table.setRowHidden(row, not match)

    def save(self):
        save_state(self.state)
        self.reload_jobs()

    def new_job(self):
        d = JobDialog(Job(name=f"Job {len(self.state.jobs) + 1}"), self.state.templates, self)

        if d.exec():
            self.state.jobs.append(d.result_job())
            self.save()

    def copy_job(self):
        j = self.selected_job()

        if not j:
            return

        d = job_from_dict(asdict(j))
        d.id = str(uuid.uuid4())
        d.name += " Copy"

        self.state.jobs.append(d)
        self.save()

    def edit_job(self):
        j = self.selected_job()

        if not j:
            return

        d = JobDialog(j, self.state.templates, self)

        if d.exec():
            nj = d.result_job()
            idx = self.state.jobs.index(j)

            self.state.jobs[idx] = nj
            self.save()

    def delete_job(self):
        j = self.selected_job()

        if j and QMessageBox.question(
            self,
            "Delete job",
            f"Delete job {j.name}?",
            QMessageBox.Yes | QMessageBox.No,
        ) == QMessageBox.Yes:
            self.state.jobs = [x for x in self.state.jobs if x.id != j.id]
            self.save()

    def export_jobs(self):
        p, _ = QFileDialog.getSaveFileName(
            self,
            "Export jobs",
            str(RUN_DIR / "darksync_jobs_export.json"),
            "JSON (*.json)",
        )

        if p:
            Path(p).write_text(json.dumps(asdict(self.state), indent=2), encoding="utf-8")

    def import_jobs(self):
        p, _ = QFileDialog.getOpenFileName(self, "Import jobs", str(RUN_DIR), "JSON (*.json)")

        if not p:
            return

        data = json.loads(Path(p).read_text(encoding="utf-8"))
        incoming = [job_from_dict(x) for x in data.get("jobs", [])]

        for j in incoming:
            j.id = str(uuid.uuid4())
            self.state.jobs.append(j)

        self.save()

    def change_theme(self, theme_name):
        """Change the application theme."""
        app = QApplication.instance()
        if app:
            stylesheet = get_stylesheet(theme_name)
            app.setStyleSheet(stylesheet)
            # Save theme preference
            config_file = RUN_DIR / "darksync_config.json"
            try:
                if config_file.exists():
                    with open(config_file, "r") as f:
                        config = json.load(f)
                else:
                    config = {}
                config["theme"] = theme_name
                with open(config_file, "w") as f:
                    json.dump(config, f, indent=2)
            except Exception:
                pass  # Silently fail if config save fails

    def load_saved_theme(self):
        """Load and apply saved theme from config."""
        config_file = RUN_DIR / "darksync_config.json"
        saved_theme = "Dark Blue"  # Default
        try:
            if config_file.exists():
                with open(config_file, "r") as f:
                    config = json.load(f)
                    saved_theme = config.get("theme", "Dark Blue")
        except Exception:
            pass
        
        # Update the theme group to reflect saved theme
        for action in self.theme_group.actions():
            action.setChecked(action.text() == saved_theme)
        
        # Apply the saved theme
        app = QApplication.instance()
        if app:
            stylesheet = get_stylesheet(saved_theme)
            app.setStyleSheet(stylesheet)

    def create_startup_task(self):
        if sys.platform != "win32":
            QMessageBox.information(
                self,
                "Windows only",
                "Task Scheduler integration is available on Windows only.",
            )
            return

        exe = sys.executable if getattr(sys, "frozen", False) else sys.executable
        arg = "" if getattr(sys, "frozen", False) else f' "{Path(__file__).resolve()}"'

        cmd = [
            "schtasks",
            "/Create",
            "/TN",
            "DarkSync",
            "/SC",
            "ONLOGON",
            "/TR",
            f'"{exe}"{arg}',
            "/F",
        ]

        try:
            subprocess.check_call(cmd)
            QMessageBox.information(self, "Created", "Windows startup task created.")
        except Exception as ex:
            QMessageBox.critical(self, "Failed", str(ex))

    def selected_running_job_ids(self):
        j = self.selected_job()

        return [j.id] if j and j.id in self.workers else list(self.running_jobs)

    def pause_resume_jobs(self):
        ids = self.selected_running_job_ids()

        if not ids:
            QMessageBox.information(self, "No running job", "There is no active operation to pause.")
            return

        pause = any(not self.workers[x].is_paused() for x in ids if x in self.workers)

        for x in ids:
            w = self.workers.get(x)

            if w:
                if pause:
                    w.pause()
                else:
                    w.resume()

        self.statusBar().showMessage("Paused" if pause else "Resumed", 5000)

    def cancel_running_jobs(self):
        ids = self.selected_running_job_ids()

        if not ids:
            QMessageBox.information(self, "No running job", "There is no active operation to cancel.")
            return

        if QMessageBox.question(
            self,
            "Cancel",
            f"Cancel {len(ids)} running job(s)?",
            QMessageBox.Yes | QMessageBox.No,
        ) != QMessageBox.Yes:
            return

        for x in ids:
            w = self.workers.get(x)

            if w:
                w.cancel()

    def run_selected(self):
        j = self.selected_job()

        if j:
            self.enqueue_jobs([j])

    def run_all(self):
        enabled_jobs = [j for j in self.state.jobs if j.enabled]

        if not enabled_jobs:
            QMessageBox.information(self, "No jobs", "No enabled jobs to run.")
            return

        # Show confirmation for bulk execution
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Warning)
        msg.setWindowTitle("Confirm Bulk Execution")
        msg.setText(f"About to run {len(enabled_jobs)} job(s)")

        job_list = "\n".join(f"• {j.name} ({j.mode})" for j in enabled_jobs)
        msg.setDetailedText(job_list)
        msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg.setDefaultButton(QMessageBox.No)

        if msg.exec() == QMessageBox.Yes:
            self.enqueue_jobs(enabled_jobs)

    def enqueue_jobs(self, jobs):
        self.job_queue.extend(jobs)
        self.start_next_jobs()

    def start_next_jobs(self):
        while self.job_queue and len(self.running_jobs) < self.state.max_parallel_jobs:
            entry = self.job_queue.pop(0)

            if isinstance(entry, tuple):
                j, op = entry
            else:
                j, op = entry, "compare_sync"

            self.start_worker(j, op)

        self.reload_jobs()

    def start_worker(self, j, op, items=None, undo_payload=None):
        if op != "undo" and (not Path(j.source).is_dir() or not Path(j.destination).is_dir()):
            QMessageBox.warning(self, "Invalid folders", f"{j.name}: source or destination missing.")
            return

        th = QThread(self)
        wk = SyncWorker(j, op, items, undo_payload)

        wk.moveToThread(th)

        th.started.connect(wk.run)

        wk.progress.connect(self.on_progress)
        wk.compared.connect(self.on_compared)
        wk.finished.connect(self.on_finished)
        wk.failed.connect(self.on_failed)

        wk.finished.connect(th.quit)
        wk.failed.connect(th.quit)

        th.finished.connect(lambda jid=j.id: self.cleanup(jid))

        self.threads[j.id] = th
        self.workers[j.id] = wk
        self.running_jobs.add(j.id)

        # Hide progress bar initially - will show during compare/sync phases
        self.prog.hide()

        th.start()
        self.tabs.setCurrentIndex(2)

    def cleanup(self, jid):
        self.threads.pop(jid, None)
        self.workers.pop(jid, None)
        self.running_jobs.discard(jid)

        self.start_next_jobs()

    def on_progress(self, jid, n, total, text):
        j = next((x for x in self.state.jobs if x.id == jid), None)
        self.current_job.setText(f"Active: {j.name if j else jid}")

        scanning = False

        if text.startswith("Scanning source and destination"):
            self.left_scan.setText("Scanning left: Starting...")
            self.right_scan.setText("Scanning right: Starting...")
            scanning = True
        elif text.startswith("Scanning left:"):
            self.left_scan.setText(text)
            scanning = True
        elif text.startswith("Scanning right:"):
            self.right_scan.setText(text)
            scanning = True

        if scanning or not total:
            # Hide progress bar during scanning phase - no animation needed
            self.prog.hide()
        else:
            # Determinate progress for comparing, synchronizing, recovering, etc.
            self.prog.setRange(0, total)
            self.prog.setValue(n)
            self.prog.setFormat(text)
            self.prog.show()

        self.statusBar().showMessage(text)

    def on_compared(self, jid, items, warnings):
        j = next((x for x in self.state.jobs if x.id == jid), None)

        self.active_items = items
        self.model.set_items(items)

        self.summary.setText(
            f"{j.name if j else jid}: {len(items)} items, "
            f"{sum(x.action not in ('Skip', 'Conflict') for x in items)} actions"
        )

        self.left_scan.setText("Scanning left: Complete")
        self.right_scan.setText("Scanning right: Complete")

    def on_finished(self, jid, result):
        j = next((x for x in self.state.jobs if x.id == jid), None)

        if not j:
            return

        dur = 0

        if result.get("operation") == "compare":
            add_history(j, "Compare", "Success", result.get("items", 0), dur)
            
            # Show completion message for compare operation
            self.prog.setRange(0, 1)
            self.prog.setValue(1)
            self.prog.setFormat("Comparison completed")
            self.statusBar().showMessage(f"Comparison completed: {result.get('items', 0)} items found", 5000)

        elif result.get("operation") == "sync":
            sm = result["summary"]

            problems = sum(sm.get(k, 0) for k in REPORT_STATUSES)

            guard = result.get("guard") or {}
            override = bool(guard.get("manually_approved"))

            if override:
                res = (
                    "Success (manual guard override)"
                    if problems == 0
                    else "Completed with issues (manual guard override)"
                )
            else:
                res = "Success" if problems == 0 else "Completed with issues"

            j.last_run = datetime.now().isoformat(timespec="seconds")
            j.last_result = res
            j.last_report = result.get("report", "")

            details_prefix = (
                "Ignored scan errors enabled. "
                if j.ignore_scan_errors
                else ("Ignored permission errors enabled. " if j.ignore_permission_errors else "")
            )

            add_history(
                j,
                "Synchronize",
                res,
                sm.get("Copied", 0) + sm.get("Removed", 0),
                dur,
                result.get("report", ""),
                details_prefix + "; ".join(result.get("errors", [])[:10]),
                int(sm.get("Bytes changed", 0)),
                int(sm.get("Files changed", 0)),
                int(sm.get("Folders changed", 0)),
            )

            self.load_report(result["report_data"])

            try:
                send_notification(j, f"DarkSync {res}: {j.name}", json.dumps(sm, indent=2), problems == 0)
            except Exception as ex:
                self.statusBar().showMessage(f"Notification failed: {ex}", 8000)

            # Show completion message in progress bar and status
            self.prog.setRange(0, 1)
            self.prog.setValue(1)
            self.prog.setFormat("Completed")
            self.statusBar().showMessage(f"Synchronization completed: {res}", 5000)

            # Exit application if configured for automated runs
            if j.exit_on_completion:
                QTimer.singleShot(2000, qApp.quit)  # Wait 2 seconds then exit

        elif result.get("operation") == "guard_blocked":
            g = result["guard"]
            details = guard_details(g)
            res = "Blocked by Ransomware Guard"

            j.last_run = datetime.now().isoformat(timespec="seconds")
            j.last_result = res

            add_history(
                j,
                "Pre-flight protection",
                res,
                g["changed"],
                0,
                details=details,
                files_changed=g["files_added"] + g["files_deleted"] + g["files_modified"],
                folders_changed=g["folders_added"] + g["folders_deleted"],
            )

            try:
                send_notification(j, f"DarkSync Blocked: {j.name}", details, False)
            except Exception as ex:
                self.statusBar().showMessage(f"Notification failed: {ex}", 8000)

            answer = QMessageBox.warning(
                self,
                "Ransomware Guard threshold exceeded",
                f"{j.name} was NOT executed.\n"
                f"{details}\n"
                f"Allowed threshold: {j.guard_threshold_percent:.2f}%\n"
                "Proceed only after verifying these are legitimate changes. DarkSync will rescan before running.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )

            if answer == QMessageBox.Yes:
                confirm = QMessageBox.question(
                    self,
                    "Confirm manual override",
                    f"Manually override Ransomware Guard for '{j.name}'?\n"
                    "This may copy encrypted or damaged files if the source is compromised.",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )

                if confirm == QMessageBox.Yes:
                    add_history(
                        j,
                        "Ransomware Guard override",
                        "Manually approved",
                        g["changed"],
                        0,
                        details=details,
                    )

                    self.job_queue.insert(0, (j, "compare_sync_override"))

        elif result.get("operation") == "undo":
            res = "Success" if result.get("success") else "Completed with issues"

            add_history(
                j,
                "Undo/Recover",
                res,
                result.get("restored", 0),
                dur,
                details="; ".join(result.get("errors", [])[:10]),
            )

            QMessageBox.information(
                self,
                "Recovery complete",
                f"Recovered/reversed: {result.get('restored', 0)}\n"
                f"Errors: {len(result.get('errors', []))}",
            )
            
            # Show completion message for undo operation
            self.prog.setRange(0, 1)
            self.prog.setValue(1)
            self.prog.setFormat("Recovery completed")
            self.statusBar().showMessage(f"Recovery completed: {res}", 5000)

        self.save()
        self.reload_history()
        self.reload_jobs()

    def on_failed(self, jid, msg):
        j = next((x for x in self.state.jobs if x.id == jid), None)

        if j:
            j.last_run = datetime.now().isoformat(timespec="seconds")
            j.last_result = "Failed"

            add_history(j, "Operation", "Failed", 0, 0, details=msg)

            self.save()
            self.reload_history()

            try:
                send_notification(j, f"DarkSync Failed: {j.name}", msg, False)
            except Exception as ex:
                self.statusBar().showMessage(f"Notification failed: {ex}", 8000)

        # Show failure message in progress bar and status
        self.prog.setRange(0, 1)
        self.prog.setValue(1)
        self.prog.setFormat("Failed")
        self.statusBar().showMessage(f"Operation failed: {msg[:100]}", 8000)

        # Exit application if configured for automated runs (even on failure)
        if j and getattr(j, 'exit_on_completion', False):
            QTimer.singleShot(2000, qApp.quit)  # Wait 2 seconds then exit

        # Enhanced error dialog with troubleshooting steps
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Critical)
        dialog.setWindowTitle("Operation Failed")
        job_name = j.name if j else "Unknown Job"
        dialog.setText(f"<b>{job_name}</b> failed")
        dialog.setInformativeText(msg)

        # Add troubleshooting steps
        details = "Troubleshooting steps:\n"
        details += "1. Check source/destination paths exist\n"
        details += "2. Verify read/write permissions\n"
        details += "3. Ensure sufficient disk space\n"
        details += "4. Review logs in: " + str(LOGS)

        dialog.setDetailedText(details)
        dialog.addButton("Open Logs", QMessageBox.ActionRole)
        dialog.addButton("Close", QMessageBox.AcceptRole)

        choice = dialog.exec()
        if choice == 0:  # Open Logs button
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOGS)))

    def load_report(self, rd):
        self.latest_report_rows = rd.get("items", [])

        sm = rd.get("summary", {})

        problems = sum(sm.get(k, 0) for k in REPORT_STATUSES)

        self.result_summary.setText(
            f"Copied: {sm.get('Copied', 0)}  |  "
            f"Removed: {sm.get('Removed', 0)}  |  "
            f"{mb_text(int(sm.get('Bytes changed', 0)))}  |  "
            f"Folders: {sm.get('Folders changed', 0)}  |  "
            f"Problems/not processed: {problems}"
        )

        self.filter_results()

        self.tabs.setCurrentIndex(3)

    def filter_results(self):
        sel = self.result_filter.currentText()

        rows = [
            r
            for r in self.latest_report_rows
            if sel == "All results"
            or (sel == "Problems only" and r.get("status") in REPORT_STATUSES)
            or r.get("status") == sel
        ]

        self.results_table.setRowCount(len(rows))

        colors = {
            "Failed": "#fc8181",
            "Cancelled": "#f6ad55",
            "Conflict": "#f6e05e",
            "Skipped": "#cbd5e1",
            "Not selected": "#94a3b8",
            "Copied": "#68d391",
            "Removed": "#73c7ff",
        }

        for r, row in enumerate(rows):
            for c, k in enumerate(["status", "action", "relative", "source", "destination", "details"]):
                it = QTableWidgetItem(str(row.get(k, "")))
                it.setToolTip(str(row.get("details", "")))

                if c == 0:
                    it.setForeground(QColor(colors.get(row.get("status"), "#e5e7eb")))

                self.results_table.setItem(r, c, it)

        self.results_table.resizeRowsToContents()

    def reload_history(self):
        # Load saved theme after UI is initialized
        if hasattr(self, 'theme_group'):
            self.load_saved_theme()
        
        rows = list(reversed(history_data()))

        self.history_table.setRowCount(len(rows))
        self.hist_label.setText(f"{len(rows)} history record(s)")

        for r, d in enumerate(rows):
            vals = [
                d.get("timestamp", "").replace("T", " "),
                d.get("job", ""),
                d.get("operation", ""),
                d.get("result", ""),
                d.get("mode", ""),
                str(d.get("items", 0)),
                f"{d.get('duration', 0):.1f}s",
                d.get("source", ""),
                d.get("destination", ""),
            ]

            for c, v in enumerate(vals):
                self.history_table.setItem(r, c, QTableWidgetItem(v))

        if hasattr(self, "card_jobs"):
            self.refresh_dashboard()

    def clear_history(self):
        if QMessageBox.question(
            self,
            "Clear history",
            "Delete operation history?",
            QMessageBox.Yes | QMessageBox.No,
        ) == QMessageBox.Yes:
            HISTORY_FILE.unlink(missing_ok=True)
            self.reload_history()

    def undo_recover(self):
        j = self.selected_job()

        if not j:
            QMessageBox.information(self, "Select job", "Select the job whose undo history you want.")
            return

        cleanup_undo(j)

        if not available_undo_manifests(j):
            QMessageBox.information(
                self,
                "No undo history",
                "No undo journals exist for this job within the last five days.",
            )
            return

        d = UndoDialog(j, self)

        if d.exec() != QDialog.Accepted:
            return

        payload = d.payload()
        side = payload.get("recovery_side")

        if side in ("source", "destination"):
            expected = Path(j.source if side == "source" else j.destination)

            if not expected.is_dir():
                chosen = QFileDialog.getExistingDirectory(
                    self,
                    "Select existing recovery folder",
                    str(RUN_DIR),
                )

                if not chosen:
                    return

                payload["recovery_root"] = chosen
            else:
                payload["recovery_root"] = str(expected)

        self.start_worker(j, "undo", undo_payload=payload)

    def check_schedules(self):
        now = datetime.now()

        today = now.strftime("%Y-%m-%d")
        hm = now.strftime("%H:%M")

        due = []

        for j in self.state.jobs:
            if j.enabled and j.scheduler_enabled and j.last_schedule_date != today and hm >= j.scheduler_time:
                j.last_schedule_date = today
                due.append(j)

        if due:
            self.save()
            self.enqueue_jobs(due)

    def export_results_csv(self):
        """Export current filtered results to CSV file."""
        if not self.latest_report_rows:
            QMessageBox.information(self, "No data", "No results to export.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export results", str(RUN_DIR / "darksync_results.csv"), "CSV Files (*.csv)"
        )

        if not path:
            return

        import csv

        try:
            with open(path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(["Result", "Action", "Relative path", "Source", "Destination", "Details"])

                for row in self.latest_report_rows:
                    writer.writerow([
                        row.get("status", ""),
                        row.get("action", ""),
                        row.get("relative", ""),
                        row.get("source", ""),
                        row.get("destination", ""),
                        row.get("details", "")
                    ])

            QMessageBox.information(self, "Export complete", f"Results exported to:\n{path}")
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))
        except Exception as ex:
            QMessageBox.critical(self, "Export failed", f"Could not export results:\n{ex}")


# Theme definitions with different color schemes
THEMES = {
    "Dark Blue": {
        "bg": "#111827",
        "window_bg": "#0b1220",
        "text": "#e5e7eb",
        "title_text": "#f8fafc",
        "muted": "#94a3b8",
        "panel_bg": "#172033",
        "card_bg": "#172033",
        "border": "#2b3952",
        "input_bg": "#0f172a",
        "button_bg": "#263348",
        "button_hover": "#334155",
        "primary": "#2563eb",
        "table_alt": "#151f30",
        "header_bg": "#1e293b",
        "header_text": "#cbd5e1",
        "progress_bg": "#0f172a",
        "progress_chunk": "#64748b",
        "tab_bg": "#172033",
        "tab_selected": "#2563eb",
        "toolbar_bg": "#111827",
        "toolbar_border": "#263348",
        "status_bg": "#0b1220",
        "status_text": "#94a3b8",
        "green": "#22c55e",
        "orange": "#f59e0b",
        "red": "#ef4444",
        "blue": "#3b82f6",
    },
    "Dark Green": {
        "bg": "#0f1a12",
        "window_bg": "#08120a",
        "text": "#d4e5d8",
        "title_text": "#f0fff4",
        "muted": "#8ba892",
        "panel_bg": "#122518",
        "card_bg": "#122518",
        "border": "#1f3d2a",
        "input_bg": "#0a1f14",
        "button_bg": "#1a3d28",
        "button_hover": "#245236",
        "primary": "#16a34a",
        "table_alt": "#112a1c",
        "header_bg": "#153524",
        "header_text": "#c4dcc8",
        "progress_bg": "#0a1f14",
        "progress_chunk": "#4a7c5e",
        "tab_bg": "#122518",
        "tab_selected": "#16a34a",
        "toolbar_bg": "#0f1a12",
        "toolbar_border": "#1f3d2a",
        "status_bg": "#08120a",
        "status_text": "#8ba892",
        "green": "#22c55e",
        "orange": "#f59e0b",
        "red": "#ef4444",
        "blue": "#3b82f6",
    },
    "Dark Orange": {
        "bg": "#1a140f",
        "window_bg": "#120d08",
        "text": "#e5ded8",
        "title_text": "#fcf8f4",
        "muted": "#a89a8a",
        "panel_bg": "#251a12",
        "card_bg": "#251a12",
        "border": "#3d2a1f",
        "input_bg": "#1f140a",
        "button_bg": "#3d281a",
        "button_hover": "#523624",
        "primary": "#ea580c",
        "table_alt": "#2a1a11",
        "header_bg": "#352215",
        "header_text": "#dcd2c4",
        "progress_bg": "#1f140a",
        "progress_chunk": "#7c5e4a",
        "tab_bg": "#251a12",
        "tab_selected": "#ea580c",
        "toolbar_bg": "#1a140f",
        "toolbar_border": "#3d281a",
        "status_bg": "#120d08",
        "status_text": "#a89a8a",
        "green": "#22c55e",
        "orange": "#f59e0b",
        "red": "#ef4444",
        "blue": "#3b82f6",
    },
    "Dark Yellow": {
        "bg": "#1a180f",
        "window_bg": "#121008",
        "text": "#e5e2d8",
        "title_text": "#fcfbf4",
        "muted": "#a8a28a",
        "panel_bg": "#252212",
        "card_bg": "#252212",
        "border": "#3d361f",
        "input_bg": "#1f1c0a",
        "button_bg": "#3d361a",
        "button_hover": "#524a24",
        "primary": "#ca8a04",
        "table_alt": "#2a2611",
        "header_bg": "#352f15",
        "header_text": "#dcd8c4",
        "progress_bg": "#1f1c0a",
        "progress_chunk": "#7c724a",
        "tab_bg": "#252212",
        "tab_selected": "#ca8a04",
        "toolbar_bg": "#1a180f",
        "toolbar_border": "#3d361a",
        "status_bg": "#121008",
        "status_text": "#a8a28a",
        "green": "#22c55e",
        "orange": "#f59e0b",
        "red": "#ef4444",
        "blue": "#3b82f6",
    },
    "Midnight Purple": {
        "bg": "#140f1a",
        "window_bg": "#0d0812",
        "text": "#ddd4e5",
        "title_text": "#f4f0fc",
        "muted": "#9a8aa8",
        "panel_bg": "#1f1525",
        "card_bg": "#1f1525",
        "border": "#33223d",
        "input_bg": "#140a1f",
        "button_bg": "#2a1a3d",
        "button_hover": "#3d2452",
        "primary": "#7c3aed",
        "table_alt": "#1a112a",
        "header_bg": "#2a1535",
        "header_text": "#d4c4dc",
        "progress_bg": "#140a1f",
        "progress_chunk": "#6a4a7c",
        "tab_bg": "#1f1525",
        "tab_selected": "#7c3aed",
        "toolbar_bg": "#140f1a",
        "toolbar_border": "#2a1a3d",
        "status_bg": "#0d0812",
        "status_text": "#9a8aa8",
        "green": "#22c55e",
        "orange": "#f59e0b",
        "red": "#ef4444",
        "blue": "#3b82f6",
    },
    "Ocean Teal": {
        "bg": "#0f1a1c",
        "window_bg": "#081214",
        "text": "#d4e5e8",
        "title_text": "#f0fdfc",
        "muted": "#8aa8ac",
        "panel_bg": "#122528",
        "card_bg": "#122528",
        "border": "#1f3d42",
        "input_bg": "#0a1f22",
        "button_bg": "#1a3d42",
        "button_hover": "#245258",
        "primary": "#0d9488",
        "table_alt": "#112a2d",
        "header_bg": "#15353a",
        "header_text": "#c4dcdc",
        "progress_bg": "#0a1f22",
        "progress_chunk": "#4a7c82",
        "tab_bg": "#122528",
        "tab_selected": "#0d9488",
        "toolbar_bg": "#0f1a1c",
        "toolbar_border": "#1a3d42",
        "status_bg": "#081214",
        "status_text": "#8aa8ac",
        "green": "#22c55e",
        "orange": "#f59e0b",
        "red": "#ef4444",
        "blue": "#3b82f6",
    },
    "Slate Gray": {
        "bg": "#14161a",
        "window_bg": "#0d0f12",
        "text": "#d8dade",
        "title_text": "#f4f6fa",
        "muted": "#8f94a0",
        "panel_bg": "#1d2026",
        "card_bg": "#1d2026",
        "border": "#2f343d",
        "input_bg": "#12151a",
        "button_bg": "#262a33",
        "button_hover": "#333842",
        "primary": "#475569",
        "table_alt": "#171a20",
        "header_bg": "#22262f",
        "header_text": "#c8ccd4",
        "progress_bg": "#12151a",
        "progress_chunk": "#5a6270",
        "tab_bg": "#1d2026",
        "tab_selected": "#475569",
        "toolbar_bg": "#14161a",
        "toolbar_border": "#262a33",
        "status_bg": "#0d0f12",
        "status_text": "#8f94a0",
        "green": "#22c55e",
        "orange": "#f59e0b",
        "red": "#ef4444",
        "blue": "#3b82f6",
    },
    "High Contrast": {
        "bg": "#000000",
        "window_bg": "#000000",
        "text": "#ffffff",
        "title_text": "#ffffff",
        "muted": "#cccccc",
        "panel_bg": "#1a1a1a",
        "card_bg": "#1a1a1a",
        "border": "#444444",
        "input_bg": "#0a0a0a",
        "button_bg": "#2a2a2a",
        "button_hover": "#444444",
        "primary": "#0066cc",
        "table_alt": "#111111",
        "header_bg": "#222222",
        "header_text": "#eeeeee",
        "progress_bg": "#0a0a0a",
        "progress_chunk": "#666666",
        "tab_bg": "#1a1a1a",
        "tab_selected": "#0066cc",
        "toolbar_bg": "#000000",
        "toolbar_border": "#2a2a2a",
        "status_bg": "#000000",
        "status_text": "#cccccc",
        "green": "#22c55e",
        "orange": "#f59e0b",
        "red": "#ef4444",
        "blue": "#3b82f6",
    },
}


def get_stylesheet(theme_name="Dark Blue"):
    """Generate stylesheet for the specified theme."""
    t = THEMES.get(theme_name, THEMES["Dark Blue"])
    return f'''QWidget{{background:{t["bg"]};color:{t["text"]};font-family:"Segoe UI",Arial;font-size:10pt}}QMainWindow{{background:{t["window_bg"]}}}QLabel#title{{font-size:20pt;font-weight:700;color:{t["title_text"]};padding:4px}}QLabel#sectionTitle{{font-size:12pt;font-weight:700;color:{t["title_text"]};padding:6px 2px}}QLabel#muted{{color:{t["muted"]};font-size:10pt}}QFrame#panel{{background:{t["panel_bg"]};border:1px solid {t["border"]};border-radius:12px}}QFrame#card{{background:{t["card_bg"]};border:1px solid {t["border"]};border-radius:14px}}QFrame#card[tone=green]{{border-color:{t["green"]}}}QFrame#card[tone=orange]{{border-color:{t["orange"]}}}QFrame#card[tone=red]{{border-color:{t["red"]}}}QFrame#card[tone=blue]{{border-color:{t["blue"]}}}QLabel#cardTitle{{color:{t["muted"]};font-size:10pt;font-weight:600}}QLabel#cardValue{{color:{t["title_text"]};font-size:26pt;font-weight:800}}QListWidget#dashboardList{{background:{t["input_bg"]};border:1px solid {t["border"]};border-radius:9px;padding:6px}}QLabel#scanCard{{background:{t["input_bg"]};border:1px solid {t["border"]};border-radius:8px;padding:9px;color:{t["title_text"]};font-weight:700}}QLineEdit,QComboBox,QSpinBox,QTimeEdit{{background:{t["input_bg"]};border:1px solid {t["border"]};border-radius:7px;padding:8px}}QPushButton{{background:{t["button_bg"]};border:1px solid {t["border"]};border-radius:7px;padding:8px 14px;font-weight:600}}QPushButton:hover{{background:{t["button_hover"]}}}QPushButton#primary{{background:{t["primary"]}}}:hover{{background:{t["button_hover"]}}}QTableView,QTableWidget{{background:{t["bg"]};alternate-background-color:{t["table_alt"]};border:1px solid {t["border"]};border-radius:10px;gridline-color:{t["button_bg"]}}}QHeaderView::section{{background:{t["header_bg"]};color:{t["header_text"]};border:0;border-right:1px solid {t["border"]};padding:9px;font-weight:700}}QProgressBar{{background:{t["progress_bg"]};border:1px solid {t["border"]};border-radius:6px;text-align:center;min-height:20px;color:{t["text"]}}}QProgressBar::chunk{{background:{t["progress_chunk"]};border-radius:5px}}QTabWidget::pane{{border:1px solid {t["border"]};border-radius:8px}}QTabBar::tab{{background:{t["tab_bg"]};padding:10px 18px;border:1px solid {t["border"]}}}QTabBar::tab:selected{{background:{t["tab_selected"]}}}QToolBar{{background:{t["toolbar_bg"]};border-bottom:1px solid {t["toolbar_border"]};spacing:4px;padding:6px}}QToolBar QToolButton{{margin:2px;padding:7px 10px;border-radius:6px;background:transparent;border:none}}QToolBar QToolButton:hover{{background:{t["button_hover"]}}}QMenuBar{{background:{t["toolbar_bg"]};border-bottom:1px solid {t["border"]}}}QMenuBar::item{{padding:8px 12px}}QMenuBar::item:selected{{background:{t["button_hover"]}}}QMenu{{background:{t["card_bg"]};border:1px solid {t["border"]};padding:4px}}QMenu::item{{padding:8px 24px}}QMenu::item:selected{{background:{t["button_hover"]}}}QStatusBar{{background:{t["status_bg"]};color:{t["status_text"]}}}'''


QSS = get_stylesheet("Dark Blue")


def main():
    global qApp
    app = QApplication(sys.argv)
    qApp = app  # Set global reference after creation

    app.setStyle("Fusion")
    app.setStyleSheet(QSS)

    w = Main()
    w.showMaximized()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()