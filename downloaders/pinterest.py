import os
import re
import asyncio
import requests
from pathlib import Path
from typing import Dict, Any, Optional
import yt_dlp

from utils.cleanup import create_task_dir

class PinterestDownloader:
    """Downloader for Pinterest videos, GIFs, and original full-resolution images."""

    @staticmethod
    async def download(url: str, task_dir: Optional[Path] = None) -> Dict[str, Any]:
        if task_dir is None:
            task_dir = create_task_dir()

        # Engine 1: Try yt-dlp (Best for video pins)
        try:
            res = await PinterestDownloader._download_ytdlp(url, task_dir)
            if res.get("success"):
                return res
        except Exception as e:
            print(f"[Pinterest yt-dlp error] {e}")

        # Engine 2: HTML Scraper (Best for high-res original images & direct videos)
        try:
            res = await asyncio.to_thread(PinterestDownloader._sync_scrape_pin, url, task_dir)
            if res.get("success"):
                return res
            elif res.get("error"):
                return res
        except Exception as e:
            print(f"[Pinterest scraper error] {e}")

        return {"success": False, "error": "Could not download this Pinterest pin. Please verify the link is public."}

    @staticmethod
    async def _download_ytdlp(url: str, task_dir: Path) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()

        def _sync_ytdlp():
            out_template = str(task_dir / "pinterest_%(id)s.%(ext)s")
            ydl_opts = {
                "format": "best",
                "outtmpl": out_template,
                "cachedir": False,
                "no_cache_dir": True,
                "nopart": True,
                "quiet": True,
                "no_warnings": True,
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    title = info.get("title", "Pinterest Media")

                    for f in sorted(task_dir.iterdir()):
                        if f.is_file():
                            ext = f.suffix.lower()
                            if ext in [".mp4", ".mkv", ".webm", ".mov"]:
                                return {
                                    "success": True,
                                    "type": "video",
                                    "path": f,
                                    "title": title,
                                }
                            elif ext in [".jpg", ".jpeg", ".png", ".webp"]:
                                return {
                                    "success": True,
                                    "type": "image",
                                    "path": f,
                                    "title": title,
                                }
            except Exception:
                pass
            return {"success": False}

        return await loop.run_in_executor(None, _sync_ytdlp)

    @staticmethod
    def _sync_scrape_pin(url: str, task_dir: Path) -> Dict[str, Any]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        }

        # Resolve shortlinks (e.g. pin.it)
        try:
            resp = requests.get(url, headers=headers, timeout=12, allow_redirects=True)
            if resp.status_code != 200:
                return {"success": False, "error": "Pinterest page returned an error."}
            html = resp.text
        except Exception as e:
            return {"success": False, "error": f"Failed to connect to Pinterest: {str(e)}"}

        # Extract title
        title_match = re.search(r'<meta property="og:title"\s+content="([^"]+)"', html)
        title = title_match.group(1) if title_match else "Pinterest Pin"
        if title == "Pinterest" or not title:
            desc_match = re.search(r'<meta property="og:description"\s+content="([^"]+)"', html)
            title = desc_match.group(1) if desc_match else "Pinterest Pin"

        # Check for direct video in meta tags or script JSON
        video_match = re.search(r'property="og:video(?::secure_url)?"\s+content="([^"]+)"', html)
        if not video_match:
            video_match = re.search(r'"contentUrl":\s*"([^"]+\.mp4[^"]*)"', html)
        if not video_match:
            video_match = re.search(r'https://v\.pinimg\.com/videos/[^\s"\'<>]+', html)

        if video_match:
            vid_url = video_match.group(1) if hasattr(video_match, 'group') and video_match.groups() else video_match.group(0)
            vid_url = vid_url.replace(r"\u0026", "&").replace("&amp;", "&")
            vid_path = task_dir / "pinterest_video.mp4"
            vr = requests.get(vid_url, headers=headers, timeout=25)
            if vr.status_code == 200:
                vid_path.write_bytes(vr.content)
                return {
                    "success": True,
                    "type": "video",
                    "path": vid_path,
                    "title": title,
                }

        # If not a video, extract maximum original resolution image
        img_match = re.search(r'https://i\.pinimg\.com/(?:[0-9]+x|originals)/([a-zA-Z0-9/_\-\.]+)', html)
        if img_match:
            path_part = img_match.group(1)
            # Try original full resolution first
            orig_url = f"https://i.pinimg.com/originals/{path_part}"
            ir = requests.get(orig_url, headers=headers, timeout=15)
            if ir.status_code != 200:
                # Fallback to 736x high resolution
                fallback_url = f"https://i.pinimg.com/736x/{path_part}"
                ir = requests.get(fallback_url, headers=headers, timeout=15)

            if ir.status_code == 200:
                ext = ".jpg"
                if ".png" in path_part.lower():
                    ext = ".png"
                elif ".webp" in path_part.lower():
                    ext = ".webp"
                
                img_path = task_dir / f"pinterest_image{ext}"
                img_path.write_bytes(ir.content)
                return {
                    "success": True,
                    "type": "image",
                    "path": img_path,
                    "title": title,
                }

        return {"success": False, "error": "No downloadable video or image found in this pin."}
