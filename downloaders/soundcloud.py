import os
import re
import asyncio
import requests
from pathlib import Path
from typing import Dict, Any, List, Optional
import yt_dlp
from mutagen.id3 import ID3, APIC

from utils.cleanup import create_task_dir

class SoundCloudDownloader:
    """Downloader for SoundCloud tracks and playlists with embedded high-res cover art & metadata."""

    @staticmethod
    async def download(url: str, task_dir: Optional[Path] = None) -> Dict[str, Any]:
        if task_dir is None:
            task_dir = create_task_dir()

        # Engine 1: Direct yt-dlp extractor
        try:
            res = await SoundCloudDownloader._download_ytdlp(url, task_dir)
            if res.get("success") and res.get("tracks"):
                return res
        except Exception as e:
            print(f"[SoundCloud yt-dlp error] {e}")

        # Engine 2: SoundCloud oEmbed + YouTube Music 320kbps engine (fallback)
        try:
            res = await asyncio.to_thread(SoundCloudDownloader._sync_oembed_fallback, url, task_dir)
            if res.get("success") and res.get("tracks"):
                return res
        except Exception as e:
            print(f"[SoundCloud oEmbed fallback error] {e}")

        return {"success": False, "error": "Failed to download SoundCloud audio. The track may be private or restricted."}

    @staticmethod
    async def _download_ytdlp(url: str, task_dir: Path) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()

        def _sync_ytdlp():
            out_template = str(task_dir / "%(title).100s.%(ext)s")
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
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    title = info.get("title", "SoundCloud Track")
                    uploader = info.get("uploader", "SoundCloud Artist")

                    mp3_files = list(task_dir.glob("*.mp3"))
                    if mp3_files:
                        thumb_files = list(task_dir.glob("*.jpg")) + list(task_dir.glob("*.png"))
                        thumb_path = thumb_files[0] if thumb_files else None
                        return {
                            "success": True,
                            "tracks": [{
                                "path": mp3_files[0],
                                "title": title,
                                "author": uploader,
                                "thumb": thumb_path,
                            }],
                        }
            except Exception:
                pass
            return {"success": False}

        return await loop.run_in_executor(None, _sync_ytdlp)

    @staticmethod
    def _sync_oembed_fallback(url: str, task_dir: Path) -> Dict[str, Any]:
        clean_url = url.split("?")[0].strip()
        oembed_url = f"https://soundcloud.com/oembed?format=json&url={clean_url}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

        try:
            r = requests.get(oembed_url, headers=headers, timeout=10)
            if r.status_code != 200:
                return {"success": False}
            data = r.json()
        except Exception as e:
            return {"success": False, "error": str(e)}

        raw_title = data.get("title", "")
        author = data.get("author_name", "SoundCloud Artist")
        thumb_url = data.get("thumbnail_url")

        if not raw_title:
            return {"success": False}

        # Clean title if formatted as "Title by Author"
        clean_title = raw_title
        if " by " in raw_title:
            clean_title = raw_title.split(" by ")[0].strip()

        # Download original cover artwork
        thumb_path = task_dir / "cover.jpg"
        if thumb_url:
            # Upgrade thumbnail to 500x500
            hi_res_thumb = thumb_url.replace("-large.", "-t500x500.").replace("-t300x300.", "-t500x500.")
            try:
                tr = requests.get(hi_res_thumb, headers=headers, timeout=10)
                if tr.status_code == 200:
                    thumb_path.write_bytes(tr.content)
            except Exception:
                pass

        # Search YouTube Music / YouTube and download 320kbps MP3
        search_query = f"{author} - {clean_title}"
        out_template = str(task_dir / "%(title).100s.%(ext)s")
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
                {"key": "FFmpegMetadata"},
            ],
            "quiet": True,
            "no_warnings": True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(f"ytsearch1:{search_query}", download=True)

            mp3_files = list(task_dir.glob("*.mp3"))
            if not mp3_files:
                return {"success": False}

            mp3_file = mp3_files[0]

            # Embed cover art
            if thumb_path.exists():
                try:
                    audio = ID3(str(mp3_file))
                    audio.add(APIC(
                        encoding=3,
                        mime="image/jpeg",
                        type=3,
                        desc="Cover",
                        data=thumb_path.read_bytes(),
                    ))
                    audio.save()
                except Exception:
                    pass

            return {
                "success": True,
                "tracks": [{
                    "path": mp3_file,
                    "title": clean_title,
                    "author": author,
                    "thumb": thumb_path if thumb_path.exists() else None,
                }],
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
