import re
import asyncio
import requests
from pathlib import Path
from typing import Dict, Any, List, Optional
import yt_dlp

from utils.cleanup import create_task_dir
from utils.converter import extract_thumbnail, get_video_params

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

        headers = {
            "User-Agent": "TelegramBot (like TwitterBot)",
        }

        for api_url in api_endpoints:
            try:
                resp = requests.get(api_url, headers=headers, timeout=12)
                if resp.status_code != 200:
                    continue

                text_content = resp.text.strip()
                if text_content.startswith("<"):
                    if "suspended" in text_content.lower():
                        return {"success": False, "error": "This Twitter / X account is suspended."}
                    if any(s in text_content.lower() for s in ["failed to scan", "deleted", "not found"]):
                        return {"success": False, "error": "This tweet was deleted, private, or does not exist."}
                    continue

                try:
                    data = resp.json()
                except Exception:
                    continue

                raw_items = []
                text = ""
                author = "Twitter User"

                # 1. Handle FxTwitter structure: data = {"code": 200, "tweet": {...}}
                if "tweet" in data and isinstance(data["tweet"], dict):
                    tweet = data["tweet"]
                    text = tweet.get("text", "")
                    author_data = tweet.get("author") or {}
                    author = f"{author_data.get('name', '')} (@{author_data.get('screen_name', '')})"
                    media = tweet.get("media") or {}
                    all_media = media.get("all") or media.get("videos") or media.get("photos") or []
                    for m in all_media:
                        m_type = m.get("type", "image")
                        m_url = m.get("url")
                        # If video has variants with multiple resolutions, pick best MP4 bitrate
                        variants = m.get("variants") or []
                        max_br = -1
                        for v in variants:
                            if v.get("content_type") == "video/mp4":
                                br = v.get("bitrate", 0)
                                if br > max_br and v.get("url"):
                                    max_br = br
                                    m_url = v.get("url")

                        if m_url:
                            raw_items.append({
                                "type": m_type,
                                "url": m_url,
                                "thumbnail_url": m.get("thumbnail_url"),
                                "duration": int(float(m.get("duration", 0))),
                                "width": m.get("width"),
                                "height": m.get("height"),
                            })

                # 2. Handle VxTwitter structure: data = {"hasMedia": true, "media_extended": [...]}
                elif data.get("hasMedia") or data.get("mediaURLs"):
                    text = data.get("text", "")
                    author = f"{data.get('user_name', '')} (@{data.get('user_screen_name', '')})"
                    media_list = data.get("media_extended") or []
                    if media_list:
                        for m in media_list:
                            m_type = m.get("type", "image")
                            m_url = m.get("url")
                            if m_url:
                                dur_ms = m.get("duration_millis")
                                dur_sec = int(dur_ms / 1000) if dur_ms else 0
                                size_dict = m.get("size") or {}
                                raw_items.append({
                                    "type": m_type,
                                    "url": m_url,
                                    "thumbnail_url": m.get("thumbnail_url"),
                                    "duration": dur_sec,
                                    "width": size_dict.get("width"),
                                    "height": size_dict.get("height"),
                                })
                    elif data.get("mediaURLs"):
                        for m_url in data["mediaURLs"]:
                            is_video = any(ext in m_url for ext in [".mp4", "video."])
                            raw_items.append({
                                "type": "video" if is_video else "image",
                                "url": m_url,
                                "thumbnail_url": None,
                                "duration": 0,
                                "width": None,
                                "height": None,
                            })

                if not raw_items:
                    continue

                downloaded_items = []
                for idx, item in enumerate(raw_items):
                    m_type = item["type"]
                    m_url = item["url"]
                    ext = ".mp4" if m_type in ["video", "gif"] else ".jpg"
                    save_path = task_dir / f"twitter_{idx+1}{ext}"

                    # Chunked stream download to file (prevents VPS OOM!)
                    try:
                        with requests.get(m_url, headers=headers, stream=True, timeout=90) as m_resp:
                            if m_resp.status_code == 200:
                                with open(save_path, "wb") as f:
                                    for chunk in m_resp.iter_content(chunk_size=1024 * 1024):
                                        if chunk:
                                            f.write(chunk)
                    except Exception as e:
                        print(f"[Twitter stream download error] {e}")
                        continue

                    if not save_path.exists() or save_path.stat().st_size == 0:
                        continue

                    thumb_file = None
                    dur_sec = item.get("duration") or 0
                    width = item.get("width")
                    height = item.get("height")

                    if m_type in ["video", "gif"]:
                        thumb_url = item.get("thumbnail_url")
                        if thumb_url:
                            try:
                                t_resp = requests.get(thumb_url, headers=headers, timeout=10)
                                if t_resp.status_code == 200 and len(t_resp.content) > 200:
                                    t_path = task_dir / f"twitter_{idx+1}_thumb.jpg"
                                    t_path.write_bytes(t_resp.content)
                                    if t_path.exists() and t_path.stat().st_size > 200:
                                        thumb_file = t_path
                            except Exception as e:
                                print(f"[Twitter thumb download error] {e}")

                        # Fallback to local frame extraction if thumbnail missing or invalid
                        if not thumb_file or not thumb_file.exists():
                            thumb_file = extract_thumbnail(save_path)

                        # Fallback to ffprobe if duration or dimensions missing
                        if not dur_sec or not width or not height:
                            p_dur, p_w, p_h = get_video_params(save_path)
                            dur_sec = dur_sec or p_dur
                            width = width or p_w
                            height = height or p_h

                    downloaded_items.append({
                        "path": save_path,
                        "type": m_type,
                        "thumb": thumb_file if (thumb_file and thumb_file.exists()) else None,
                        "duration": dur_sec,
                        "width": width,
                        "height": height,
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
                                thumb = extract_thumbnail(f)
                                dur, w, h = get_video_params(f)
                                downloaded.append({
                                    "path": f,
                                    "type": "video",
                                    "thumb": thumb if (thumb and thumb.exists()) else None,
                                    "duration": dur,
                                    "width": w,
                                    "height": h,
                                })
                            elif ext in [".jpg", ".jpeg", ".png", ".webp"]:
                                downloaded.append({"path": f, "type": "image"})
                            elif ext in [".gif"]:
                                thumb = extract_thumbnail(f)
                                dur, w, h = get_video_params(f)
                                downloaded.append({
                                    "path": f,
                                    "type": "gif",
                                    "thumb": thumb if (thumb and thumb.exists()) else None,
                                    "duration": dur,
                                    "width": w,
                                    "height": h,
                                })

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
