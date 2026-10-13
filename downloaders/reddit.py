import os
import re
import json
import asyncio
import subprocess
import requests
from pathlib import Path
from typing import Dict, Any, List, Optional
import yt_dlp

from utils.cleanup import create_task_dir

class RedditDownloader:
    """Downloader for Reddit videos (with audio merged), multi-image galleries, and GIFs."""

    @staticmethod
    async def download(url: str, task_dir: Optional[Path] = None) -> Dict[str, Any]:
        if task_dir is None:
            task_dir = create_task_dir()

        # Engine 1: yt-dlp (Muxes video + DASH audio seamlessly)
        try:
            res = await RedditDownloader._download_ytdlp(url, task_dir)
            if res.get("success") and res.get("media_items"):
                return res
        except Exception as e:
            print(f"[Reddit yt-dlp error] {e}")

        # Engine 2: Native Reddit JSON scraper + FFmpeg muxer (handles galleries & fallback videos)
        try:
            res = await asyncio.to_thread(RedditDownloader._sync_scrape_reddit, url, task_dir)
            if res.get("success") and res.get("media_items"):
                return res
            elif res.get("error"):
                return res
        except Exception as e:
            print(f"[Reddit scraper error] {e}")

        return {"success": False, "error": "Failed to download Reddit media. The post might be private or deleted."}

    @staticmethod
    async def _download_ytdlp(url: str, task_dir: Path) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()

        def _sync_ytdlp():
            out_template = str(task_dir / "reddit_%(id)s.%(ext)s")
            ydl_opts = {
                "format": "bestvideo+bestaudio/best",
                "outtmpl": out_template,
                "cachedir": False,
                "no_cache_dir": True,
                "nopart": True,
                "merge_output_format": "mp4",
                "quiet": True,
                "no_warnings": True,
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    title = info.get("title", "Reddit Media")
                    author = info.get("uploader", "Reddit User")

                    media_items = []
                    for f in sorted(task_dir.iterdir()):
                        if f.is_file():
                            ext = f.suffix.lower()
                            if ext in [".mp4", ".mkv", ".webm", ".mov"]:
                                media_items.append({"path": f, "type": "video"})
                            elif ext in [".jpg", ".jpeg", ".png", ".webp"]:
                                media_items.append({"path": f, "type": "image"})
                            elif ext in [".gif"]:
                                media_items.append({"path": f, "type": "gif"})

                    if media_items:
                        return {
                            "success": True,
                            "media_items": media_items,
                            "title": title,
                            "author": author,
                        }
            except Exception:
                pass
            return {"success": False}

        return await loop.run_in_executor(None, _sync_ytdlp)

    @staticmethod
    def _sync_scrape_reddit(url: str, task_dir: Path) -> Dict[str, Any]:
        # Normalize Reddit URL to JSON endpoint
        clean_url = url.split("?")[0].rstrip("/")
        if not clean_url.endswith(".json"):
            json_url = clean_url + ".json"
        else:
            json_url = clean_url

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
        }

        try:
            resp = requests.get(json_url, headers=headers, timeout=12)
            if resp.status_code != 200:
                return {"success": False}
            data = resp.json()
        except Exception as e:
            return {"success": False, "error": str(e)}

        try:
            post_data = data[0]["data"]["children"][0]["data"]
        except (IndexError, KeyError, TypeError):
            return {"success": False, "error": "Invalid Reddit post structure."}

        title = post_data.get("title", "Reddit Post")
        author = post_data.get("author", "Reddit User")

        # Case 1: Native Reddit Video (Mux video + DASH audio)
        is_video = post_data.get("is_video", False)
        reddit_video = post_data.get("secure_media", {}).get("reddit_video") or post_data.get("media", {}).get("reddit_video")

        if is_video and reddit_video:
            fallback_url = reddit_video.get("fallback_url")
            if fallback_url:
                v_path = task_dir / "temp_video.mp4"
                a_path = task_dir / "temp_audio.mp4"
                out_path = task_dir / "reddit_video.mp4"

                # Download video stream
                vr = requests.get(fallback_url, headers=headers, timeout=25)
                if vr.status_code == 200:
                    v_path.write_bytes(vr.content)

                    # Try to fetch audio stream (usually DASH_AUDIO.mp4 or DASH_audio.mp4)
                    base_dash = fallback_url.split("DASH_")[0]
                    audio_found = False
                    for a_suffix in ["DASH_AUDIO_128.mp4", "DASH_audio.mp4", "DASH_AUDIO.mp4", "audio"]:
                        a_url = base_dash + a_suffix
                        ar = requests.get(a_url, headers=headers, timeout=10)
                        if ar.status_code == 200 and len(ar.content) > 1000:
                            a_path.write_bytes(ar.content)
                            audio_found = True
                            break

                    # Mux with FFmpeg if audio exists, else use video stream directly
                    if audio_found and a_path.exists():
                        cmd = [
                            "ffmpeg", "-y",
                            "-i", str(v_path),
                            "-i", str(a_path),
                            "-c:v", "copy",
                            "-c:a", "aac",
                            str(out_path),
                        ]
                        subprocess.run(cmd, capture_output=True)
                        if out_path.exists():
                            v_path.unlink(missing_ok=True)
                            a_path.unlink(missing_ok=True)
                            return {
                                "success": True,
                                "media_items": [{"path": out_path, "type": "video"}],
                                "title": title,
                                "author": author,
                            }

                    # If no audio or mux failed, return video
                    return {
                        "success": True,
                        "media_items": [{"path": v_path, "type": "video"}],
                        "title": title,
                        "author": author,
                    }

        # Case 2: Multi-image gallery
        gallery_data = post_data.get("gallery_data", {}).get("items", [])
        media_metadata = post_data.get("media_metadata", {})
        if gallery_data and media_metadata:
            downloaded_items = []
            for idx, item in enumerate(gallery_data):
                media_id = item.get("media_id")
                meta = media_metadata.get(media_id, {})
                s = meta.get("s", {})
                img_url = s.get("u") or s.get("gif")
                if img_url:
                    img_url = img_url.replace("&amp;", "&")
                    ext = ".gif" if "gif" in meta.get("m", "") else ".jpg"
                    img_path = task_dir / f"gallery_{idx+1}{ext}"
                    ir = requests.get(img_url, headers=headers, timeout=15)
                    if ir.status_code == 200:
                        img_path.write_bytes(ir.content)
                        downloaded_items.append({"path": img_path, "type": "image"})

            if downloaded_items:
                return {
                    "success": True,
                    "media_items": downloaded_items,
                    "title": title,
                    "author": author,
                }

        # Case 3: Direct image / GIF link
        dest_url = post_data.get("url_overridden_by_dest", "")
        if any(dest_url.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]):
            ext = ".gif" if dest_url.lower().endswith(".gif") else ".jpg"
            img_path = task_dir / f"reddit_image{ext}"
            ir = requests.get(dest_url, headers=headers, timeout=15)
            if ir.status_code == 200:
                img_path.write_bytes(ir.content)
                return {
                    "success": True,
                    "media_items": [{"path": img_path, "type": "image" if ext != ".gif" else "gif"}],
                    "title": title,
                    "author": author,
                }

        return {"success": False, "error": "No downloadable video, gallery, or image found in this Reddit post."}
