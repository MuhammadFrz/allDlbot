import os
import requests
import asyncio
from pathlib import Path
from typing import Dict, Any, List, Optional
import yt_dlp

from utils.cleanup import create_task_dir

class TikTokDownloader:
    """Downloader for TikTok videos (no watermark), audio, and photo slideshows."""

    @staticmethod
    async def download(url: str, task_dir: Optional[Path] = None) -> Dict[str, Any]:
        if task_dir is None:
            task_dir = create_task_dir()

        # Engine 1: TikWM API via requests
        try:
            res = await asyncio.to_thread(TikTokDownloader._sync_download_tikwm, url, task_dir)
            if res.get("success"):
                return res
            elif res.get("error"):
                return res
        except Exception as e:
            print(f"[TikWM error] {e}")

        # Engine 2: yt-dlp fallback
        try:
            res = await TikTokDownloader._download_ytdlp(url, task_dir)
            if res.get("success"):
                return res
            elif res.get("error"):
                return res
        except Exception as e:
            print(f"[TikTok yt-dlp error] {e}")

        return {"success": False, "error": "This TikTok video is unavailable, private, or has been deleted."}

    @staticmethod
    def _sync_download_tikwm(url: str, task_dir: Path) -> Dict[str, Any]:
        api_url = "https://www.tikwm.com/api/"
        headers = {
            "User-Agent": "TelegramBot (like TwitterBot)",
        }
        try:
            resp = requests.get(api_url, params={"url": url}, headers=headers, timeout=15)
            if resp.status_code != 200:
                return {"success": False}
            data = resp.json()
        except Exception as e:
            return {"success": False}

        if data.get("code") != 0 or not data.get("data"):
            msg = data.get("msg", "")
            if "failed" in msg.lower() or "not found" in msg.lower() or "check url" in msg.lower():
                return {"success": False, "error": "This TikTok video was not found, is private, or has been deleted."}
            return {"success": False}

        d = data["data"]
        title = d.get("title", "TikTok Media")
        author = d.get("author", {}).get("nickname", "TikTok User")
        images = d.get("images", [])

        # Check if it is a photo slideshow
        if images and isinstance(images, list) and len(images) > 0:
            downloaded_photos = []
            for idx, img_url in enumerate(images):
                img_path = task_dir / f"slide_{idx+1:02d}.jpg"
                img_resp = requests.get(img_url, headers=headers, timeout=15)
                if img_resp.status_code == 200:
                    img_path.write_bytes(img_resp.content)
                    downloaded_photos.append(img_path)

            music_file = None
            music_url = d.get("music")
            if music_url:
                m_path = task_dir / "music.mp3"
                m_resp = requests.get(music_url, headers=headers, timeout=15)
                if m_resp.status_code == 200:
                    m_path.write_bytes(m_resp.content)
                    music_file = m_path

            return {
                "success": True,
                "type": "photos",
                "media_files": downloaded_photos,
                "audio_file": music_file,
                "title": title,
                "author": author,
            }

        # Otherwise it is a video (watermark-free HD)
        video_url = d.get("hdplay") or d.get("play")
        if video_url:
            vid_path = task_dir / "tiktok_video.mp4"
            vid_resp = requests.get(video_url, headers=headers, timeout=25)
            if vid_resp.status_code == 200:
                vid_path.write_bytes(vid_resp.content)

            music_file = None
            music_url = d.get("music")
            if music_url:
                m_path = task_dir / "music.mp3"
                m_resp = requests.get(music_url, headers=headers, timeout=15)
                if m_resp.status_code == 200:
                    m_path.write_bytes(m_resp.content)
                    music_file = m_path

            return {
                "success": True,
                "type": "video",
                "media_files": [vid_path],
                "audio_file": music_file,
                "title": title,
                "author": author,
            }

        return {"success": False}

    @staticmethod
    async def _download_ytdlp(url: str, task_dir: Path) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        def _sync_ytdlp():
            out_template = str(task_dir / "tiktok_video.%(ext)s")
            ydl_opts = {
                "format": "b",
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
                    title = info.get("title", "TikTok Video")
                    uploader = info.get("uploader", "TikTok")
                    
                    for f in task_dir.iterdir():
                        if f.suffix.lower() in [".mp4", ".mkv", ".webm"]:
                            return {
                                "success": True,
                                "type": "video",
                                "media_files": [f],
                                "audio_file": None,
                                "title": title,
                                "author": uploader,
                            }
            except Exception as e:
                err_str = str(e)
                if "blocked" in err_str.lower() or "unexpected" in err_str.lower():
                    return {"success": False, "error": "This TikTok video is unavailable, deleted, or blocked."}
                return {"success": False, "error": err_str}

            return {"success": False}

        return await loop.run_in_executor(None, _sync_ytdlp)
