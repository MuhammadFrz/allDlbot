import re
import time
import urllib.parse
import mimetypes
import asyncio
from pathlib import Path
from typing import Dict, Any, Optional, Callable
import requests
import yt_dlp

import config
from utils.cleanup import create_task_dir
from utils.converter import extract_thumbnail, get_video_duration

class DirectDownloader:
    """
    High-speed downloader for arbitrary direct HTTP / HTTPS file download links.
    Handles videos, music/audio, documents, archives (ZIP, RAR, 7Z, TAR, GZ), PDFs, APKs, ISOs, and images.
    Features:
    - Pre-flight size check against config.MAX_FILE_SIZE_MB
    - Automatic filename extraction from Content-Disposition (RFC 5987 / 6266) & URL path
    - Streamed chunked downloads with throttled progress reporting
    - Automatic media classification (video, audio, photo, document)
    - Video thumbnail extraction & streaming playback support
    - Audio tag extraction for Telegram player integration
    """

    VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".flv", ".ts", ".3gp", ".wmv"}
    AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".ogg", ".wav", ".opus", ".aac", ".wma"}
    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".svg"}

    @staticmethod
    def _extract_filename(url: str, headers: Dict[str, str]) -> str:
        content_disp = headers.get("content-disposition", "")
        content_type = headers.get("content-type", "").split(";")[0].strip().lower()
        
        filename = None
        # 1. Try RFC 5987 / 6266 filename*=UTF-8''...
        if "filename*=" in content_disp:
            m = re.search(r"filename\*\s*=\s*(?:UTF-8''|utf-8'')([^;]+)", content_disp, re.I)
            if m:
                filename = urllib.parse.unquote(m.group(1).strip("\"'"))
                
        # 2. Try standard filename="..."
        if not filename and "filename=" in content_disp:
            m = re.search(r'filename\s*=\s*"([^"]+)"', content_disp) or re.search(r'filename\s*=\s*([^; ]+)', content_disp)
            if m:
                filename = m.group(1).strip("\"'")

        # 3. Try URL path
        if not filename:
            parsed = urllib.parse.urlparse(url)
            path_name = Path(parsed.path).name
            if path_name and "." in path_name:
                filename = urllib.parse.unquote(path_name)

        # 4. Fallback extension from content-type
        if not filename:
            ext = mimetypes.guess_extension(content_type) or ""
            filename = f"downloaded_file{ext}"
        elif "." not in filename:
            ext = mimetypes.guess_extension(content_type) or ""
            filename = f"{filename}{ext}"

        # Sanitize filename
        filename = re.sub(r'[\\/*?:"<>|]', '_', filename).strip()
        return filename or "downloaded_file"

    @staticmethod
    def _determine_type(path: Path, content_type: str) -> str:
        ext = path.suffix.lower()
        if ext in DirectDownloader.VIDEO_EXTENSIONS or content_type.startswith("video/"):
            return "video"
        if ext in DirectDownloader.AUDIO_EXTENSIONS or content_type.startswith("audio/"):
            return "audio"
        if ext in DirectDownloader.IMAGE_EXTENSIONS or content_type.startswith("image/"):
            return "photo"
        return "document"

    @classmethod
    async def download(
        cls,
        url: str,
        task_dir: Optional[Path] = None,
        progress_callback: Optional[Callable[[int, Optional[int]], None]] = None
    ) -> Dict[str, Any]:
        if task_dir is None:
            task_dir = create_task_dir()
        task_dir.mkdir(parents=True, exist_ok=True)

        loop = asyncio.get_running_loop()

        def _sync_work():
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "*/*",
                "Connection": "keep-alive",
            }
            try:
                resp = requests.get(url, headers=headers, stream=True, timeout=20, allow_redirects=True)
            except Exception as e:
                return {"success": False, "error": f"Failed to connect to host: {str(e)}"}

            if resp.status_code not in [200, 206]:
                # If requests fails with 403, try generic yt-dlp as a fallback for media links
                return cls._fallback_ytdlp(url, task_dir)

            content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            content_disp = resp.headers.get("content-disposition", "")
            
            # Check if this is an HTML webpage rather than a file
            parsed_path = urllib.parse.urlparse(resp.url).path.lower()
            if content_type == "text/html" and "attachment" not in content_disp and not parsed_path.endswith((".html", ".htm")):
                return {
                    "success": False,
                    "error": "This link points to a web page (HTML), not a direct file. Please send a direct download link."
                }

            # Size verification
            content_len = resp.headers.get("content-length")
            total_size = int(content_len) if content_len and content_len.isdigit() else None
            max_bytes = config.MAX_FILE_SIZE_MB * 1024 * 1024
            if total_size and total_size > max_bytes:
                mb = total_size / (1024 * 1024)
                return {
                    "success": False,
                    "error": f"File is too large ({mb:.1f} MB). Telegram upload limit is {config.MAX_FILE_SIZE_MB} MB."
                }

            filename = cls._extract_filename(resp.url, resp.headers)
            file_path = task_dir / filename

            # Ensure unique filename in task directory
            counter = 1
            while file_path.exists():
                file_path = task_dir / f"{file_path.stem}_{counter}{file_path.suffix}"
                counter += 1

            downloaded_bytes = 0
            last_report = time.time()

            try:
                with open(file_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=131072): # 128 KB
                        if chunk:
                            f.write(chunk)
                            downloaded_bytes += len(chunk)
                            if downloaded_bytes > max_bytes:
                                file_path.unlink(missing_ok=True)
                                return {
                                    "success": False,
                                    "error": f"File exceeded maximum allowed size of {config.MAX_FILE_SIZE_MB} MB."
                                }
                            
                            now = time.time()
                            if progress_callback and (now - last_report >= 2.5 or (total_size and downloaded_bytes >= total_size)):
                                last_report = now
                                try:
                                    progress_callback(downloaded_bytes, total_size)
                                except Exception:
                                    pass

            except Exception as e:
                file_path.unlink(missing_ok=True)
                return {"success": False, "error": f"Download interrupted: {str(e)}"}

            if not file_path.exists() or file_path.stat().st_size == 0:
                return {"success": False, "error": "Downloaded file is empty or missing."}

            file_type = cls._determine_type(file_path, content_type)
            thumb_path = None
            duration = 0

            if file_type == "video":
                thumb_path = extract_thumbnail(file_path)
                duration = int(get_video_duration(file_path))
            elif file_type == "audio":
                try:
                    from mutagen.mp3 import MP3
                    audio = MP3(str(file_path))
                    duration = int(audio.info.length)
                except Exception:
                    pass

            return {
                "success": True,
                "path": file_path,
                "filename": file_path.name,
                "size_bytes": file_path.stat().st_size,
                "type": file_type,
                "thumb": thumb_path,
                "duration": duration,
            }

        return await loop.run_in_executor(None, _sync_work)

    @classmethod
    def _fallback_ytdlp(cls, url: str, task_dir: Path) -> Dict[str, Any]:
        """Fallback to yt-dlp generic extractor if HTTP request receives 403 or server anti-hotlinking."""
        out_template = str(task_dir / "direct_media_%(id)s.%(ext)s")
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
                title = info.get("title", "Direct Media")
                for f in sorted(task_dir.iterdir()):
                    if f.is_file() and f.stat().st_size > 0:
                        file_type = cls._determine_type(f, "")
                        thumb = extract_thumbnail(f) if file_type == "video" else None
                        return {
                            "success": True,
                            "path": f,
                            "filename": f.name,
                            "size_bytes": f.stat().st_size,
                            "type": file_type,
                            "thumb": thumb,
                            "duration": int(info.get("duration", 0)),
                        }
        except Exception as e:
            return {"success": False, "error": f"Direct download failed: {str(e)}"}
        return {"success": False, "error": "Could not download file from direct link."}
