import os
import re
import asyncio
from pathlib import Path
from typing import Dict, Any, Optional
import yt_dlp

from utils.cleanup import create_task_dir

class FacebookDownloader:
    """Downloader for Facebook videos, Reels, and Watch clips."""

    @staticmethod
    async def download(url: str, task_dir: Optional[Path] = None) -> Dict[str, Any]:
        if task_dir is None:
            task_dir = create_task_dir()

        loop = asyncio.get_running_loop()

        def _sync_download():
            out_template = str(task_dir / "fb_video_%(id)s.%(ext)s")
            ydl_opts = {
                "format": "bestvideo+bestaudio/best[ext=mp4]/best",
                "outtmpl": out_template,
                "cachedir": False,
                "no_cache_dir": True,
                "nopart": True,
                "merge_output_format": "mp4",
                "quiet": True,
                "no_warnings": True,
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    title = info.get("title", "Facebook Video")
                    uploader = info.get("uploader", "Facebook User")

                    for f in sorted(task_dir.iterdir()):
                        if f.is_file() and f.suffix.lower() in [".mp4", ".mkv", ".webm", ".mov"]:
                            return {
                                "success": True,
                                "path": f,
                                "title": title,
                                "author": uploader,
                            }
            except Exception as e:
                err_str = str(e)
                if "private" in err_str.lower() or "login" in err_str.lower():
                    return {"success": False, "error": "This Facebook video is private or requires login."}
                return {"success": False, "error": err_str}

            return {"success": False, "error": "Could not download Facebook video. Please verify the post is public."}

        return await loop.run_in_executor(None, _sync_download)
