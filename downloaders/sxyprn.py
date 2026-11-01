import os
import re
import json
import base64
import asyncio
import requests
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
      2. Dynamic geo-block bypass for the manifest scraper (elite proxy rotation for US state blocks).
      3. Direct high-speed CDN streaming (trafficdeposit.com CDN is unblocked) with HTTP Range resumption.
      4. Exact byte-length verification (prevents truncated uploads).
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
    def _get_proxy_candidates(cls) -> List[Optional[Dict[str, str]]]:
        candidates: List[Optional[Dict[str, str]]] = []

        # 1. User-configured environment proxy
        env_proxy = os.getenv("SXYPRN_PROXY") or os.getenv("PORNHUB_PROXY")
        if env_proxy:
            candidates.append({"http": env_proxy, "https": env_proxy})

        # 2. Dynamic elite proxies from low-latency countries
        try:
            r = requests.get(
                "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=2000&anonymity=elite&country=us,ca,de,fr,gb,nl",
                timeout=4,
            )
            for line in r.text.strip().split("\r\n"):
                line = line.strip()
                if line and ":" in line:
                    candidates.append({"http": f"http://{line}", "https": f"http://{line}"})
        except Exception:
            pass

        # 3. Dynamic generic elite proxies
        try:
            r2 = requests.get(
                "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=2000&anonymity=elite",
                timeout=4,
            )
            for line in r2.text.strip().split("\r\n"):
                line = line.strip()
                if line and ":" in line:
                    p_dict = {"http": f"http://{line}", "https": f"http://{line}"}
                    if p_dict not in candidates:
                        candidates.append(p_dict)
        except Exception:
            pass

        # 4. Direct connection (unrestricted regions)
        candidates.append(None)

        return candidates

    # ------------------------------------------------------------------ #
    # Public entry-point
    # ------------------------------------------------------------------ #

    @staticmethod
    async def download(url: str, task_dir: Optional[Path] = None) -> Dict[str, Any]:
        if task_dir is None:
            task_dir = create_task_dir()

        # Engine 1: Native reverse-engineered decoder + resilient streaming
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
    # Engine 1: Native scraper and downloader
    # ------------------------------------------------------------------ #

    @classmethod
    def _scrape_and_download(cls, url: str, task_dir: Path) -> Dict[str, Any]:
        task_dir.mkdir(parents=True, exist_ok=True)
        out_path = task_dir / "sxyprn_video.mp4"

        proxy_candidates = cls._get_proxy_candidates()

        cdn_url: Optional[str] = None
        title: str = "SXYPRN Video"
        scraping_proxy: Optional[Dict[str, str]] = None

        # Step 1: Scrape post HTML and obtain 302 CDN location
        for p_dict in proxy_candidates:
            try:
                r = requests.get(url, headers=cls.HEADERS, proxies=p_dict, timeout=6)
                if r.status_code != 200 or "data-vnfo" not in r.text:
                    continue

                html = r.text

                # Extract title
                m_title = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
                if m_title:
                    t = m_title.group(1).strip()
                    for sfx in [
                        " - SXYPRN", " | SXYPRN", " - sxyprn.com",
                        " | sxyprn.com", " - SexyPorn", " | SexyPorn"
                    ]:
                        t = t.replace(sfx, "").replace(sfx.lower(), "")
                    title = t.strip() or title

                # Extract data-vnfo
                m_vnfo = re.search(r"data-vnfo='([^']+)'", html)
                if not m_vnfo:
                    m_vnfo = re.search(r'data-vnfo="([^"]+)"', html)
                if not m_vnfo:
                    continue

                vnfo_raw = m_vnfo.group(1).replace("&quot;", '"')
                vnfo_data = json.loads(vnfo_raw)

                # Decode video URL for candidate streams
                for pid, src in vnfo_data.items():
                    tmp = src.split("/")
                    b_val = cls._boo(cls._ssut51(tmp[6]), cls._ssut51(tmp[7]), "sxyprn.com")
                    tmp[1] += "8/" + b_val
                    tmp = cls._preda(tmp)
                    final_url = "https://sxyprn.com" + "/".join(tmp)

                    # Fetch 302 redirect location
                    r_redir = requests.get(
                        final_url,
                        headers=cls.HEADERS,
                        proxies=p_dict,
                        timeout=6,
                        allow_redirects=False,
                    )
                    cdn = r_redir.headers.get("Location")
                    if not cdn:
                        continue

                    if cdn.startswith("//"):
                        cdn = "https:" + cdn

                    cdn_url = cdn
                    scraping_proxy = p_dict
                    break

                if cdn_url:
                    break

            except Exception:
                continue

        if not cdn_url:
            return {
                "success": False,
                "error": "Failed to decode video source from sxyprn page."
            }

        # Step 2: Download video from CDN
        # Direct VPS connection to CDN is fastest and doesn't drop.
        # If direct fails, fall back to scraping proxy, then other candidates.
        dl_proxies = [None]
        if scraping_proxy and scraping_proxy not in dl_proxies:
            dl_proxies.append(scraping_proxy)
        for p in proxy_candidates:
            if p and p not in dl_proxies:
                dl_proxies.append(p)

        total_bytes: Optional[int] = None

        for p_curr in dl_proxies:
            retries = 0
            while retries < 3:
                cur_size = out_path.stat().st_size if out_path.exists() else 0
                if total_bytes and cur_size >= total_bytes:
                    break

                dl_headers = {**cls.HEADERS, "Referer": "https://sxyprn.com/"}
                if cur_size > 0:
                    dl_headers["Range"] = f"bytes={cur_size}-"

                try:
                    resp = requests.get(
                        cdn_url,
                        headers=dl_headers,
                        proxies=p_curr,
                        stream=True,
                        timeout=(10, 30),
                    )

                    if resp.status_code not in (200, 206):
                        retries += 1
                        break

                    # Parse total expected size
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

                    with open(out_path, "ab" if cur_size > 0 else "wb") as f:
                        for chunk in resp.iter_content(chunk_size=512 * 1024):
                            if chunk:
                                f.write(chunk)

                    new_size = out_path.stat().st_size if out_path.exists() else 0
                    if total_bytes and new_size >= total_bytes:
                        break

                    retries += 1

                except Exception:
                    retries += 1
                    continue

            final_cur = out_path.stat().st_size if out_path.exists() else 0
            if total_bytes and final_cur >= total_bytes:
                break

        # Step 3: Validate output file
        if not out_path.exists():
            return {"success": False, "error": "Downloaded video file does not exist."}

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
            "title": title,
            "video_url": cdn_url,
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
