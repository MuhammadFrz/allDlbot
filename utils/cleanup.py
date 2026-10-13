import os
import time
import shutil
import uuid
import asyncio
from pathlib import Path
from config import DOWNLOAD_DIR

def create_task_dir(task_id: str = None) -> Path:
    """Create a unique temporary folder for a download task."""
    if not task_id:
        task_id = uuid.uuid4().hex[:10]
    task_path = DOWNLOAD_DIR / task_id
    task_path.mkdir(parents=True, exist_ok=True)
    return task_path

def cleanup_dir(dir_path: Path):
    """Safely remove a directory and all its contents immediately."""
    if dir_path and dir_path.exists():
        try:
            shutil.rmtree(dir_path, ignore_errors=True)
        except Exception as e:
            print(f"[Cleanup Error] {e}")

def get_file_size_mb(file_path: Path) -> float:
    """Return file size in megabytes."""
    if file_path.exists():
        return file_path.stat().st_size / (1024 * 1024)
    return 0.0

def purge_all_downloads():
    """Completely wipe all items inside DOWNLOAD_DIR to ensure 0 MB disk usage on startup."""
    if DOWNLOAD_DIR.exists():
        for item in DOWNLOAD_DIR.iterdir():
            try:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
            except Exception as e:
                print(f"[Purge error] {e}")

def purge_old_files(max_age_seconds: int = 120):
    """Remove any temporary folders or files older than max_age_seconds."""
    now = time.time()
    if not DOWNLOAD_DIR.exists():
        return

    for item in DOWNLOAD_DIR.iterdir():
        try:
            mtime = item.stat().st_mtime
            if now - mtime > max_age_seconds:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
        except Exception:
            pass

async def periodic_cleaner_task():
    """Background garbage collector that purges stale files every 2 minutes."""
    while True:
        try:
            await asyncio.sleep(120) # Every 2 minutes
            purge_old_files(max_age_seconds=120)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[Periodic cleaner error] {e}")
