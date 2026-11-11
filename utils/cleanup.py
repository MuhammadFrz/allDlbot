import os
import time
import shutil
import uuid
import asyncio
import threading
from pathlib import Path
from typing import Optional, Set
from config import DOWNLOAD_DIR

_ACTIVE_LOCK = threading.Lock()
_ACTIVE_TASK_DIRS: Set[Path] = set()


def create_task_dir(task_id: str = None) -> Path:
    """Create a unique temporary folder for a download task and register it as active."""
    if not task_id:
        task_id = uuid.uuid4().hex[:10]
    task_path = DOWNLOAD_DIR / task_id
    task_path.mkdir(parents=True, exist_ok=True)
    with _ACTIVE_LOCK:
        _ACTIVE_TASK_DIRS.add(task_path.resolve())
    return task_path


def cleanup_dir(dir_path: Path):
    """Safely remove a directory, deregister it from active tasks, and purge contents immediately."""
    if dir_path:
        try:
            with _ACTIVE_LOCK:
                _ACTIVE_TASK_DIRS.discard(Path(dir_path).resolve())
        except Exception:
            pass
        if dir_path.exists():
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
    with _ACTIVE_LOCK:
        _ACTIVE_TASK_DIRS.clear()
    if DOWNLOAD_DIR.exists():
        for item in DOWNLOAD_DIR.iterdir():
            try:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
            except Exception as e:
                print(f"[Purge error] {e}")


def purge_old_files(max_age_seconds: int = 1800, active_dirs: Optional[set] = None):
    """Remove abandoned folders/files older than max_age_seconds (default 30 mins), preserving all active task dirs."""
    now = time.time()
    if not DOWNLOAD_DIR.exists():
        return

    with _ACTIVE_LOCK:
        active_paths = set(_ACTIVE_TASK_DIRS)
    for p in (active_dirs or set()):
        if p:
            try:
                active_paths.add(Path(p).resolve())
            except Exception:
                pass

    for item in DOWNLOAD_DIR.iterdir():
        try:
            if item.resolve() in active_paths:
                continue
            mtime = item.stat().st_mtime
            if now - mtime > max_age_seconds:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
        except Exception:
            pass


async def periodic_cleaner_task(get_active_dirs=None):
    """Background garbage collector that purges stale orphaned files every 10 minutes."""
    while True:
        try:
            await asyncio.sleep(600) # Every 10 minutes
            active_dirs = get_active_dirs() if get_active_dirs else None
            purge_old_files(max_age_seconds=1800, active_dirs=active_dirs)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[Periodic cleaner error] {e}")
