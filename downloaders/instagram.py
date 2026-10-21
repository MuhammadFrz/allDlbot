import os
import re
import html
import asyncio
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional
import requests
import yt_dlp
import instaloader

from utils.cleanup import create_task_dir

class InstagramDownloader:
    """
    Robust multi-engine downloader for Instagram reels, posts, carousels, and stories.
    Supports:
    1. SnapSave engine (high-speed bypass for login walls & audience/age-restricted reels & photos)
    2. Instagram Embed engine (direct high-resolution photo/carousel & caption extraction)
    3. yt-dlp engine (with optional cookies.txt or session)
    4. gallery-dl engine (with optional cookies.txt or session)
    5. Instaloader engine (when session is provided)
    """

    COOKIES_FILENAMES = ["cookies.txt", "instagram_cookies.txt", "ig_cookies.txt"]

    @classmethod
    def _find_cookies_file(cls) -> Optional[Path]:
        try:
            from config import BASE_DIR
            for name in cls.COOKIES_FILENAMES:
                candidate = BASE_DIR / name
                if candidate.is_file() and candidate.stat().st_size > 0:
                    return candidate
        except Exception:
            pass
        return None

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Strip query parameters and clean Instagram URL."""
        return re.sub(r'\?.*', '', url).strip().rstrip('/')

    @staticmethod
    def _extract_shortcode(url: str) -> Optional[str]:
        match = re.search(r'instagram\.com/(?:share/)?(?:p|reel|reels|tv)/([^/?&#]+)', url)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _decode_snapsave(text: str) -> str:
        """Pure-Python deobfuscation of SnapSave JavaScript payload."""
        match = re.search(r'\(\s*"([^"]+)"\s*,\s*(\d+)\s*,\s*"([^"]+)"\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)', text)
        if not match:
            return ""
        
        h = match.group(1)
        n = match.group(3)
        t = int(match.group(4))
        e = int(match.group(5))

        charset = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ+/"
        
        def to_dec(d: str, base_e: int) -> int:
            sub = charset[:base_e]
            val = 0
            for idx, ch in enumerate(reversed(d)):
                if ch in sub:
                    val += sub.index(ch) * (base_e ** idx)
            return val

        res = []
        i = 0
        delimiter = n[e]
        len_h = len(h)
        len_n = len(n)

        while i < len_h:
            s = ""
            while i < len_h and h[i] != delimiter:
                s += h[i]
                i += 1
            i += 1
            
            for j in range(len_n):
                s = s.replace(n[j], str(j))
                
            if s:
                try:
                    char_code = to_dec(s, e) - t
                    res.append(chr(char_code))
                except Exception:
                    pass

        raw_str = "".join(res)
        try:
            return raw_str.encode('latin1').decode('utf-8')
        except Exception:
            return raw_str

    @staticmethod
    def _fetch_embed_metadata(shortcode: str) -> Dict[str, str]:
        """Fetch caption and username from Instagram embed page."""
        meta = {"caption": "", "author": "Instagram"}
        try:
            embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
            }
            resp = requests.get(embed_url, headers=headers, timeout=6)
            if resp.status_code == 200:
                text = resp.text
                user_match = re.search(r'data-log-event=["\']captionProfileClick["\'][^>]*>([^<]+)</a>', text)
                if user_match:
                    meta["author"] = user_match.group(1).strip()
                else:
                    user_match2 = re.search(r'class="UsernameText"[^>]*>(.*?)</div>', text, re.DOTALL)
                    if user_match2:
                        meta["author"] = html.unescape(re.sub(r'<[^>]+>', '', user_match2.group(1)).strip())

                cap_match = re.search(r'data-log-event=["\']captionProfileClick["\'][^>]*>[^<]+</a>(?:<br\s*/?>)*(.*?)(?:<div class=["\']CaptionComments|<div class=["\']CaptionTime)', text, re.DOTALL)
                if cap_match:
                    raw_caption = re.sub(r'<[^>]+>', '', cap_match.group(1)).strip()
                    meta["caption"] = html.unescape(raw_caption)
                else:
                    cap_match2 = re.search(r'class="Caption"[^>]*>(.*?)</div>', text, re.DOTALL)
                    if cap_match2:
                        raw_caption = re.sub(r'<[^>]+>', '', cap_match2.group(1)).strip()
                        raw_caption = re.sub(r'View all \d+ comments.*$', '', raw_caption, flags=re.I).strip()
                        meta["caption"] = html.unescape(raw_caption)
        except Exception:
            pass
        return meta

    @staticmethod
    async def download(url: str, task_dir: Optional[Path] = None) -> Dict[str, Any]:
        if task_dir is None:
            task_dir = create_task_dir()
        task_dir.mkdir(parents=True, exist_ok=True)

        session_id = os.getenv("INSTAGRAM_SESSIONID")
        cookies_file = InstagramDownloader._find_cookies_file()

        # Engine 1: SnapSave (Best for public reels, videos, restricted audience, photos, carousels)
        try:
            res = await InstagramDownloader._download_snapsave(url, task_dir)
            if res.get("success") and res.get("media_items"):
                return res
        except Exception as e:
            print(f"[SnapSave error] {e}")

        # Engine 2: Instagram Embed Direct Extractor (Best for direct photos & photo carousels)
        try:
            res = await InstagramDownloader._download_embed(url, task_dir)
            if res.get("success") and res.get("media_items"):
                return res
        except Exception as e:
            print(f"[Instagram Embed error] {e}")

        # Engine 3: yt-dlp (with optional session or cookies file)
        try:
            res = await InstagramDownloader._download_ytdlp(url, task_dir, session_id, cookies_file)
            if res.get("success") and res.get("media_items"):
                return res
        except Exception as e:
            print(f"[yt-dlp error] {e}")

        # Engine 4: gallery-dl (with optional session or cookies file)
        try:
            res = await InstagramDownloader._download_gallery_dl(url, task_dir, session_id, cookies_file)
            if res.get("success") and res.get("media_items"):
                return res
        except Exception as e:
            print(f"[gallery-dl error] {e}")

        # Engine 5: Instaloader (Only if session_id is configured)
        if session_id:
            try:
                res = await InstagramDownloader._download_instaloader(url, task_dir, session_id)
                if res.get("success") and res.get("media_items"):
                    return res
            except Exception as e:
                print(f"[Instaloader error] {e}")

        return {
            "success": False,
            "error": "Failed to download Instagram media. The post might be private or Instagram requires authentication."
        }

    @staticmethod
    async def _download_snapsave(url: str, task_dir: Path) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()

        def _work():
            shortcode = InstagramDownloader._extract_shortcode(url)
            clean_url = InstagramDownloader._normalize_url(url)
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                'Referer': 'https://snapsave.app/'
            }
            resp = requests.post("https://snapsave.app/action.php", data={'url': clean_url}, headers=headers, timeout=15)
            if resp.status_code != 200 or 'eval(function(' not in resp.text:
                return {"success": False}

            decoded = InstagramDownloader._decode_snapsave(resp.text)
            clean_html = decoded.replace(r'\"', '"').replace(r"\'", "'")
            
            items = re.findall(r'<div class="download-items">([\s\S]*?)</div>\s*</div>', clean_html)
            download_urls = []
            
            if items:
                for item in items:
                    is_video = 'icon-dlvideo' in item or 'Download video' in item or '.mp4' in item
                    link_m = re.search(r'href="([^"]+)"', item)
                    if link_m:
                        download_urls.append({
                            "url": link_m.group(1),
                            "type": "video" if is_video else "photo"
                        })
            else:
                for link in re.findall(r'href="([^"]+)"', clean_html):
                    if 'token=' in link or 'fbcdn.net' in link:
                        is_video = '.mp4' in link or 'Download video' in clean_html
                        download_urls.append({
                            "url": link,
                            "type": "video" if is_video else "photo"
                        })

            if not download_urls:
                return {"success": False}

            media_items = []
            for i, itm in enumerate(download_urls):
                ext = ".mp4" if itm["type"] == "video" else ".jpg"
                file_path = task_dir / f"instagram_{i+1}{ext}"
                try:
                    r_file = requests.get(itm["url"], headers=headers, stream=True, timeout=30)
                    if r_file.status_code == 200:
                        with open(file_path, "wb") as f:
                            for chunk in r_file.iter_content(chunk_size=16384):
                                f.write(chunk)
                        if file_path.stat().st_size > 0:
                            media_items.append({"path": file_path, "type": itm["type"]})
                except Exception as e:
                    print(f"SnapSave media download error: {e}")

            if media_items:
                meta = InstagramDownloader._fetch_embed_metadata(shortcode) if shortcode else {}
                return {
                    "success": True,
                    "media_items": media_items,
                    "caption": meta.get("caption", ""),
                    "author": meta.get("author", "Instagram"),
                }
            return {"success": False}

        return await loop.run_in_executor(None, _work)

    @staticmethod
    async def _download_embed(url: str, task_dir: Path) -> Dict[str, Any]:
        """Download public images directly from Instagram embed."""
        loop = asyncio.get_running_loop()

        def _work():
            shortcode = InstagramDownloader._extract_shortcode(url)
            if not shortcode:
                return {"success": False}

            embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
            }
            resp = requests.get(embed_url, headers=headers, timeout=10)
            if resp.status_code != 200:
                return {"success": False}

            text = resp.text
            cdn_links = set(re.findall(r'https://scontent[^\s"\'<>]+', text))
            best_photos = []
            for raw_link in cdn_links:
                clean_link = html.unescape(raw_link).replace('\\u0026', '&')
                if 't51.82787-19' in clean_link:
                    continue
                if '.jpg' in clean_link or '.png' in clean_link or '.webp' in clean_link:
                    best_photos.append(clean_link)

            if not best_photos:
                return {"success": False}

            def photo_score(link):
                if '1080x1080' in link: return 10
                if '750x750' in link: return 8
                if '640x640' in link: return 6
                if 's240x240' in link or 's320x320' in link: return 1
                return 5

            best_photos.sort(key=photo_score, reverse=True)
            chosen_photo = best_photos[0]

            file_path = task_dir / f"instagram_embed_{shortcode}.jpg"
            img_r = requests.get(chosen_photo, headers=headers, timeout=20)
            if img_r.status_code == 200 and len(img_r.content) > 1000:
                file_path.write_bytes(img_r.content)
                meta = InstagramDownloader._fetch_embed_metadata(shortcode)
                return {
                    "success": True,
                    "media_items": [{"path": file_path, "type": "photo"}],
                    "caption": meta.get("caption", ""),
                    "author": meta.get("author", "Instagram"),
                }
            return {"success": False}

        return await loop.run_in_executor(None, _work)

    @staticmethod
    async def _download_ytdlp(url: str, task_dir: Path, session_id: Optional[str], cookies_file: Optional[Path]) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()

        def _work():
            out_template = str(task_dir / "%(id)s_%(autonumber)s.%(ext)s")
            ydl_opts = {
                "format": "best",
                "outtmpl": out_template,
                "cachedir": False,
                "no_cache_dir": True,
                "nopart": True,
                "yes_playlist": True,
                "quiet": True,
                "no_warnings": True,
            }
            if cookies_file and cookies_file.exists():
                ydl_opts["cookiefile"] = str(cookies_file)
            elif session_id:
                ydl_opts["http_headers"] = {"Cookie": f"sessionid={session_id}"}

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get("title", "")
                uploader = info.get("uploader", "Instagram")

                media_items = []
                for f in sorted(task_dir.iterdir()):
                    if f.is_file():
                        ext = f.suffix.lower()
                        if ext in [".mp4", ".mkv"]:
                            media_items.append({"path": f, "type": "video"})
                        elif ext in [".jpg", ".jpeg", ".png", ".webp"]:
                            media_items.append({"path": f, "type": "photo"})

                if media_items:
                    return {
                        "success": True,
                        "media_items": media_items,
                        "caption": title,
                        "author": uploader,
                    }
                return {"success": False}

        return await loop.run_in_executor(None, _work)

    @staticmethod
    async def _download_gallery_dl(url: str, task_dir: Path, session_id: Optional[str], cookies_file: Optional[Path]) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()

        def _work():
            cmd = [
                "gallery-dl",
                "--directory", str(task_dir),
                "--no-mtime",
                "--quiet",
            ]
            if cookies_file and cookies_file.exists():
                cmd.extend(["--cookies", str(cookies_file)])
            elif session_id:
                cmd.extend(["--header", f"Cookie: sessionid={session_id}"])
            cmd.append(url)

            subprocess.run(cmd, capture_output=True, text=True)

            media_items = []
            for root, _, files in os.walk(task_dir):
                for file_name in sorted(files):
                    f = Path(root) / file_name
                    ext = f.suffix.lower()
                    if ext in [".mp4", ".mkv", ".webm"]:
                        media_items.append({"path": f, "type": "video"})
                    elif ext in [".jpg", ".jpeg", ".png", ".webp"]:
                        media_items.append({"path": f, "type": "photo"})

            if media_items:
                return {
                    "success": True,
                    "media_items": media_items,
                    "caption": "",
                    "author": "Instagram User",
                }
            return {"success": False}

        return await loop.run_in_executor(None, _work)

    @staticmethod
    async def _download_instaloader(url: str, task_dir: Path, session_id: Optional[str]) -> Dict[str, Any]:
        shortcode = InstagramDownloader._extract_shortcode(url)
        if not shortcode:
            return {"success": False}

        loop = asyncio.get_running_loop()

        def _work():
            L = instaloader.Instaloader(
                dirname_pattern=str(task_dir),
                filename_pattern="{date_utc}_UTC_{media_id}",
                download_pictures=True,
                download_videos=True,
                download_video_thumbnails=False,
                download_geotags=False,
                download_comments=False,
                save_metadata=False,
                compress_json=False,
                quiet=True,
            )

            if session_id:
                L.context._session.cookies.set("sessionid", session_id, domain=".instagram.com")

            post = instaloader.Post.from_shortcode(L.context, shortcode)
            caption = post.caption or ""
            owner = post.owner_username or ""

            L.download_post(post, target=str(task_dir))

            media_items = []
            for f in sorted(task_dir.iterdir()):
                if f.is_file():
                    ext = f.suffix.lower()
                    if ext in [".mp4", ".mkv"]:
                        media_items.append({"path": f, "type": "video"})
                    elif ext in [".jpg", ".jpeg", ".png", ".webp"]:
                        media_items.append({"path": f, "type": "photo"})

            if media_items:
                return {
                    "success": True,
                    "media_items": media_items,
                    "caption": caption,
                    "author": owner,
                }
            return {"success": False}

        return await loop.run_in_executor(None, _work)
