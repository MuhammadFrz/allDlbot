import re
import json
import asyncio
import requests
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional
import yt_dlp

from utils.cleanup import create_task_dir


class SxyprnDownloader:
    """
    Downloader for sxyprn.com video pages.

    Strategy (in order):
      1. Fetch the raw HTML and parse the embedded JavaScript for a direct
         MP4 URL (flashvars / playerConfig / plain mp4 href patterns).
      2. Fall back to yt-dlp's generic extractor.
    """

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://sxyprn.com/",
        "Accept-Language": "en-US,en;q=0.9",
    }

    # ------------------------------------------------------------------ #
    # Public entry-point
    # ------------------------------------------------------------------ #

    @staticmethod
    async def download(url: str, task_dir: Optional[Path] = None) -> Dict[str, Any]:
        if task_dir is None:
            task_dir = create_task_dir()

        # Engine 1: HTML scrape → direct MP4 download
        try:
            res = await asyncio.to_thread(SxyprnDownloader._scrape_and_download, url, task_dir)
            if res.get("success") and res.get("path"):
                return res
        except Exception as e:
            print(f"[sxyprn scrape error] {e}")

        # Engine 2: yt-dlp generic extractor
        try:
            res = await asyncio.to_thread(SxyprnDownloader._ytdlp_download, url, task_dir)
            if res.get("success") and res.get("path"):
                return res
        except Exception as e:
            print(f"[sxyprn yt-dlp error] {e}")

        return {
            "success": False,
            "error": (
                "Could not extract video from this sxyprn page. "
                "The post may be private, deleted, or the site layout has changed."
            ),
        }

    # ------------------------------------------------------------------ #
    # Engine 1: HTML scraping
    # ------------------------------------------------------------------ #

    @staticmethod
    def _scrape_and_download(url: str, task_dir: Path) -> Dict[str, Any]:
        try:
            resp = requests.get(url, headers=SxyprnDownloader.HEADERS, timeout=20)
        except Exception as e:
            return {"success": False, "error": f"HTTP request failed: {e}"}

        if resp.status_code != 200:
            return {"success": False, "error": f"HTTP {resp.status_code} from sxyprn"}

        html = resp.text
        video_url = SxyprnDownloader._extract_video_url(html)
        if not video_url:
            return {"success": False, "error": "No direct video URL found in page source."}

        # Extract title (best-effort)
        title = SxyprnDownloader._extract_title(html)

        # Download the MP4
        out_path = task_dir / "sxyprn_video.mp4"
        try:
            dl = requests.get(
                video_url,
                headers={**SxyprnDownloader.HEADERS, "Referer": url},
                stream=True,
                timeout=60,
            )
            dl.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in dl.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        except Exception as e:
            return {"success": False, "error": f"Video download failed: {e}"}

        if not out_path.exists() or out_path.stat().st_size < 10_000:
            return {"success": False, "error": "Downloaded file is empty or too small."}

        return {
            "success": True,
            "path": out_path,
            "title": title,
            "video_url": video_url,
        }

    @staticmethod
    def _extract_video_url(html: str) -> Optional[str]:
        """
        Try several regex patterns that cover common sxyprn player
        embedding styles (flashvars, playerConfig sources array, bare mp4 links).
        """
        # Pattern 1: flashvars video_url / video_url_hd
        for key in ("video_url_hd", "video_url"):
            m = re.search(
                rf'["\']?{key}["\']?\s*[=:]\s*["\']?(https?://[^"\'>\s]+\.mp4[^"\'>\s]*)',
                html,
                re.IGNORECASE,
            )
            if m:
                return m.group(1)

        # Pattern 2: JWPlayer / generic sources array  { "file": "..." }
        m = re.search(
            r'["\']?file["\']?\s*:\s*["\']?(https?://[^"\'>\s]+\.mp4[^"\'>\s]*)',
            html,
            re.IGNORECASE,
        )
        if m:
            return m.group(1)

        # Pattern 3: sources array in JSON  "src": "..."
        m = re.search(
            r'["\']?src["\']?\s*:\s*["\']?(https?://[^"\'>\s]+\.mp4[^"\'>\s]*)',
            html,
            re.IGNORECASE,
        )
        if m:
            return m.group(1)

        # Pattern 4: bare <source src="...mp4..."> or <video src="...">
        m = re.search(
            r'<(?:source|video)[^>]+src=["\']?(https?://[^"\'>\s]+\.mp4[^"\'>\s]*)',
            html,
            re.IGNORECASE,
        )
        if m:
            return m.group(1)

        # Pattern 5: any bare https://...mp4 URL in script blocks (last resort)
        candidates = re.findall(
            r'(https?://[^\s"\'<>]+\.mp4(?:\?[^\s"\'<>]*)?)',
            html,
            re.IGNORECASE,
        )
        if candidates:
            # Prefer CDN-looking URLs; filter out thumbnail-style ones
            for c in candidates:
                if "cdn" in c or "media" in c or "content" in c or "sxyprn" in c:
                    return c
            return candidates[0]

        return None

    @staticmethod
    def _extract_title(html: str) -> str:
        # Try <title> tag first
        m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
        if m:
            title = m.group(1).strip()
            # Strip common site suffixes
            for suffix in [" - SXYPRN", " | SXYPRN", " - sxyprn.com", " | sxyprn.com"]:
                title = title.replace(suffix, "").replace(suffix.lower(), "")
            return title.strip() or "SXYPRN Video"

        # Try og:title
        m = re.search(r'property="og:title"\s+content="([^"]+)"', html, re.IGNORECASE)
        if m:
            return m.group(1).strip()

        return "SXYPRN Video"

    # ------------------------------------------------------------------ #
    # Engine 2: yt-dlp generic
    # ------------------------------------------------------------------ #

    @staticmethod
    def _ytdlp_download(url: str, task_dir: Path) -> Dict[str, Any]:
        out_template = str(task_dir / "sxyprn_%(id)s.%(ext)s")
        ydl_opts = {
            "format": "bestvideo+bestaudio/best",
            "outtmpl": out_template,
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "cachedir": False,
            "no_cache_dir": True,
            "nopart": True,
            "http_headers": SxyprnDownloader.HEADERS,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get("title", "SXYPRN Video") if info else "SXYPRN Video"

                # Find the downloaded file
                for f in sorted(task_dir.iterdir()):
                    if f.is_file() and f.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov"):
                        return {"success": True, "path": f, "title": title}
        except Exception as e:
            return {"success": False, "error": str(e)}

        return {"success": False, "error": "yt-dlp could not extract this video."}

