import os
import re
import asyncio
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse
import instaloader
import yt_dlp

from utils.cleanup import create_task_dir

class InstagramDownloader:
    """
    Downloader for Instagram reels, posts, stories, and carousels (multiple media).
    Always downloads ALL items in a multi-media carousel.
    """

    @staticmethod
    async def download(url: str, task_dir: Optional[Path] = None) -> Dict[str, Any]:
        if task_dir is None:
            task_dir = create_task_dir()

        session_id = os.getenv("INSTAGRAM_SESSIONID")

        # Engine 1: Instaloader (Superb for carousels / sidecars)
        try:
            res = await InstagramDownloader._download_instaloader(url, task_dir, session_id)
            if res.get("success") and res.get("media_items"):
                return res
        except Exception as e:
            print(f"[Instaloader error] {e}")

        # Engine 2: gallery-dl
        try:
            res = await InstagramDownloader._download_gallery_dl(url, task_dir, session_id)
            if res.get("success") and res.get("media_items"):
                return res
        except Exception as e:
            print(f"[gallery-dl error] {e}")

        # Engine 3: yt-dlp fallback
        try:
            res = await InstagramDownloader._download_ytdlp(url, task_dir, session_id)
            if res.get("success") and res.get("media_items"):
                return res
        except Exception as e:
            print(f"[Instagram yt-dlp error] {e}")

        return {
            "success": False,
            "error": "Failed to download Instagram media. The post might be private or Instagram requires authentication."
        }

    @staticmethod
    def _extract_shortcode(url: str) -> Optional[str]:
        match = re.search(r'instagram\.com/(?:p|reel|reels|tv)/([^/?&#]+)', url)
        if match:
            return match.group(1)
        return None

    @staticmethod
    async def _download_instaloader(url: str, task_dir: Path, session_id: Optional[str]) -> Dict[str, Any]:
        shortcode = InstagramDownloader._extract_shortcode(url)
        if not shortcode:
            return {"success": False}

        loop = asyncio.get_running_loop()

        def _sync_work():
            L = instaloader.Instaloader(
                dirname_pattern=str(task_dir),
                filename_pattern="{date_utc}_UTC_{media_id}",
                download_pictures=True,
                download_videos=True,
                download_video_thumbnails=False,
                download_geotags=False,
                download_comments=False,
                save_metadata=False,
                compress_json=False,
                quiet=True,
            )

            if session_id:
                L.context._session.cookies.set("sessionid", session_id, domain=".instagram.com")

            post = instaloader.Post.from_shortcode(L.context, shortcode)
            caption = post.caption or ""
            owner = post.owner_username or ""

            # Download post (downloads all carousel elements automatically)
            L.download_post(post, target=str(task_dir))

            media_items = []
            for f in sorted(task_dir.iterdir()):
                if f.is_file():
                    ext = f.suffix.lower()
                    if ext in [".mp4", ".mkv"]:
                        media_items.append({"path": f, "type": "video"})
                    elif ext in [".jpg", ".jpeg", ".png", ".webp"]:
                        media_items.append({"path": f, "type": "photo"})

            if media_items:
                return {
                    "success": True,
                    "media_items": media_items,
                    "caption": caption,
                    "author": owner,
                }
            return {"success": False}

        return await loop.run_in_executor(None, _sync_work)

    @staticmethod
    async def _download_gallery_dl(url: str, task_dir: Path, session_id: Optional[str]) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()

        def _sync_work():
            cmd = [
                "gallery-dl",
                "--directory", str(task_dir),
                "--no-mtime",
                "--quiet",
            ]
            if session_id:
                cmd.extend(["--header", f"Cookie: sessionid={session_id}"])
            cmd.append(url)

            subprocess.run(cmd, capture_output=True, text=True)

            media_items = []
            for root, _, files in os.walk(task_dir):
                for file_name in sorted(files):
                    f = Path(root) / file_name
                    ext = f.suffix.lower()
                    if ext in [".mp4", ".mkv", ".webm"]:
                        media_items.append({"path": f, "type": "video"})
                    elif ext in [".jpg", ".jpeg", ".png", ".webp"]:
                        media_items.append({"path": f, "type": "photo"})

            if media_items:
                return {
                    "success": True,
                    "media_items": media_items,
                    "caption": "",
                    "author": "Instagram User",
                }
            return {"success": False}

        return await loop.run_in_executor(None, _sync_work)

    @staticmethod
    async def _download_ytdlp(url: str, task_dir: Path, session_id: Optional[str]) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()

        def _sync_work():
            out_template = str(task_dir / "%(id)s_%(autonumber)s.%(ext)s")
            ydl_opts = {
                "format": "best",
                "outtmpl": out_template,
                "cachedir": False,
                "no_cache_dir": True,
                "nopart": True,
                "yes_playlist": True,
                "quiet": True,
                "no_warnings": True,
            }
            if session_id:
                ydl_opts["http_headers"] = {"Cookie": f"sessionid={session_id}"}

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get("title", "")
                uploader = info.get("uploader", "Instagram")

                media_items = []
                for f in sorted(task_dir.iterdir()):
                    if f.is_file():
                        ext = f.suffix.lower()
                        if ext in [".mp4", ".mkv"]:
                            media_items.append({"path": f, "type": "video"})
                        elif ext in [".jpg", ".jpeg", ".png", ".webp"]:
                            media_items.append({"path": f, "type": "photo"})

                if media_items:
                    return {
                        "success": True,
                        "media_items": media_items,
                        "caption": title,
                        "author": uploader,
                    }
                return {"success": False}

        return await loop.run_in_executor(None, _sync_work)
