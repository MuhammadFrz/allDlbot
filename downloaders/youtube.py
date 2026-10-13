import os
import asyncio
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
import yt_dlp

from utils.cleanup import create_task_dir
from utils.converter import extract_thumbnail

class YouTubeDownloader:
    """Downloader and quality extractor for YouTube videos and shorts."""

    @staticmethod
    def get_info(url: str) -> Optional[Dict[str, Any]]:
        """Fetch metadata without downloading video."""
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "cachedir": False,
            "no_cache_dir": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return None
                
                # Check available heights
                formats = info.get("formats", [])
                available_heights = set()
                for f in formats:
                    h = f.get("height")
                    if h and f.get("vcodec") != "none":
                        available_heights.add(h)

                return {
                    "id": info.get("id"),
                    "title": info.get("title", "YouTube Media"),
                    "duration": info.get("duration", 0),
                    "uploader": info.get("uploader", "Unknown"),
                    "thumbnail": info.get("thumbnail"),
                    "view_count": info.get("view_count", 0),
                    "available_heights": sorted(list(available_heights), reverse=True),
                    "url": url,
                }
            except Exception as e:
                print(f"[YouTube info error] {e}")
                return None

    @staticmethod
    def download(url: str, quality: str = "720", task_dir: Optional[Path] = None) -> Tuple[Optional[Path], Optional[Path], Dict[str, Any]]:
        """
        Download YouTube video with selected quality or MP3.
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
            }
        else:
            try:
                height = int(quality)
                # Select best video up to requested height + best audio
                fmt = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
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
            }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=True)
                # Find downloaded media file
                media_file = None
                thumb_file = None

                for file in task_dir.iterdir():
                    if file.is_file():
                        if file.suffix.lower() in [".mp4", ".mp3", ".mkv", ".webm", ".m4a"]:
                            media_file = file
                        elif file.suffix.lower() in [".jpg", ".jpeg", ".webp", ".png"]:
                            thumb_file = file

                # If no thumbnail downloaded, extract one from video
                if media_file and not is_audio and not thumb_file:
                    thumb_file = extract_thumbnail(media_file)

                meta = {
                    "title": info.get("title", "YouTube Media"),
                    "duration": info.get("duration", 0),
                    "uploader": info.get("uploader", ""),
                    "is_audio": is_audio,
                    "width": info.get("width", 0),
                    "height": info.get("height", 0),
                }
                return media_file, thumb_file, meta

            except Exception as e:
                print(f"[YouTube download error] {e}")
                return None, None, {"error": str(e)}
