import os
import re
import json
import errno
import time
import queue
import shutil
import socket
import random
import base64
import asyncio
import logging
import itertools
import threading
import requests
import concurrent.futures
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List, Callable

from utils.cleanup import create_task_dir, purge_old_files
from utils.converter import get_video_params
from utils.progress import human_bytes

logger = logging.getLogger("AllDLBot")


class SxyprnDownloader:
    """
    Downloader for sxyprn.com video pages.

    Features:
      1. Reverse-engineered decoder for client-side getvsrc() / data-vnfo video manifest.
      2. Warm-session parallel proxy verification with 64KB throughput benchmarking.
      3. High-speed multi-proxy parallel Range downloader (4MB verified chunks + tail hedging)
         so slow or dropping public proxies no longer stall or fail mid-download.
      4. Proactive server disk space validation and explicit ENOSPC handling.
      5. Exact byte-length verification, FFprobe integrity validation, and cancellation support.
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
    _proxy_tier2_cache: List[str] = []
    _proxy_tier3_cache: List[str] = []
    _proxy_cache_time: float = 0.0
    _good_proxies: List[str] = []
    _warm_proxies: List[str] = []
    _cache_lock = threading.Lock()
    _harvester_started: bool = False
    _harvester_stop = threading.Event()

    CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB per chunk
    NUM_WORKERS = 5
    MAX_CHUNK_SECONDS = 26.0
    MAX_VERIFY_CHUNK_SECONDS = 12.0

    # ------------------------------------------------------------------ #
    # Reverse-engineered decoding primitives (from main2.js)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _ssut51(arg: str) -> int:
        return sum(int(c) for c in re.sub(r"[^0-9]", "", arg))

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

    @staticmethod
    def _proxy_dict(p_str: str) -> Dict[str, str]:
        uri = p_str if "://" in p_str else f"http://{p_str}"
        return {"http": uri, "https": uri}

    @classmethod
    def _mark_good_proxy(cls, p_str: str) -> None:
        if not p_str:
            return
        with cls._cache_lock:
            if p_str in cls._good_proxies:
                cls._good_proxies.remove(p_str)
            cls._good_proxies.insert(0, p_str)
            if len(cls._good_proxies) > 40:
                del cls._good_proxies[40:]
            if p_str in cls._warm_proxies:
                cls._warm_proxies.remove(p_str)
            cls._warm_proxies.insert(0, p_str)
            if len(cls._warm_proxies) > 50:
                del cls._warm_proxies[50:]

    @classmethod
    def _unmark_good_proxy(cls, p_str: str) -> None:
        if not p_str:
            return
        with cls._cache_lock:
            if p_str in cls._good_proxies:
                cls._good_proxies.remove(p_str)

    @classmethod
    def _get_hot_proxies(cls) -> List[str]:
        env_proxy = os.getenv("SXYPRN_PROXY") or os.getenv("PORNHUB_PROXY")
        with cls._cache_lock:
            good = list(cls._good_proxies)
            warm = list(cls._warm_proxies)
        seen = set()
        hot: List[str] = []
        for p in ([env_proxy] if env_proxy else []) + good + warm:
            if p and p not in seen:
                seen.add(p)
                hot.append(p)
        return hot

    @classmethod
    def _tcp_alive(cls, uri: str, timeout: float = 1.3) -> bool:
        try:
            hp = uri.split("://", 1)[1] if "://" in uri else uri
            ip, port = hp.rsplit(":", 1)
            s = socket.create_connection((ip, int(port)), timeout=timeout)
            s.close()
            return True
        except Exception:
            return False

    @classmethod
    def _filter_tcp_alive(cls, uris: List[str], max_workers: int = 300, timeout: float = 1.3) -> List[str]:
        if not uris:
            return []
        alive: List[str] = []
        workers = min(max_workers, len(uris))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(cls._tcp_alive, u, timeout): u for u in uris}
            for f in concurrent.futures.as_completed(futs):
                try:
                    if f.result():
                        alive.append(futs[f])
                except Exception:
                    pass
        return alive

    @classmethod
    def _check_homepage_access(cls, uri: str, timeout: Tuple[float, float] = (2.5, 4.5)) -> bool:
        sess = requests.Session()
        sess.proxies.update(cls._proxy_dict(uri))
        sess.headers.update(cls.HEADERS)
        try:
            r = sess.get("https://sxyprn.com/", timeout=timeout)
            if r.status_code == 200 and "post_text" in r.text and "not available in your region" not in r.text:
                return True
        except Exception:
            pass
        finally:
            sess.close()
        return False

    @classmethod
    def _fetch_proxy_pools(cls, force_refresh: bool = False) -> Tuple[List[str], List[str], List[str]]:
        now = time.time()
        with cls._cache_lock:
            if not force_refresh and cls._proxy_cache and (now - cls._proxy_cache_time) < 300:
                return list(cls._proxy_cache), list(cls._proxy_tier2_cache), list(cls._proxy_tier3_cache)

        # Tier 1: Fast fresh HTTP proxy feeds (port 80 filtered out — reverse proxies break HTTPS CONNECT)
        tier1_http = [
            "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
            "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=2500",
            "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/http.txt",
        ]
        # Tier 2: Large HTTP backup feeds (fetched only if needed; cached alongside Tier 1)
        tier2_http = [
            "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
            "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/http/data.txt",
        ]
        # Tier 3: SOCKS4 fallback feeds
        tier3_socks4 = [
            "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt",
            "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks4&timeout=2500",
        ]

        ip_port_re = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3}:\d{2,5})")

        def fetch_source(scheme: str, src_url: str) -> List[str]:
            found: List[str] = []
            try:
                r = requests.get(src_url, timeout=4)
                if r.status_code == 200:
                    for line in r.text.splitlines():
                        m = ip_port_re.search(line)
                        if not m:
                            continue
                        hp = m.group(1)
                        if scheme == "http" and hp.endswith(":80"):
                            continue
                        found.append(f"{scheme}://{hp}")
            except Exception:
                pass
            return found

        specs = (
            [("http", u) for u in tier1_http]
            + [("http", u) for u in tier2_http]
            + [("socks4", u) for u in tier3_socks4]
        )
        results_by_url: Dict[str, List[str]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(specs)) as ex:
            fut_map = {ex.submit(fetch_source, sch, u): u for sch, u in specs}
            for fut in concurrent.futures.as_completed(fut_map):
                results_by_url[fut_map[fut]] = fut.result()

        seen = set()
        t1: List[str] = []
        for u in tier1_http:
            for p in results_by_url.get(u, []):
                if p not in seen:
                    seen.add(p)
                    t1.append(p)

        t2: List[str] = []
        for u in tier2_http:
            for p in results_by_url.get(u, []):
                if p not in seen:
                    seen.add(p)
                    t2.append(p)

        t3: List[str] = []
        for u in tier3_socks4:
            for p in results_by_url.get(u, []):
                if p not in seen:
                    seen.add(p)
                    t3.append(p)

        with cls._cache_lock:
            cls._proxy_cache = t1
            cls._proxy_tier2_cache = t2
            cls._proxy_tier3_cache = t3
            cls._proxy_cache_time = now

        return t1, t2, t3

    @classmethod
    def _get_proxy_candidates(cls, force_refresh: bool = False) -> List[str]:
        hot = cls._get_hot_proxies()
        seen = set(hot)
        t1, t2, t3 = cls._fetch_proxy_pools(force_refresh=force_refresh)
        ordered = list(hot)
        for pool in (t1, t2, t3):
            shuf = [p for p in pool if p not in seen]
            random.shuffle(shuf)
            ordered.extend(shuf)
            seen.update(shuf)
        return ordered

    @classmethod
    def refresh_warm_proxies(cls) -> int:
        """Background pass to keep a warm RAM pool of unblocked sxyprn.com proxies ready."""
        with cls._cache_lock:
            existing = list(dict.fromkeys(cls._good_proxies + cls._warm_proxies))

        surviving: List[str] = []
        if existing:
            ex_check = concurrent.futures.ThreadPoolExecutor(max_workers=min(30, len(existing)))
            try:
                futs = {ex_check.submit(cls._check_homepage_access, u, (2.5, 4.5)): u for u in existing}
                done, _ = concurrent.futures.wait(futs.keys(), timeout=15.0)
                for f in done:
                    try:
                        if f.result():
                            surviving.append(futs[f])
                    except Exception:
                        pass
            finally:
                ex_check.shutdown(wait=False, cancel_futures=True)

        with cls._cache_lock:
            cls._warm_proxies = surviving[:50]
            cls._good_proxies = [p for p in cls._good_proxies if p in surviving]

        if len(surviving) >= 12:
            return len(surviving)

        t1, t2, _ = cls._fetch_proxy_pools(force_refresh=True)
        already = set(surviving)
        t1_shuf = [p for p in t1 if p not in already]
        random.shuffle(t1_shuf)
        candidates = t1_shuf
        alive = cls._filter_tcp_alive(candidates, max_workers=300, timeout=1.3)

        if len(alive) < 150 and t2:
            t2_shuf = [p for p in t2 if p not in already]
            random.shuffle(t2_shuf)
            alive.extend(cls._filter_tcp_alive(t2_shuf[:500], max_workers=300, timeout=1.3))

        found = list(surviving)
        if alive:
            ex_home = concurrent.futures.ThreadPoolExecutor(max_workers=min(100, len(alive)))
            try:
                futs = {ex_home.submit(cls._check_homepage_access, u, (2.5, 4.5)): u for u in alive}
                done, _ = concurrent.futures.wait(futs.keys(), timeout=25.0)
                for f in done:
                    try:
                        if f.result() and futs[f] not in already:
                            already.add(futs[f])
                            found.append(futs[f])
                    except Exception:
                        pass
            finally:
                ex_home.shutdown(wait=False, cancel_futures=True)

        with cls._cache_lock:
            merged = list(dict.fromkeys(cls._good_proxies + found + cls._warm_proxies))
            cls._warm_proxies = merged[:50]

        logger.info(f"SXYPRN warm proxy pool refreshed: {len(found)} live unblocked proxies")
        return len(found)

    @classmethod
    def start_harvester(cls) -> None:
        """Start daemon background thread that keeps _warm_proxies fresh while bot runs."""
        with cls._cache_lock:
            if cls._harvester_started:
                return
            cls._harvester_started = True

        def _loop():
            while not cls._harvester_stop.is_set():
                try:
                    cls.refresh_warm_proxies()
                except Exception as e:
                    logger.warning(f"SXYPRN harvester error: {e}")
                # Refresh every 10 minutes
                cls._harvester_stop.wait(600.0)

        threading.Thread(target=_loop, name="sxyprn-proxy-harvester", daemon=True).start()

    # ------------------------------------------------------------------ #
    # Public entry-point
    # ------------------------------------------------------------------ #

    @staticmethod
    async def download(
        url: str,
        task_dir: Optional[Path] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
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
                cancel_event,
            )
            if res.get("success") and res.get("path"):
                return res
            if res.get("error"):
                return res
        except Exception as e:
            return {"success": False, "error": f"Download error: {e}"}

        return {
            "success": False,
            "error": "Failed to extract and download video from this page.",
        }

    # ------------------------------------------------------------------ #
    # Warm-Session Proxy Stream Verification
    # ------------------------------------------------------------------ #

    @classmethod
    def _verify_proxy_session(
        cls,
        url: str,
        p_str: str,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> Optional[Tuple[float, str, str, str, Optional[int], requests.Session]]:
        """
        Verify a proxy over a single kept-alive Session and benchmark a 64KB Range read.
        Keeping the Session alive avoids cold-reconnect ProxyErrors on initial download.
        """
        if stop_check and stop_check():
            return None

        p_dict = cls._proxy_dict(p_str)
        sess = requests.Session()
        sess.proxies.update(p_dict)
        sess.headers.update(cls.HEADERS)

        try:
            r_page = sess.get(url, timeout=(3.0, 5.0))
            if (stop_check and stop_check()) or r_page.status_code != 200 or "data-vnfo" not in r_page.text:
                sess.close()
                return None

            m_title = re.search(r"<title[^>]*>([^<]+)</title>", r_page.text, re.I)
            title = "Adult Video"
            if m_title:
                t = m_title.group(1).strip()
                for sfx in [
                    " - SXYPRN", " | SXYPRN", " - sxyprn.com",
                    " | sxyprn.com", " - SexyPorn", " | SexyPorn",
                ]:
                    t = t.replace(sfx, "").replace(sfx.lower(), "")
                title = t.strip() or title

            m_vnfo = re.search(r"data-vnfo='([^']+)'", r_page.text) or re.search(
                r'data-vnfo="([^"]+)"', r_page.text
            )
            if not m_vnfo:
                sess.close()
                return None

            vnfo_data = json.loads(m_vnfo.group(1).replace("&quot;", '"'))
            for _, src in vnfo_data.items():
                if stop_check and stop_check():
                    break
                tmp = src.split("/")
                b_val = cls._boo(cls._ssut51(tmp[6]), cls._ssut51(tmp[7]), "sxyprn.com")
                tmp[1] += "8/" + b_val
                tmp = cls._preda(tmp)
                final_url = "https://sxyprn.com" + "/".join(tmp)

                r_redir = sess.get(
                    final_url,
                    headers={"Referer": url},
                    timeout=(3.0, 5.0),
                    allow_redirects=False,
                )
                cdn = r_redir.headers.get("Location")
                if not cdn:
                    continue
                if cdn.startswith("//"):
                    cdn = "https:" + cdn

                t_chunk = time.time()
                r_test = sess.get(
                    cdn,
                    headers={"Range": "bytes=0-65535", "Referer": url},
                    timeout=(4.0, 8.0),
                    stream=True,
                )
                try:
                    if r_test.status_code != 206:
                        continue
                    cr = r_test.headers.get("Content-Range", "")
                    m_total = re.search(r"/(\d+)", cr)
                    total_bytes = int(m_total.group(1)) if m_total else None
                    expected_verify = min(65536, total_bytes) if total_bytes else 65536

                    t_hdr = time.time()
                    buf = bytearray()
                    for part in r_test.iter_content(chunk_size=16384):
                        if stop_check and stop_check():
                            break
                        if part:
                            buf.extend(part)
                        if len(buf) >= expected_verify or (time.time() - t_chunk) > cls.MAX_VERIFY_CHUNK_SECONDS:
                            break
                    dt = time.time() - t_chunk
                    read_dt = time.time() - t_hdr
                finally:
                    r_test.close()

                if (
                    not (stop_check and stop_check())
                    and len(buf) >= expected_verify
                    and dt <= cls.MAX_VERIFY_CHUNK_SECONDS
                ):
                    cls._mark_good_proxy(p_str)
                    score = read_dt + 0.15 * dt
                    return (score, p_str, cdn, title, total_bytes, sess)
        except Exception:
            pass

        sess.close()
        return None

    # ------------------------------------------------------------------ #
    # Parallel Chunked & Resilient Sequential Engines
    # ------------------------------------------------------------------ #

    @staticmethod
    def _emit_status(cb: Optional[Callable[[str], None]], msg: str):
        if cb:
            try:
                cb(msg)
            except Exception:
                pass

    @classmethod
    def _scrape_and_download(
        cls,
        url: str,
        task_dir: Path,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        task_dir.mkdir(parents=True, exist_ok=True)
        out_path = task_dir / "sxyprn_video.mp4"

        def is_cancelled() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        cls._emit_status(status_callback, "🔍 Finding fast streaming servers...")

        # PriorityQueue items: (dt, tie_seq, p_str, cdn, title, total_bytes, sess)
        verified_pq: queue.PriorityQueue = queue.PriorityQueue()
        seq_counter = itertools.count()
        used_proxies = set()
        used_lock = threading.Lock()
        stop_event = threading.Event()
        producer_exhausted = threading.Event()
        need_proxies_event = threading.Event()
        need_proxies_event.set()

        def producer_loop():
            try:
                stop_fn = lambda: stop_event.is_set() or is_cancelled()
                for refresh_round in range(2):
                    if stop_fn():
                        return
                    candidates = cls._get_proxy_candidates(force_refresh=(refresh_round > 0))
                    with used_lock:
                        batch_list = [p for p in candidates if p not in used_proxies]
                        for p in batch_list:
                            used_proxies.add(p)

                    if not batch_list:
                        continue

                    hot_set = set(cls._get_hot_proxies())
                    hot_batch = [p for p in batch_list if p in hot_set]
                    cold_batch = [p for p in batch_list if p not in hot_set]

                    # Fast TCP pre-filter on cold candidates to avoid wasting worker threads on dead IPs
                    alive_cold = cls._filter_tcp_alive(cold_batch[:400], max_workers=250, timeout=1.2)
                    alive_cold_set = set(alive_cold)
                    remaining_cold = [p for p in cold_batch if p not in alive_cold_set]

                    ordered_candidates = hot_batch + alive_cold + remaining_cold

                    max_workers = 64
                    ex = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
                    pending = set()
                    cand_iter = iter(ordered_candidates)
                    exhausted = False

                    def fill_pool():
                        nonlocal exhausted
                        while len(pending) < max_workers and not exhausted and not stop_fn():
                            if verified_pq.qsize() >= 10:
                                break
                            try:
                                p = next(cand_iter)
                            except StopIteration:
                                exhausted = True
                                break
                            pending.add(ex.submit(cls._verify_proxy_session, url, p, stop_fn))

                    try:
                        while not stop_fn():
                            while (
                                not stop_fn()
                                and verified_pq.qsize() >= 10
                                and not pending
                            ):
                                need_proxies_event.wait(0.5)
                                need_proxies_event.clear()

                            if stop_fn():
                                break

                            fill_pool()
                            if not pending:
                                if exhausted:
                                    break
                                continue

                            done, pending = concurrent.futures.wait(
                                pending,
                                timeout=0.5,
                                return_when=concurrent.futures.FIRST_COMPLETED,
                            )
                            for f in done:
                                try:
                                    res = f.result()
                                except Exception:
                                    res = None
                                if res:
                                    dt, p_str, cdn, title, tot, sess = res
                                    if stop_fn():
                                        sess.close()
                                    else:
                                        verified_pq.put((dt, next(seq_counter), p_str, cdn, title, tot, sess))
                    finally:
                        for f in pending:
                            f.cancel()
                        ex.shutdown(wait=False, cancel_futures=True)
            finally:
                producer_exhausted.set()

        producer_thread = threading.Thread(target=producer_loop, daemon=True)
        producer_thread.start()

        # Wait for the first verified stream
        first_item = None
        wait_deadline = time.time() + 75.0
        while time.time() < wait_deadline:
            if is_cancelled():
                stop_event.set()
                cls._drain_pq(verified_pq)
                return {"success": False, "error": "Download cancelled."}
            try:
                first_item = verified_pq.get(timeout=0.5)
                break
            except queue.Empty:
                if producer_exhausted.is_set():
                    break

        if not first_item:
            stop_event.set()
            cls._drain_pq(verified_pq)
            return {
                "success": False,
                "error": (
                    "Could not connect to streaming servers. "
                    "The post may be deleted, blocked, or temporarily unavailable."
                ),
            }

        _, _, first_proxy, first_cdn, video_title, total_bytes, first_sess = first_item

        # Pre-check server disk space cleanly before writing anything
        free_bytes = shutil.disk_usage(task_dir).free
        needed_bytes = (total_bytes or 0) + (16 * 1024 * 1024)
        if total_bytes and free_bytes < needed_bytes:
            purge_old_files(max_age_seconds=1800)
            free_bytes = shutil.disk_usage(task_dir).free
        if total_bytes and free_bytes < needed_bytes:
            stop_event.set()
            first_sess.close()
            cls._drain_pq(verified_pq)
            return {
                "success": False,
                "error": (
                    f"Insufficient server disk space "
                    f"({human_bytes(total_bytes)} needed, {human_bytes(free_bytes)} free)."
                ),
            }

        size_label = f" ({human_bytes(total_bytes)})" if total_bytes else ""
        cls._emit_status(status_callback, f"⚡ Connected to fast streams{size_label}! Downloading video...")

        try:
            if total_bytes and total_bytes >= cls.CHUNK_SIZE:
                dl_res = cls._download_parallel(
                    url=url,
                    out_path=out_path,
                    total_bytes=total_bytes,
                    first_item=first_item,
                    verified_pq=verified_pq,
                    stop_event=stop_event,
                    producer_exhausted=producer_exhausted,
                    need_proxies_event=need_proxies_event,
                    progress_callback=progress_callback,
                    cancel_event=cancel_event,
                )
            else:
                dl_res = cls._download_sequential(
                    url=url,
                    out_path=out_path,
                    total_bytes=total_bytes,
                    first_item=first_item,
                    verified_pq=verified_pq,
                    stop_event=stop_event,
                    producer_exhausted=producer_exhausted,
                    need_proxies_event=need_proxies_event,
                    progress_callback=progress_callback,
                    cancel_event=cancel_event,
                )
        finally:
            stop_event.set()
            need_proxies_event.set()
            cls._drain_pq(verified_pq)

        if not dl_res.get("success"):
            return dl_res

        if is_cancelled():
            return {"success": False, "error": "Download cancelled."}

        if not out_path.exists() or out_path.stat().st_size == 0:
            return {"success": False, "error": "Failed to download video file."}

        final_size = out_path.stat().st_size
        if total_bytes and final_size < total_bytes:
            return {
                "success": False,
                "error": (
                    f"Incomplete download: received {final_size/(1024*1024):.1f}MB "
                    f"of {total_bytes/(1024*1024):.1f}MB."
                ),
            }

        # Verify playability with ffprobe
        dur, _, _ = get_video_params(out_path)
        if dur <= 0:
            return {"success": False, "error": "Downloaded file is corrupted or not a valid video."}

        return {
            "success": True,
            "path": out_path,
            "title": video_title,
            "video_url": dl_res.get("last_cdn") or first_cdn,
        }

    @staticmethod
    def _drain_pq(pq: queue.PriorityQueue):
        while True:
            try:
                item = pq.get_nowait()
                try:
                    item[6].close()
                except Exception:
                    pass
            except queue.Empty:
                break

    @classmethod
    def _download_parallel(
        cls,
        url: str,
        out_path: Path,
        total_bytes: int,
        first_item: tuple,
        verified_pq: queue.PriorityQueue,
        stop_event: threading.Event,
        producer_exhausted: threading.Event,
        need_proxies_event: threading.Event,
        progress_callback: Optional[Callable[[int, int], None]],
        cancel_event: Optional[threading.Event],
    ) -> Dict[str, Any]:
        with open(out_path, "wb") as f:
            f.truncate(total_bytes)

        chunk_q: queue.Queue = queue.Queue()
        total_chunks = 0
        for idx, start in enumerate(range(0, total_bytes, cls.CHUNK_SIZE)):
            end = min(start + cls.CHUNK_SIZE - 1, total_bytes - 1)
            chunk_q.put((idx, start, end, 0))
            total_chunks += 1

        state_lock = threading.Lock()
        completed_chunks = set()
        in_flight: Dict[int, Tuple[int, int, float]] = {}
        hedged_cids = set()
        written_bytes = 0
        last_progress_time = 0.0
        last_activity_time = time.time()
        fatal_error: Optional[str] = None
        last_cdn = first_item[3]
        done_event = threading.Event()

        def is_stopped() -> bool:
            return stop_event.is_set() or (cancel_event is not None and cancel_event.is_set())

        def worker(initial_item=None):
            nonlocal written_bytes, last_progress_time, last_activity_time, fatal_error, last_cdn
            proxy_item = initial_item
            fails = 0

            try:
                while not is_stopped() and not done_event.is_set():
                    if proxy_item is None:
                        need_proxies_event.set()
                        try:
                            proxy_item = verified_pq.get(timeout=1.0)
                            fails = 0
                        except queue.Empty:
                            with state_lock:
                                if len(completed_chunks) >= total_chunks:
                                    break
                            if producer_exhausted.is_set():
                                break
                            continue

                    _, _, p_str, cdn, _, _, sess = proxy_item
                    is_hedged = False
                    cid = -1
                    start = end = retries = 0

                    try:
                        cid, start, end, retries = chunk_q.get_nowait()
                        with state_lock:
                            if cid in completed_chunks:
                                continue
                            in_flight[cid] = (start, end, time.time())
                    except queue.Empty:
                        with state_lock:
                            if len(completed_chunks) >= total_chunks:
                                break
                            now = time.time()
                            # Speculatively race any tail chunk stuck longer than 8 seconds
                            for active_cid, (a_start, a_end, started_at) in in_flight.items():
                                if (
                                    active_cid not in completed_chunks
                                    and active_cid not in hedged_cids
                                    and (now - started_at) > 8.0
                                ):
                                    cid, start, end = active_cid, a_start, a_end
                                    hedged_cids.add(cid)
                                    is_hedged = True
                                    break
                        if not is_hedged:
                            time.sleep(0.25)
                            continue

                    expected_len = end - start + 1
                    chunk_ok = False
                    try:
                        chunk_t0 = time.time()
                        with sess.get(
                            cdn,
                            headers={"Range": f"bytes={start}-{end}", "Referer": url},
                            stream=True,
                            timeout=(6, 15),
                        ) as r:
                            if r.status_code != 206:
                                raise ValueError(f"Unexpected status {r.status_code}")

                            buf = bytearray()
                            for part in r.iter_content(chunk_size=128 * 1024):
                                if is_stopped() or done_event.is_set():
                                    break
                                if part:
                                    buf.extend(part)
                                    if len(buf) > expected_len:
                                        break
                                if (time.time() - chunk_t0) > cls.MAX_CHUNK_SECONDS and len(buf) < expected_len:
                                    raise TimeoutError("Chunk transfer exceeded speed threshold")

                        if is_stopped() or done_event.is_set():
                            break

                        if len(buf) != expected_len:
                            raise ValueError(f"Short chunk read: {len(buf)} != {expected_len}")

                        with state_lock:
                            if cid not in completed_chunks:
                                try:
                                    with open(out_path, "r+b") as f:
                                        f.seek(start)
                                        f.write(buf)
                                except OSError as disk_err:
                                    if disk_err.errno == errno.ENOSPC:
                                        fatal_error = "Server ran out of disk space during download."
                                    else:
                                        fatal_error = f"Disk write error: {disk_err}"
                                    stop_event.set()
                                    done_event.set()
                                    return

                                completed_chunks.add(cid)
                                written_bytes += expected_len
                                last_cdn = cdn
                                now = time.time()
                                last_activity_time = now
                                if progress_callback and (
                                    (now - last_progress_time) >= 2.5 or written_bytes >= total_bytes
                                ):
                                    last_progress_time = now
                                    try:
                                        progress_callback(written_bytes, total_bytes)
                                    except Exception:
                                        pass
                                if len(completed_chunks) >= total_chunks:
                                    done_event.set()
                            in_flight.pop(cid, None)
                            hedged_cids.discard(cid)

                        chunk_ok = True
                        fails = 0
                    except Exception:
                        with state_lock:
                            if not is_hedged:
                                in_flight.pop(cid, None)
                                if cid not in completed_chunks:
                                    if retries + 1 <= 15:
                                        chunk_q.put((cid, start, end, retries + 1))
                                    else:
                                        fatal_error = "Exceeded retry limit on video segment due to unstable streaming servers."
                                        stop_event.set()
                                        done_event.set()
                            else:
                                hedged_cids.discard(cid)

                        if not chunk_ok and not is_stopped():
                            fails += 1
                            if fails >= 2:
                                cls._unmark_good_proxy(p_str)
                                try:
                                    sess.close()
                                except Exception:
                                    pass
                                proxy_item = None
                                need_proxies_event.set()
            finally:
                if proxy_item is not None:
                    try:
                        proxy_item[6].close()
                    except Exception:
                        pass

        workers = []
        for i in range(cls.NUM_WORKERS):
            t = threading.Thread(target=worker, args=(first_item if i == 0 else None,), daemon=True)
            t.start()
            workers.append(t)

        # Deadlock-free coordinator watchdog loop
        while not done_event.wait(0.5):
            if is_stopped():
                stop_event.set()
                break
            with state_lock:
                if len(completed_chunks) >= total_chunks:
                    done_event.set()
                    break
                idle_secs = time.time() - last_activity_time
            if not any(w.is_alive() for w in workers):
                break
            if idle_secs > 90.0:
                fatal_error = "Download stalled: streaming proxies stopped responding."
                stop_event.set()
                break

        stop_event.set()
        for w in workers:
            w.join(timeout=2.0)

        if cancel_event is not None and cancel_event.is_set():
            return {"success": False, "error": "Download cancelled."}
        if fatal_error:
            return {"success": False, "error": fatal_error}
        return {"success": True, "last_cdn": last_cdn}

    @classmethod
    def _download_sequential(
        cls,
        url: str,
        out_path: Path,
        total_bytes: Optional[int],
        first_item: tuple,
        verified_pq: queue.PriorityQueue,
        stop_event: threading.Event,
        producer_exhausted: threading.Event,
        need_proxies_event: threading.Event,
        progress_callback: Optional[Callable[[int, int], None]],
        cancel_event: Optional[threading.Event],
    ) -> Dict[str, Any]:
        """Fallback resumable single-stream downloader for small or unknown-length videos."""
        proxy_item = first_item
        attempts = 0
        no_progress_attempts = 0
        last_cdn = first_item[3]

        while attempts < 20 and no_progress_attempts < 8:
            if cancel_event is not None and cancel_event.is_set():
                return {"success": False, "error": "Download cancelled."}

            cur_sz = out_path.stat().st_size if out_path.exists() else 0
            if total_bytes and cur_sz >= total_bytes:
                return {"success": True, "last_cdn": last_cdn}

            if proxy_item is None:
                need_proxies_event.set()
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        return {"success": False, "error": "Download cancelled."}
                    try:
                        proxy_item = verified_pq.get(timeout=1.0)
                        break
                    except queue.Empty:
                        if producer_exhausted.is_set():
                            return {
                                "success": False,
                                "error": "No more streaming servers available to finish download.",
                            }

            attempts += 1
            _, _, _, cdn_url, _, verified_total, sess = proxy_item
            last_cdn = cdn_url
            if total_bytes is None and verified_total:
                total_bytes = verified_total

            headers = {"Referer": url}
            if cur_sz > 0:
                headers["Range"] = f"bytes={cur_sz}-"

            bytes_this_try = 0
            try:
                with sess.get(cdn_url, headers=headers, stream=True, timeout=(8, 25)) as resp:
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    if "text/html" in ctype:
                        raise ValueError("Received HTML instead of video stream")

                    if cur_sz > 0 and resp.status_code != 206:
                        raise ValueError(f"Expected 206 for resume, got {resp.status_code}")
                    if cur_sz == 0 and resp.status_code not in (200, 206):
                        raise ValueError(f"Unexpected status {resp.status_code}")

                    if total_bytes is None:
                        cr = resp.headers.get("Content-Range")
                        if cr:
                            m_cr = re.search(r"/(\d+)", cr)
                            if m_cr:
                                total_bytes = int(m_cr.group(1))
                        if total_bytes is None and resp.status_code == 200:
                            cl = resp.headers.get("Content-Length")
                            if cl:
                                total_bytes = int(cl)

                    last_progress_time = 0.0
                    with open(out_path, "ab" if cur_sz > 0 else "wb") as f:
                        for chunk in resp.iter_content(chunk_size=256 * 1024):
                            if cancel_event is not None and cancel_event.is_set():
                                return {"success": False, "error": "Download cancelled."}
                            if chunk:
                                f.write(chunk)
                                bytes_this_try += len(chunk)
                                cur_sz += len(chunk)
                                now = time.time()
                                if progress_callback and (now - last_progress_time) >= 3.0:
                                    last_progress_time = now
                                    try:
                                        progress_callback(cur_sz, total_bytes or cur_sz)
                                    except Exception:
                                        pass

                final_sz = out_path.stat().st_size if out_path.exists() else 0
                if (total_bytes and final_sz >= total_bytes) or (total_bytes is None and final_sz > 0):
                    return {"success": True, "last_cdn": last_cdn}

            except OSError as disk_err:
                if disk_err.errno == errno.ENOSPC:
                    return {"success": False, "error": "Server ran out of disk space during download."}
            except Exception:
                pass
            finally:
                try:
                    sess.close()
                except Exception:
                    pass
                proxy_item = None

            if bytes_this_try == 0:
                no_progress_attempts += 1
            else:
                no_progress_attempts = 0

        return {"success": False, "error": "Failed to complete video stream after multiple resumes."}
