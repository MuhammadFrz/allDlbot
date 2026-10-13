import os
import asyncio
import subprocess
import requests
from pathlib import Path
from typing import Dict, Any, List, Optional
import yt_dlp
from mutagen.id3 import ID3, APIC

from utils.cleanup import create_task_dir

class SpotifyDownloader:
    """Downloader for Spotify tracks, albums, and playlists with full ID3 metadata & album art."""

    @staticmethod
    async def download(url: str, task_dir: Optional[Path] = None) -> Dict[str, Any]:
        if task_dir is None:
            task_dir = create_task_dir()

        # Engine 1: Spotify oEmbed metadata + YouTube Music high-fidelity audio extraction
        try:
            res = await asyncio.to_thread(SpotifyDownloader._sync_download_track, url, task_dir)
            if res.get("success") and res.get("tracks"):
                return res
        except Exception as e:
            print(f"[Spotify oEmbed extractor error] {e}")

        # Engine 2: SpotDL fallback (with strict timeout)
        try:
            res = await SpotifyDownloader._download_spotdl(url, task_dir)
            if res.get("success") and res.get("tracks"):
                return res
        except Exception as e:
            print(f"[SpotDL error] {e}")

        return {"success": False, "error": "Failed to download Spotify audio."}

    @staticmethod
    def _sync_download_track(url: str, task_dir: Path) -> Dict[str, Any]:
        # Strip tracking parameters (e.g. ?si=...&utm_source=...)
        clean_url = url.split("?")[0].strip()

        headers = {
            "User-Agent": "TelegramBot (like TwitterBot)",
        }
        oembed_url = f"https://open.spotify.com/oembed?url={clean_url}"
        
        try:
            r = requests.get(oembed_url, headers=headers, timeout=10)
            if r.status_code != 200:
                return {"success": False}
            data = r.json()
        except Exception as e:
            print(f"[Spotify oembed fetch error] {e}")
            return {"success": False}

        title = data.get("title", "")
        thumb_url = data.get("thumbnail_url")
        if not title:
            return {"success": False}

        # Download original Spotify album artwork
        thumb_path = task_dir / "cover.jpg"
        if thumb_url:
            try:
                tr = requests.get(thumb_url, headers=headers, timeout=10)
                if tr.status_code == 200:
                    thumb_path.write_bytes(tr.content)
            except Exception:
                pass

        # Search YouTube / YouTube Music and download highest quality MP3
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
                    "key": "FFmpegMetadata",
                },
            ],
            "quiet": True,
            "no_warnings": True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(f"ytsearch1:{title}", download=True)

            mp3_files = list(task_dir.glob("*.mp3"))
            if not mp3_files:
                return {"success": False}

            mp3_file = mp3_files[0]

            # Embed Spotify album art into MP3 ID3 tags
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
                except Exception as e:
                    print(f"[ID3 cover embed note] {e}")

            return {
                "success": True,
                "tracks": [{
                    "path": mp3_file,
                    "title": title,
                    "thumb": thumb_path if thumb_path.exists() else None,
                }],
            }
        except Exception as e:
            print(f"[Spotify yt-dlp search error] {e}")
            return {"success": False}

    @staticmethod
    async def _download_spotdl(url: str, task_dir: Path) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()

        def _sync_spotdl():
            clean_url = url.split("?")[0].strip()
            cmd = [
                "spotdl",
                "download",
                clean_url,
                "--output", str(task_dir / "{artist} - {title}.{output-ext}"),
                "--format", "mp3",
                "--bitrate", "320k",
            ]
            try:
                # Add strict 30 second timeout to prevent hanging
                process = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                mp3_files = list(task_dir.glob("*.mp3"))
                if mp3_files:
                    return {
                        "success": True,
                        "tracks": [{"path": f, "title": f.stem, "thumb": None} for f in mp3_files],
                    }
            except subprocess.TimeoutExpired:
                print("[SpotDL timed out after 30s]")
            except Exception as e:
                print(f"[SpotDL subprocess error] {e}")

            return {"success": False}

        return await loop.run_in_executor(None, _sync_spotdl)
