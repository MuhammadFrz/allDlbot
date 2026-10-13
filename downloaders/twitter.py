import re
import asyncio
import requests
from pathlib import Path
from typing import Dict, Any, List, Optional
import yt_dlp

from utils.cleanup import create_task_dir

class TwitterDownloader:
    """Downloader for Twitter/X videos, GIFs, and single/multi-images."""

    @staticmethod
    async def download(url: str, task_dir: Optional[Path] = None) -> Dict[str, Any]:
        if task_dir is None:
            task_dir = create_task_dir()

        # Engine 1: VxTwitter / FxTwitter API via requests with Telegram UA
        try:
            res = await asyncio.to_thread(TwitterDownloader._sync_download_api, url, task_dir)
            if res.get("success"):
                return res
            elif res.get("error"):
                return res
        except Exception as e:
            print(f"[Twitter API error] {e}")

        # Engine 2: yt-dlp fallback
        try:
            res = await TwitterDownloader._download_ytdlp(url, task_dir)
            if res.get("success"):
                return res
            elif res.get("error"):
                return res
        except Exception as e:
            print(f"[Twitter yt-dlp error] {e}")

        return {"success": False, "error": "Failed to download Twitter/X media. The tweet may be deleted or private."}

    @staticmethod
    def _sync_download_api(url: str, task_dir: Path) -> Dict[str, Any]:
        match = re.search(r'(?:twitter\.com|x\.com)/([^/]+)/status/(\d+)', url)
        if not match:
            return {"success": False, "error": "Invalid Twitter / X link format."}

        user, tweet_id = match.group(1), match.group(2)
        api_endpoints = [
            f"https://api.vxtwitter.com/{user}/status/{tweet_id}",
            f"https://api.fxtwitter.com/{user}/status/{tweet_id}",
        ]

        # Use TelegramBot User-Agent so Twitter embed APIs permit bot access
        headers = {
            "User-Agent": "TelegramBot (like TwitterBot)",
        }

        for api_url in api_endpoints:
            try:
                resp = requests.get(api_url, headers=headers, timeout=12)
                if resp.status_code != 200:
                    continue

                # Check if API returned an HTML error page (e.g. suspended / deleted)
                text_content = resp.text.strip()
                if text_content.startswith("<"):
                    if "Suspended" in text_content or "suspended" in text_content.lower():
                        return {"success": False, "error": "This Twitter / X account is suspended."}
                    if "Failed to scan" in text_content or "deleted" in text_content.lower() or "not found" in text_content.lower():
                        return {"success": False, "error": "This tweet was deleted, private, or does not exist."}
                    continue

                try:
                    data = resp.json()
                except Exception:
                    continue

                if not data.get("hasMedia") and not data.get("mediaURLs"):
                    return {"success": False, "error": "This tweet contains no downloadable video or images."}

                media_list = data.get("media_extended", [])
                text = data.get("text", "")
                author = f"{data.get('user_name', '')} (@{data.get('user_screen_name', '')})"
                
                downloaded_items = []

                if media_list:
                    for idx, item in enumerate(media_list):
                        m_type = item.get("type", "image")
                        m_url = item.get("url")
                        if not m_url:
                            continue

                        ext = ".mp4" if m_type in ["video", "gif"] else ".jpg"
                        save_path = task_dir / f"twitter_{idx+1}{ext}"

                        m_resp = requests.get(m_url, headers=headers, timeout=25)
                        if m_resp.status_code == 200:
                            save_path.write_bytes(m_resp.content)
                            downloaded_items.append({
                                "path": save_path,
                                "type": m_type,
                            })
                elif data.get("mediaURLs"):
                    for idx, m_url in enumerate(data["mediaURLs"]):
                        is_video = any(ext in m_url for ext in [".mp4", "video."])
                        ext = ".mp4" if is_video else ".jpg"
                        save_path = task_dir / f"twitter_{idx+1}{ext}"

                        m_resp = requests.get(m_url, headers=headers, timeout=25)
                        if m_resp.status_code == 200:
                            save_path.write_bytes(m_resp.content)
                            downloaded_items.append({
                                "path": save_path,
                                "type": "video" if is_video else "image",
                            })

                if downloaded_items:
                    return {
                        "success": True,
                        "media_items": downloaded_items,
                        "text": text,
                        "author": author,
                    }

            except Exception as e:
                print(f"[Twitter endpoint {api_url} error] {e}")

        return {"success": False}

    @staticmethod
    async def _download_ytdlp(url: str, task_dir: Path) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()

        def _sync_ytdlp():
            out_template = str(task_dir / "twitter_media_%(autonumber)s.%(ext)s")
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
                    title = info.get("title", "")
                    uploader = info.get("uploader", "Twitter User")

                    downloaded = []
                    for f in sorted(task_dir.iterdir()):
                        if f.is_file():
                            ext = f.suffix.lower()
                            if ext in [".mp4", ".mkv", ".webm"]:
                                downloaded.append({"path": f, "type": "video"})
                            elif ext in [".jpg", ".jpeg", ".png", ".webp"]:
                                downloaded.append({"path": f, "type": "image"})
                            elif ext in [".gif"]:
                                downloaded.append({"path": f, "type": "gif"})

                    if downloaded:
                        return {
                            "success": True,
                            "media_items": downloaded,
                            "text": title,
                            "author": uploader,
                        }
            except Exception as e:
                err_str = str(e)
                if "Suspended" in err_str:
                    return {"success": False, "error": "This Twitter / X account is suspended."}
                if "No video could be found" in err_str:
                    return {"success": False, "error": "No media found in this tweet."}
                return {"success": False, "error": err_str}

            return {"success": False}

        return await loop.run_in_executor(None, _sync_ytdlp)
