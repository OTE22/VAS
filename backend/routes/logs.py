"""
System Logs Viewer Routes
==========================
Admin-only endpoints for viewing all system logs (DEBUG, INFO, WARNING, ERROR, CRITICAL).
All logic is in the backend - frontend only displays.
"""

import os
import sys
import re
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel
from pathlib import Path

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import settings
from backend.auth.auth_service import get_current_user, require_role
from db_models import User

logger = logging.getLogger(__name__)

router = APIRouter()


async def _read_lines_off_loop(path) -> list:
    """Read a (potentially large) log file in the default executor, off the event loop."""
    import asyncio

    def _read():
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.readlines()

    return await asyncio.get_running_loop().run_in_executor(None, _read)


class LogEntry(BaseModel):
    """Single log entry (all levels)"""
    timestamp: str
    level: str  # DEBUG, INFO, WARNING, ERROR, CRITICAL
    process_id: Optional[str] = None
    logger_name: Optional[str] = None
    message: str
    full_line: str
    line_number: Optional[int] = None


class LogsResponse(BaseModel):
    """Response with paginated logs"""
    logs: List[LogEntry]
    total_count: int
    page: int
    page_size: int
    total_pages: int
    has_next: bool
    has_previous: bool
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    level_filter: Optional[str] = None
    filtered_count: int


def parse_log_line(line: str, line_number: int) -> Optional[LogEntry]:
    """
    Parse a single log line - supports ALL log levels (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    Supports multiple log formats:
    - Gunicorn: [2026-01-03 18:03:08 +0000] [21] [ERROR] message
    - Python: 2026-01-03 18:03:08,123 | ERROR | 21 | module | message
    """
    if not line.strip():
        return None
    
    # Try Gunicorn format: [2026-01-03 18:03:08 +0000] [21] [LEVEL] message
    gunicorn_pattern = r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[^\]]+)\] \[(\d+)\] \[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\] (.+)'
    match = re.match(gunicorn_pattern, line)
    if match:
        timestamp = match.group(1)
        process_id = match.group(2)
        level = match.group(3)
        message = match.group(4).strip()
        return LogEntry(
            timestamp=timestamp,
            level=level,
            process_id=process_id,
            message=message,
            full_line=line,
            line_number=line_number
        )
    
    # Try Python logging format: 2026-01-03 18:03:08,123 | LEVEL | 21 | module | message
    python_pattern = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d+)?) \| (DEBUG|INFO|WARNING|ERROR|CRITICAL) \| (\d+) \| ([^|]+) \| (.+)'
    match = re.match(python_pattern, line)
    if match:
        timestamp = match.group(1)
        level = match.group(2)
        process_id = match.group(3)
        logger_name = match.group(4).strip()
        message = match.group(5).strip()
        return LogEntry(
            timestamp=timestamp,
            level=level,
            process_id=process_id,
            logger_name=logger_name,
            message=message,
            full_line=line,
            line_number=line_number
        )
    
    # Try simple format: timestamp LEVEL message
    simple_pattern = r'(\d{4}-\d{2}-\d{2}[^\s]+).*?(DEBUG|INFO|WARNING|ERROR|CRITICAL).*?:(.+)'
    match = re.search(simple_pattern, line, re.IGNORECASE)
    if match:
        timestamp = match.group(1)
        level = match.group(2).upper()
        message = match.group(3).strip()
        return LogEntry(
            timestamp=timestamp,
            level=level,
            message=message,
            full_line=line,
            line_number=line_number
        )
    
    # Fallback: Check if line contains any log level keywords
    level_keywords = {
        'DEBUG': ['DEBUG'],
        'INFO': ['INFO'],
        'WARNING': ['WARNING', 'WARN'],
        'ERROR': ['ERROR', 'Exception', 'Traceback'],
        'CRITICAL': ['CRITICAL', 'FATAL']
    }
    
    line_upper = line.upper()
    for level, keywords in level_keywords.items():
        if any(keyword in line_upper for keyword in keywords):
            # Try to extract timestamp from beginning of line
            timestamp_match = re.search(r'(\d{4}-\d{2}-\d{2}[^\s]+)', line)
            timestamp = timestamp_match.group(1) if timestamp_match else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            return LogEntry(
                timestamp=timestamp,
                level=level,
                message=line.strip(),
                full_line=line,
                line_number=line_number
            )
    
    return None


def parse_timestamp(timestamp_str: str) -> Optional[datetime]:
    """Parse various timestamp formats"""
    formats = [
        "%Y-%m-%d %H:%M:%S %z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S,%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
    ]
    
    for fmt in formats:
        try:
            return datetime.strptime(timestamp_str, fmt)
        except:
            continue
    
    return None


def filter_by_date(log_entry: LogEntry, date_from: Optional[str], date_to: Optional[str]) -> bool:
    """Check if log entry is within date range"""
    if not date_from and not date_to:
        return True
    
    parsed_time = parse_timestamp(log_entry.timestamp)
    if not parsed_time:
        return True  # Include if we can't parse timestamp
    
    if date_from:
        try:
            from_date = datetime.strptime(date_from, "%Y-%m-%d")
            if parsed_time.date() < from_date.date():
                return False
        except:
            pass
    
    if date_to:
        try:
            to_date = datetime.strptime(date_to, "%Y-%m-%d")
            if parsed_time.date() > to_date.date():
                return False
        except:
            pass
    
    return True


@router.get("/api/logs", response_model=LogsResponse, summary="Get System Logs", description="""
Get all system logs with pagination and filtering.

**Access:** Admin only

**Features:**
- Returns ALL log levels (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- Supports pagination
- Supports date range filtering
- Supports log level filtering
- All parsing and filtering done in backend
- Frontend only displays the results

**Query Parameters:**
- `page`: Page number (default: 1)
- `page_size`: Items per page (default: 50, max: 200)
- `date_from`: Filter from date (YYYY-MM-DD format)
- `date_to`: Filter to date (YYYY-MM-DD format)
- `level`: Filter by log level (DEBUG, INFO, WARNING, ERROR, CRITICAL, or "all" for all levels)
""")
async def get_logs(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=200, description="Items per page"),
    date_from: Optional[str] = Query(None, description="Filter from date (YYYY-MM-DD)"),
    date_to: Optional[str] = Query(None, description="Filter to date (YYYY-MM-DD)"),
    level: Optional[str] = Query(None, description="Filter by log level (DEBUG, INFO, WARNING, ERROR, CRITICAL, or 'all')"),
    current_user: User = Depends(require_role(["admin"]))
):
    """
    Get all system logs with pagination and filtering.
    All logic is in the backend - frontend just displays.
    """
    try:
        # Get log file path - try app.log first (has all logs), fallback to error.log
        log_dir = settings.LOG_DIR
        log_dir_path = Path(log_dir)
        
        # Ensure log directory exists
        if not log_dir_path.exists():
            logger.warning(f"Log directory not found: {log_dir_path}")
            return LogsResponse(
                logs=[],
                total_count=0,
                page=page,
                page_size=page_size,
                total_pages=0,
                has_next=False,
                has_previous=False,
                date_from=date_from,
                date_to=date_to,
                level_filter=level,
                filtered_count=0
            )
        
        app_log_path = log_dir_path / "app.log"
        error_log_path = log_dir_path / "error.log"
        access_log_path = log_dir_path / "access.log"
        
        # Try multiple log files in order of preference
        # 1. app.log (has all levels from Python logging)
        # 2. error.log (Gunicorn error logs)
        # 3. access.log (Gunicorn access logs - has INFO level)
        log_path = None
        if app_log_path.exists() and app_log_path.stat().st_size > 0:
            log_path = app_log_path
        elif error_log_path.exists() and error_log_path.stat().st_size > 0:
            log_path = error_log_path
        elif access_log_path.exists() and access_log_path.stat().st_size > 0:
            log_path = access_log_path
        
        if not log_path:
            logger.warning(f"No log files found or all log files are empty in: {log_dir_path}")
            return LogsResponse(
                logs=[],
                total_count=0,
                page=page,
                page_size=page_size,
                total_pages=0,
                has_next=False,
                has_previous=False,
                date_from=date_from,
                date_to=date_to,
                level_filter=level,
                filtered_count=0
            )
        
        logger.debug(f"Reading logs from: {log_path}")
        
        # Read log file
        try:
            lines = await _read_lines_off_loop(log_path)

            if not lines:
                logger.warning(f"Log file is empty: {log_path}")
                return LogsResponse(
                    logs=[],
                    total_count=0,
                    page=page,
                    page_size=page_size,
                    total_pages=0,
                    has_next=False,
                    has_previous=False,
                    date_from=date_from,
                    date_to=date_to,
                    level_filter=level,
                    filtered_count=0
                )
        except PermissionError as e:
            logger.error(f"Permission denied reading log file: {log_path} - {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Permission denied reading log file: {str(e)}"
            )
        except Exception as e:
            logger.error(f"Error reading log file {log_path}: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to read log file: {str(e)}"
            )
        
        # Parse all lines (all log levels)
        all_logs = []
        for line_num, line in enumerate(lines, 1):
            parsed = parse_log_line(line, line_num)
            if parsed:
                # Apply level filtering
                if level and level.upper() != 'ALL':
                    if parsed.level.upper() != level.upper():
                        continue
                
                # Apply date filtering
                if filter_by_date(parsed, date_from, date_to):
                    all_logs.append(parsed)
        
        # Reverse to show most recent first
        all_logs.reverse()
        
        total_count = len(all_logs)
        
        # Calculate pagination
        total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 0
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        
        # Get page of logs
        paginated_logs = all_logs[start_idx:end_idx]
        
        return LogsResponse(
            logs=paginated_logs,
            total_count=total_count,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            has_next=page < total_pages,
            has_previous=page > 1,
            date_from=date_from,
            date_to=date_to,
            level_filter=level,
            filtered_count=total_count
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting error logs: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get error logs: {str(e)}"
        )


@router.post("/api/logs/cleanup", summary="Manual Log Cleanup", description="""
Manually trigger log cleanup to remove old log entries.

**Access:** Admin only

**What it does:**
- Removes log entries older than LOGS_LIFE_TIME_HOURS (default: 48 hours)
- Processes app.log, error.log, and access.log files
- Returns statistics about deleted lines and freed space

**Note:** This runs automatically every 6 hours, but you can trigger it manually for testing.
""")
async def manual_log_cleanup(
    current_user: User = Depends(require_role(["admin"]))
):
    """Manually trigger log cleanup"""
    try:
        from backend.core.log_cleanup import log_cleanup_manager
        
        if not log_cleanup_manager:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Log cleanup manager not available"
            )
        
        deleted_lines, freed_space_mb = await log_cleanup_manager.cleanup_old_logs()
        
        return {
            "success": True,
            "message": "Log cleanup completed",
            "deleted_lines": deleted_lines,
            "freed_space_mb": round(freed_space_mb, 2),
            "retention_hours": log_cleanup_manager.retention_hours
        }
    except Exception as e:
        logger.error(f"Error during manual log cleanup: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to cleanup logs: {str(e)}"
        )


@router.get("/api/logs/stats", summary="Get Log Statistics", description="Get statistics about all logs (Admin only)")
async def get_log_stats(
    current_user: User = Depends(require_role(["admin"]))
):
    """Get log statistics for all log levels"""
    try:
        log_dir = settings.LOG_DIR
        log_dir_path = Path(log_dir)
        
        if not log_dir_path.exists():
            return {
                "total_debug": 0,
                "total_info": 0,
                "total_warning": 0,
                "total_errors": 0,
                "total_critical": 0,
                "file_size_mb": 0,
                "last_modified": None,
                "file_path": str(log_dir_path),
                "log_files": [],
                "logs_life_time_hours": settings.LOGS_LIFE_TIME_HOURS
            }
        
        app_log_path = log_dir_path / "app.log"
        error_log_path = log_dir_path / "error.log"
        access_log_path = log_dir_path / "access.log"
        
        # Check all log files
        log_files = []
        if app_log_path.exists():
            log_files.append(("app.log", app_log_path))
        if error_log_path.exists():
            log_files.append(("error.log", error_log_path))
        if access_log_path.exists():
            log_files.append(("access.log", access_log_path))
        
        if not log_files:
            return {
                "total_debug": 0,
                "total_info": 0,
                "total_warning": 0,
                "total_errors": 0,
                "total_critical": 0,
                "file_size_mb": 0,
                "last_modified": None,
                "file_path": str(log_dir_path),
                "log_files": [],
                "logs_life_time_hours": settings.LOGS_LIFE_TIME_HOURS
            }
        
        # Aggregate stats from all log files
        total_debug = 0
        total_info = 0
        total_warning = 0
        total_errors = 0
        total_critical = 0
        total_size_mb = 0.0
        last_modified = None
        log_file_info = []
        
        for log_name, log_file_path in log_files:
            try:
                file_stats = log_file_path.stat()
                file_size_mb = file_stats.st_size / (1024 * 1024)
                file_modified = datetime.fromtimestamp(file_stats.st_mtime).isoformat()
                total_size_mb += file_size_mb
                
                if not last_modified or file_modified > last_modified:
                    last_modified = file_modified
                
                log_file_info.append({
                    "name": log_name,
                    "size_mb": round(file_size_mb, 2),
                    "last_modified": file_modified
                })
                
                # Count log levels in this file
                try:
                    lines = await _read_lines_off_loop(log_file_path)

                    for line in lines:
                        line_upper = line.upper()
                        if '[DEBUG]' in line or '| DEBUG |' in line or ' DEBUG ' in line_upper:
                            total_debug += 1
                        if '[INFO]' in line or '| INFO |' in line or ' INFO ' in line_upper:
                            total_info += 1
                        if '[WARNING]' in line or '[WARN]' in line or '| WARNING |' in line or ' WARNING ' in line_upper:
                            total_warning += 1
                        if '[ERROR]' in line or '| ERROR |' in line or ' ERROR ' in line_upper:
                            total_errors += 1
                        if '[CRITICAL]' in line or '[FATAL]' in line or '| CRITICAL |' in line or ' CRITICAL ' in line_upper:
                            total_critical += 1
                except Exception as e:
                    logger.error(f"Error reading log file {log_file_path} for stats: {e}")
            except Exception as e:
                logger.error(f"Error getting stats for {log_file_path}: {e}")
        
        return {
            "total_debug": total_debug,
            "total_info": total_info,
            "total_warning": total_warning,
            "total_errors": total_errors,
            "total_critical": total_critical,
            "file_size_mb": round(total_size_mb, 2),
            "last_modified": last_modified,
            "file_path": str(log_dir_path),
            "log_files": log_file_info,
            "logs_life_time_hours": settings.LOGS_LIFE_TIME_HOURS
        }
    
    except Exception as e:
        logger.error(f"Error getting log stats: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get log statistics: {str(e)}"
        )

