# DarkSync 2.0 - Daily Automated Backup Guide for Windows

## Overview
DarkSync 2.0 now supports automated daily runs with proper exit behavior, making it ideal for scheduled backup tasks on Windows environments.

## New Features for Automated Runs

### 1. Enhanced Windows Exclusions
Default exclusion patterns now include Windows-specific system files:
- `$Recycle.Bin` - Windows Recycle Bin
- `System Volume Information` - System restore points
- `pagefile.sys` - Windows paging file
- `hiberfil.sys` - Windows hibernation file
- `*.tmp`, `*.temp` - Temporary files
- `.DS_Store`, `Thumbs.db` - Cross-platform cache files

### 2. Exit on Completion
Jobs can now be configured to automatically exit the application after completion, perfect for scheduled tasks.

## Setup Instructions

### Option 1: Using Windows Task Scheduler (Recommended)

1. **Configure Your Job**:
   - Open DarkSync 2.0
   - Create or edit a backup job
   - Go to the "Ransomware Guard" tab
   - Check **"Exit application after job completion (for automated runs)"**
   - Save the job

2. **Create Scheduled Task**:
   ```powershell
   # Open Task Scheduler and create a basic task
   
   # Action: Start a program
   # Program/script: python.exe (or your DarkSync executable)
   # Add arguments: "DarkSync 2.0.py" --minimize (if supported)
   
   # Or if using compiled executable:
   # Program/script: C:\Path\To\DarkSync.exe
   ```

3. **Configure Triggers**:
   - Set to run daily at your preferred time (e.g., 2:00 AM)
   - Configure to run whether user is logged on or not
   - Check "Run with highest privileges" if accessing system folders

4. **Configure Conditions**:
   - Uncheck "Start only if computer is on AC power" for laptops
   - Configure wake timers if needed

### Option 2: Using Built-in Scheduler

1. **Enable Job Scheduler**:
   - Open job settings
   - Go to "Schedule" tab
   - Check "Scheduled"
   - Set time (e.g., 02:00)
   - Set action to "Compare and synchronize"
   
2. **Keep Application Running**:
   - The built-in scheduler requires the app to be running
   - Use this for interactive daily use

## Command Line Usage

For automated scripts:

```batch
@echo off
REM Daily backup script
cd C:\Path\To\DarkSync
python "DarkSync 2.0.py" --job "BackupJob" --auto-exit
REM Application will exit automatically after job completes
```

## Best Practices for Daily Runs

### 1. Notification Configuration
- Enable email or ntfy notifications for failure alerts
- Disable success notifications to reduce noise
- Use the "Use Backup Monitoring Defaults" button in Notifications tab

### 2. Ransomware Protection
- Keep Guard enabled with appropriate threshold (4-10%)
- Review blocked operations periodically
- Configure ignore permissions only if expected

### 3. Performance Optimization
- Set appropriate worker count (2-8 for most systems)
- Exclude unnecessary file types
- Schedule during off-hours

### 4. Monitoring
- Check history logs regularly
- Review notification reports
- Monitor disk space on destination

## Troubleshooting

### Application Doesn't Exit
- Verify "Exit on completion" is checked in job settings
- Check for modal dialogs blocking exit
- Review logs for errors preventing completion

### Job Fails to Run
- Verify paths are accessible
- Check permissions on source/destination
- Ensure sufficient disk space
- Review error logs in `/logs` folder

### Scheduled Task Issues
- Run task manually to test
- Check task scheduler history
- Verify user account has necessary permissions
- Ensure paths use correct format (C:\ vs \\server\)

## Example Task Scheduler XML

```xml
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2">
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2024-01-01T02:00:00</StartBoundary>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Actions>
    <Exec>
      <Command>C:\Python39\python.exe</Command>
      <Arguments>"C:\DarkSync\DarkSync 2.0.py"</Arguments>
    </Exec>
  </Actions>
  <Settings>
    <AllowStartIfOnBatteries>false</AllowStartIfOnBatteries>
    <StopIfGoingOnBatteries>true</StopIfGoingOnBatteries>
    <WakeToRun>true</WakeToRun>
  </Settings>
</Task>
```

## Version History

### v2.6.5+ Improvements
- Added `exit_on_completion` flag for automated runs
- Enhanced Windows exclusions (pagefile.sys, hiberfil.sys)
- Improved completion status messages
- Automatic exit on both success and failure
- 2-second delay before exit to show status

## Support

For issues or questions:
1. Check logs in the `/logs` directory
2. Review job history in the application
3. Test jobs manually before scheduling
4. Ensure Python/dependencies are properly installed

---

**Note**: Always test your automated setup manually first to ensure proper configuration before relying on scheduled runs.
