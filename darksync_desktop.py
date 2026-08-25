#!/usr/bin/env python3
"""DarkSync Desktop — Standalone Edition.

A standalone PySide6 application with the darksync.html visual design.
All filesystem operations (scan, compare, sync, undo, guard, scheduling,
notifications) run locally in this process — no browser helper needed.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import platform
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
    Slot,
    Signal,
    QTimer,
    QTime,
    QUrl,
)
from PySide6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QShortcut, QKeySequence
from PySide6.QtWidgets import *

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
HISTORY_MAX_ENTRIES = max(1, HISTORY_MAX_ENTRIES)
GUARD_DEFAULT_THRESHOLD_PERCENT = float(os.environ.get("DARKSYNC_GUARD_THRESHOLD_PERCENT", "4.0"))
IO_RETRY_ATTEMPTS = max(1, int(os.environ.get("DARKSYNC_IO_RETRY_ATTEMPTS", "3")))
IO_RETRY_DELAY_SECONDS = max(0.0, float(os.environ.get("DARKSYNC_IO_RETRY_DELAY_SECONDS", "0.25")))
SCAN_BATCH_SIZE = max(1, int(os.environ.get("DARKSYNC_SCAN_BATCH_SIZE", "128")))
MAX_TOOLTIP_LENGTH = max(80, int(os.environ.get("DARKSYNC_MAX_TOOLTIP_LENGTH", "512")))
MAX_SCAN_ERROR_DETAILS = 20
RECENT_HISTORY_ITEMS = 8
SHUTDOWN_TIMEOUT_SECONDS = 15
AUTO_EXIT_DELAY_MS = 2000
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"

_history_lock = threading.Lock()
_load_warning: str = ""

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
    exclude: str = ".DS_Store;Thumbs.db;desktop.ini;.darksync_*;*.darksync_tmp_*;logs/*;$Recycle.Bin/;System Volume Information/;$WINDOWS.~BT/;$WinREAgent/;hiberfil.sys;pagefile.sys;swapfile.sys;~$*;~*.docx;~*.xlsx;~*.pptx;.~lock.*;*.tmp;~WRL*.tmp;*.asd;*.temp"
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
        validate_job_configuration(self)

        if self.source and self.destination:
            src = Path(self.source).resolve()
            dst = Path(self.destination).resolve()
            
            if src == dst:
                raise ValueError("Source and destination cannot be the same path")
            
            if src in dst.parents:
                raise ValueError("Destination cannot be inside source directory")
            
            if dst in src.parents:
                raise ValueError("Source cannot be inside destination directory")
        
        if self.workers < MIN_WORKERS:
            self.workers = MIN_WORKERS
        elif self.workers > MAX_WORKERS:
            self.workers = MAX_WORKERS
        
        if self.guard_threshold_percent < 0:
            self.guard_threshold_percent = 0.0
        elif self.guard_threshold_percent > 100:
            self.guard_threshold_percent = 100.0


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
    global _load_warning

    def default_state():
        state = AppState()
        state.templates = [
            Job(name="Mirror Backup Template", template="Template", mode="Mirror"),
            Job(name="Update Archive Template", template="Template", mode="Update"),
        ]
        return state

    if not JOBS_FILE.exists():
        state = default_state()
        save_state(state)
        return state

    try:
        data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except OSError as exc:
        _load_warning = (
            f"Could not read {JOBS_FILE.name}: {exc}\n"
            "The application opened with empty defaults. If the file is locked by "
            "another process, close it and restart — your jobs will be restored."
        )
        return default_state()
    except json.JSONDecodeError as exc:
        _load_warning = (
            f"{JOBS_FILE.name} is corrupted ({exc}).\n"
            "The application opened with empty defaults. Your previous jobs are "
            "still on disk; back up the file before saving any changes."
        )
        return default_state()

    state = AppState(
        active_job_id=data.get("active_job_id", ""),
        max_parallel_jobs=int(data.get("max_parallel_jobs", 1)),
    )

    state.jobs = [job_from_dict(x) for x in data.get("jobs", [])]
    state.templates = [job_from_dict(x) for x in data.get("templates", [])]

    return state


def save_state(state: AppState):
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(JOBS_FILE, json.dumps(asdict(state), indent=2))


def history_data():
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _retryable_io_error(ex: BaseException) -> bool:
    return isinstance(ex, (PermissionError, BlockingIOError, TimeoutError)) or getattr(ex, "errno", None) in {
        13,  # EACCES / sharing violations on Windows
        16,  # EBUSY
        26,  # ETXTBSY
        35,  # EAGAIN on some platforms
    }


def retry_io(operation, description: str):
    """Run a filesystem operation with bounded retries for transient locks."""
    last_error = None

    for attempt in range(IO_RETRY_ATTEMPTS):
        try:
            return operation()
        except (OSError, IOError) as ex:
            last_error = ex
            if not _retryable_io_error(ex) or attempt + 1 >= IO_RETRY_ATTEMPTS:
                raise
            time.sleep(IO_RETRY_DELAY_SECONDS * (attempt + 1))

    raise OSError(f"{description} failed after retries: {last_error}")


def atomic_write_text(path: Path, text: str):
    """Persist text without exposing a partially written destination file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def write_once():
        tmp = path.with_name(f".{path.name}.tmp_{os.getpid()}_{threading.get_ident()}")
        try:
            with open(tmp, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    retry_io(write_once, f"Writing {path}")


def timestamp_now() -> str:
    return datetime.now().strftime(TIMESTAMP_FORMAT)


def tooltip_text(value) -> str:
    value = str(value or "")
    if len(value) <= MAX_TOOLTIP_LENGTH:
        return value
    return value[: MAX_TOOLTIP_LENGTH - 1] + "…"


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
    with _history_lock:
        rows = history_data()

        rows.append(
            {
                "timestamp": timestamp_now(),
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

        # Keep only the most recent entries to prevent unbounded growth.
        try:
            atomic_write_text(HISTORY_FILE, json.dumps(rows[-HISTORY_MAX_ENTRIES:], indent=2))
        except OSError as ex:
            # History must never take down a completed file operation.
            sys.stderr.write(f"DarkSync history write failed: {ex}\n")


def patterns(s: str) -> List[str]:
    return [x.strip().replace("\\", "/") for x in s.replace(",", ";").split(";") if x.strip()]


def validate_patterns(value: str, field_name: str = "patterns") -> List[str]:
    """Validate user-controlled globs before they reach filesystem traversal."""
    result = patterns(value or "")

    for pattern in result:
        if "\x00" in pattern:
            raise ValueError(f"{field_name} cannot contain NUL characters")
        if len(pattern) > MAX_TOOLTIP_LENGTH:
            raise ValueError(f"{field_name} entries must be {MAX_TOOLTIP_LENGTH} characters or fewer")

        normalized = pattern.strip("/")
        if pattern.startswith("/") or Path(pattern).is_absolute() or any(part == ".." for part in normalized.split("/")):
            raise ValueError(f"{field_name} cannot contain absolute paths or parent-directory traversal: {pattern}")

    return result


def validate_job_configuration(job: Job):
    validate_patterns(job.include, "Include patterns")
    validate_patterns(job.exclude, "Exclude patterns")

    if job.source and job.destination:
        source = Path(job.source).expanduser().resolve()
        destination = Path(job.destination).expanduser().resolve()
        if source == destination:
            raise ValueError("Source and destination cannot be the same path")
        if source in destination.parents:
            raise ValueError("Destination cannot be inside source directory")
        if destination in source.parents:
            raise ValueError("Source cannot be inside destination directory")


def load_gitignore_patterns() -> List[str]:
    """Read .gitignore from the project directory and return its patterns."""
    gitignore_path = RUN_DIR / ".gitignore"
    if not gitignore_path.exists():
        return []

    try:
        lines = gitignore_path.read_text(encoding="utf-8").splitlines()
        patterns = []
        for line in lines:
            line = line.strip()
            # Skip empty lines and comments
            if not line or line.startswith("#"):
                continue
            # Skip negation patterns (not supported in sync context)
            if line.startswith("!"):
                continue
            patterns.append(line)
        return patterns
    except (OSError, UnicodeDecodeError):
        return []


def allowed(rel: str, job: Job, extra_exclude: Optional[List[str]] = None) -> bool:
    rel = rel.replace("\\", "/")
    name = Path(rel).name

    inc = validate_patterns(job.include, "Include patterns") or ["*"]
    exc = validate_patterns(job.exclude, "Exclude patterns")

    # Merge .gitignore patterns if provided
    if extra_exclude:
        exc = exc + extra_exclude

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
    except InterruptedError:
        raise
    except FileNotFoundError as exc:
        raise IOError(f"File not found: {path}") from exc
    except PermissionError as exc:
        raise IOError(f"Permission denied: {path}") from exc
    except Exception as e:
        raise IOError(f"Error reading {path}: {e}") from e


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

    if not rootp.is_dir():
        raise FileNotFoundError(f"{side} folder missing: {rootp}")

    # Load .gitignore patterns once per scan
    gitignore_patterns = load_gitignore_patterns()

    pending = [rootp]

    def directory_batches(directory: Path):
        """Yield bounded scan batches so one huge directory never builds a giant list."""
        dirs, files, errs = [], [], []

        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if cancel.is_set():
                            raise InterruptedError

                        wait_if_paused(cancel, pause)
                        path = Path(entry.path)
                        rel = path.relative_to(rootp).as_posix()

                        if entry.is_symlink() and not job.follow_links:
                            continue

                        if entry.is_dir(follow_symlinks=job.follow_links):
                            dirs.append((path, rel))
                        elif entry.is_file(follow_symlinks=job.follow_links) and allowed(rel, job, gitignore_patterns):
                            st = entry.stat(follow_symlinks=job.follow_links)
                            files.append(Info(rel, str(path), st.st_size, st.st_mtime_ns))
                    except InterruptedError:
                        raise
                    except Exception as ex:
                        if not should_ignore_scan_error(job, ex):
                            errs.append(f"{entry.path}: {ex}")

                    if len(dirs) + len(files) + len(errs) >= SCAN_BATCH_SIZE:
                        yield dirs, files, errs
                        dirs, files, errs = [], [], []
                        time.sleep(0)
        except InterruptedError:
            raise
        except Exception as ex:
            if not should_ignore_scan_error(job, ex):
                errs.append(f"{directory}: {ex}")

        if dirs or files or errs:
            yield dirs, files, errs

    while pending and not cancel.is_set():
        wait_if_paused(cancel, pause)
        directory = pending.pop()

        for dirs, files, errs in directory_batches(directory):
            for child, rel in dirs:
                pending.append(child)
                folders.add(rel)

            out.update({item.rel: item for item in files})
            errors.extend(errs)

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
            r[0]: (r[1], r[2])
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
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass

    con = None

    try:
        con = sqlite3.connect(tmp)

        con.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        con.execute("CREATE TABLE files (path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL)")
        con.execute("CREATE TABLE folders (path TEXT PRIMARY KEY)")

        con.execute(
            "INSERT INTO metadata VALUES ('created', ?)",
            (timestamp_now(),),
        )

        con.executemany(
            "INSERT INTO files VALUES (?, ?, ?)",
            ((r, v[0], v[1]) for r, v in files.items()),
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

    retry_io(lambda: os.replace(tmp, db), f"Replacing guard baseline {db}")


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
        elif x.status == Status.ERROR:
            x.action = "Conflict"
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
                except InterruptedError:
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
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


def required_sync_space(items: List[Item], job: Job) -> Dict[Path, int]:
    """Estimate temporary-copy and undo-backup space required per destination volume."""
    roots = {"left": Path(job.source), "right": Path(job.destination)}
    required = {roots["left"]: 0, roots["right"]: 0}

    for item in items:
        if not item.selected or item.action in ("Skip", "Conflict"):
            continue

        if item.action == "Copy left -> right":
            destination_root = roots["right"]
            source_info = item.left
            destination = destination_root / item.rel
        elif item.action == "Copy right -> left":
            destination_root = roots["left"]
            source_info = item.right
            destination = destination_root / item.rel
        else:
            continue

        if source_info:
            # Atomic copy needs a temporary source-sized file. Replacements also
            # retain the existing destination in the undo journal.
            required[destination_root] += int(source_info.size)
        try:
            if destination.is_file():
                required[destination_root] += destination.stat().st_size
        except OSError as ex:
            raise IOError(f"Cannot inspect destination space for {destination}: {ex}") from ex

    return required


def validate_sync_space(items: List[Item], job: Job):
    for root, needed in required_sync_space(items, job).items():
        if needed <= 0:
            continue

        try:
            free = shutil.disk_usage(root).free
        except OSError as ex:
            raise IOError(f"Cannot determine free space for {root}: {ex}") from ex

        safety_margin = max(BLOCK_SIZE_BYTES, needed // 100)
        if free < needed + safety_margin:
            raise IOError(
                f"Insufficient disk space on {root}: need at least "
                f"{hsize(needed + safety_margin)}, only {hsize(free)} available"
            )


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

    def copy_once():
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
        finally:
            if not cleaned_up:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

    return retry_io(copy_once, f"Copying {src} to {dst}")


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

    @Slot()
    def cancel(self):
        self.cancel_event.set()
        self.pause_event.clear()

    @Slot()
    def pause(self):
        self.pause_event.set()

    @Slot()
    def resume(self):
        self.pause_event.clear()

    def is_paused(self):
        return self.pause_event.is_set()

    @Slot()
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

        if le and guard_check:
            raise RuntimeError(
                "Source scan errors prevent a trusted ransomware check. "
                "To allow skipped/unreadable entries, enable Ignore all scan errors or "
                "Ignore permission errors in Job settings > Ransomware Guard. Details: "
                + " | ".join(le[:MAX_SCAN_ERROR_DETAILS])
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

        validate_sync_space(todo, self.job)

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
                    retry_io(lambda: shutil.copy2(dst, b), f"Backing up {dst}")

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
                retry_io(lambda: os.replace(dst, b), f"Moving {dst} to the undo journal")

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
            "created": timestamp_now(),
            "source": str(L),
            "destination": str(R),
            "mode": self.job.mode,
            "records": records,
            "undone": False,
        }

        mfp = tx / "manifest.json"
        atomic_write_text(mfp, json.dumps(mf, indent=2))

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

        atomic_write_text(log, "\n".join(problem_lines))

        rp = LOGS / f"{self.job.id}_{stamp}_report.json"

        rd = {
            "created": timestamp_now(),
            "job": self.job.name,
            "summary": summary,
            "items": problem_rows,
            "text_log": str(log),
            "undo_manifest": str(mfp),
        }

        atomic_write_text(rp, json.dumps(rd, indent=2))

        success = summary["Failed"] == 0 and summary["Cancelled"] == 0

        guard = None

        snapshot = getattr(self, "guard_snapshot", None)
        if success and self.job.guard_enabled and snapshot:
            guard = getattr(self, "guard_result", None)
            guard_save_baseline(self.job, *snapshot)

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

                        retry_io(lambda: shutil.copy2(dst, ob), f"Backing up recovery target {dst}")

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
                "time": timestamp_now(),
                "folder": folder or "<ALL>",
                "side": side,
                "overwrite": overwrite,
                "restored": restored,
                "errors": errors,
            }
        )

        if not folder and side == "original" and not errors:
            data["undone"] = True

        atomic_write_text(mf, json.dumps(data, indent=2))

        self.finished.emit(
            self.job.id,
            {
                "operation": "undo",
                "restored": restored,
                "errors": errors,
                "success": not errors,
            },
        )


# ── darksync.html colour palette ────────────────────────────────────────
INK     = "#edf2f7"
MUTED   = "#8ea1b5"
BG      = "#08111d"
PANEL   = "#0e1a2a"
PANEL2  = "#132238"
LINE    = "#223650"
ACCENT  = "#b7f36b"
ACCENT2 = "#77c8ff"
DANGER  = "#ff7d8d"
WARNING = "#f7c66a"
GOOD    = "#64e6a7"
TOPBAR_BG  = "#0a1524"
SIDEBAR_BG = "#091421"
FIELD_BG   = "#091525"


# ── Helpers ─────────────────────────────────────────────────────────────
def _badge(text, kind="default"):
    """Return a styled QLabel badge."""
    color_map = {"good": GOOD, "warn": WARNING, "danger": DANGER, "blue": ACCENT2}
    c = color_map.get(kind, MUTED)
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"QLabel{{color:{c};border:1px solid {c};border-radius:999px;"
        f"padding:3px 10px;font-size:11px;font-weight:600;background:transparent;}}"
    )
    return lbl


def _result_badge(result):
    if not result:
        return _badge("Never run")
    if result == "Success":
        return _badge(result, "good")
    if "Guard" in result or "Blocked" in result:
        return _badge(result, "warn")
    if result in ("Failed", "Cancelled"):
        return _badge(result, "danger")
    return _badge(result)


class NavButton(QPushButton):
    """Sidebar navigation button matching darksync.html."""
    def __init__(self, icon_char, text, parent=None):
        super().__init__(f"  {icon_char}   {text}", parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(f"""
            QPushButton{{border:none;border-radius:9px;background:transparent;
            color:{MUTED};padding:11px 14px;text-align:left;font-size:13px;}}
            QPushButton:hover{{background:{PANEL2};color:{INK};}}
            QPushButton:checked{{background:{PANEL2};color:{INK};
            border-left:3px solid {ACCENT};}}
        """)


class Toast(QLabel):
    """Floating toast notification at bottom-right."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self.hide()
        self.setStyleSheet(f"""
            QLabel{{
                border:1px solid {LINE};border-radius:10px;
                background:#122238;color:{INK};
                padding:13px 16px;font-size:13px;
            }}
        """)
        self.setWordWrap(True)
        self.setMinimumWidth(280)
        self.setMaximumWidth(440)

    def show_message(self, text, error=False, duration_ms=4500):
        self.setText(text)
        border = "rgba(255,125,141,0.5)" if error else LINE
        self.setStyleSheet(f"""
            QLabel{{
                border:1px solid {border};border-radius:10px;
                background:#122238;color:{INK};
                padding:13px 16px;font-size:13px;
            }}
        """)
        self.adjustSize()
        self.show()
        self.raise_()
        self._timer.start(duration_ms)


class ItemModel(QAbstractTableModel):
    headers = ["Sync", "Relative path", "Left size", "Status", "Action", "Right size"]

    def __init__(self):
        super().__init__()
        self.items = []

    def set_items(self, items):
        self.beginResetModel()
        self.items = [i for i in items if i.status != Status.EQUAL]
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
                    Status.LEFT: ACCENT2,
                    Status.RIGHT: WARNING,
                    Status.LNEW: GOOD,
                    Status.RNEW: "#b794f4",
                    Status.DIFF: "#f6e05e",
                    Status.ERROR: DANGER,
                }.get(x.status, MUTED)
            )

    def flags(self, i):
        return super().flags(i) | (Qt.ItemIsUserCheckable if i.column() == 0 else Qt.NoItemFlags)

    def setData(self, i, v, r=Qt.EditRole):
        if i.column() == 0 and r == Qt.CheckStateRole:
            self.items[i.row()].selected = v == Qt.Checked
            self.dataChanged.emit(i, i)
            return True
        return False


# ── Job Editor Dialog (darksync.html modal style) ──────────────────────
class JobDialog(QDialog):
    def __init__(self, job: Job, templates: List[Job], parent=None):
        super().__init__(parent)
        self.job = job_from_dict(asdict(job))
        self.setWindowTitle("Job settings")
        self.setMinimumSize(900, 720)
        self.resize(960, 760)

        root = QVBoxLayout(self)
        root.setSpacing(12)

        # ── header
        head = QLabel("New synchronization job" if not job.name else f"Edit: {job.name}")
        head.setStyleSheet(f"font-size:20px;font-weight:900;color:{INK};letter-spacing:-0.5px;")
        root.addWidget(head)
        sub = QLabel("These settings are written to the existing DarkSync JSON state.")
        sub.setStyleSheet(f"color:{MUTED};font-size:12px;")
        root.addWidget(sub)

        tabs = QTabWidget()
        tabs.setStyleSheet(f"""
            QTabWidget::pane{{border:1px solid {LINE};border-radius:8px;background:{BG};}}
            QTabBar::tab{{background:{PANEL};color:{INK};padding:10px 18px;border:1px solid {LINE};
                         border-bottom:none;border-radius:8px 8px 0 0;margin-right:2px;}}
            QTabBar::tab:selected{{background:{ACCENT};color:#12200e;border-color:{ACCENT};}}
            QTabBar::tab:hover{{background:{PANEL2};}}
        """)

        # ── Job tab
        main_tab = QWidget()
        form = QGridLayout(main_tab)
        form.setSpacing(10)
        form.setColumnStretch(0, 0)
        form.setColumnStretch(1, 1)
        form.setColumnStretch(2, 0)
        form.setColumnStretch(3, 1)

        def _label(text, bold=True):
            lbl = QLabel(text.upper())
            lbl.setStyleSheet(f"color:{MUTED};font-size:10px;font-weight:800;letter-spacing:1px;background:transparent;")
            return lbl

        def _field(text=""):
            le = QLineEdit(text)
            le.setStyleSheet(f"background:{FIELD_BG};border:1px solid {LINE};border-radius:7px;color:{INK};padding:8px 10px;")
            return le

        def _combo(items, current=""):
            cb = QComboBox()
            cb.addItems(items)
            if current:
                cb.setCurrentText(current)
            cb.setStyleSheet(f"background:{FIELD_BG};border:1px solid {LINE};border-radius:7px;color:{INK};padding:8px 10px;")
            return cb

        def _spin(lo, hi, val):
            sp = QSpinBox()
            sp.setRange(lo, hi)
            sp.setValue(val)
            sp.setStyleSheet(f"background:{FIELD_BG};border:1px solid {LINE};border-radius:7px;color:{INK};padding:8px 10px;")
            return sp

        def _check(text, checked=False):
            cb = QCheckBox(text)
            cb.setChecked(checked)
            cb.setStyleSheet(f"color:{MUTED};")
            return cb

        row = 0
        form.addWidget(_label("Template"), row, 0)
        self.template = QComboBox()
        self.template.setStyleSheet(f"background:{FIELD_BG};border:1px solid {LINE};border-radius:7px;color:{INK};padding:8px 10px;")
        self.template.addItem("Custom", None)
        for t in templates:
            self.template.addItem(t.name, t)
        form.addWidget(self.template, row, 1, 1, 3)

        row += 1
        form.addWidget(_label("Job name"), row, 0)
        self.name = _field(self.job.name)
        form.addWidget(self.name, row, 1)
        form.addWidget(_label("Run state"), row, 2)
        self.enabled = _combo(["Enabled", "Disabled"], "Enabled" if self.job.enabled else "Disabled")
        form.addWidget(self.enabled, row, 3)

        row += 1
        form.addWidget(_label("Source folder"), row, 0)
        src_row = QHBoxLayout()
        self.src = _field(self.job.source)
        src_row.addWidget(self.src)
        bs = QPushButton("Browse")
        bs.setStyleSheet(f"background:{PANEL2};border:1px solid {LINE};border-radius:7px;color:{INK};padding:8px 14px;")
        bs.clicked.connect(lambda: self._pick(self.src))
        src_row.addWidget(bs)
        form.addLayout(src_row, row, 1, 1, 3)

        row += 1
        form.addWidget(_label("Destination folder"), row, 0)
        dst_row = QHBoxLayout()
        self.dst = _field(self.job.destination)
        dst_row.addWidget(self.dst)
        bd = QPushButton("Browse")
        bd.setStyleSheet(f"background:{PANEL2};border:1px solid {LINE};border-radius:7px;color:{INK};padding:8px 14px;")
        bd.clicked.connect(lambda: self._pick(self.dst))
        dst_row.addWidget(bd)
        form.addLayout(dst_row, row, 1, 1, 3)

        row += 1
        form.addWidget(_label("Sync mode"), row, 0)
        self.mode = _combo(["Two way", "Mirror", "Update"], self.job.mode)
        form.addWidget(self.mode, row, 1)
        form.addWidget(_label("Compare by"), row, 2)
        self.comp = _combo(["Time and size", "Size", "Content"], self.job.compare)
        form.addWidget(self.comp, row, 3)

        row += 1
        form.addWidget(_label("Parallel workers"), row, 0)
        self.work = _spin(1, 64, self.job.workers)
        form.addWidget(self.work, row, 1)
        form.addWidget(_label("Timestamp tolerance"), row, 2)
        self.tol = _spin(0, 3600, self.job.tolerance)
        self.tol.setSuffix(" s")
        form.addWidget(self.tol, row, 3)

        row += 1
        form.addWidget(_label("Include globs"), row, 0)
        self.include = _field(self.job.include)
        form.addWidget(self.include, row, 1, 1, 3)

        row += 1
        form.addWidget(_label("Exclude globs (also reads .gitignore)"), row, 0)
        self.exclude = _field(self.job.exclude)
        form.addWidget(self.exclude, row, 1, 1, 3)

        row += 1
        sep = QLabel("  SAFETY AND COPY POLICY")
        sep.setStyleSheet(f"color:{ACCENT};font-size:10px;font-weight:900;letter-spacing:0.14em;border-top:1px solid {LINE};padding-top:12px;background:transparent;")
        form.addWidget(sep, row, 0, 1, 4)

        row += 1
        self.verify = _check("Verify copied files with SHA-256", self.job.verify)
        form.addWidget(self.verify, row, 0, 1, 2)
        self.preserve = _check("Preserve timestamps", self.job.preserve_times)
        form.addWidget(self.preserve, row, 2, 1, 2)

        row += 1
        self.links = _check("Follow symbolic links", self.job.follow_links)
        form.addWidget(self.links, row, 0, 1, 2)
        form.addWidget(_label("Deletion policy"), row, 2)
        self.delete = _combo(["Recycle bin", "Permanent"], self.job.deletion)
        form.addWidget(self.delete, row, 3)

        tabs.addTab(main_tab, "Job")

        # ── Schedule tab
        sched_tab = QWidget()
        sf = QFormLayout(sched_tab)
        sf.setSpacing(10)

        self.sched_en = _check("Schedule this job", self.job.scheduler_enabled)
        sf.addRow("Scheduled", self.sched_en)

        self.sched_time = QTimeEdit()
        self.sched_time.setDisplayFormat("HH:mm")
        qt = QTime.fromString(self.job.scheduler_time, "HH:mm")
        if not qt.isValid():
            qt = QTime(2, 0)
        self.sched_time.setTime(qt)
        self.sched_time.setStyleSheet(f"background:{FIELD_BG};border:1px solid {LINE};border-radius:7px;color:{INK};padding:8px 10px;")
        sf.addRow("24-hour time", self.sched_time)

        self.sched_action = _combo(["Compare only", "Compare and synchronize"], self.job.scheduler_action)
        sf.addRow("Action", self.sched_action)

        tabs.addTab(sched_tab, "Schedule")

        # ── Guard tab
        guard_tab = QWidget()
        gf = QFormLayout(guard_tab)
        gf.setSpacing(10)

        self.guard_en = _check("Use shared source scan before synchronization", self.job.guard_enabled)
        gf.addRow("Enable protection", self.guard_en)

        self.guard_pct = QDoubleSpinBox()
        self.guard_pct.setRange(0.01, 100)
        self.guard_pct.setDecimals(2)
        self.guard_pct.setSuffix(" %")
        self.guard_pct.setValue(self.job.guard_threshold_percent)
        self.guard_pct.setStyleSheet(f"background:{FIELD_BG};border:1px solid {LINE};border-radius:7px;color:{INK};padding:8px 10px;")
        gf.addRow("Maximum allowed change", self.guard_pct)

        self.ignore_perm = _check("Ignore permission errors on files/folders", self.job.ignore_permission_errors)
        gf.addRow("", self.ignore_perm)

        self.ignore_all = _check("Ignore all scan errors", self.job.ignore_scan_errors)
        gf.addRow("", self.ignore_all)

        self.exit_on_complete = _check("Exit application after job completion (for automated runs)", self.job.exit_on_completion)
        gf.addRow("", self.exit_on_complete)

        warn = QLabel("Warning: ignored scan errors mean unreadable/skipped items are excluded from compare and from the Ransomware Guard baseline. Use only when expected.")
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color:{MUTED};font-size:11px;")
        gf.addRow("Error handling", warn)

        tabs.addTab(guard_tab, "Ransomware Guard")

        # ── Notifications tab
        notify_tab = QWidget()
        nf = QFormLayout(notify_tab)
        nf.setSpacing(10)
        n = self.job.notify

        self.n_en = _check("Enable email notifications", n.enabled)
        nf.addRow("", self.n_en)
        self.n_succ = _check("Notify on success", n.on_success)
        nf.addRow("", self.n_succ)
        self.n_fail = _check("Notify on failure", n.on_failure)
        nf.addRow("", self.n_fail)

        nf.addRow("SMTP host", _field(n.smtp_host))
        self.smtp = nf.itemAt(nf.rowCount() - 1, QFormLayout.FieldRole).widget()

        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(n.smtp_port)
        self.port.setStyleSheet(f"background:{FIELD_BG};border:1px solid {LINE};border-radius:7px;color:{INK};padding:8px 10px;")
        nf.addRow("SMTP port", self.port)

        nf.addRow("SMTP user", _field(n.smtp_user))
        self.user = nf.itemAt(nf.rowCount() - 1, QFormLayout.FieldRole).widget()

        pw_field = _field(n.smtp_password)
        pw_field.setEchoMode(QLineEdit.Password)
        nf.addRow("SMTP password", pw_field)
        self.pw = pw_field

        nf.addRow("From", _field(n.mail_from))
        self.mfrom = nf.itemAt(nf.rowCount() - 1, QFormLayout.FieldRole).widget()

        nf.addRow("To", _field(n.mail_to))
        self.mto = nf.itemAt(nf.rowCount() - 1, QFormLayout.FieldRole).widget()

        nf.addRow(QLabel(""))

        self.ntfy_en = _check("Enable ntfy notifications", n.ntfy_enabled)
        nf.addRow("", self.ntfy_en)

        nf.addRow("ntfy server", _field(n.ntfy_server or "https://ntfy.sh"))
        self.ntfy_server = nf.itemAt(nf.rowCount() - 1, QFormLayout.FieldRole).widget()

        nf.addRow("ntfy topic", _field(n.ntfy_topic or "PA_Backups"))
        self.ntfy_topic = nf.itemAt(nf.rowCount() - 1, QFormLayout.FieldRole).widget()

        ntfy_token_field = _field(n.ntfy_token)
        ntfy_token_field.setEchoMode(QLineEdit.Password)
        nf.addRow("ntfy token", ntfy_token_field)
        self.ntfy_token = ntfy_token_field

        self.ntfy_prio = _combo(["min", "low", "default", "high", "urgent"], n.ntfy_priority or "high")
        nf.addRow("ntfy priority", self.ntfy_prio)

        tabs.addTab(notify_tab, "Notifications")

        self.template.currentIndexChanged.connect(self._apply_template)
        root.addWidget(tabs)

        # ── footer buttons
        footer = QHBoxLayout()
        footer.addStretch()
        cancel = QPushButton("Cancel")
        cancel.setStyleSheet(f"background:transparent;border:1px solid {LINE};border-radius:7px;color:{MUTED};padding:8px 14px;")
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        save = QPushButton("Save job")
        save.setObjectName("primary")
        save.setStyleSheet(f"background:{ACCENT};color:#12200e;border:1px solid {ACCENT};border-radius:7px;padding:8px 14px;font-weight:700;")
        save.clicked.connect(self._do_accept)
        footer.addWidget(save)
        root.addLayout(footer)

    def _pick(self, e):
        p = QFileDialog.getExistingDirectory(self, "Select folder", e.text() or str(Path.home()))
        if p:
            e.setText(p)

    def _apply_template(self):
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

    def _do_accept(self):
        try:
            self.result_job()
        except ValueError as ex:
            QMessageBox.warning(self, "Invalid job configuration", str(ex))
            return
        self.accept()

    def current_notify_config(self):
        return NotifyConfig(
            self.n_en.isChecked(), self.n_succ.isChecked(), self.n_fail.isChecked(),
            self.smtp.text(), self.port.value(), self.user.text(), self.pw.text(),
            self.mfrom.text(), self.mto.text(), self.ntfy_en.isChecked(),
            self.ntfy_server.text().strip() or "https://ntfy.sh",
            self.ntfy_topic.text().strip() or "PA_Backups",
            self.ntfy_token.text().strip(), self.ntfy_prio.currentText(),
        )

    def result_job(self):
        j = self.job
        j.name = self.name.text().strip() or "Unnamed Job"
        j.source = self.src.text().strip()
        j.destination = self.dst.text().strip()
        j.enabled = self.enabled.currentText() == "Enabled"
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
        validate_job_configuration(j)
        return j


# ── Undo / Recovery Dialog ─────────────────────────────────────────────
class UndoDialog(QDialog):
    def __init__(self, job: Job, parent=None):
        super().__init__(parent)
        self.job = job
        self.setWindowTitle("Undo / recovery")
        self.setMinimumSize(820, 380)
        self.resize(860, 400)

        root = QVBoxLayout(self)
        root.setSpacing(12)

        head = QLabel("Recover journal")
        head.setStyleSheet(f"font-size:18px;font-weight:900;color:{INK};")
        root.addWidget(head)
        sub = QLabel("Original restores are guarded by post-sync file metadata.")
        sub.setStyleSheet(f"color:{MUTED};font-size:12px;")
        root.addWidget(sub)

        form = QFormLayout()
        form.setSpacing(10)

        self.journals = available_undo_manifests(job)
        self.journal = QComboBox()
        self.journal.setStyleSheet(f"background:{FIELD_BG};border:1px solid {LINE};border-radius:7px;color:{INK};padding:8px 10px;")
        for created, path, data in self.journals:
            self.journal.addItem(
                f"{created:%Y-%m-%d %H:%M:%S} | {len(data.get('records', [])):,} records | {data.get('mode', '')}",
                (str(path), data),
            )

        self.all = QRadioButton("Recover all files and folders")
        self.all.setStyleSheet(f"color:{INK};")
        self.folder = QRadioButton("Recover a specific folder")
        self.folder.setStyleSheet(f"color:{INK};")
        self.all.setChecked(True)

        self.folder_combo = QComboBox()
        self.folder_combo.setStyleSheet(f"background:{FIELD_BG};border:1px solid {LINE};border-radius:7px;color:{INK};padding:8px 10px;")
        self.folder_combo.setEnabled(False)

        self.side = QComboBox()
        self.side.setStyleSheet(f"background:{FIELD_BG};border:1px solid {LINE};border-radius:7px;color:{INK};padding:8px 10px;")
        self.side.addItem("Original affected location (true undo)", "original")
        self.side.addItem("Source / left folder", "source")
        self.side.addItem("Destination / right folder", "destination")

        self.overwrite = QCheckBox("Overwrite existing files at selected recovery location")
        self.overwrite.setStyleSheet(f"color:{MUTED};")

        form.addRow("Synchronization journal", self.journal)
        form.addRow("", self.all)
        form.addRow("", self.folder)
        form.addRow("Folder", self.folder_combo)
        form.addRow("Recover into", self.side)
        form.addRow("Overwrite", self.overwrite)
        root.addLayout(form)

        self.folder.toggled.connect(self.folder_combo.setEnabled)
        self.journal.currentIndexChanged.connect(self._reload_folders)
        self._reload_folders()

        footer = QHBoxLayout()
        footer.addStretch()
        cancel = QPushButton("Cancel")
        cancel.setStyleSheet(f"background:transparent;border:1px solid {LINE};border-radius:7px;color:{MUTED};padding:8px 14px;")
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        recover = QPushButton("Recover")
        recover.setObjectName("primary")
        recover.setStyleSheet(f"background:{ACCENT};color:#12200e;border:1px solid {ACCENT};border-radius:7px;padding:8px 14px;font-weight:700;")
        recover.clicked.connect(self.accept)
        footer.addWidget(recover)
        root.addLayout(footer)

    def _reload_folders(self):
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
        folder = "" if self.all.isChecked() else (self.folder_combo.currentData() or self.folder_combo.currentText())
        return {"manifest": path, "folder": folder, "recovery_side": self.side.currentData(), "overwrite_existing": self.overwrite.isChecked()}


# ── Main Window (darksync.html layout) ─────────────────────────────────
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
        self.results_jid = ""
        self.shutting_down = False
        self.exit_when_idle = False
        self._selected_row = -1
        self._guard_blocked_job = None
        self._guard_blocked_details = ""

        self.setWindowTitle(f"{APP} {VERSION}")
        self.resize(1520, 900)
        self.setMinimumSize(1100, 680)

        self.ui()
        self.reload_jobs()
        self.reload_history()

        if _load_warning:
            QMessageBox.warning(self, f"{APP} – jobs file unreadable", _load_warning)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check_schedules)
        self.timer.start(15000)

    # ── UI layout ───────────────────────────────────────────────────
    def ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Sidebar
        sidebar = QFrame()
        sidebar.setFixedWidth(220)
        sidebar.setStyleSheet(f"QFrame{{background:{SIDEBAR_BG};border-right:1px solid {LINE};}}")
        sb = QVBoxLayout(sidebar)
        sb.setContentsMargins(10, 22, 10, 22)
        sb.setSpacing(4)

        eyebrow = QLabel("WORKSPACE")
        eyebrow.setStyleSheet(f"color:{MUTED};font-size:9px;font-weight:800;letter-spacing:2px;padding-left:12px;background:transparent;")
        sb.addWidget(eyebrow)
        sb.addSpacing(8)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons = []
        self._page_names = ["Dashboard", "Jobs", "Synchronization", "Recovery", "History"]
        for i, (icon, text) in enumerate([
            ("\u25c8", "Dashboard"), ("\u25a6", "Jobs"), ("\u21c4", "Synchronization"),
            ("\u21ba", "Recovery"), ("\u224b", "History"),
        ]):
            btn = NavButton(icon, text)
            btn.setCheckable(True)
            self.nav_group.addButton(btn, i)
            self.nav_buttons.append(btn)
            btn.clicked.connect(lambda _, idx=i: self._switch_page(idx))
            sb.addWidget(btn)
        sb.addStretch()

        # Sidebar note
        note = QFrame()
        note.setStyleSheet(f"QFrame{{border:1px solid {LINE};border-radius:11px;background:#0d1b2b;}}")
        nl = QVBoxLayout(note)
        nl.setContentsMargins(12, 10, 12, 10)
        nl_title = QLabel("<b>Native engine</b>")
        nl_title.setStyleSheet(f"color:{INK};font-size:12px;background:transparent;")
        nl.addWidget(nl_title)
        nl_body = QLabel("All filesystem writes, scheduler, Guard, recovery, and notifications run locally — no browser helper needed.")
        nl_body.setWordWrap(True)
        nl_body.setStyleSheet(f"color:{MUTED};font-size:11px;background:transparent;")
        nl.addWidget(nl_body)
        sb.addWidget(note)

        root.addWidget(sidebar)

        # ── Main area
        main_area = QWidget()
        ml = QVBoxLayout(main_area)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(0)

        # Topbar
        topbar = QFrame()
        topbar.setFixedHeight(68)
        topbar.setStyleSheet(f"QFrame{{background:{TOPBAR_BG};border-bottom:1px solid {LINE};}}")
        tb = QHBoxLayout(topbar)
        tb.setContentsMargins(28, 0, 28, 0)
        mark = QLabel("DS")
        mark.setFixedSize(34, 34)
        mark.setAlignment(Qt.AlignCenter)
        mark.setStyleSheet(f"QLabel{{background:{ACCENT};color:#12200e;border-radius:10px;font-weight:900;font-size:14px;}}")
        tb.addWidget(mark)
        tb.addSpacing(12)
        bv = QVBoxLayout()
        bv.setSpacing(0)
        bh = QLabel("DarkSync")
        bh.setStyleSheet(f"font-size:15px;font-weight:900;letter-spacing:1.5px;color:{INK};background:transparent;")
        bs = QLabel("Desktop console")
        bs.setStyleSheet(f"color:{MUTED};font-size:11px;background:transparent;")
        bv.addWidget(bh)
        bv.addWidget(bs)
        tb.addLayout(bv)
        tb.addStretch()
        self.status_label = QLabel()
        self.status_label.setStyleSheet(f"color:{MUTED};font-size:12px;background:transparent;")
        tb.addWidget(self.status_label)
        ml.addWidget(topbar)

        # Stacked pages
        self.stack = QStackedWidget()
        self._build_dashboard()
        self._build_jobs()
        self._build_sync()
        self._build_recovery()
        self._build_history()
        ml.addWidget(self.stack)

        root.addWidget(main_area, 1)

        # Toast
        self.toast = Toast(self.centralWidget())

        # Keyboard shortcuts
        QShortcut(QKeySequence("Ctrl+N"), self, self.new_job)
        QShortcut(QKeySequence("Ctrl+E"), self, self.edit_job)
        QShortcut(QKeySequence("Delete"), self, self.delete_job)
        QShortcut(QKeySequence("F5"), self, self.reload_history)
        QShortcut(QKeySequence("Ctrl+R"), self, self.run_selected)
        QShortcut(QKeySequence("Ctrl+Shift+R"), self, self.run_all)
        QShortcut(QKeySequence("Ctrl+Z"), self, self.undo_recover)

        self.nav_buttons[0].setChecked(True)
        self.stack.setCurrentIndex(0)

    def _switch_page(self, idx):
        self.stack.setCurrentIndex(idx)

    # ── Dashboard ───────────────────────────────────────────────────
    def _build_dashboard(self):
        page = QWidget()
        lo = QVBoxLayout(page)
        lo.setContentsMargins(28, 18, 28, 18)
        lo.setSpacing(16)

        head = QHBoxLayout()
        vbox = QVBoxLayout()
        t = QLabel("Backup health")
        t.setStyleSheet("font-size:26px;font-weight:900;letter-spacing:-0.5px;")
        vbox.addWidget(t)
        s = QLabel("One calm place to see what is protected, what is due, and what needs attention.")
        s.setStyleSheet(f"color:{MUTED};font-size:13px;")
        vbox.addWidget(s)
        head.addLayout(vbox)
        head.addStretch()
        new_btn = QPushButton("New job")
        new_btn.setObjectName("primary")
        new_btn.setStyleSheet(f"background:{ACCENT};color:#12200e;border:1px solid {ACCENT};border-radius:8px;padding:9px 13px;font-weight:700;")
        new_btn.setCursor(Qt.PointingHandCursor)
        new_btn.clicked.connect(self.new_job)
        head.addWidget(new_btn)
        lo.addLayout(head)

        # Metric cards
        cards = QHBoxLayout()
        cards.setSpacing(13)
        self.card_jobs = self._make_metric_card("Configured jobs", "0", "Enabled and disabled jobs", ACCENT)
        self.card_success = self._make_metric_card("Healthy runs", "0", "Successful history records", GOOD)
        self.card_guard = self._make_metric_card("Guard status", "0/0", "Baseline protection", WARNING)
        self.card_failed = self._make_metric_card("Needs review", "0", "Failed or blocked runs", DANGER)
        for card in [self.card_jobs, self.card_success, self.card_guard, self.card_failed]:
            cards.addWidget(card)
        lo.addLayout(cards)

        # Bottom panels
        panels = QHBoxLayout()
        panels.setSpacing(16)

        # Recent activity
        rp = self._panel("Recent activity", "Latest native operations")
        self.recent_list = QListWidget()
        self.recent_list.setStyleSheet(f"QListWidget{{background:{FIELD_BG};border:1px solid {LINE};border-radius:9px;padding:6px;}}")
        self.recent_list.itemDoubleClicked.connect(self._on_dashboard_item_clicked)
        rp.layout().addWidget(self.recent_list)
        panels.addWidget(rp, 6)

        # Schedule watch
        sp = self._panel("Schedule watch", "Daily local scheduler")
        self.upcoming_list = QListWidget()
        self.upcoming_list.setStyleSheet(f"QListWidget{{background:{FIELD_BG};border:1px solid {LINE};border-radius:9px;padding:6px;}}")
        self.upcoming_list.itemDoubleClicked.connect(self._on_dashboard_item_clicked)
        sp.layout().addWidget(self.upcoming_list)
        panels.addWidget(sp, 4)
        lo.addLayout(panels)
        lo.addStretch()

        self.stack.addWidget(page)

    def _make_metric_card(self, label, value, note, accent):
        frame = QFrame()
        frame.setFixedHeight(110)
        frame.setStyleSheet(f"QFrame{{border:1px solid {LINE};background:{PANEL};border-radius:13px;}}")
        lo = QVBoxLayout(frame)
        lo.setContentsMargins(18, 14, 18, 14)
        lo.setSpacing(2)
        lbl = QLabel(label.upper())
        lbl.setStyleSheet(f"color:{MUTED};font-size:10px;font-weight:800;letter-spacing:1.5px;")
        lo.addWidget(lbl)
        val = QLabel(str(value))
        val.setStyleSheet(f"color:{accent};font-size:30px;font-weight:900;letter-spacing:-0.5px;")
        lo.addWidget(val)
        n = QLabel(note)
        n.setStyleSheet(f"color:{MUTED};font-size:11px;")
        lo.addWidget(n)
        frame.value_label = val
        return frame

    def _panel(self, title, subtitle):
        frame = QFrame()
        frame.setStyleSheet(f"QFrame{{border:1px solid {LINE};background:{PANEL};border-radius:13px;}}")
        lo = QVBoxLayout(frame)
        lo.setContentsMargins(18, 16, 18, 16)
        lo.setSpacing(8)
        head = QHBoxLayout()
        tl = QLabel(f"<b>{title}</b>")
        tl.setStyleSheet("font-size:14px;background:transparent;")
        head.addWidget(tl)
        head.addStretch()
        sl = QLabel(subtitle)
        sl.setStyleSheet(f"color:{MUTED};font-size:11px;background:transparent;")
        head.addWidget(sl)
        lo.addLayout(head)
        return frame

    def _on_dashboard_item_clicked(self, item):
        self._switch_page(1)

    # ── Jobs page ───────────────────────────────────────────────────
    def _build_jobs(self):
        page = QWidget()
        lo = QVBoxLayout(page)
        lo.setContentsMargins(10, 10, 10, 10)
        lo.setSpacing(10)

        head = QHBoxLayout()
        vbox = QVBoxLayout()
        t = QLabel("Jobs")
        t.setStyleSheet("font-size:26px;font-weight:900;letter-spacing:-0.5px;")
        vbox.addWidget(t)
        s = QLabel("Configure source, destination, policy, Guard, scheduling, and notifications.")
        s.setStyleSheet(f"color:{MUTED};font-size:13px;")
        vbox.addWidget(s)
        head.addLayout(vbox)
        head.addStretch()

        for text, slot, name in [
            ("Copy", self.copy_job, ""), ("Edit selected", self.edit_job, ""),
            ("Delete", self.delete_job, "danger"), ("Run", self.run_selected, ""),
            ("Run All", self.run_all, "primary"), ("New job", self.new_job, "primary"),
        ]:
            btn = QPushButton(text)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(f"background:{ACCENT if name == 'primary' else ('transparent' if name == 'danger' else '#122238')};"
                              f"color:{DANGER if name == 'danger' else INK};"
                              f"border:1px solid {DANGER if name == 'danger' else LINE};"
                              f"border-radius:8px;padding:9px 13px;font-weight:700;")
            btn.clicked.connect(slot)
            head.addWidget(btn)
        lo.addLayout(head)

        # Search
        search_layout = QHBoxLayout()
        self.job_search = QLineEdit()
        self.job_search.setPlaceholderText("Filter by name or path...")
        self.job_search.setStyleSheet(f"background:{FIELD_BG};border:1px solid {LINE};border-radius:8px;color:{INK};padding:8px 10px;")
        self.job_search.textChanged.connect(self._filter_jobs)
        search_layout.addWidget(QLabel("Filter:"))
        search_layout.addWidget(self.job_search)
        search_layout.addStretch()
        pl = QLabel("Max parallel:")
        pl.setStyleSheet(f"color:{MUTED};background:transparent;")
        search_layout.addWidget(pl)
        self.parallel = QSpinBox()
        self.parallel.setRange(1, 8)
        self.parallel.setValue(self.state.max_parallel_jobs)
        self.parallel.valueChanged.connect(self.set_parallel)
        self.parallel.setStyleSheet(f"background:{FIELD_BG};border:1px solid {LINE};border-radius:7px;color:{INK};padding:8px 10px;width:68px;")
        search_layout.addWidget(self.parallel)
        lo.addLayout(search_layout)

        # Table
        frame = QFrame()
        frame.setStyleSheet(f"QFrame{{border:1px solid {LINE};background:{PANEL};border-radius:10px;}}")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(0, 0, 0, 0)
        self.jobs_table = QTableWidget(0, 8)
        self.jobs_table.setHorizontalHeaderLabels(["Name", "Mode", "Enabled", "Schedule", "Last run", "Result", "Source", "Destination"])
        self.jobs_table.setStyleSheet(f"""
            QTableWidget{{background:transparent;border:none;gridline-color:{LINE};
                          selection-background-color:#1a3050;color:{INK};}}
            QHeaderView::section{{background:#111f32;color:{MUTED};border:none;
                                 border-bottom:1px solid {LINE};padding:10px 12px;
                                 font-size:10px;font-weight:800;}}
        """)
        self.jobs_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.jobs_table.horizontalHeader().setStretchLastSection(True)
        self.jobs_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.jobs_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.jobs_table.verticalHeader().setVisible(False)
        self.jobs_table.cellClicked.connect(self._on_job_row_clicked)
        widths = [200, 80, 70, 120, 160, 170, 300, 360]
        for i, wid in enumerate(widths):
            self.jobs_table.setColumnWidth(i, wid)
        fl.addWidget(self.jobs_table)
        lo.addWidget(frame, 1)

        self.stack.addWidget(page)

    def _on_job_row_clicked(self, row, _col):
        self._selected_row = row
        j = self.selected_job()
        if j:
            self._sync_src.setText(j.source or "—")
            self._sync_dst.setText(j.destination or "—")

    def _filter_jobs(self, text):
        text = text.lower()
        for row in range(self.jobs_table.rowCount()):
            match = False
            for col in range(self.jobs_table.columnCount()):
                item = self.jobs_table.item(row, col)
                if item and text in item.text().lower():
                    match = True
                    break
            self.jobs_table.setRowHidden(row, not match)

    # ── Sync page ───────────────────────────────────────────────────
    def _build_sync(self):
        page = QWidget()
        lo = QVBoxLayout(page)
        lo.setContentsMargins(10, 10, 10, 10)
        lo.setSpacing(10)

        head = QHBoxLayout()
        vbox = QVBoxLayout()
        t = QLabel("Synchronization")
        t.setStyleSheet("font-size:26px;font-weight:900;letter-spacing:-0.5px;")
        vbox.addWidget(t)
        s = QLabel("Compare first, review actions, then commit with Guard protection.")
        s.setStyleSheet(f"color:{MUTED};font-size:13px;")
        vbox.addWidget(s)
        head.addLayout(vbox)
        head.addStretch()
        self._compare_btn = QPushButton("Compare")
        self._compare_btn.setCursor(Qt.PointingHandCursor)
        self._compare_btn.setStyleSheet(f"background:#122238;border:1px solid {LINE};border-radius:8px;color:{INK};padding:9px 13px;font-weight:700;")
        self._compare_btn.clicked.connect(self.run_compare)
        head.addWidget(self._compare_btn)
        self._sync_btn = QPushButton("Compare & sync")
        self._sync_btn.setObjectName("primary")
        self._sync_btn.setCursor(Qt.PointingHandCursor)
        self._sync_btn.setStyleSheet(f"background:{ACCENT};color:#12200e;border:1px solid {ACCENT};border-radius:8px;padding:9px 13px;font-weight:700;")
        self._sync_btn.clicked.connect(self.run_compare_sync)
        head.addWidget(self._sync_btn)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setObjectName("danger")
        self._cancel_btn.setCursor(Qt.PointingHandCursor)
        self._cancel_btn.setStyleSheet(f"background:transparent;border:1px solid {DANGER};border-radius:8px;color:{DANGER};padding:9px 13px;font-weight:700;")
        self._cancel_btn.hide()
        self._cancel_btn.clicked.connect(self.cancel_running_jobs)
        head.addWidget(self._cancel_btn)
        lo.addLayout(head)

        self.current_job_label = QLabel("No active job")
        self.current_job_label.setStyleSheet(f"color:{MUTED};font-size:12px;background:transparent;")
        lo.addWidget(self.current_job_label)

        # Path cards
        path_cards = QHBoxLayout()
        path_cards.setSpacing(12)
        for label_text, holder in [("Source / left", "left"), ("Destination / right", "right")]:
            card = QFrame()
            card.setStyleSheet(f"QFrame{{padding:14px;background:{FIELD_BG};border:1px solid {LINE};border-radius:10px;}}")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(14, 10, 14, 10)
            ll = QLabel(label_text.upper())
            ll.setStyleSheet(f"color:{MUTED};font-size:10px;font-weight:800;letter-spacing:1.2px;background:transparent;")
            cl.addWidget(ll)
            vl = QLabel("—")
            vl.setStyleSheet(f"font-family:'Consolas','SF Mono',monospace;font-size:12px;background:transparent;")
            cl.addWidget(vl)
            path_cards.addWidget(card)
            if holder == "left":
                self._sync_src = vl
            else:
                self._sync_dst = vl
        lo.addLayout(path_cards)

        # Guard banner (hidden)
        self._guard_frame = QFrame()
        self._guard_frame.setStyleSheet(f"QFrame{{border:1px solid rgba(247,198,106,0.35);background:rgba(247,198,106,0.06);border-radius:10px;}}")
        gl = QHBoxLayout(self._guard_frame)
        gl.setContentsMargins(16, 12, 16, 12)
        gv = QVBoxLayout()
        self._guard_title = QLabel("Guard check")
        self._guard_title.setStyleSheet(f"color:{WARNING};font-weight:800;background:transparent;")
        gv.addWidget(self._guard_title)
        self._guard_detail = QLabel("")
        self._guard_detail.setStyleSheet(f"color:{MUTED};font-size:12px;background:transparent;")
        gv.addWidget(self._guard_detail)
        gl.addLayout(gv, 1)
        self._guard_sync_btn = QPushButton("Sync anyway")
        self._guard_sync_btn.setObjectName("primary")
        self._guard_sync_btn.setStyleSheet(f"background:{DANGER};color:white;border:none;border-radius:8px;padding:10px 20px;font-weight:800;")
        self._guard_sync_btn.clicked.connect(self._guard_override_sync)
        self._guard_sync_btn.hide()
        gl.addWidget(self._guard_sync_btn)
        self._guard_frame.hide()
        lo.addWidget(self._guard_frame)

        # Sync table
        frame = QFrame()
        frame.setStyleSheet(f"QFrame{{border:1px solid {LINE};background:{PANEL};border-radius:10px;}}")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(0, 0, 0, 0)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setStyleSheet(f"""
            QTableView{{background:transparent;border:none;gridline-color:{LINE};
                         selection-background-color:#1a3050;color:{INK};}}
            QHeaderView::section{{background:#111f32;color:{MUTED};border:none;
                                 border-bottom:1px solid {LINE};padding:10px 12px;
                                 font-size:10px;font-weight:800;}}
        """)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._sync_context_menu)
        fl.addWidget(self.table)
        lo.addWidget(frame, 1)

        # Progress
        pl = QHBoxLayout()
        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color:{MUTED};font-size:12px;background:transparent;")
        pl.addWidget(self.summary)
        pl.addStretch()
        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet(f"color:{MUTED};font-size:12px;background:transparent;")
        pl.addWidget(self.progress_label)
        lo.addLayout(pl)
        self.prog = QProgressBar()
        self.prog.setRange(0, 100)
        self.prog.setValue(0)
        self.prog.setTextVisible(False)
        self.prog.setFixedHeight(7)
        self.prog.hide()
        lo.addWidget(self.prog)

        # Left/Right scan labels
        scan_row = QHBoxLayout()
        scan_row.setSpacing(12)
        self.left_scan = QLabel("Scanning left: Ready")
        self.left_scan.setStyleSheet(f"background:{FIELD_BG};border:1px solid {LINE};border-radius:8px;padding:9px;color:{INK};font-weight:700;")
        self.left_scan.setAlignment(Qt.AlignCenter)
        self.left_scan.setMinimumHeight(38)
        self.right_scan = QLabel("Scanning right: Ready")
        self.right_scan.setStyleSheet(f"background:{FIELD_BG};border:1px solid {LINE};border-radius:8px;padding:9px;color:{INK};font-weight:700;")
        self.right_scan.setAlignment(Qt.AlignCenter)
        self.right_scan.setMinimumHeight(38)
        scan_row.addWidget(self.left_scan, 1)
        scan_row.addWidget(self.right_scan, 1)
        lo.addLayout(scan_row)

        # Result panel (hidden)
        self._result_frame = QFrame()
        self._result_frame.setStyleSheet(f"QFrame{{border:1px solid {LINE};background:{PANEL};border-radius:13px;}}")
        rl = QVBoxLayout(self._result_frame)
        rl.setContentsMargins(18, 16, 18, 16)
        self._result_label = QLabel("")
        self._result_label.setWordWrap(True)
        self._result_label.setStyleSheet("background:transparent;")
        rl.addWidget(self._result_label)
        self._result_frame.hide()
        lo.addWidget(self._result_frame)

        self.stack.addWidget(page)

    def _sync_context_menu(self, pos):
        from PySide6.QtWidgets import QMenu as _QMenu
        row = self.table.rowAt(pos.y())
        if row < 0 or row >= len(self.model.items):
            return
        item = self.model.items[row]
        rel = item.rel
        fname = Path(rel).name if rel else rel
        ext = Path(rel).suffix
        parent = Path(rel).parent.as_posix() if Path(rel).parent != Path(".") else ""

        menu = _QMenu(self)
        menu.setStyleSheet(f"QMenu{{background:{PANEL};border:1px solid {LINE};border-radius:8px;padding:4px;}}"
                           f"QMenu::item{{padding:8px 16px;color:{INK};}}"
                           f"QMenu::item:selected{{background:{PANEL2};}}")

        # -- exclude actions
        menu.addAction(f"Exclude '{fname}' from sync")
        if ext:
            menu.addAction(f"Exclude pattern '*{ext}' (all {ext} files)")
        if parent:
            menu.addAction(f"Exclude folder '{parent}'")

        # -- show error/note details for problem items
        if item.status == Status.ERROR or item.action == "Conflict":
            menu.addSeparator()
            detail = item.note or f"{item.status.value}: {rel}"
            act_info = menu.addAction(detail)
            act_info.setEnabled(False)
            act_info.setStyleSheet(f"color:{DANGER};font-size:11px;")

        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        text = chosen.text()
        if text.startswith("Exclude '"):
            pattern = fname
        elif text.startswith("Exclude pattern"):
            pattern = f"*{ext}"
        elif text.startswith("Exclude folder"):
            pattern = f"{parent}/*"
        else:
            return

        j = next((x for x in self.state.jobs if x.id == self.results_jid), None)
        if not j:
            j = self.selected_job()
        if not j:
            return
        existing = [p.strip() for p in j.exclude.split(";") if p.strip()]
        if pattern in existing:
            self.toast.show_message(f"'{pattern}' is already in the exclude list")
            return
        existing.append(pattern)
        j.exclude = ";".join(existing)
        save_state(self.state)
        self.toast.show_message(f"Added '{pattern}' to {j.name} exclude list")

    # ── Recovery page ───────────────────────────────────────────────
    def _build_recovery(self):
        page = QWidget()
        lo = QVBoxLayout(page)
        lo.setContentsMargins(10, 10, 10, 10)
        lo.setSpacing(10)

        head = QHBoxLayout()
        vbox = QVBoxLayout()
        t = QLabel("Recovery")
        t.setStyleSheet("font-size:26px;font-weight:900;letter-spacing:-0.5px;")
        vbox.addWidget(t)
        s = QLabel("Restore a protected transaction or recover a file set to another folder.")
        s.setStyleSheet(f"color:{MUTED};font-size:13px;")
        vbox.addWidget(s)
        head.addLayout(vbox)
        head.addStretch()
        refresh = QPushButton("Refresh journals")
        refresh.setStyleSheet(f"background:#122238;border:1px solid {LINE};border-radius:8px;color:{INK};padding:9px 13px;font-weight:700;")
        refresh.clicked.connect(self.undo_recover)
        head.addWidget(refresh)
        lo.addLayout(head)

        self.recovery_combo = QComboBox()
        self.recovery_combo.setStyleSheet(f"background:{FIELD_BG};border:1px solid {LINE};border-radius:8px;color:{INK};padding:8px 10px;min-width:300px;")
        lo.addWidget(self.recovery_combo)

        empty = QLabel("Select a job and click Refresh journals to load recovery points.")
        empty.setStyleSheet(f"color:{MUTED};padding:40px;background:transparent;")
        empty.setAlignment(Qt.AlignCenter)
        self._recovery_empty = empty
        lo.addWidget(empty)

        self.stack.addWidget(page)

    # ── History page ────────────────────────────────────────────────
    def _build_history(self):
        page = QWidget()
        lo = QVBoxLayout(page)
        lo.setContentsMargins(10, 10, 10, 10)
        lo.setSpacing(10)

        head = QHBoxLayout()
        vbox = QVBoxLayout()
        t = QLabel("History")
        t.setStyleSheet("font-size:26px;font-weight:900;letter-spacing:-0.5px;")
        vbox.addWidget(t)
        s = QLabel("Recent operations and their native helper outcomes.")
        s.setStyleSheet(f"color:{MUTED};font-size:13px;")
        vbox.addWidget(s)
        head.addLayout(vbox)
        head.addStretch()
        clear = QPushButton("Clear history")
        clear.setStyleSheet(f"background:transparent;border:1px solid {DANGER};border-radius:8px;color:{DANGER};padding:9px 13px;font-weight:700;")
        clear.clicked.connect(self.clear_history)
        head.addWidget(clear)
        lo.addLayout(head)

        frame = QFrame()
        frame.setStyleSheet(f"QFrame{{border:1px solid {LINE};background:{PANEL};border-radius:10px;}}")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(0, 0, 0, 0)
        self.history_table = QTableWidget(0, 9)
        self.history_table.setHorizontalHeaderLabels(
            ["Time", "Job", "Operation", "Result", "Mode", "Items", "Duration", "Source", "Destination"]
        )
        self.history_table.setStyleSheet(f"""
            QTableWidget{{background:transparent;border:none;gridline-color:{LINE};
                          selection-background-color:#1a3050;color:{INK};}}
            QHeaderView::section{{background:#111f32;color:{MUTED};border:none;
                                 border-bottom:1px solid {LINE};padding:10px 12px;
                                 font-size:10px;font-weight:800;}}
        """)
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.history_table.horizontalHeader().setStretchLastSection(True)
        self.history_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history_table.verticalHeader().setVisible(False)
        hwidths = [165, 180, 130, 170, 90, 80, 90, 300, 360]
        for i, wid in enumerate(hwidths):
            self.history_table.setColumnWidth(i, wid)
        fl.addWidget(self.history_table)
        lo.addWidget(frame, 1)

        self.stack.addWidget(page)

    # ── Helpers ─────────────────────────────────────────────────────
    def selected_job(self) -> Optional[Job]:
        rows = self.jobs_table.selectionModel().selectedRows()
        if not rows:
            return None
        jid = self.jobs_table.item(rows[0].row(), 0).data(Qt.UserRole)
        return next((j for j in self.state.jobs if j.id == jid), None)

    def set_parallel(self, n):
        self.state.max_parallel_jobs = n
        save_state(self.state)

    # ── Reload data ─────────────────────────────────────────────────
    def reload_jobs(self):
        self.jobs_table.setRowCount(len(self.state.jobs))
        for r, j in enumerate(self.state.jobs):
            vals = [
                j.name, j.mode, "Yes" if j.enabled else "No",
                j.scheduler_time if j.scheduler_enabled else "Manual",
                j.last_run, j.last_result, j.source, j.destination,
            ]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                if c == 0:
                    it.setData(Qt.UserRole, j.id)
                if c == 5:
                    lr = str(v)
                    color = GOOD if lr == "Success" else (WARNING if "Guard" in lr else (DANGER if lr == "Failed" else MUTED))
                    it.setForeground(QColor(color))
                self.jobs_table.setItem(r, c, it)
        self._refresh_dashboard()

    def _refresh_dashboard(self):
        rows = list(reversed(history_data()))
        jobs = self.state.jobs

        success = sum(1 for r in rows if r.get("result", "") == "Success")
        failed = sum(1 for r in rows if "Failed" in r.get("result", "") or "Guard blocked" in r.get("result", ""))
        gc = sum(1 for j in jobs if j.guard_enabled)

        self.card_jobs.value_label.setText(str(len(jobs)))
        self.card_success.value_label.setText(str(success))
        self.card_guard.value_label.setText(f"{gc}/{len(jobs)}")
        self.card_failed.value_label.setText(str(failed))

        self.recent_list.clear()
        for r in rows[:RECENT_HISTORY_ITEMS]:
            result = r.get("result", "")
            icon = "[OK]" if result == "Success" else ("[X]" if "Failed" in result else "[!]")
            meta = ""
            if r.get("operation") == "Synchronize":
                meta = f"  |  {mb_text(int(r.get('bytes_changed', 0)))}  |  {int(r.get('files_changed', 0)):,} files"
            item = QListWidgetItem(f"{icon} {r.get('timestamp', '').replace('T', ' ')}  {r.get('job', '')}  -  {result}{meta}")
            item.setData(Qt.UserRole, r.get("job_id"))
            self.recent_list.addItem(item)

        self.upcoming_list.clear()
        now = datetime.now()
        upcoming = []
        for j in jobs:
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
            self.upcoming_list.addItem(f"{run:%Y-%m-%d %H:%M}  {name}  -  {action}")
        if not upcoming:
            self.upcoming_list.addItem("No scheduled jobs configured")

    def reload_history(self):
        rows = list(reversed(history_data()))
        self.history_table.setRowCount(len(rows))
        for r, d in enumerate(rows):
            vals = [
                d.get("timestamp", "").replace("T", " "), d.get("job", ""),
                d.get("operation", ""), d.get("result", ""), d.get("mode", ""),
                str(d.get("items", 0)), f"{d.get('duration', 0):.1f}s",
                d.get("source", ""), d.get("destination", ""),
            ]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                if c == 3:
                    lr = str(v)
                    color = GOOD if lr == "Success" else (WARNING if "Guard" in lr else DANGER)
                    it.setForeground(QColor(color))
                self.history_table.setItem(r, c, it)
        self._refresh_dashboard()

    def clear_history(self):
        if QMessageBox.question(self, "Clear history", "Delete operation history?",
                                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            HISTORY_FILE.unlink(missing_ok=True)
            self.reload_history()
            self.toast.show_message("History cleared")

    # ── Job actions ─────────────────────────────────────────────────
    def new_job(self):
        d = JobDialog(Job(name=f"Job {len(self.state.jobs) + 1}"), self.state.templates, self)
        if d.exec():
            self.state.jobs.append(d.result_job())
            save_state(self.state)
            self.reload_jobs()
            self.toast.show_message(f"Job '{d.result_job().name}' created")

    def copy_job(self):
        j = self.selected_job()
        if not j:
            self.toast.show_message("Select a job to copy", error=True)
            return
        d = job_from_dict(asdict(j))
        d.id = str(uuid.uuid4())
        d.name += " Copy"
        self.state.jobs.append(d)
        save_state(self.state)
        self.reload_jobs()
        self.toast.show_message(f"Job copied as '{d.name}'")

    def edit_job(self):
        j = self.selected_job()
        if not j:
            self.toast.show_message("Select a job to edit", error=True)
            return
        d = JobDialog(j, self.state.templates, self)
        if d.exec():
            nj = d.result_job()
            idx = self.state.jobs.index(j)
            self.state.jobs[idx] = nj
            save_state(self.state)
            self.reload_jobs()
            self.toast.show_message(f"Job '{nj.name}' saved")

    def delete_job(self):
        j = self.selected_job()
        if not j:
            return
        if QMessageBox.question(self, "Delete job", f'Delete "{j.name}"? This cannot be undone.',
                                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self.state.jobs = [x for x in self.state.jobs if x.id != j.id]
            save_state(self.state)
            self.reload_jobs()
            self.toast.show_message(f"Job '{j.name}' deleted")

    # ── Run actions ─────────────────────────────────────────────────
    def run_compare(self):
        j = self.selected_job()
        if j:
            self.enqueue_jobs([j], "compare")

    def run_compare_sync(self):
        j = self.selected_job()
        if j:
            self.enqueue_jobs([j], "compare_sync")

    def _guard_override_sync(self):
        """Sync despite guard blocking — re-queue with override."""
        j = getattr(self, "_guard_blocked_job", None)
        if not j:
            return
        details = getattr(self, "_guard_blocked_details", "")
        confirm = QMessageBox.question(
            self, "Confirm manual override",
            f"Manually override Ransomware Guard for '{j.name}'?"
            + (f"\n\n{details}" if details else ""),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm == QMessageBox.Yes:
            add_history(j, "Ransomware Guard override", "Manually approved", 0, 0, details=details)
            self._guard_frame.hide()
            self._guard_sync_btn.hide()
            self.job_queue.insert(0, (j, "compare_sync_override"))
            self.start_next_jobs()
            self.toast.show_message(f"Guard overridden for {j.name} — syncing")

    def run_selected(self):
        j = self.selected_job()
        if j:
            self.enqueue_jobs([j])

    def run_all(self):
        enabled = [j for j in self.state.jobs if j.enabled]
        if not enabled:
            self.toast.show_message("No enabled jobs to run", error=True)
            return
        if QMessageBox.question(self, "Run All", f"Run {len(enabled)} job(s)?",
                                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self.enqueue_jobs(enabled)

    def undo_recover(self):
        j = self.selected_job()
        if not j:
            self.toast.show_message("Select a job first", error=True)
            return
        cleanup_undo(j)
        if not available_undo_manifests(j):
            self.toast.show_message("No undo journals within the last 5 days", error=True)
            return
        d = UndoDialog(j, self)
        if d.exec() == QDialog.Accepted:
            payload = d.payload()
            self.start_worker(j, "undo", undo_payload=payload)

    # ── Job queue & worker management ───────────────────────────────
    def enqueue_jobs(self, jobs, op=None):
        if self.shutting_down:
            return
        for j in jobs:
            self.job_queue.append((j, op) if op else j)
        self.start_next_jobs()

    def start_next_jobs(self):
        if self.shutting_down:
            return
        while self.job_queue and len(self.running_jobs) < self.state.max_parallel_jobs:
            entry = self.job_queue.pop(0)
            j, op = entry if isinstance(entry, tuple) else (entry, "compare_sync")
            self.start_worker(j, op)
        self.reload_jobs()

    def start_worker(self, j, op, items=None, undo_payload=None):
        if op != "undo" and (not Path(j.source).is_dir() or not Path(j.destination).is_dir()):
            self.toast.show_message(f"{j.name}: source or destination missing.", error=True)
            return

        # Reset guard banner for new operation
        self._guard_frame.hide()
        self._guard_sync_btn.hide()
        self._guard_blocked_job = None
        self._guard_blocked_details = ""

        th = QThread(self)
        wk = SyncWorker(j, op, items, undo_payload)
        wk.moveToThread(th)
        th.started.connect(wk.run)
        wk.progress.connect(self.on_progress, Qt.QueuedConnection)
        wk.compared.connect(self.on_compared, Qt.QueuedConnection)
        wk.finished.connect(self.on_finished, Qt.QueuedConnection)
        wk.failed.connect(self.on_failed, Qt.QueuedConnection)
        wk.finished.connect(th.quit)
        wk.failed.connect(th.quit)
        th.finished.connect(lambda jid=j.id: self.cleanup(jid))
        self.threads[j.id] = th
        self.workers[j.id] = wk
        self.running_jobs.add(j.id)
        self.prog.hide()
        self._cancel_btn.setVisible(True)
        th.start()
        self._sync_src.setText(j.source or "—")
        self._sync_dst.setText(j.destination or "—")
        self._switch_page(2)

    def cancel_running_jobs(self):
        ids = [x for x in list(self.running_jobs)]
        if not ids:
            return
        for x in ids:
            w = self.workers.get(x)
            if w:
                w.cancel()
        self.toast.show_message("Cancelling...")

    def closeEvent(self, event):
        self.shutting_down = True
        self.timer.stop()
        self.job_queue.clear()
        for worker in list(self.workers.values()):
            worker.cancel()
        deadline = time.monotonic() + SHUTDOWN_TIMEOUT_SECONDS
        for thread in list(self.threads.values()):
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            if not thread.wait(remaining_ms):
                self.shutting_down = False
                self.toast.show_message("Close cancelled: an operation is still finishing.")
                event.ignore()
                return
        try:
            save_state(self.state)
        except OSError:
            pass
        event.accept()

    @Slot(str)
    def cleanup(self, jid):
        self.threads.pop(jid, None)
        self.workers.pop(jid, None)
        self.running_jobs.discard(jid)
        self._cancel_btn.setVisible(bool(self.running_jobs))
        if not self.shutting_down:
            self.start_next_jobs()
        if self.exit_when_idle and not self.running_jobs and not self.job_queue:
            self.exit_when_idle = False
            QTimer.singleShot(AUTO_EXIT_DELAY_MS, QApplication.instance().quit)

    # ── Worker signal handlers ──────────────────────────────────────
    @Slot(str, int, int, str)
    def on_progress(self, jid, n, total, text):
        j = next((x for x in self.state.jobs if x.id == jid), None)
        # Skip redundant label updates to avoid layout thrash during rapid ticks
        job_name = j.name if j else jid
        if self.current_job_label.text() != f"Active: {job_name}":
            self.current_job_label.setText(f"Active: {job_name}")
        scanning = False
        if text.startswith("Scanning source and destination"):
            if self.left_scan.text() != "Scanning left: Starting...":
                self.left_scan.setText("Scanning left: Starting...")
            if self.right_scan.text() != "Scanning right: Starting...":
                self.right_scan.setText("Scanning right: Starting...")
            scanning = True
        elif text.startswith("Scanning left:"):
            self.left_scan.setText(text)
            scanning = True
        elif text.startswith("Scanning right:"):
            self.right_scan.setText(text)
            scanning = True
        if scanning or not total:
            self.prog.hide()
        else:
            self.prog.setRange(0, total)
            self.prog.setValue(n)
            self.prog.setFormat(text)
            self.prog.show()
        # Do NOT toggle cancel button visibility here — it triggers a full
        # layout recalculation on every tick, unlike DarkSync 2.0.py which
        # only toggles it in start_worker / cleanup. The button visibility
        # is managed in on_compared, on_finished, on_failed, and start_worker.

    @Slot(str, object, object)
    def on_compared(self, jid, items, warnings):
        j = next((x for x in self.state.jobs if x.id == jid), None)
        self.results_jid = jid
        self.active_items = items
        self.model.set_items(items)
        self.summary.setText(f"{j.name if j else jid}: {len(items)} items, "
                             f"{sum(x.action not in ('Skip', 'Conflict') for x in items)} actions")
        self.left_scan.setText("Scanning left: Complete")
        self.right_scan.setText("Scanning right: Complete")

    @Slot(str, object)
    def on_finished(self, jid, result):
        j = next((x for x in self.state.jobs if x.id == jid), None)
        if not j:
            return
        dur = 0

        if result.get("operation") == "compare":
            add_history(j, "Compare", "Success", result.get("items", 0), dur)
            self.prog.setRange(0, 1)
            self.prog.setValue(1)
            self.prog.setFormat("Comparison completed")
            self.toast.show_message(f"Comparison completed: {result.get('items', 0)} items found")

        elif result.get("operation") == "sync":
            sm = result["summary"]
            problems = sum(sm.get(k, 0) for k in REPORT_STATUSES)
            guard = result.get("guard") or {}
            override = bool(guard.get("manually_approved"))
            if override:
                res = "Success (manual guard override)" if problems == 0 else "Completed with issues (manual guard override)"
            else:
                res = "Success" if problems == 0 else "Completed with issues"
            j.last_run = timestamp_now()
            j.last_result = res
            j.last_report = result.get("report", "")
            add_history(j, "Synchronize", res, sm.get("Copied", 0) + sm.get("Removed", 0), dur,
                        result.get("report", ""), "", int(sm.get("Bytes changed", 0)),
                        int(sm.get("Files changed", 0)), int(sm.get("Folders changed", 0)))
            self.load_report(result["report_data"])
            try:
                send_notification(j, f"DarkSync {res}: {j.name}", json.dumps(sm, indent=2), problems == 0)
            except Exception as ex:
                self.toast.show_message(f"Notification failed: {ex}", error=True)
            self.prog.setRange(0, 1)
            self.prog.setValue(1)
            self.prog.setFormat("Completed")
            self.toast.show_message(f"Synchronization completed: {res}")
            if j.exit_on_completion:
                self.exit_when_idle = True

        elif result.get("operation") == "guard_blocked":
            g = result["guard"]
            details = guard_details(g)
            res = "Blocked by Ransomware Guard"
            j.last_run = timestamp_now()
            j.last_result = res
            add_history(j, "Pre-flight protection", res, g["changed"], 0, details=details,
                        files_changed=g["files_added"] + g["files_deleted"] + g["files_modified"],
                        folders_changed=g["folders_added"] + g["folders_deleted"])
            self._guard_blocked_job = j
            self._guard_blocked_details = details
            self._guard_frame.setStyleSheet(f"QFrame{{border:1px solid rgba(220,80,80,0.5);background:rgba(220,80,80,0.08);border-radius:10px;}}")
            self._guard_title.setText(f"Guard blocked: {j.name}")
            self._guard_title.setStyleSheet(f"color:{DANGER};font-weight:800;background:transparent;")
            self._guard_detail.setText(
                f"{details}\nThreshold: {j.guard_threshold_percent:.2f}% — "
                f"Review the changes below. Click 'Sync anyway' only if they are expected."
            )
            self._guard_sync_btn.show()
            self._guard_frame.show()
            self.toast.show_message(f"Guard blocked {j.name} — review and click Sync anyway if expected", error=True)

        elif result.get("operation") == "undo":
            res = "Success" if result.get("success") else "Completed with issues"
            add_history(j, "Undo/Recover", res, result.get("restored", 0), dur,
                        details="; ".join(result.get("errors", [])[:MAX_SCAN_ERROR_DETAILS]))
            self.toast.show_message(f"Recovered: {result.get('restored', 0)} files")

        save_state(self.state)
        self.reload_history()
        self.reload_jobs()
        self._cancel_btn.setVisible(bool(self.running_jobs))

    @Slot(str, str)
    def on_failed(self, jid, msg):
        j = next((x for x in self.state.jobs if x.id == jid), None)
        if j:
            j.last_run = timestamp_now()
            j.last_result = "Failed"
            add_history(j, "Operation", "Failed", 0, 0, details=msg)
            save_state(self.state)
            self.reload_history()
            try:
                send_notification(j, f"DarkSync Failed: {j.name}", msg, False)
            except Exception:
                pass
        self.prog.setRange(0, 1)
        self.prog.setValue(1)
        self.prog.setFormat("Failed")
        self.toast.show_message(f"Operation failed: {msg[:120]}", error=True)
        self._cancel_btn.setVisible(bool(self.running_jobs))
        QMessageBox.critical(self, "Operation Failed", f"Job failed:\n{msg[:500]}")

    def load_report(self, rd):
        self.latest_report_rows = rd.get("items", [])
        self.toast.show_message(f"Report loaded: {len(self.latest_report_rows)} items")
        self.tabs_switch_to_results()

    def tabs_switch_to_results(self):
        # Show results inline on sync page
        if self.latest_report_rows:
            problems = sum(1 for r in self.latest_report_rows if r.get("status") in REPORT_STATUSES)
            self._result_frame.show()
            self._result_label.setText(
                f"<b>Report loaded</b> — {len(self.latest_report_rows)} items, {problems} problems"
            )

    # ── Schedule ────────────────────────────────────────────────────
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
            save_state(self.state)
            self.enqueue_jobs(due)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "toast"):
            self.toast.move(self.width() - 460, self.height() - 60)


# ── Theme definitions ───────────────────────────────────────────────────
THEMES = {"DarkSync": {"bg": BG, "muted": MUTED, "ink": INK, "panel": PANEL, "line": LINE, "accent": ACCENT}}


# ── Global stylesheet (darksync.html palette) ──────────────────────────
STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {BG}; color: {INK};
    font-family: "Segoe UI", "SF Pro Text", "Helvetica Neue", sans-serif;
    font-size: 13px;
}}
QLabel {{ color: {INK}; background: transparent; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit, QTimeEdit {{
    background: {FIELD_BG}; border: 1px solid {LINE};
    border-radius: 8px; color: {INK}; padding: 8px 10px;
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTimeEdit:focus {{
    border-color: {ACCENT2};
}}
QComboBox::drop-down {{ border: none; padding-right: 8px; }}
QComboBox QAbstractItemView {{
    background: {PANEL}; border: 1px solid {LINE};
    color: {INK}; selection-background-color: {PANEL2};
}}
QPushButton {{
    border: 1px solid {LINE}; border-radius: 8px;
    padding: 8px 14px; background: #122238; color: {INK}; font-weight: 700;
}}
QPushButton:hover {{ border-color: #426487; background: #172b47; }}
QPushButton:disabled {{ opacity: 0.45; }}
QPushButton#primary {{ background: {ACCENT}; color: #12200e; border-color: {ACCENT}; }}
QPushButton#primary:hover {{ background: #ccff8f; }}
QTableWidget, QTableView {{
    background: {PANEL}; border: 1px solid {LINE}; border-radius: 10px;
    gridline-color: {LINE}; selection-background-color: #1a3050;
}}
QTableWidget::item, QTableView::item {{ padding: 8px 10px; border-bottom: 1px solid rgba(34,54,80,120); }}
QHeaderView::section {{
    background: #111f32; color: {MUTED}; border: none;
    border-bottom: 1px solid {LINE}; padding: 10px 12px;
    font-size: 10px; font-weight: 800;
}}
QProgressBar {{
    background: {FIELD_BG}; border: 1px solid {LINE};
    border-radius: 4px; height: 7px; text-align: center;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QScrollBar:vertical {{ background: transparent; width: 8px; }}
QScrollBar::handle:vertical {{ background: {LINE}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
QCheckBox {{ color: {MUTED}; spacing: 8px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid {LINE}; background: {FIELD_BG};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QRadioButton {{ color: {INK}; spacing: 8px; }}
QRadioButton::indicator {{
    width: 16px; height: 16px; border-radius: 9px;
    border: 1px solid {LINE}; background: {FIELD_BG};
}}
QRadioButton::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QMenuBar {{ background: {SIDEBAR_BG}; border-bottom: 1px solid {LINE}; color: {INK}; }}
QMenuBar::item {{ padding: 8px 12px; }}
QMenuBar::item:selected {{ background: {PANEL2}; }}
QMenu {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 8px; padding: 4px; color: {INK}; }}
QMenu::item {{ padding: 8px 24px; }}
QMenu::item:selected {{ background: {PANEL2}; }}
QStatusBar {{ background: #0b1220; color: {MUTED}; }}
QToolTip {{ background: {PANEL2}; border: 1px solid {LINE}; color: {INK}; padding: 6px; border-radius: 6px; }}
QTabWidget::pane {{ border: 1px solid {LINE}; border-radius: 8px; background: {BG}; }}
QTabBar::tab {{
    background: {PANEL}; color: {INK}; padding: 10px 18px;
    border: 1px solid {LINE}; border-bottom: none;
    border-radius: 8px 8px 0 0; margin-right: 2px;
}}
QTabBar::tab:selected {{ background: {ACCENT}; color: #12200e; border-color: {ACCENT}; }}
QTabBar::tab:hover {{ background: {PANEL2}; }}
"""


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)

    w = Main()
    w.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
