import os
import asyncio
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List
import yt_dlp

from utils.cleanup import create_task_dir
from utils.converter import extract_thumbnail, get_video_params

class PornhubDownloader:
    """Downloader and quality extractor for Pornhub videos."""

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.pornhub.com/",
    }

    @staticmethod
    def get_info(url: str) -> Optional[Dict[str, Any]]:
        """Fetch metadata without downloading video."""
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "cachedir": False,
            "no_cache_dir": True,
            "http_headers": PornhubDownloader.HEADERS,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return None

                # Extract available video resolutions
                formats = info.get("formats", [])
                available_heights = set()
                for f in formats:
                    h = f.get("height")
                    if h and f.get("vcodec") != "none":
                        available_heights.add(int(h))

                return {
                    "id": info.get("id"),
                    "title": info.get("title", "Pornhub Video"),
                    "duration": int(info.get("duration", 0) or 0),
                    "uploader": info.get("uploader") or info.get("creator") or "Pornhub",
                    "thumbnail": info.get("thumbnail"),
                    "view_count": info.get("view_count", 0),
                    "available_heights": sorted(list(available_heights), reverse=True),
                    "url": url,
                }
            except Exception as e:
                print(f"[Pornhub info error] {e}")
                return None

    @staticmethod
    def download(
        url: str,
        quality: str = "720",
        task_dir: Optional[Path] = None,
    ) -> Tuple[Optional[Path], Optional[Path], Dict[str, Any]]:
        """
        Download Pornhub video with selected quality or MP3.
        Returns (media_path, thumbnail_path, metadata_dict).
        """
        if task_dir is None:
            task_dir = create_task_dir()

        out_template = str(task_dir / "%(title).100s.%(ext)s")
        is_audio = (quality == "mp3")

        if is_audio:
            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": out_template,
                "cachedir": False,
                "no_cache_dir": True,
                "nopart": True,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "320",
                    },
                    {
                        "key": "EmbedThumbnail",
                    },
                    {
                        "key": "FFmpegMetadata",
                    },
                ],
                "writethumbnail": True,
                "quiet": True,
                "no_warnings": True,
                "http_headers": PornhubDownloader.HEADERS,
            }
        else:
            if quality == "best":
                fmt = "bestvideo+bestaudio/best"
            else:
                try:
                    height = int(quality)
                    fmt = f"bestvideo[height={height}]+bestaudio/bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
                except (ValueError, TypeError):
                    fmt = "bestvideo[height<=720]+bestaudio/best[height<=720]/best"

            ydl_opts = {
                "format": fmt,
                "outtmpl": out_template,
                "cachedir": False,
                "no_cache_dir": True,
                "nopart": True,
                "merge_output_format": "mp4",
                "writethumbnail": True,
                "quiet": True,
                "no_warnings": True,
                "http_headers": PornhubDownloader.HEADERS,
            }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=True)
                media_file = None
                thumb_file = None

                for file in task_dir.iterdir():
                    if file.is_file():
                        ext = file.suffix.lower()
                        if ext in [".mp4", ".mp3", ".mkv", ".webm", ".m4a"]:
                            media_file = file
                        elif ext in [".jpg", ".jpeg", ".webp", ".png"]:
                            thumb_file = file

                # If no thumbnail downloaded, extract one from video
                if media_file and not is_audio and not thumb_file:
                    thumb_file = extract_thumbnail(media_file)

                dur = int(info.get("duration", 0) or 0)
                w = info.get("width")
                h = info.get("height")

                if media_file and not is_audio and (not dur or not w or not h):
                    p_dur, p_w, p_h = get_video_params(media_file)
                    dur = dur or p_dur
                    w = w or p_w
                    h = h or p_h

                meta = {
                    "title": info.get("title", "Pornhub Video"),
                    "duration": dur,
                    "uploader": info.get("uploader") or info.get("creator") or "Pornhub",
                    "is_audio": is_audio,
                    "width": w or 1280,
                    "height": h or 720,
                }
                return media_file, thumb_file, meta

            except Exception as e:
                print(f"[Pornhub download error] {e}")
                return None, None, {"error": str(e)}
