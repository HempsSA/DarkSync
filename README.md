# DarkSync 2.0 - Multi-Job Folder Synchronization Utility

A comprehensive folder comparison and synchronization utility built with Python and PySide6, featuring multi-job management, scheduling, ransomware protection, system tray integration, and advanced recovery capabilities.

## Screenshots

| Main Dashboard | Job Setup |
|:---:|:---:|
| ![Main Dashboard](screenshots/dashboard.png) | ![Job Setup](screenshots/job_setup.png) |
| Synchronization | System Tray |
| ![Synchronization](screenshots/sync.png) | ![System Tray](screenshots/system_tray.png) |

> **Note**: Add your own screenshots by placing PNG files in the `screenshots/` folder with these names: `dashboard.png`, `job_setup.png`, `sync.png`, `system_tray.png`.

## Features

### Core Functionality
- **Folder Comparison**: Identify differences between source and destination folders using time/size or hash-based comparison
- **Multiple Sync Modes**: Mirror, Update, Bidirectional, and Custom synchronization modes
- **Multi-Job Support**: Manage multiple independent sync jobs simultaneously
- **Multi-threading**: Configurable worker threads (1-32) for optimized performance

### Advanced Features
- **System Tray Integration**: Minimize to the system tray with custom icon. Both the close button (X) and minimize button hide to tray. Double-click the tray icon to restore. Right-click for Restore and Quit options.
- **Scheduling**: Automated sync jobs with customizable timing and actions
- **History Tracking**: Detailed logs and statistics (up to 5000 entries)
- **Undo/Recovery**: 5-day retention period for reversed operations
- **Guard System**: Ransomware detection using baseline snapshots with configurable threshold alerts
- **Notifications**: Email (SMTP) and push notifications (ntfy.sh) support
- **Conflict Resolution**: Handle file conflicts intelligently
- **Advanced Filtering**: Include/exclude patterns with glob support (default excludes: `.DS_Store`, `Thumbs.db`, `$Recycle.Bin`, `System Volume Information`, temporary files)
- **Hash Verification**: SHA256 verification for data integrity
- **Recycle Bin Support**: Safe deletion with recovery options

### Report Generation
Generated reports include only:
- Failed operations
- Cancelled operations
- Not selected items
- Conflicts

*Skipped entries are intentionally excluded from reports.*

## Editions

### DarkSync 2.0 (Main)
The primary PySide6 desktop application with full toolbar, tabbed interface, and theme support.

```bash
python "DarkSync 2.0.py"
```

### DarkSync Desktop
A standalone PySide6 edition with a sidebar-based visual design. Includes all features of the main edition — filesystem operations, scheduling, Guard, recovery, notifications — running locally in a single process.

```bash
python darksync_desktop.py
```

Both editions support system tray minimize-to-tray with a custom sync icon.

## Quick Start

### Fresh Install (Windows) — Inno Setup Installer (Recommended)

Download `DarkSync-2.6.5-Setup.exe` from the [Releases](https://github.com/HempsSA/DarkSync/releases) page and run it.

The installer will:
1. Verify Python 3.8+ is installed
2. Copy application files to your chosen folder
3. Install Python dependencies automatically
4. Create Desktop and Start-Menu shortcuts
5. Offer to launch DarkSync immediately

> **Building the installer yourself** — see [Building the Installer](#building-the-installer) below.

### Fresh Install (Windows) — Manual Setup

Run the setup script on a new machine:

```cmd
setup.bat                        Install to C:\DarkSync
setup.bat D:\Tools\DarkSync      Install to a custom location
```

This will:
1. Check for Git and Python
2. Clone the repository
3. Install Python dependencies
4. Optionally launch DarkSync

### Update Existing Install

```cmd
update.bat            Pull latest changes (Windows)
update.bat --check    Only check for updates
```

```bash
./update.sh           Pull latest changes (macOS/Linux)
./update.sh --check   Only check for updates
```

The update scripts automatically stash and restore any local changes, so your job configurations and customizations are preserved.

If `.git` is missing (e.g. after a manual copy), the scripts will automatically recover by re-initializing from the remote.

## Building the Installer

An [Inno Setup](https://jrsoftware.org/isdl.php) script is included in the `installer/` folder.

### Prerequisites

- **Windows 10/11** (build 17763+)
- **Python 3.8+** installed and in PATH
- **Inno Setup 6+** — [download here](https://jrsoftware.org/isdl.php)

### Build Steps

**Option A — Double-click** (easiest):

Double-click `build_installer.bat` from the repo root. It auto-discovers the Inno Setup compiler.

**Option B — Command line**:

```cmd
:: From the repo root:
iscc installer\DarkSync.iss
```

The compiled installer will appear in `dist/DarkSync-2.6.5-Setup.exe`.

### What the installer does

1. **Pre-flight check** — Verifies Python 3.8+ is present; offers to open the download page if not.
2. **File copy** — Copies all application files, icons, scripts, and docs to the chosen folder.
3. **Dependency install** — Runs `pip install -r requirements.txt` silently in the background.
4. **Shortcuts** — Creates Start-Menu shortcuts for both editions plus Desktop shortcuts via `create_shortcuts.ps1`.
5. **Post-install** — Offers to launch DarkSync 2.0 or DarkSync Desktop.
6. **Uninstaller** — Registers in *Add or Remove Programs* with full cleanup.

> **Tip:** Inno Setup can also build silent/unattended installs (`/SILENT` or `/VERYSILENT`) for enterprise deployment.

## Requirements

- Python 3.8+
- PySide6
- SQLite3 (included with Python)

### Install Dependencies
```bash
pip install -r requirements.txt
```

## Configuration

Environment variables can override default settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `DARKSYNC_UNDO_RETENTION_DAYS` | 5 | Days to retain undo history |
| `DARKSYNC_BLOCK_SIZE_BYTES` | 4194304 (4MB) | Block size for hashing |
| `DARKSYNC_MAX_WORKERS` | 32 | Maximum worker threads |
| `DARKSYNC_MIN_WORKERS` | 1 | Minimum worker threads |
| `DARKSYNC_HISTORY_MAX_ENTRIES` | 5000 | Maximum history entries |
| `DARKSYNC_GUARD_THRESHOLD_PERCENT` | 4.0 | Guard alert threshold (%) |

## Data Files

The application stores configuration and data in the following files (located in the application directory):

- `darksync_jobs.json` - Job configurations
- `darksync_history.json` - Operation history
- `logs/` - Log files directory
- `.darksync_undo/` - Undo/recovery data
- `.darksync_guard/` - Guard baseline snapshots (SQLite)

*These files are unique per installation and are excluded from the git repository.*

## Project Structure

```
DarkSync/
├── DarkSync 2.0.py              # Main application (PySide6)
├── darksync_desktop.py          # Standalone desktop edition (PySide6)
├── darksync_icon.png            # System tray icon
├── icon_main.png                # Shortcut icon — main edition
├── icon_main.ico                # Shortcut icon — main edition (ICO)
├── icon_desktop.png             # Shortcut icon — desktop edition
├── icon_desktop.ico             # Shortcut icon — desktop edition (ICO)
├── setup.bat                    # Fresh Windows installer
├── build_installer.bat          # Build Inno Setup installer (double-click)
├── update.bat                   # Windows updater (auto-stash)
├── update.sh                    # macOS/Linux updater (auto-stash)
├── create_shortcuts.bat         # Create Desktop shortcuts
├── create_shortcuts.ps1         # PowerShell shortcut creator
├── requirements.txt             # Python dependencies
├── installer/
│   └── DarkSync.iss             # Inno Setup installer script
├── screenshots/                 # Dashboard and UI screenshots
├── DAILY_RUN_GUIDE.md           # Automated daily run guide
├── README.md                    # This file
├── .gitignore                   # Git exclusions
├── darksync_jobs.json           # Job configs (runtime, not tracked)
├── darksync_history.json        # History data (runtime, not tracked)
├── logs/                        # Log files (runtime, not tracked)
├── .darksync_undo/              # Undo data (runtime, not tracked)
└── .darksync_guard/             # Guard baselines (runtime, not tracked)
```

### Default Exclusion Patterns

The following patterns are excluded by default on Windows environments:

- `.DS_Store` - macOS metadata files
- `Thumbs.db` - Windows thumbnail cache
- `.darksync_*` - DarkSync internal files
- `logs/*` - Log directories
- `$Recycle.Bin` - Windows Recycle Bin
- `System Volume Information` - Windows system restore points
- `*.tmp`, `*.temp` - Temporary files

Additional patterns can be added in the job configuration using semicolon-separated glob patterns.

## System Tray

Both editions minimize to the system tray using a custom sync icon:

- **Close button (X)** → Hides to tray (does not exit)
- **Minimize button** → Hides to tray
- **Double-click tray icon** → Restores the window
- **Right-click tray icon** → Menu with Restore and Quit
- **Tray → Quit** → Actually exits the application

This ensures background scheduled jobs and Guard monitoring continue running even when the window is hidden.

## Multi-Location Deployment

DarkSync can be deployed across multiple machines using this git repository:

1. **Install** on each machine using `setup.bat` (Windows) or clone the repo manually
2. **Update** all locations by running `update.bat` or `./update.sh` — the scripts auto-stash local changes and pull the latest code
3. Each installation has its own independent `darksync_jobs.json` and `darksync_history.json` — job configurations are not shared between locations

## Platform Support

- Windows
- macOS
- Linux

## Version

**Current Version**: 2.6.5

### Recent Changes
- System tray integration with custom icon — minimize to tray, close-to-tray, double-click to restore
- Standalone desktop edition (`darksync_desktop.py`)
- Windows installer (`setup.bat`) and updater (`update.bat`)
- Inno Setup installer (`installer/DarkSync.iss`) — proper Windows setup wizard with dependency install
- macOS/Linux updater (`update.sh`) with auto-stash and `.git` recovery
- Multi-location deployment support via GitHub repository

### Previous Changes (v2.6.5)
- Removed two-week dashboard calendar/history
- Generated reports include only Failed, Cancelled, Not selected, and Conflict entries
- Skipped entries excluded from generated report files
- Removed scrolling/indeterminate blue progress bar during scanning
- Scanning left/right labels remain visible side by side
- Normal determinate progress bar for compare, sync, and recovery operations

## License

This project is provided as-is for backup and synchronization purposes.

## Security Notice

The Guard system provides ransomware detection by monitoring changes against baseline snapshots. Configure appropriate thresholds based on your typical file change patterns to minimize false positives while maintaining security.

---

**DarkSync 2.0** - Professional-grade backup and synchronization with ransomware protection.
