import os
import re
import json
import base64
import time
import asyncio
import requests
import concurrent.futures
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List, Callable

from utils.cleanup import create_task_dir
from utils.converter import get_video_params


class SxyprnDownloader:
    """
    Downloader for sxyprn.com video pages.

    Features:
      1. Reverse-engineered decoder for sxyprn's client-side getvsrc() / data-vnfo video manifest.
      2. Fast parallel proxy discovery with real CDN chunk pre-verification (prevents picking broken proxies).
      3. Multi-proxy HTTP Range resumption chaining for large files (prevents truncated downloads).
      4. Exact byte-length verification against CDN Content-Range / Content-Length.
      5. FFprobe integrity check on output file.
      6. Real-time download progress callback reporting to Telegram.
    """

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://sxyprn.com/",
        "Accept-Language": "en-US,en;q=0.9",
    }

    _proxy_cache: List[str] = []
    _proxy_cache_time: float = 0.0

    # ------------------------------------------------------------------ #
    # Reverse-engineered decoding primitives (from main2.js)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _ssut51(arg: str) -> int:
        return sum(int(c) for c in re.sub(r'[^0-9]', '', arg))

    @staticmethod
    def _boo(ss: int, es: int, host: str = "sxyprn.com") -> str:
        raw = f"{ss}-{host}-{es}"
        b = base64.b64encode(raw.encode("utf-8")).decode("utf-8")
        return b.replace("+", "-").replace("/", "_").replace("=", ".")

    @staticmethod
    def _preda(arg: list) -> list:
        s6 = SxyprnDownloader._ssut51(arg[6])
        s7 = SxyprnDownloader._ssut51(arg[7])
        arg[5] = str(int(arg[5]) - (s6 + s7))
        return arg

    @classmethod
    def _get_proxy_candidates(cls) -> List[str]:
        now = time.time()
        if cls._proxy_cache and (now - cls._proxy_cache_time) < 300:
            return cls._proxy_cache

        candidates: List[str] = []

        # 1. User-configured environment proxy
        env_proxy = os.getenv("SXYPRN_PROXY") or os.getenv("PORNHUB_PROXY")
        if env_proxy:
            candidates.append(env_proxy)

        # 2. Monosans continuously checked live proxies
        try:
            r = requests.get(
                "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
                timeout=4,
            )
            for line in r.text.splitlines():
                line = line.strip()
                if line and ":" in line and line not in candidates:
                    candidates.append(line)
        except Exception:
            pass

        # 3. Proxyscrape pool
        try:
            r2 = requests.get(
                "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=1500",
                timeout=4,
            )
            for line in r2.text.splitlines():
                line = line.strip()
                if line and ":" in line and line not in candidates:
                    candidates.append(line)
        except Exception:
            pass

        cls._proxy_cache = candidates
        cls._proxy_cache_time = now
        return candidates

    # ------------------------------------------------------------------ #
    # Public entry-point
    # ------------------------------------------------------------------ #

    @staticmethod
    async def download(
        url: str,
        task_dir: Optional[Path] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        if task_dir is None:
            task_dir = create_task_dir()

        try:
            res = await asyncio.to_thread(
                SxyprnDownloader._scrape_and_download,
                url,
                task_dir,
                progress_callback,
                status_callback,
            )
            if res.get("success") and res.get("path"):
                return res
            elif res.get("error"):
                return res
        except Exception as e:
            return {"success": False, "error": f"Download error: {e}"}

        return {
            "success": False,
            "error": "Failed to extract and download video from this page.",
        }

    # ------------------------------------------------------------------ #
    # Fast Parallel Verified Stream Discovery
    # ------------------------------------------------------------------ #

    @classmethod
    def _verify_proxy_stream(cls, url: str, p_str: str) -> Optional[Tuple[str, str, str, Optional[int]]]:
        """
        Tests candidate proxy through all 4 stages:
          1. Page HTML fetch & data-vnfo check
          2. Decoding manifest
          3. 302 location check
          4. 1KB Range GET request to verify the proxy can ACTUALLY stream video chunks over HTTPS.
        """
        p_dict = {"http": f"http://{p_str}", "https": f"http://{p_str}"}
        try:
            r_page = requests.get(url, headers=cls.HEADERS, proxies=p_dict, timeout=(2, 3))
            if r_page.status_code != 200 or "data-vnfo" not in r_page.text:
                return None

            m_title = re.search(r"<title[^>]*>([^<]+)</title>", r_page.text, re.I)
            title = "Video"
            if m_title:
                t = m_title.group(1).strip()
                for sfx in [
                    " - SXYPRN", " | SXYPRN", " - sxyprn.com",
                    " | sxyprn.com", " - SexyPorn", " | SexyPorn"
                ]:
                    t = t.replace(sfx, "").replace(sfx.lower(), "")
                title = t.strip() or title

            m_vnfo = re.search(r"data-vnfo='([^']+)'", r_page.text) or re.search(r'data-vnfo="([^"]+)"', r_page.text)
            if not m_vnfo:
                return None

            vnfo_raw = m_vnfo.group(1).replace("&quot;", '"')
            vnfo_data = json.loads(vnfo_raw)

            for pid, src in vnfo_data.items():
                tmp = src.split("/")
                b_val = cls._boo(cls._ssut51(tmp[6]), cls._ssut51(tmp[7]), "sxyprn.com")
                tmp[1] += "8/" + b_val
                tmp = cls._preda(tmp)
                final_url = "https://sxyprn.com" + "/".join(tmp)

                r_redir = requests.get(
                    final_url,
                    headers={**cls.HEADERS, "Referer": url},
                    proxies=p_dict,
                    timeout=4,
                    allow_redirects=False,
                )
                cdn = r_redir.headers.get("Location")
                if not cdn:
                    continue
                if cdn.startswith("//"):
                    cdn = "https:" + cdn

                # Step 4: Verify actual video streaming capability with 1KB Range request
                r_test = requests.get(
                    cdn,
                    headers={**cls.HEADERS, "Range": "bytes=0-1023", "Referer": url},
                    proxies=p_dict,
                    timeout=4,
                )
                if r_test.status_code == 206 and len(r_test.content) == 1024:
                    cr = r_test.headers.get("Content-Range", "")
                    m_total = re.search(r"/(\d+)", cr)
                    total_bytes = int(m_total.group(1)) if m_total else None
                    return (p_str, cdn, title, total_bytes)

        except Exception:
            pass

        return None

    @classmethod
    def _find_verified_stream(
        cls,
        url: str,
        exclude_proxies: set,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> Optional[Tuple[str, str, str, Optional[int]]]:
        candidates = [p for p in cls._get_proxy_candidates() if p not in exclude_proxies]
        batch_size = 40
        max_scan = min(len(candidates), 160)

        for i in range(0, max_scan, batch_size):
            batch = candidates[i : i + batch_size]
            if status_callback and i > 0:
                try:
                    status_callback(f"🔍 Searching fast streaming servers (tested {i}/{max_scan})...")
                except Exception:
                    pass
            with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as ex:
                future_to_p = {ex.submit(cls._verify_proxy_stream, url, p): p for p in batch}
                for f in concurrent.futures.as_completed(future_to_p):
                    res = f.result()
                    if res:
                        # Cancel remaining futures in batch to release resources
                        for other_f in future_to_p:
                            if other_f != f:
                                other_f.cancel()
                        return res

        return None

    # ------------------------------------------------------------------ #
    # Core Scraper and Downloader Engine
    # ------------------------------------------------------------------ #

    @classmethod
    def _scrape_and_download(
        cls,
        url: str,
        task_dir: Path,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        task_dir.mkdir(parents=True, exist_ok=True)
        out_path = task_dir / "sxyprn_video.mp4"

        total_bytes: Optional[int] = None
        video_title: str = "Adult Video"
        last_cdn_url: Optional[str] = None
        used_proxies: set = set()
        max_resumes = 6
        attempts = 0

        while attempts < max_resumes:
            attempts += 1
            cur_sz = out_path.stat().st_size if out_path.exists() else 0
            if total_bytes and cur_sz >= total_bytes:
                break

            if status_callback and attempts == 1:
                try:
                    status_callback("🔍 Finding streaming servers...")
                except Exception:
                    pass
            elif status_callback and attempts > 1:
                try:
                    from utils.progress import human_bytes
                    status_callback(f"🔄 Resuming stream ({human_bytes(cur_sz)} / {human_bytes(total_bytes or 0)})...")
                except Exception:
                    pass

            # Find a verified working proxy that can stream
            stream_info = cls._find_verified_stream(url, exclude_proxies=used_proxies, status_callback=status_callback)
            if not stream_info:
                if out_path.exists() and total_bytes and out_path.stat().st_size >= total_bytes:
                    break
                return {
                    "success": False,
                    "error": (
                        "Could not connect to streaming servers. "
                        "The post may be deleted, blocked, or unavailable."
                    ),
                }

            p_str, cdn_url, title, verified_total = stream_info
            used_proxies.add(p_str)
            video_title = title
            last_cdn_url = cdn_url
            if total_bytes is None and verified_total:
                total_bytes = verified_total

            if status_callback:
                try:
                    from utils.progress import human_bytes
                    size_label = f" ({human_bytes(total_bytes)})" if total_bytes else ""
                    status_callback(f"⚡ Connected to stream{size_label}! Downloading video...")
                except Exception:
                    pass

            cur_sz = out_path.stat().st_size if out_path.exists() else 0
            dl_headers = {**cls.HEADERS, "Referer": url}
            if cur_sz > 0:
                dl_headers["Range"] = f"bytes={cur_sz}-"

            p_dict = {"http": f"http://{p_str}", "https": f"http://{p_str}"}

            try:
                with requests.get(
                    cdn_url,
                    headers=dl_headers,
                    proxies=p_dict,
                    stream=True,
                    timeout=(8, 25),
                ) as resp:
                    if resp.status_code not in (200, 206):
                        continue

                    if total_bytes is None:
                        cr = resp.headers.get("Content-Range")
                        if cr:
                            m_cr = re.search(r"/(\d+)", cr)
                            if m_cr:
                                total_bytes = int(m_cr.group(1))
                        if total_bytes is None:
                            cl = resp.headers.get("Content-Length")
                            if cl and resp.status_code == 200:
                                total_bytes = int(cl)

                    last_progress_time = 0.0
                    with open(out_path, "ab" if cur_sz > 0 else "wb") as f:
                        for chunk in resp.iter_content(chunk_size=256 * 1024):
                            if chunk:
                                f.write(chunk)
                                cur_sz += len(chunk)
                                now = time.time()
                                if progress_callback and (now - last_progress_time) >= 3.0:
                                    last_progress_time = now
                                    try:
                                        progress_callback(cur_sz, total_bytes or cur_sz)
                                    except Exception:
                                        pass

                # Check if download finished
                final_sz = out_path.stat().st_size if out_path.exists() else 0
                if total_bytes and final_sz >= total_bytes:
                    break

            except Exception:
                # Connection dropped or timed out; continue loop to resume via next verified proxy
                continue

        if not out_path.exists() or out_path.stat().st_size == 0:
            return {
                "success": False,
                "error": "Failed to download video file.",
            }

        final_size = out_path.stat().st_size
        if total_bytes and final_size < total_bytes:
            return {
                "success": False,
                "error": f"Incomplete download: received {final_size/(1024*1024):.1f}MB of {total_bytes/(1024*1024):.1f}MB.",
            }

        # Verify video playability with ffprobe
        dur, w, h = get_video_params(out_path)
        if dur <= 0:
            return {"success": False, "error": "Downloaded file is corrupted or not a valid video."}

        return {
            "success": True,
            "path": out_path,
            "title": video_title,
            "video_url": last_cdn_url,
        }
