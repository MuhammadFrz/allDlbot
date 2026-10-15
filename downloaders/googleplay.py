import os
import re
import base64
import urllib.parse
from pathlib import Path
from typing import Dict, Any, Optional
import requests
from PIL import Image
import io

import config
from utils.cleanup import create_task_dir

GOOGLE_PLAY_REGEX = re.compile(
    r'(?:(?:https?://)?(?:play\.google\.com/store/apps/details\?|market://details\?)[^\s"\'<>]*id=([a-zA-Z0-9._]+))',
    re.IGNORECASE
)

PUREAPK_API_URL = "https://api.pureapk.com/m/v3/cms/app_version"
DOWNLOAD_URL_REGEX = re.compile(r'(X?APKJ)..(https?://[^\s"\'<>]+\b)')

ARCH_MAP = {
    "arm64-v8a": "arm64-v8a",
    "armeabi-v7a": "armeabi-v7a",
    "x86_64": "x86_64",
    "x86": "x86",
    "universal": "arm64-v8a,armeabi-v7a,x86,x86_64",
}

ARCH_LABELS = {
    "arm64-v8a": "ARM64 (64-bit | 4GB+ RAM)",
    "armeabi-v7a": "ARMv7 (32-bit | <4GB RAM)",
    "x86_64": "x86_64 (64-bit PC / Emulator)",
    "x86": "x86 (32-bit PC / Emulator)",
    "universal": "Universal (All Architectures)",
}


class GooglePlayDownloader:
    """Downloader for Google Play Store apps with architecture selection."""

    @staticmethod
    def extract_package_id(text: str) -> Optional[str]:
        """Extract Google Play package ID from URL or text."""
        if not text:
            return None
        match = GOOGLE_PLAY_REGEX.search(text)
        if match:
            pkg = match.group(1)
            # Remove any trailing query parameters if caught
            pkg = pkg.split('&')[0].strip('.,;!?)]}')
            return pkg
        
        # If user passed a clean package name directly
        clean = text.strip()
        if re.match(r'^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$', clean):
            return clean

        return None

    @staticmethod
    def get_app_info(package_id: str) -> Optional[Dict[str, Any]]:
        """Fetch application metadata from Google Play Store web or mirror."""
        info: Dict[str, Any] = {
            "package_id": package_id,
            "title": package_id,
            "developer": "Google Play Developer",
            "version": None,
            "rating": None,
            "icon_url": None,
            "genre": None,
            "description": None,
            "url": f"https://play.google.com/store/apps/details?id={package_id}",
        }

        # 1. Try scraping official Google Play Store page
        try:
            gp_url = f"https://play.google.com/store/apps/details?id={package_id}&hl=en"
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            }
            resp = requests.get(gp_url, headers=headers, timeout=8)
            if resp.status_code == 200:
                html = resp.text

                # Title
                title_match = re.search(r'<h1[^>]*>.*?<span[^>]*>(.*?)</span>', html, re.DOTALL)
                if not title_match:
                    title_match = re.search(r'<title>(.*?) - Apps on Google Play</title>', html)
                if title_match:
                    info["title"] = title_match.group(1).strip()

                # Developer
                dev_match = re.search(r'href="/store/apps/dev\?id=[^"]*"[^>]*><span[^>]*>(.*?)</span>', html)
                if not dev_match:
                    dev_match = re.search(r'href="/store/apps/developer\?id=[^"]*"[^>]*><span[^>]*>(.*?)</span>', html)
                if dev_match:
                    info["developer"] = dev_match.group(1).strip()

                # Star Rating
                rating_match = re.search(r'<div class="TT9eCd">([0-9\.]+)★</div>', html)
                if not rating_match:
                    rating_match = re.search(r'aria-label="Rated ([0-9\.]+) stars out of five', html)
                if rating_match:
                    info["rating"] = f"{rating_match.group(1)} ⭐"

                # Genre / Category
                genre_match = re.search(r'itemprop="genre">([^<]+)</span>', html)
                if genre_match:
                    info["genre"] = genre_match.group(1).strip()

                # Icon
                icon_match = re.search(r'<img[^>]+src="([^">]+googleusercontent\.com/[^">]+)"[^>]+alt="Cover art"', html)
                if not icon_match:
                    icon_match = re.search(r'<meta property="og:image" content="([^"]+)"', html)
                if icon_match:
                    info["icon_url"] = icon_match.group(1)

                # Description summary
                desc_match = re.search(r'<meta property="og:description" content="([^"]+)"', html)
                if desc_match:
                    info["description"] = desc_match.group(1).strip()
        except Exception as e:
            print(f"[GooglePlayDownloader] Web scraping error: {e}")

        # 2. Query mirror endpoint for version info and backup metadata
        try:
            mirror_headers = {
                "x-cv": "3172501",
                "x-sv": "29",
                "x-abis": "arm64-v8a,armeabi-v7a,x86,x86_64",
                "x-gp": "1",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            }
            m_url = f"{PUREAPK_API_URL}?hl=en-US&package_name={package_id}"
            m_resp = requests.get(m_url, headers=mirror_headers, timeout=8)
            if m_resp.status_code == 200:
                m_text = m_resp.text
                matches = DOWNLOAD_URL_REGEX.findall(m_text)
                if matches:
                    _, dl_url = matches[0]
                    parsed = urllib.parse.urlparse(dl_url)
                    params = urllib.parse.parse_qs(parsed.query)
                    
                    if info["title"] == package_id and "_fn" in params:
                        try:
                            fn = base64.b64decode(params["_fn"][0]).decode("utf-8", errors="ignore")
                            title_part = fn.split("_v")[0] if "_v" in fn else fn
                            info["title"] = title_part.replace("_", " ")
                        except Exception:
                            pass

                    # Parse version from 'c' parameter
                    c_param = params.get("c", [""])[0]
                    for part in c_param.split("|"):
                        try:
                            decoded = base64.b64decode(part).decode("utf-8", errors="ignore")
                            c_sub = urllib.parse.parse_qs(decoded)
                            if "vn" in c_sub and not info.get("version"):
                                info["version"] = c_sub["vn"][0]
                            if "dev" in c_sub and info["developer"] == "Google Play Developer":
                                info["developer"] = c_sub["dev"][0]
                        except Exception:
                            pass

                # Backup icon if not found
                if not info.get("icon_url"):
                    icon_m = re.search(r'(https://image\.winudf\.com/v2/image[^\s"\'<>]+\b)', m_text)
                    if icon_m:
                        info["icon_url"] = icon_m.group(1)
        except Exception as e:
            print(f"[GooglePlayDownloader] Mirror metadata query error: {e}")

        return info

    @staticmethod
    def download(
        package_id: str,
        arch: str = "arm64-v8a",
        task_dir: Optional[Path] = None,
        cancel_check: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Download the APK or XAPK file for the specified architecture.
        Returns a result dictionary with path, metadata, and status.
        """
        if task_dir is None:
            task_dir = create_task_dir()

        x_abis = ARCH_MAP.get(arch, "arm64-v8a")
        headers = {
            "x-cv": "3172501",
            "x-sv": "29",
            "x-abis": x_abis,
            "x-gp": "1",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        }
        api_url = f"{PUREAPK_API_URL}?hl=en-US&package_name={package_id}"

        try:
            resp = requests.get(api_url, headers=headers, timeout=15)
            if resp.status_code != 200:
                return {
                    "success": False,
                    "error": f"API request failed with status code {resp.status_code}.",
                }

            matches = DOWNLOAD_URL_REGEX.findall(resp.text)
            if not matches:
                # If specific architecture had no direct match, fallback to universal
                if arch != "universal":
                    fallback_headers = dict(headers)
                    fallback_headers["x-abis"] = ARCH_MAP["universal"]
                    f_resp = requests.get(api_url, headers=fallback_headers, timeout=15)
                    if f_resp.status_code == 200:
                        matches = DOWNLOAD_URL_REGEX.findall(f_resp.text)

            if not matches:
                return {
                    "success": False,
                    "error": (
                        f"No downloadable APK found for `{package_id}` with architecture `{arch}`. "
                        "The app may be region-locked, paid, or incompatible."
                    ),
                }

            dl_type, dl_url = matches[0]
            ext = "xapk" if dl_type.startswith("XAPK") else "apk"

            # Parse query parameters for metadata
            parsed = urllib.parse.urlparse(dl_url)
            params = urllib.parse.parse_qs(parsed.query)

            app_title = package_id
            version_str = "latest"
            dev_name = "Google Play App"

            if "_fn" in params:
                try:
                    fn_decoded = base64.b64decode(params["_fn"][0]).decode("utf-8", errors="ignore")
                    app_title = fn_decoded.split("_v")[0] if "_v" in fn_decoded else fn_decoded
                    app_title = app_title.replace("_", " ")
                except Exception:
                    pass

            c_param = params.get("c", [""])[0]
            for part in c_param.split("|"):
                try:
                    decoded = base64.b64decode(part).decode("utf-8", errors="ignore")
                    sub = urllib.parse.parse_qs(decoded)
                    if "vn" in sub:
                        version_str = sub["vn"][0]
                    if "dev" in sub:
                        dev_name = sub["dev"][0]
                except Exception:
                    pass

            # Prepare safe filename
            safe_title = re.sub(r'[\\/*?:"<>|]', "", app_title).strip()
            safe_title = safe_title[:40].strip() or package_id
            filename = f"{safe_title}_v{version_str}_{arch}.{ext}"
            file_path = task_dir / filename

            # Stream download to disk
            dl_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            }
            with requests.get(dl_url, headers=dl_headers, stream=True, timeout=20) as r:
                r.raise_for_status()

                # Check content length
                content_length = int(r.headers.get("content-length", 0))
                if content_length > 0:
                    size_mb = content_length / (1024 * 1024)
                    if size_mb > config.MAX_FILE_SIZE_MB:
                        return {
                            "success": False,
                            "error": f"File size ({size_mb:.1f} MB) exceeds maximum allowed limit ({config.MAX_FILE_SIZE_MB} MB).",
                        }

                with open(file_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=128 * 1024):
                        if cancel_check and cancel_check():
                            file_path.unlink(missing_ok=True)
                            return {"success": False, "error": "Download cancelled by user."}
                        if chunk:
                            f.write(chunk)

            if not file_path.exists() or file_path.stat().st_size == 0:
                return {"success": False, "error": "Downloaded file is empty or corrupted."}

            # Attempt to download thumbnail icon
            thumb_path = None
            icon_m = re.search(r'(https://image\.winudf\.com/v2/image[^\s"\'<>]+\b)', resp.text)
            if icon_m:
                try:
                    icon_url = icon_m.group(1)
                    icon_resp = requests.get(icon_url, timeout=6)
                    if icon_resp.status_code == 200:
                        img = Image.open(io.BytesIO(icon_resp.content)).convert("RGB")
                        img.thumbnail((320, 320))
                        thumb_file = task_dir / "thumb.jpg"
                        img.save(thumb_file, "JPEG", quality=85)
                        if thumb_file.exists():
                            thumb_path = thumb_file
                except Exception as e:
                    print(f"[GooglePlayDownloader] Thumbnail error: {e}")

            return {
                "success": True,
                "path": file_path,
                "thumb_path": thumb_path,
                "title": app_title,
                "developer": dev_name,
                "version": version_str,
                "arch": arch,
                "file_type": ext,
                "size_bytes": file_path.stat().st_size,
            }

        except Exception as e:
            return {"success": False, "error": str(e)}
