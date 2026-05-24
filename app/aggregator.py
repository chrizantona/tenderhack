from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, wait
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
APP = ROOT / "app"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(APP))

from regions import resolve_city, resolve_location, list_supported_regions
from geo_resolver import resolve_wb_dest

from wb_common import DEFAULT_DEST
from wb_parser import WildberriesClient, WildberriesProductParser, WildberriesSearchParser
from ym_parser import YandexMarketClient, YandexMarketProductParser, YandexMarketSearchParser

from clustering import cluster_products

try:
    from ozon_parser import OzonClient, OzonProductParser, OzonSearchParser
    OZON_PARSER_AVAILABLE = True
except Exception as exc:
    OZON_PARSER_AVAILABLE = False
    OZON_IMPORT_ERROR = repr(exc)

try:
    from ozon_selenium import OzonSeleniumClient, discover_browser_binary, discover_driver_binary
    OZON_SELENIUM_AVAILABLE = True
except Exception as exc:
    OZON_SELENIUM_AVAILABLE = False
    OZON_SELENIUM_IMPORT_ERROR = repr(exc)

try:
    from ozon_camoufox import OzonCamoufoxClient
    OZON_CAMOUFOX_AVAILABLE = True
except Exception as exc:
    OZON_CAMOUFOX_AVAILABLE = False
    OZON_CAMOUFOX_IMPORT_ERROR = repr(exc)

try:
    from other_runet.search_parser import OtherRunetSearchParser
    from other_runet.qwen_client import QwenClient
    from other_runet.qwen_lifecycle import QwenServer, DEFAULT_MODEL_PATH as QWEN_DEFAULT_MODEL_PATH
    OTHER_RUNET_AVAILABLE = True
except Exception as exc:
    OTHER_RUNET_AVAILABLE = False
    OTHER_RUNET_IMPORT_ERROR = repr(exc)


SOURCE_KEYS = ("wildberries", "ozon", "yandex_market", "other_runet")


OZON_PROXY_USER = "dNXQ6p"
OZON_PROXY_PASS = "39TqAh"
OZON_PROXY_SERVERS = (
    "195.208.170.23:8000",
    "195.208.170.209:8000",
    "195.208.171.192:8000",
    "195.208.170.156:8000",
    "195.208.170.151:8000",
    "195.208.170.252:8000",
    "195.208.170.20:8000",
    "195.208.170.194:8000",
    "195.208.170.31:8000",
    "185.126.87.151:8000",
)


def _resolve_ym_proxy() -> str | None:
    """Pick a single HTTP proxy URL for the Yandex Market session.

    Order: explicit YM_PROXY env, then the first entry of OZON_PROXIES so a
    user that already wired up the SOCKS5 bridges gets YM coverage for free.
    Returns None if disabled (YM_PROXY_DISABLE=1) or nothing is configured.
    """
    if (os.environ.get("YM_PROXY_DISABLE") or "").lower() in ("1", "true", "yes"):
        return None
    explicit = (os.environ.get("YM_PROXY") or "").strip()
    if explicit:
        return explicit if "://" in explicit else f"http://{explicit}"
    pool_raw = (os.environ.get("OZON_PROXIES") or "").strip()
    if not pool_raw:
        return None
    first = pool_raw.split(",")[0].strip()
    if not first:
        return None
    return first if "://" in first else f"http://{first}"


def _load_ozon_proxy_pool() -> list[dict]:
    # Explicit opt-out: useful inside RU where the embedded proxy6.net pool
    # adds latency (or just doesn't answer) compared with a direct connection.
    if (os.environ.get("OZON_PROXY_DISABLE") or "").lower() in ("1", "true", "yes"):
        return []
    raw = (os.environ.get("OZON_PROXIES") or "").strip()
    if raw.lower() in ("none", "off", "disable", "disabled"):
        return []
    user = (os.environ.get("OZON_PROXY_USER") or "").strip() or OZON_PROXY_USER
    password = (os.environ.get("OZON_PROXY_PASS") or "").strip() or OZON_PROXY_PASS
    scheme = (os.environ.get("OZON_PROXY_SCHEME") or "http").strip() or "http"
    servers = tuple(token.strip() for token in raw.split(",") if token.strip()) if raw else OZON_PROXY_SERVERS
    pool: list[dict] = []
    for token in servers:
        server = token if "://" in token else f"{scheme}://{token}"
        entry: dict = {"server": server}
        if user:
            entry["username"] = user
        if password:
            entry["password"] = password
        pool.append(entry)
    return pool


class Aggregator:
    def __init__(
        self,
        *,
        timeout: float = 15.0,
        ozon_headless: bool = True,
        ozon_enabled: bool = True,
        ozon_browser_binary: str | None = None,
        ozon_driver_binary: str | None = None,
        ozon_profile_dir: str | None = None,
        ozon_chrome_version: int | None = None,
        wb_default_enrich_limit: int = 10,
        other_runet_enabled: bool = True,
        qwen_enabled: bool = True,
        qwen_model_path: str | None = None,
        qwen_host: str = "127.0.0.1",
        qwen_port: int = 8765,
    ) -> None:
        self.timeout = timeout
        self.ozon_headless = ozon_headless
        self.ozon_browser_binary = ozon_browser_binary or (
            discover_browser_binary() if OZON_SELENIUM_AVAILABLE else None
        )
        self.ozon_driver_binary = ozon_driver_binary or (
            discover_driver_binary() if OZON_SELENIUM_AVAILABLE else None
        )
        self.ozon_profile_dir = ozon_profile_dir
        self.ozon_chrome_version = ozon_chrome_version
        self.wb_default_enrich_limit = max(0, wb_default_enrich_limit)
        self._ozon_disabled_reason = self._resolve_ozon_disabled_reason(ozon_enabled)
        self._ozon_browser_warning = self._resolve_ozon_browser_warning()
        self.ozon_enabled = self._ozon_disabled_reason is None

        # Proxy state must be set BEFORE the WB/YM stack inits — they read
        # _proxy_enabled to decide whether to wire the local RU bridges.
        self._proxy_enabled = True
        self._proxy_lock = threading.Lock()

        # WB stack: curl_cffi impersonate=chrome120 + RU proxy mirrors a real
        # browser. Plain `requests` gets banned within a few calls because the
        # OpenSSL TLS fingerprint is trivial.
        self._init_wb_stack(initial=True)
        # YM tends to 403 from non-RU IPs; same RU bridge used as for Ozon.
        self._init_ym_stack()

        self._ozon_client: Any = None
        self._ozon_init_error: str | None = None
        self._ozon_lock = threading.Lock()
        # Playwright sync API binds a browser to its creator thread; the
        # request-time ThreadPoolExecutor spawns fresh threads per search, so
        # we pin Ozon search through a single dedicated thread. Without this
        # the client is unusable on the 2nd search and incurs a 13s restart.
        self._ozon_search_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="ozon-search"
        )
        self._ozon_circuit_open = False
        self._ozon_circuit_open_at: float = 0.0
        # After this window, auto-reset the breaker and force-recreate the
        # browser. Most "stuck" Ozon errors are queue contention rather than
        # a real Node crash, and re-launching Camoufox clears them.
        self._ozon_circuit_cooldown_seconds = 90.0
        self._ozon_enrich_pool: ThreadPoolExecutor | None = None
        self._ozon_enrich_pool_lock = threading.Lock()
        self._ozon_enrich_thread_state = threading.local()
        self._ozon_enrich_thread_clients: list[Any] = []
        self._ozon_enrich_thread_clients_lock = threading.Lock()
        self._ozon_block_until: float = 0.0
        self._ozon_block_lock = threading.Lock()
        # 8 parallel Camoufox workers. Each is ~150-250MB RAM idle, spikes to
        # 1 CPU core during page render. At peak (limit=10 enrich) the pool
        # consumes ~1.5-2GB / 4-6 cores briefly, then drops to idle.
        self._ozon_enrich_workers = 8
        self._ozon_enrich_stagger_seconds = 0.5
        # No cap by default — enrich every item the user asked for. With 8
        # parallel Camoufox workers, limit=20 finishes in ~40s, limit=30 in
        # ~60s. Set OZON_ENRICH_MAX=N env to clip the eager-enrich window
        # if you prefer base items to appear faster (rest still enrich on
        # modal open via /api/product).
        self._ozon_enrich_default_cap = None
        self._ozon_proxy_pool: list[dict] = _load_ozon_proxy_pool()
        self._ozon_proxy_index_lock = threading.Lock()
        self._ozon_proxy_index = 0

        # ---- other_runet 4th source ----
        self.other_runet_enabled = other_runet_enabled and OTHER_RUNET_AVAILABLE
        self._other_runet_init_error = None if OTHER_RUNET_AVAILABLE else OTHER_RUNET_IMPORT_ERROR
        self.qwen_enabled = qwen_enabled and OTHER_RUNET_AVAILABLE
        self._qwen_model_path = qwen_model_path or str(QWEN_DEFAULT_MODEL_PATH) if OTHER_RUNET_AVAILABLE else None
        self._qwen_host = qwen_host
        self._qwen_port = qwen_port
        self._qwen_server: QwenServer | None = None
        self._qwen_client: QwenClient | None = None
        self.other_runet_search: OtherRunetSearchParser | None = None
        if self.other_runet_enabled:
            self._qwen_client = QwenClient(host=qwen_host, port=qwen_port)
            self.other_runet_search = OtherRunetSearchParser(qwen_client=self._qwen_client)
            if self.qwen_enabled:
                try:
                    log_path = Path(os.environ.get("QWEN_SERVER_LOG", "qwen_server.log"))
                    self._qwen_server = QwenServer(
                        model_path=self._qwen_model_path,
                        host=qwen_host,
                        port=qwen_port,
                        log_path=log_path,
                    )
                    self._qwen_server.start()
                except Exception as exc:
                    self._qwen_server = None
                    self._other_runet_init_error = f"qwen_server_failed: {exc!r}"

    def _init_ym_stack(self) -> None:
        ym_proxy = _resolve_ym_proxy() if self._proxy_enabled else None
        ym_client = YandexMarketClient(proxy_url=ym_proxy)
        ym_product = YandexMarketProductParser(client=ym_client)
        self.ym_search = YandexMarketSearchParser(client=ym_client, product_parser=ym_product)
        self.ym_product = ym_product

    def _init_wb_stack(self, *, initial: bool = False) -> None:
        # Hand WB the full proxy pool when enabled so we can round-robin across
        # IPs. A single proxy hits WB's per-IP rate limit within 2-3 queries.
        wb_proxy_pool: list[str] = []
        if self._proxy_enabled:
            pool_raw = (os.environ.get("OZON_PROXIES") or "").strip()
            if pool_raw:
                wb_proxy_pool = [
                    (entry if "://" in entry else f"http://{entry}")
                    for entry in (e.strip() for e in pool_raw.split(",")) if entry
                ]
        wb_client = WildberriesClient(
            timeout=self.timeout,
            proxy_pool=wb_proxy_pool or None,
        )
        for limiter_name in ("search_limiter", "basket_limiter", "card_limiter"):
            limiter = getattr(wb_client, limiter_name, None)
            if limiter is not None:
                # 5s is enough to let WB cool down between bursts without
                # eating the user's whole task_timeout.
                limiter.backoff_seconds = 5.0
        wb_product = WildberriesProductParser(client=wb_client)
        self.wb_search = WildberriesSearchParser(client=wb_client, product_parser=wb_product)
        self.wb_product = wb_product

    def get_proxy_enabled(self) -> bool:
        return self._proxy_enabled

    def set_proxy_enabled(self, enabled: bool) -> dict[str, Any]:
        """Toggle proxy use at runtime. Rebuilds YM stack and force-resets the
        Ozon Camoufox client so the next search re-launches the browser with
        the new proxy config. The cost: ~13s warmup on next Ozon search.
        """
        with self._proxy_lock:
            if self._proxy_enabled == enabled:
                return {"proxy_enabled": self._proxy_enabled, "changed": False}
            self._proxy_enabled = enabled
            # Rebuild YM + WB clients with new proxy setting.
            self._init_ym_stack()
            self._init_wb_stack()
            # Force Ozon to reinit on next call (must close from same thread).
            self._reset_ozon_client_for_proxy_swap()
            # Tell filters_service to drop its cached YM filter client.
            try:
                from filters_service import on_proxy_state_changed
                on_proxy_state_changed(enabled)
            except Exception:
                pass
            return {"proxy_enabled": self._proxy_enabled, "changed": True}

    def _reset_ozon_client_for_proxy_swap(self) -> None:
        def _close() -> None:
            client = self._ozon_client
            self._ozon_client = None
            self._ozon_init_error = None
            self._ozon_circuit_open = False
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            # Also shut down enrichment-thread clients (each owns its browser).
            with self._ozon_enrich_thread_clients_lock:
                pool_clients = list(self._ozon_enrich_thread_clients)
                self._ozon_enrich_thread_clients.clear()
            for pc in pool_clients:
                try:
                    pc.close()
                except Exception:
                    pass
        # Camoufox must be closed from its creator thread; route through the
        # same dedicated executor that owns it.
        try:
            self._ozon_search_executor.submit(_close).result(timeout=15)
        except Exception:
            pass

    def health(self) -> dict[str, Any]:
        qwen_ok = bool(self._qwen_client and self._qwen_client.is_available())
        return {
            "status": "ok",
            "sources": list(SOURCE_KEYS),
            "regions": list_supported_regions(),
            "ozon_enabled": self.ozon_enabled,
            "ozon_import_error": None if OZON_PARSER_AVAILABLE else OZON_IMPORT_ERROR,
            "ozon_selenium_error": None if OZON_SELENIUM_AVAILABLE else OZON_SELENIUM_IMPORT_ERROR,
            "ozon_browser_binary": self.ozon_browser_binary,
            "ozon_driver_binary": self.ozon_driver_binary,
            "ozon_disabled_reason": self._ozon_disabled_reason,
            "ozon_browser_warning": self._ozon_browser_warning,
            "proxy_enabled": self._proxy_enabled,
            "other_runet_enabled": self.other_runet_enabled,
            "other_runet_error": self._other_runet_init_error,
            "qwen_enabled": self.qwen_enabled,
            "qwen_available": qwen_ok,
            "qwen_model_path": self._qwen_model_path,
            "qwen_host": f"{self._qwen_host}:{self._qwen_port}",
        }

    def search(
        self,
        query: str,
        *,
        region: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        limit: int = 10,
        sources: list[str] | None = None,
        enrich: bool = False,
        enrich_limit: int | None = None,
        task_timeout: float = 20.0,
        filter_specs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        result = None
        for event in self.search_events(
            query,
            region=region,
            latitude=latitude,
            longitude=longitude,
            limit=limit,
            sources=sources,
            enrich=enrich,
            enrich_limit=enrich_limit,
            task_timeout=task_timeout,
            filter_specs=filter_specs,
        ):
            if event.get("event") == "done":
                result = event.get("data")
        if result is None:
            raise RuntimeError("search did not produce a final result")
        return result

    def search_events(
        self,
        query: str,
        *,
        region: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        limit: int = 10,
        sources: list[str] | None = None,
        enrich: bool = False,
        enrich_limit: int | None = None,
        task_timeout: float = 20.0,
        filter_specs: list[dict[str, Any]] | None = None,
    ):
        targets = self._select_sources(sources)
        region_key, region_info, region_diag = resolve_location(
            region,
            latitude=latitude,
            longitude=longitude,
        )
        started_at = time.time()

        by_source: dict[str, dict[str, Any]] = {}
        diagnostics: dict[str, Any] = {}
        if region_diag:
            diagnostics["region_resolution"] = region_diag

        yield {
            "event": "start",
            "data": {
                "schema_version": "0.2",
                "query": query,
                "region": region_info.get("name"),
                "region_key": region_key,
                "region_label": region_info.get("region_label"),
                "latitude": region_info.get("requested_latitude"),
                "longitude": region_info.get("requested_longitude"),
                "sources": targets,
                "diagnostics": diagnostics,
                "limit": limit,
            },
        }

        per_source_filters = _translate_filter_specs(filter_specs or [])
        post_filter = _build_post_filter(filter_specs or [])
        # Streamed products cache per source so a timeout doesn't erase what
        # the source has already emitted via item_callback.
        streamed: dict[str, dict[int, dict[str, Any]]] = {name: {} for name in targets}

        product_events: Queue[dict[str, Any]] = Queue()
        pool = ThreadPoolExecutor(max_workers=max(1, len(targets)))
        try:
            futures = {}
            for name in targets:
                fn = self._dispatcher(name)
                if fn is None:
                    payload = {"status": "skipped", "count": 0, "products": [], "diagnostics": ["unknown_source"]}
                    by_source[name] = payload
                    yield self._source_event(name, payload, by_source, targets, started_at)
                    continue
                source_predicate = (post_filter or {}).get(name) if post_filter else None
                future = pool.submit(
                    fn,
                    query,
                    region_key,
                    region_info,
                    limit,
                    enrich,
                    enrich_limit,
                    self._product_callback(
                        name, product_events, started_at, source_predicate, streamed[name]
                    ),
                    per_source_filters.get(name),
                )
                futures[future] = name
                yield {
                    "event": "source_start",
                    "data": {
                        "source": name,
                        "took_seconds": round(time.time() - started_at, 3),
                    },
                }

            pending = set(futures)
            deadline = time.time() + task_timeout
            last_heartbeat_at = 0.0

            while pending:
                while True:
                    try:
                        yield product_events.get_nowait()
                    except Empty:
                        break

                remaining = deadline - time.time()
                if remaining <= 0:
                    for future in list(pending):
                        name = futures[future]
                        cached = streamed.get(name, {})
                        partial = [cached[idx] for idx in sorted(cached)]
                        payload = {
                            "status": "timeout" if not partial else "partial",
                            "count": len(partial),
                            "products": partial,
                            "error": f"source exceeded {task_timeout}s budget",
                        }
                        by_source[name] = payload
                        future.cancel()
                        yield self._source_event(name, payload, by_source, targets, started_at)
                    pending.clear()
                    break

                done, pending = wait(
                    pending,
                    timeout=min(0.2, remaining),
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    now = time.time()
                    if now - last_heartbeat_at >= 1.0:
                        yield {
                            "event": "heartbeat",
                            "data": {
                                "completed": len(by_source),
                                "pending": [futures[future] for future in pending],
                                "took_seconds": round(now - started_at, 3),
                            },
                        }
                        last_heartbeat_at = now
                    continue

                while True:
                    try:
                        yield product_events.get_nowait()
                    except Empty:
                        break

                for future in done:
                    name = futures[future]
                    try:
                        payload = future.result()
                    except Exception as exc:
                        payload = {
                            "status": "error",
                            "count": 0,
                            "products": [],
                            "error": repr(exc),
                            "traceback": traceback.format_exc(limit=4),
                        }
                    src_pred = (post_filter or {}).get(name) if post_filter else None
                    if src_pred and payload.get("products"):
                        before = len(payload["products"])
                        payload["products"] = [p for p in payload["products"] if src_pred(p)]
                        payload["count"] = len(payload["products"])
                        diags = list(payload.get("diagnostics") or [])
                        diags.append(f"post_filter_kept={payload['count']}_of_{before}")
                        payload["diagnostics"] = diags
                        if not payload["products"] and payload.get("status") == "ok":
                            payload["status"] = "empty"
                    by_source[name] = payload
                    yield self._source_event(name, payload, by_source, targets, started_at)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        while True:
            try:
                yield product_events.get_nowait()
            except Empty:
                break

        for name in targets:
            if name not in by_source:
                cached = streamed.get(name, {})
                partial = [cached[idx] for idx in sorted(cached)]
                payload = {
                    "status": "timeout" if not partial else "partial",
                    "count": len(partial),
                    "products": partial,
                    "error": f"source exceeded {task_timeout}s budget",
                }
                by_source[name] = payload
                yield self._source_event(name, payload, by_source, targets, started_at)

        clusters_payload, clusters_diag = _compute_clusters(by_source, query)
        if clusters_diag:
            diagnostics["clusters"] = clusters_diag

        yield {
            "event": "done",
            "data": {
                "schema_version": "0.2",
                "query": query,
                "query_corrected": None,
                "synonyms_applied": [],
                "region": region_info.get("name"),
                "region_key": region_key,
                "region_label": region_info.get("region_label"),
                "latitude": region_info.get("requested_latitude"),
                "longitude": region_info.get("requested_longitude"),
                "sources": targets,
                "by_source": by_source,
                "clusters": clusters_payload,
                "diagnostics": diagnostics,
                "took_seconds": round(time.time() - started_at, 3),
            },
        }

    def _product_callback(
        self,
        source: str,
        product_events: Queue[dict[str, Any]],
        started_at: float,
        post_filter: Callable[[dict[str, Any]], bool] | None = None,
        cache: dict[int, dict[str, Any]] | None = None,
    ) -> Callable[[dict[str, Any], int], None]:
        def emit(product: dict[str, Any], index: int) -> None:
            if post_filter is not None and not post_filter(product):
                return
            if cache is not None:
                # Each enrichment phase re-emits with the same index; latest wins.
                cache[index] = product
            product_events.put(
                {
                    "event": "product",
                    "data": {
                        "source": source,
                        "product": product,
                        "index": index,
                        "count": index + 1,
                        "took_seconds": round(time.time() - started_at, 3),
                    },
                }
            )

        return emit

    def _source_event(
        self,
        source: str,
        payload: dict[str, Any],
        by_source: dict[str, dict[str, Any]],
        targets: list[str],
        started_at: float,
    ) -> dict[str, Any]:
        return {
            "event": "source_result",
            "data": {
                "source": source,
                "payload": payload,
                "completed": len(by_source),
                "total_sources": len(targets),
                "took_seconds": round(time.time() - started_at, 3),
            },
        }

    def _select_sources(self, sources: list[str] | None) -> list[str]:
        if not sources:
            return list(SOURCE_KEYS)
        return [s for s in sources if s in SOURCE_KEYS]

    def _dispatcher(self, name: str):
        return {
            "wildberries": self._search_wildberries,
            "ozon": self._search_ozon,
            "yandex_market": self._search_yandex_market,
            "other_runet": self._search_other_runet,
        }.get(name)

    def _search_wildberries(
        self,
        query: str,
        region_key: str,
        region_info: dict[str, Any],
        limit: int,
        enrich: bool,
        enrich_limit: int | None,
        item_callback: Callable[[dict[str, Any], int], None] | None = None,
        source_filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Translate the chronic 429 cascade (all 3 WB endpoints rate-limited)
        # into a Camoufox SERP-HTML fallback. If even Camoufox fails, surface
        # a clear short error instead of a 60s task_timeout.
        rate_limited = False
        try:
            result = self._search_wildberries_inner(
                query, region_key, region_info, limit, enrich, enrich_limit, item_callback, source_filter,
            )
            if result.get("status") == "ok" and (result.get("count") or 0) > 0:
                return result
        except Exception as exc:
            msg = repr(exc)
            rate_limited = "429" in msg or "Too Many Requests" in msg
            if not rate_limited:
                raise
        # Either rate-limited or zero results → try Camoufox SERP HTML fallback.
        fallback = self._search_wildberries_camoufox_fallback(
            query, region_info, limit, item_callback
        )
        if fallback.get("count"):
            return fallback
        return {
            "status": "error",
            "count": 0,
            "products": [],
            "error": "wb_rate_limited: WB временно режет наш IP (HTTP 429) и Camoufox-фолбэк тоже пуст. Подожди 2-3 мин и повтори.",
        }

    def _search_wildberries_camoufox_fallback(
        self,
        query: str,
        region_info: dict[str, Any],
        limit: int,
        item_callback: Callable[[dict[str, Any], int], None] | None,
    ) -> dict[str, Any]:
        """Camoufox-driven WB SERP scrape when the JSON API is blocked.

        Loads `wildberries.ru/catalog/0/search.aspx?search=Q` in headless
        Firefox, waits for the `.product-card` tiles to render, then parses
        the visible cards via filters_service._WBFilterRunner.fetch_serp_html.
        Emits each parsed product through item_callback in order so the UI
        stream still shows progressive cards.
        """
        from urllib.parse import quote_plus
        try:
            from filters_service import _get_wb_filter_runner
            from wb_serp_html import parse_wb_serp_html
        except Exception as exc:
            return {
                "status": "error",
                "count": 0,
                "products": [],
                "error": f"camoufox_fallback_import_failed: {exc!r}",
            }
        runner = _get_wb_filter_runner()
        if runner is None:
            return {
                "status": "error",
                "count": 0,
                "products": [],
                "error": "camoufox_fallback_unavailable",
            }
        started = time.time()
        url = f"https://www.wildberries.ru/catalog/0/search.aspx?search={quote_plus(query)}"
        try:
            html = runner.fetch_serp_html(url, timeout=60)
        except Exception as exc:
            return {
                "status": "error",
                "count": 0,
                "products": [],
                "error": f"camoufox_fallback_fetch_failed: {exc!r}",
            }
        products = parse_wb_serp_html(
            html or "",
            query=query,
            region_name=region_info.get("region_label") or "RU/Moscow",
        )[:limit]
        # Stream each parsed card so the UI gets progressive emissions.
        if item_callback is not None and products:
            for idx, p in enumerate(products):
                item_callback(p, idx)
                # 100ms stagger so cards visibly appear one by one.
                if idx < len(products) - 1:
                    time.sleep(0.1)
        return {
            "status": "ok" if products else "empty",
            "count": len(products),
            "products": products,
            "diagnostics": [{"stage": "camoufox_serp_fallback", "message": f"parsed {len(products)} cards"}],
            "took_seconds": round(time.time() - started, 3),
        }

    def _search_wildberries_inner(
        self,
        query: str,
        region_key: str,
        region_info: dict[str, Any],
        limit: int,
        enrich: bool,
        enrich_limit: int | None,
        item_callback: Callable[[dict[str, Any], int], None] | None = None,
        source_filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.time()
        wb_enrich = enrich or self.wb_default_enrich_limit > 0
        wb_enrich_limit = enrich_limit if enrich_limit is not None else min(limit, self.wb_default_enrich_limit)
        wb_dest = region_info.get("wb_dest") or DEFAULT_DEST
        latitude = region_info.get("requested_latitude")
        longitude = region_info.get("requested_longitude")
        if latitude is not None and longitude is not None:
            try:
                dynamic_dest = resolve_wb_dest(float(latitude), float(longitude))
            except Exception:
                dynamic_dest = None
            if dynamic_dest:
                wb_dest = dynamic_dest
        extra_params = (source_filter or {}).get("extra_params") if source_filter else None
        result = self.wb_search.search(
            query,
            page=1,
            limit=limit,
            sort="popular",
            dest=wb_dest,
            region_name=region_info.get("region_label") or "RU/Moscow",
            enrich=wb_enrich,
            enrich_limit=wb_enrich_limit,
            item_callback=item_callback,
            extra_params=extra_params,
        )
        products = result.get("items") or result.get("products") or []
        return {
            "status": "ok" if products else "empty",
            "count": len(products),
            "products": products,
            "diagnostics": (result.get("source_meta") or {}).get("diagnostics") or [],
            "took_seconds": round(time.time() - started, 3),
        }

    def _search_yandex_market(
        self,
        query: str,
        region_key: str,
        region_info: dict[str, Any],
        limit: int,
        enrich: bool,
        enrich_limit: int | None,
        item_callback: Callable[[dict[str, Any], int], None] | None = None,
        source_filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.time()
        extra_query = (source_filter or {}).get("extra_query") if source_filter else None
        result = self.ym_search.search(
            query,
            page=1,
            limit=limit,
            region_name=region_info.get("region_label") or "RU/Moscow",
            region_id=region_info.get("ym_region_id"),
            enrich=enrich,
            enrich_limit=enrich_limit,
            item_callback=item_callback,
            extra_query=extra_query,
        )
        products = result.get("items") or result.get("products") or []
        return {
            "status": "ok" if products else "empty",
            "count": len(products),
            "products": products,
            "diagnostics": (result.get("source_meta") or {}).get("diagnostics") or [],
            "took_seconds": round(time.time() - started, 3),
        }

    def _search_ozon(
        self,
        query: str,
        region_key: str,
        region_info: dict[str, Any],
        limit: int,
        enrich: bool,
        enrich_limit: int | None,
        item_callback: Callable[[dict[str, Any], int], None] | None = None,
        source_filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Circuit breaker: when the Playwright Node bridge crashes (see
        # PAGE_LOAD_TIMEOUT_MS comment in ozon_camoufox.py), the browser stops
        # responding and the dedicated thread blocks. After the first hard
        # failure we mark Ozon as broken so the user can still get WB/YM
        # results without burning the whole task_timeout on dead Camoufox.
        if self._ozon_circuit_open:
            elapsed = time.time() - self._ozon_circuit_open_at
            if elapsed >= self._ozon_circuit_cooldown_seconds:
                # Cool-down elapsed — recreate the browser and clear the flag.
                # If it crashes again on the next call, breaker re-opens.
                self._reset_ozon_client_for_proxy_swap()
                self._ozon_circuit_open = False
            else:
                wait_left = int(self._ozon_circuit_cooldown_seconds - elapsed)
                return {
                    "status": "error",
                    "count": 0,
                    "products": [],
                    "error": f"ozon_cooldown: Camoufox перегрелся, ждём ~{wait_left}с. Дальше — авто-restart браузера на следующем поиске.",
                }
        # Marshal through the single Ozon-dedicated thread so the Camoufox
        # browser stays attached to its creator (see __init__ note).
        future = self._ozon_search_executor.submit(
            self._search_ozon_inner,
            query,
            region_key,
            region_info,
            limit,
            enrich,
            enrich_limit,
            item_callback,
            source_filter,
        )
        # Per-call wall-clock guard. A crashed Camoufox makes page.goto hang
        # past its declared timeout, so we cap it from the caller side too.
        try:
            # 180s caller-side cap. With limit=20 enrich-all via 8 parallel
            # workers (~10s/item now that wait_for_tiles is skipped for
            # product pages), wall time is ~30-40s. Larger limits or
            # transient slowness still fit. Real Node-crash hangs >180s.
            return future.result(timeout=180.0)
        except FuturesTimeoutError:
            self._ozon_circuit_open = True
            self._ozon_circuit_open_at = time.time()
            return {
                "status": "error",
                "count": 0,
                "products": [],
                "error": f"ozon_timeout: Camoufox завис >180с. Автоматически перезапустим через ~{int(self._ozon_circuit_cooldown_seconds)}с.",
            }

    def _search_ozon_inner(
        self,
        query: str,
        region_key: str,
        region_info: dict[str, Any],
        limit: int,
        enrich: bool,
        enrich_limit: int | None,
        item_callback: Callable[[dict[str, Any], int], None] | None = None,
        source_filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.ozon_enabled:
            reason = self._ozon_disabled_reason or "ozon_disabled"
            return {"status": "disabled", "count": 0, "products": [], "diagnostics": [reason]}
        # NOTE: Ozon filter application is not wired through OzonSearchParser yet;
        # diagnostics will surface that selections were received but not applied.
        ozon_filter_note = (source_filter or {}).get("note") if source_filter else None

        with self._ozon_lock:
            started = time.time()
            client = self._ensure_ozon_client()
            if client is None:
                return {
                    "status": "error",
                    "count": 0,
                    "products": [],
                    "error": self._ozon_init_error or "ozon_init_failed",
                }
            browser_client = (OZON_CAMOUFOX_AVAILABLE and isinstance(client, OzonCamoufoxClient)) or (
                OZON_SELENIUM_AVAILABLE and isinstance(client, OzonSeleniumClient)
            )
            if browser_client:
                # Each Ozon enrichment opens a separate browser page (~5-15s).
                # OZON_ENRICH_MAX clips the eager-enrich window. When unset
                # (and _ozon_enrich_default_cap is None), enrich every item
                # the user asked for — the 8-worker pool keeps wall time
                # reasonable (~30-60s for limit=20-30).
                env_cap = os.environ.get("OZON_ENRICH_MAX")
                if env_cap:
                    cap = int(env_cap)
                elif self._ozon_enrich_default_cap is not None:
                    cap = int(self._ozon_enrich_default_cap)
                else:
                    cap = limit  # no clipping
                requested = enrich_limit if enrich_limit is not None else limit
                effective_enrich = enrich and requested > 0
                effective_enrich_limit = min(max(0, requested), limit, cap)
            else:
                effective_enrich = enrich
                effective_enrich_limit = enrich_limit
            try:
                parser = OzonSearchParser(client=client)
                if browser_client and effective_enrich_limit and effective_enrich_limit > 1:
                    enrich_executor = self._get_ozon_enrich_executor()
                    enrich_factory = self._ozon_enrich_client_factory
                    enrich_block_check = self._ozon_block_active
                    enrich_block_trigger = self._ozon_trigger_block
                    enrich_stagger = self._ozon_enrich_stagger_seconds
                else:
                    enrich_executor = None
                    enrich_factory = None
                    enrich_block_check = None
                    enrich_block_trigger = None
                    enrich_stagger = 0.0
                result = parser.search(
                    query,
                    page=1,
                    limit=limit,
                    region_name=region_info.get("region_label") or "RU/Moscow",
                    enrich=effective_enrich,
                    enrich_limit=effective_enrich_limit,
                    item_callback=item_callback,
                    enrich_executor=enrich_executor,
                    enrich_client_factory=enrich_factory,
                    enrich_block_check=enrich_block_check,
                    enrich_block_trigger=enrich_block_trigger,
                    enrich_stagger_seconds=enrich_stagger,
                )
            except Exception as exc:
                return {
                    "status": "error",
                    "count": 0,
                    "products": [],
                    "error": repr(exc),
                    "traceback": traceback.format_exc(limit=4),
                }
            products = result.get("items") or result.get("products") or []
            diags = list((result.get("source_meta") or {}).get("diagnostics") or [])
            if ozon_filter_note:
                diags.append(ozon_filter_note)
            return {
                "status": "ok" if products else "empty",
                "count": len(products),
                "products": products,
                "diagnostics": diags,
                "took_seconds": round(time.time() - started, 3),
            }

    def _search_other_runet(
        self,
        query: str,
        region_key: str,
        region_info: dict[str, Any],
        limit: int,
        enrich: bool,
        enrich_limit: int | None,
        item_callback: Callable[[dict[str, Any], int], None] | None = None,
        source_filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.other_runet_enabled or self.other_runet_search is None:
            reason = self._other_runet_init_error or "other_runet_disabled"
            return {"status": "disabled", "count": 0, "products": [], "diagnostics": [reason]}
        started = time.time()
        try:
            result = self.other_runet_search.search(
                query,
                region_name=region_info.get("region_label") or "RU/Moscow",
                limit=limit,
                item_callback=item_callback,
                max_discovery_candidates=25,
                max_live_candidates=10,
                max_per_domain=1,
                time_budget=180.0,
            )
        except Exception as exc:
            return {
                "status": "error",
                "count": 0,
                "products": [],
                "error": repr(exc),
                "traceback": traceback.format_exc(limit=4),
            }
        return {
            "status": result.get("status", "ok"),
            "count": result.get("count", 0),
            "products": result.get("products") or [],
            "diagnostics": result.get("diagnostics") or [],
            "stats": result.get("stats") or {},
            "took_seconds": round(time.time() - started, 3),
        }

    def _get_ozon_enrich_executor(self) -> ThreadPoolExecutor:
        if self._ozon_enrich_pool is None:
            with self._ozon_enrich_pool_lock:
                if self._ozon_enrich_pool is None:
                    self._ozon_enrich_pool = ThreadPoolExecutor(
                        max_workers=self._ozon_enrich_workers,
                        thread_name_prefix="ozon-enrich",
                    )
        return self._ozon_enrich_pool

    def _ozon_enrich_client_factory(self) -> Any:
        client = getattr(self._ozon_enrich_thread_state, "client", None)
        if client is not None:
            return client
        if OZON_CAMOUFOX_AVAILABLE:
            proxy_subpool = self._take_proxy_chunk(2) if self._proxy_enabled else []
            client = OzonCamoufoxClient(
                headless=self.ozon_headless,
                warmup=True,
                proxy=proxy_subpool or None,
            )
        else:
            client = OzonClient(timeout=self.timeout, warmup=True)
        self._ozon_enrich_thread_state.client = client
        with self._ozon_enrich_thread_clients_lock:
            self._ozon_enrich_thread_clients.append(client)
        return client

    def _take_proxy_chunk(self, size: int) -> list[dict]:
        if not self._ozon_proxy_pool:
            return []
        with self._ozon_proxy_index_lock:
            chunk: list[dict] = []
            for _ in range(size):
                proxy = self._ozon_proxy_pool[self._ozon_proxy_index % len(self._ozon_proxy_pool)]
                self._ozon_proxy_index += 1
                chunk.append(proxy)
        return chunk

    def _ozon_block_active(self) -> bool:
        with self._ozon_block_lock:
            return time.monotonic() < self._ozon_block_until

    def _ozon_trigger_block(self, seconds: float) -> None:
        with self._ozon_block_lock:
            self._ozon_block_until = max(
                self._ozon_block_until, time.monotonic() + seconds
            )

    def _ensure_ozon_client(self):
        if self._ozon_client is not None:
            return self._ozon_client
        if self._ozon_init_error is not None:
            return None
        errors: list[str] = []
        if OZON_CAMOUFOX_AVAILABLE:
            try:
                main_proxies = self._take_proxy_chunk(1) if self._proxy_enabled else []
                self._ozon_client = OzonCamoufoxClient(
                    headless=self.ozon_headless,
                    warmup=True,
                    proxy=main_proxies or None,
                )
                return self._ozon_client
            except Exception as exc:
                errors.append(f"camoufox: {exc!r}")
        try:
            self._ozon_client = OzonClient(timeout=self.timeout, warmup=True)
            return self._ozon_client
        except Exception as exc:
            errors.append(f"curl_cffi: {exc!r}")
        if OZON_SELENIUM_AVAILABLE:
            try:
                self._ozon_client = OzonSeleniumClient(
                    headless=self.ozon_headless,
                    profile_dir=self.ozon_profile_dir,
                    version_main=self.ozon_chrome_version,
                    browser_binary=self.ozon_browser_binary,
                    driver_binary=self.ozon_driver_binary,
                )
                return self._ozon_client
            except Exception as exc:
                errors.append(f"selenium: {exc!r}")
        self._ozon_init_error = "ozon_init_failed: " + (" | ".join(errors) if errors else "no engine available")
        return None

    def get_ozon_client(self):
        if not self.ozon_enabled:
            return None
        with self._ozon_lock:
            return self._ensure_ozon_client()

    def get_product(
        self,
        source: str,
        url_or_id: str,
        *,
        region: str | None = None,
    ) -> dict[str, Any]:
        region_key, region_info, _ = resolve_city(region)
        if source == "wildberries":
            return self.wb_product.get_product(
                url_or_id,
                dest=region_info.get("wb_dest") or DEFAULT_DEST,
                region_name=region_info.get("region_label") or "RU/Moscow",
            )
        if source == "yandex_market":
            return self.ym_product.get_product(
                url_or_id,
                region_name=region_info.get("region_label") or "RU/Moscow",
                region_id=region_info.get("ym_region_id"),
            )
        if source == "ozon":
            if not self.ozon_enabled:
                return {"error": "ozon_disabled"}
            # Use the 8-worker enrichment pool, NOT the single-thread search
            # executor. The search executor handles one item at a time — if
            # the user clicks several modals in a row, each /api/product
            # blocks the next search for ~12s × N. Routing through the
            # enrich pool gives 8 parallel lanes (each lane owns its own
            # Camoufox via thread-local state, satisfying Playwright's
            # thread-affinity rule).
            executor = self._get_ozon_enrich_executor()
            future = executor.submit(self._get_ozon_product_inner, url_or_id, region_info)
            try:
                return future.result(timeout=45.0)
            except FuturesTimeoutError:
                return {
                    "error": "ozon_product_timeout: Camoufox >45с. Скорее всего Ozon ограничил доступ или Camoufox перегружен — попробуй позже.",
                }
        raise ValueError(f"unknown_source={source!r}")

    def _get_ozon_product_inner(self, url_or_id: str, region_info: dict[str, Any]) -> dict[str, Any]:
        # Runs inside an ozon-enrich worker thread; each thread owns its own
        # Camoufox via _ozon_enrich_client_factory (thread-local). No lock
        # needed because per-thread state never overlaps between workers.
        try:
            client = self._ozon_enrich_client_factory()
        except Exception as exc:
            return {"error": f"ozon_enrich_client_init_failed: {exc!r}"}
        if client is None:
            return {"error": "ozon_enrich_client_unavailable"}
        # Single layout fetch (~10s) instead of 3 (~30s). The main page
        # already carries title/price/description/attributes.
        try:
            return OzonProductParser(client=client).get_product(
                url_or_id,
                region_name=region_info.get("region_label") or "RU/Moscow",
                include_extra_layouts=False,
            )
        except Exception as exc:
            return {"error": f"ozon_product_fetch_failed: {exc!r}"}

    def _resolve_ozon_disabled_reason(self, ozon_requested: bool) -> str | None:
        if not ozon_requested:
            return "ozon_disabled_by_flag"
        if not OZON_PARSER_AVAILABLE:
            return f"ozon_parser_import_error: {OZON_IMPORT_ERROR}"
        if not OZON_SELENIUM_AVAILABLE:
            return f"ozon_selenium_import_error: {OZON_SELENIUM_IMPORT_ERROR}"
        return None

    def _resolve_ozon_browser_warning(self) -> str | None:
        if not OZON_SELENIUM_AVAILABLE:
            return None
        if not self.ozon_browser_binary:
            return "ozon_browser_binary_not_found: install Google Chrome/Chromium or pass --ozon-browser-binary"
        return None

    def close(self) -> None:
        try:
            self._ozon_search_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        if self._ozon_enrich_pool is not None:
            try:
                self._ozon_enrich_pool.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            self._ozon_enrich_pool = None
        with self._ozon_enrich_thread_clients_lock:
            pool_clients = list(self._ozon_enrich_thread_clients)
            self._ozon_enrich_thread_clients.clear()
        for pool_client in pool_clients:
            try:
                pool_client.close()
            except Exception:
                pass
        if self._ozon_client is not None:
            try:
                self._ozon_client.close()
            except Exception:
                pass
            self._ozon_client = None
        if self._qwen_server is not None:
            try:
                self._qwen_server.stop()
            except Exception:
                pass
            self._qwen_server = None


def list_regions() -> list[dict[str, Any]]:
    return list_supported_regions()


def _compute_clusters(
    by_source: dict[str, dict[str, Any]],
    query: str,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Run cross-source clustering once at the end of a search.

    Returns (clusters, diagnostics). Diagnostics may surface an error string so
    a flaky clustering pass doesn't tank the whole response.
    """
    all_products: list[dict[str, Any]] = []
    for payload in by_source.values():
        for p in (payload.get("products") or []):
            if isinstance(p, dict):
                all_products.append(p)
    if not all_products:
        return [], None
    try:
        clusters = cluster_products(all_products, query=query)
    except Exception as exc:  # clustering is best-effort, never fatal
        return [], {"error": repr(exc), "products_count": len(all_products)}
    return clusters, {
        "products_count": len(all_products),
        "cluster_count": len(clusters),
    }


def _build_post_filter(
    filter_specs: list[dict[str, Any]],
) -> dict[str, Callable[[dict[str, Any]], bool]] | None:
    """Build per-source product filters from user-selected filter values.

    Returns a dict mapping source -> predicate. Sources with no selections
    are absent from the dict so callers can pass everything through.
    Marketplace search APIs differ wildly (WB's `?fbrand=` is ignored on
    the u-search endpoint), so we filter what came back. Covers label-based
    "brand" and "color" filters; range/boolean specs are skipped because the
    UI doesn't collect their bounds yet.
    """
    if not filter_specs:
        return None

    # per source: {"brand": set[str_lower], "color": set[str_lower]}
    per_source: dict[str, dict[str, set[str]]] = {}

    for spec in filter_specs:
        if not isinstance(spec, dict):
            continue
        label = (spec.get("label") or "").lower()
        kind: str | None = None
        if "бренд" in label or "brand" in label or "производитель" in label:
            kind = "brand"
        elif "цвет" in label or "color" in label:
            kind = "color"
        if kind is None:
            continue
        for sel in spec.get("selections") or []:
            if not isinstance(sel, dict):
                continue
            source = sel.get("source")
            if not source:
                continue
            names = [str(n).strip().lower() for n in (sel.get("value_names") or []) if n]
            if not names:
                # Fall back to spec-level values for legacy specs.
                names = [str(v).strip().lower() for v in (spec.get("values") or []) if v]
            if not names:
                continue
            per_source.setdefault(source, {}).setdefault(kind, set()).update(names)

    if not per_source:
        return None

    def _product_brand(product: dict[str, Any]) -> str:
        brand = product.get("brand")
        if isinstance(brand, dict):
            return (brand.get("name") or "").lower()
        if isinstance(brand, str):
            return brand.lower()
        return ""

    def _product_color(product: dict[str, Any]) -> str:
        attrs = (product.get("attributes") or {})
        raw = attrs.get("raw") or {}
        for key in ("Цвет", "Цвет товара", "color", "Color"):
            v = raw.get(key)
            if isinstance(v, str) and v.strip():
                return v.lower()
        norm = attrs.get("normalized") or {}
        v = norm.get("color")
        return (v or "").lower() if isinstance(v, str) else ""

    filters: dict[str, Callable[[dict[str, Any]], bool]] = {}
    for source, kinds in per_source.items():
        wanted_brands = kinds.get("brand") or set()
        wanted_colors = kinds.get("color") or set()

        def _make_pred(brands: set[str], colors: set[str]):
            def _accept(product: dict[str, Any]) -> bool:
                if brands:
                    if _product_brand(product) not in brands:
                        return False
                if colors:
                    color = _product_color(product)
                    if not any(w in color or color in w for w in colors):
                        return False
                return True

            return _accept

        filters[source] = _make_pred(wanted_brands, wanted_colors)
    return filters


def _translate_filter_specs(
    filter_specs: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Translate frontend filter specs to per-marketplace search params.

    Spec shape (one entry per matched group):
        {"label": str, "selections": [
            {"source": "wildberries"|"ozon"|"yandex_market",
             "raw_id": str, "value_ids": [str, ...]},
        ]}

    Returns: {source_key: {"extra_params": dict, "extra_query": str, "note": str}}.
    Sources without any selection are absent from the returned dict.
    """
    by_source: dict[str, dict[str, Any]] = {}

    wb_params: dict[str, str] = {}
    ym_pairs: list[str] = []
    ozon_pairs: list[str] = []
    ozon_notes: list[str] = []

    for spec in filter_specs or []:
        if not isinstance(spec, dict):
            continue
        for sel in spec.get("selections") or []:
            if not isinstance(sel, dict):
                continue
            source = sel.get("source")
            raw_id = sel.get("raw_id")
            values = [str(v) for v in (sel.get("value_ids") or []) if v is not None and str(v) != ""]
            if not source or not raw_id or not values:
                continue
            if source == "wildberries":
                wb_params[str(raw_id)] = ";".join(values)
            elif source == "yandex_market":
                for v in values:
                    ym_pairs.append(f"glfilter={raw_id}:{v}")
            elif source == "ozon":
                for v in values:
                    ozon_pairs.append(f"{raw_id}={v}")
                ozon_notes.append(f"selected_filter:{spec.get('label')}={values}")

    if wb_params:
        by_source["wildberries"] = {"extra_params": wb_params}
    if ym_pairs:
        by_source["yandex_market"] = {"extra_query": "&".join(ym_pairs)}
    if ozon_pairs:
        by_source["ozon"] = {
            "extra_query": "&".join(ozon_pairs),
            "note": "ozon_filter_selected_but_not_applied: " + "; ".join(ozon_notes[:3]),
        }
    return by_source
