# DarkSync 2.0 - Multi-Job Folder Synchronization Utility

A comprehensive folder comparison and synchronization utility built with Python and PySide6, featuring multi-job management, scheduling, ransomware protection, and advanced recovery capabilities.

## Features

### Core Functionality
- **Folder Comparison**: Identify differences between source and destination folders using time/size or hash-based comparison
- **Multiple Sync Modes**: Mirror, Update, Bidirectional, and Custom synchronization modes
- **Multi-Job Support**: Manage multiple independent sync jobs simultaneously
- **Multi-threading**: Configurable worker threads (1-32) for optimized performance

### Advanced Features
- **Scheduling**: Automated sync jobs with customizable timing and actions
- **History Tracking**: Detailed logs and statistics (up to 5000 entries)
- **Undo/Recovery**: 5-day retention period for reversed operations
- **Guard System**: Ransomware detection using baseline snapshots with configurable threshold alerts
- **Notifications**: Email (SMTP) and push notifications (ntfy.sh) support
- **Conflict Resolution**: Handle file conflicts intelligently
- **Advanced Filtering**: Include/exclude patterns with glob support
- **Hash Verification**: SHA256 verification for data integrity
- **Recycle Bin Support**: Safe deletion with recovery options

### Report Generation
Generated reports include only:
- Failed operations
- Cancelled operations
- Not selected items
- Conflicts

*Skipped entries are intentionally excluded from reports.*

## Requirements

- Python 3.8+
- PySide6
- SQLite3 (included with Python)

### Install Dependencies
```bash
pip install PySide6
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

## Usage

Run the application:
```bash
python "DarkSync 2.0.py"
```

Or if frozen as executable:
```bash
./DarkSync\ 2.0
```

## Project Structure

```
/workspace/
├── DarkSync 2.0.py          # Main application file
├── DarkSync 2.0.py.backup   # Backup of main application
├── darksync_jobs.json       # Job configurations (generated at runtime)
├── darksync_history.json    # History data (generated at runtime)
├── logs/                    # Log files directory (generated at runtime)
├── .darksync_undo/          # Undo recovery data (generated at runtime)
└── .darksync_guard/         # Guard baselines (generated at runtime)
```

## Version

**Current Version**: 2.6.5

### Recent Changes (v2.6.5)
- Removed two-week dashboard calendar/history
- Generated reports include only Failed, Cancelled, Not selected, and Conflict entries
- Skipped entries excluded from generated report files
- Removed scrolling/indeterminate blue progress bar during scanning
- Scanning left/right labels remain visible side by side
- Normal determinate progress bar for compare, sync, and recovery operations

## Platform Support

- Windows
- macOS
- Linux

## License

This project is provided as-is for backup and synchronization purposes.

## Security Notice

The Guard system provides ransomware detection by monitoring changes against baseline snapshots. Configure appropriate thresholds based on your typical file change patterns to minimize false positives while maintaining security.

---

**DarkSync 2.0** - Professional-grade backup and synchronization with ransomware protection.
