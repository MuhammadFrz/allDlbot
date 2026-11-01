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


class SxyprnDownloader:
    """
    Downloader for sxyprn.com video pages.

    Features:
      1. Native reverse-engineered decoder for sxyprn's client-side
         getvsrc() / data-vnfo video manifest.
      2. Dynamic geo-block bypass (handles US state blocks such as Missouri/Kansas).
      3. Resilient chunk streaming with HTTP Range resumption against proxy disconnects.
      4. yt-dlp fallback if direct extraction fails.
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

        # 2. Direct connection (unrestricted regions)
        candidates.append(None)

        # 3. Dynamic public elite proxies to bypass regional IP blocks
        try:
            r = requests.get(
                "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=3000&anonymity=elite",
                timeout=4,
            )
            for line in r.text.strip().split("\r\n"):
                line = line.strip()
                if line and ":" in line:
                    candidates.append({"http": f"http://{line}", "https": f"http://{line}"})
        except Exception:
            pass

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

        for proxy_dict in proxy_candidates:
            try:
                # 1. Fetch page HTML
                r = requests.get(url, headers=cls.HEADERS, proxies=proxy_dict, timeout=7)
                if r.status_code != 200 or "data-vnfo" not in r.text:
                    continue

                html = r.text

                # 2. Extract title
                m_title = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
                title = "SXYPRN Video"
                if m_title:
                    t = m_title.group(1).strip()
                    for sfx in [" - SXYPRN", " | SXYPRN", " - sxyprn.com", " | sxyprn.com", " - SexyPorn", " | SexyPorn"]:
                        t = t.replace(sfx, "").replace(sfx.lower(), "")
                    title = t.strip() or title

                # 3. Extract data-vnfo
                m_vnfo = re.search(r"data-vnfo='([^']+)'", html)
                if not m_vnfo:
                    m_vnfo = re.search(r'data-vnfo="([^"]+)"', html)
                if not m_vnfo:
                    continue

                vnfo_raw = m_vnfo.group(1).replace("&quot;", '"')
                vnfo_data = json.loads(vnfo_raw)

                # 4. Decode video URL for each stream
                for pid, src in vnfo_data.items():
                    tmp = src.split("/")
                    b_val = cls._boo(cls._ssut51(tmp[6]), cls._ssut51(tmp[7]), "sxyprn.com")
                    tmp[1] += "8/" + b_val
                    tmp = cls._preda(tmp)
                    final_url = "https://sxyprn.com" + "/".join(tmp)

                    # 5. Fetch 302 redirect location
                    r_redir = requests.get(
                        final_url,
                        headers=cls.HEADERS,
                        proxies=proxy_dict,
                        timeout=7,
                        allow_redirects=False,
                    )
                    cdn_url = r_redir.headers.get("Location")
                    if not cdn_url:
                        continue

                    if cdn_url.startswith("//"):
                        cdn_url = "https:" + cdn_url

                    # 6. Stream video from CDN with HTTP Range resumption
                    retries = 0
                    max_retries = 5
                    total_bytes = None

                    while retries < max_retries:
                        current_size = out_path.stat().st_size if out_path.exists() else 0
                        dl_headers = {**cls.HEADERS, "Referer": url}
                        if current_size > 0:
                            dl_headers["Range"] = f"bytes={current_size}-"

                        try:
                            dl = requests.get(
                                cdn_url,
                                headers=dl_headers,
                                proxies=proxy_dict,
                                stream=True,
                                timeout=15,
                            )
                            if dl.status_code not in (200, 206):
                                break

                            if total_bytes is None:
                                cl = dl.headers.get("Content-Length")
                                if cl and dl.status_code == 200:
                                    total_bytes = int(cl)

                            with open(out_path, "ab" if current_size > 0 else "wb") as f:
                                for chunk in dl.iter_content(chunk_size=512 * 1024):
                                    if chunk:
                                        f.write(chunk)

                            # If complete or size is large enough
                            if out_path.exists() and out_path.stat().st_size > 50_000:
                                if total_bytes and out_path.stat().st_size >= total_bytes:
                                    break
                                elif not total_bytes:
                                    break

                        except (requests.exceptions.RequestException, Exception):
                            retries += 1
                            continue

                    if out_path.exists() and out_path.stat().st_size > 50_000:
                        return {
                            "success": True,
                            "path": out_path,
                            "title": title,
                            "video_url": cdn_url,
                        }

            except Exception:
                continue

        return {
            "success": False,
            "error": "Failed to decode and download video from sxyprn.",
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
