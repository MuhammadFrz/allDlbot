import os
import re
import json
import base64
import time
import asyncio
import requests
import concurrent.futures
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List
import yt_dlp

from utils.cleanup import create_task_dir
from utils.converter import get_video_params


class SxyprnDownloader:
    """
    Downloader for sxyprn.com video pages.

    Features:
      1. Reverse-engineered decoder for sxyprn's client-side getvsrc() / data-vnfo video manifest.
      2. Fast concurrent proxy discovery (ThreadPoolExecutor) to bypass US regional geo-blocks.
      3. Resilient multi-proxy HTTP Range resumption chaining for large files (prevents truncated downloads).
      4. Exact byte-length verification against CDN Content-Range / Content-Length.
      5. FFprobe integrity check on output file.
      6. yt-dlp fallback if direct extraction fails.
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
    def _get_proxy_candidates(cls) -> List[Optional[str]]:
        now = time.time()
        if cls._proxy_cache and (now - cls._proxy_cache_time) < 600:
            return cls._proxy_cache

        candidates: List[Optional[str]] = []

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

        # 4. Direct connection fallback
        candidates.append(None)

        cls._proxy_cache = candidates
        cls._proxy_cache_time = now
        return candidates

    # ------------------------------------------------------------------ #
    # Public entry-point
    # ------------------------------------------------------------------ #

    @staticmethod
    async def download(url: str, task_dir: Optional[Path] = None) -> Dict[str, Any]:
        if task_dir is None:
            task_dir = create_task_dir()

        # Engine 1: Native reverse-engineered decoder + resilient multi-proxy streaming
        try:
            res = await asyncio.to_thread(SxyprnDownloader._scrape_and_download, url, task_dir)
            if res.get("success") and res.get("path"):
                return res
        except Exception as e:
            print(f"[sxyprn scrape error] {e}")

        # Engine 2: yt-dlp fallback
        try:
            res = await asyncio.to_thread(SxyprnDownloader._ytdlp_download, url, task_dir)
            if res.get("success") and res.get("path"):
                return res
        except Exception as e:
            print(f"[sxyprn yt-dlp error] {e}")

        return {
            "success": False,
            "error": (
                "Could not extract video from this sxyprn page. "
                "The post may be private, deleted, or temporarily unavailable."
            ),
        }

    # ------------------------------------------------------------------ #
    # Stream Resolver via Candidate Proxy
    # ------------------------------------------------------------------ #

    @classmethod
    def _resolve_cdn_stream(cls, url: str, p_str: Optional[str]) -> Optional[Tuple[str, str, str]]:
        p_dict = {"http": f"http://{p_str}", "https": f"http://{p_str}"} if p_str else None
        try:
            r_page = requests.get(url, headers=cls.HEADERS, proxies=p_dict, timeout=(2, 4))
            if r_page.status_code != 200 or "data-vnfo" not in r_page.text:
                return None

            # Extract title
            m_title = re.search(r"<title[^>]*>([^<]+)</title>", r_page.text, re.I)
            title = "SXYPRN Video"
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
                    timeout=5,
                    allow_redirects=False,
                )
                cdn = r_redir.headers.get("Location")
                if cdn:
                    if cdn.startswith("//"):
                        cdn = "https:" + cdn
                    return (p_str or "direct", cdn, title)

        except Exception:
            pass

        return None

    # ------------------------------------------------------------------ #
    # Engine 1: Native scraper and downloader
    # ------------------------------------------------------------------ #

    @classmethod
    def _scrape_and_download(cls, url: str, task_dir: Path) -> Dict[str, Any]:
        task_dir.mkdir(parents=True, exist_ok=True)
        out_path = task_dir / "sxyprn_video.mp4"

        proxy_candidates = cls._get_proxy_candidates()

        total_bytes: Optional[int] = None
        video_title: str = "SXYPRN Video"
        last_cdn_url: Optional[str] = None
        completed: bool = False

        batch_size = 30
        proxy_idx = 0
        max_scan = min(len(proxy_candidates), 180)

        while not completed and proxy_idx < max_scan:
            batch = proxy_candidates[proxy_idx : proxy_idx + batch_size]
            proxy_idx += batch_size

            resolved_stream = None
            with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as ex:
                future_to_proxy = {ex.submit(cls._resolve_cdn_stream, url, p): p for p in batch}
                for f in concurrent.futures.as_completed(future_to_proxy):
                    res = f.result()
                    if res:
                        resolved_stream = res
                        break

            if not resolved_stream:
                continue

            p_str, cdn_url, video_title = resolved_stream
            last_cdn_url = cdn_url
            p_dict = {"http": f"http://{p_str}", "https": f"http://{p_str}"} if p_str != "direct" else None

            # Stream chunks from this proxy
            cur_sz = out_path.stat().st_size if out_path.exists() else 0
            if total_bytes and cur_sz >= total_bytes:
                completed = True
                break

            dl_headers = {**cls.HEADERS, "Referer": url}
            if cur_sz > 0:
                dl_headers["Range"] = f"bytes={cur_sz}-"

            try:
                with requests.get(
                    cdn_url,
                    headers=dl_headers,
                    proxies=p_dict,
                    stream=True,
                    timeout=(6, 25),
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

                    with open(out_path, "ab" if cur_sz > 0 else "wb") as f:
                        for chunk in resp.iter_content(chunk_size=256 * 1024):
                            if chunk:
                                f.write(chunk)

                final_sz = out_path.stat().st_size if out_path.exists() else 0
                if total_bytes and final_sz >= total_bytes:
                    completed = True
                    break

            except Exception:
                # Connection dropped or timed out; continue outer loop to resume remaining bytes
                continue

        if not out_path.exists():
            return {
                "success": False,
                "error": "Failed to decode and download video from sxyprn."
            }

        final_size = out_path.stat().st_size
        if total_bytes and final_size < total_bytes:
            return {
                "success": False,
                "error": f"Incomplete video download: received {final_size/(1024*1024):.1f}MB of {total_bytes/(1024*1024):.1f}MB."
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

    # ------------------------------------------------------------------ #
    # Engine 2: yt-dlp fallback
    # ------------------------------------------------------------------ #

    @staticmethod
    def _ytdlp_download(url: str, task_dir: Path) -> Dict[str, Any]:
        out_template = str(task_dir / "sxyprn_%(id)s.%(ext)s")
        ydl_opts = {
            "format": "bestvideo+bestaudio/best",
            "outtmpl": out_template,
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "cachedir": False,
            "no_cache_dir": True,
            "nopart": True,
            "http_headers": SxyprnDownloader.HEADERS,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get("title", "SXYPRN Video") if info else "SXYPRN Video"

                for f in sorted(task_dir.iterdir()):
                    if f.is_file() and f.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov"):
                        return {"success": True, "path": f, "title": title}
        except Exception as e:
            return {"success": False, "error": str(e)}

        return {"success": False, "error": "yt-dlp could not extract this video."}
