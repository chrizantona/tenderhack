from __future__ import annotations

import os
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from filters_match import match_filter_lists, match_filters

try:
    from filters_match_llm import match_filters_llm
except Exception:
    match_filters_llm = None  # type: ignore[assignment]
from filters_ozon import parse_ozon_filters_file, parse_ozon_filters_html
from filters_wb import fetch_wildberries_filters
from filters_wb_html import parse_wb_filters_html
from filters_ym import parse_yandex_market_filters_file, parse_yandex_market_filters_html

try:
    from ym_parser import YandexMarketClient
except Exception:
    YandexMarketClient = None  # type: ignore[assignment]

try:
    from ozon_headless_common import search_url as ozon_search_url
except Exception:
    ozon_search_url = None  # type: ignore[assignment]

try:
    from ozon_camoufox import OzonCamoufoxClient
    OZON_CAMOUFOX_OK = True
except Exception:
    OZON_CAMOUFOX_OK = False


class _OzonFilterRunner:
    """Owns a Camoufox client pinned to a single worker thread.

    Playwright's sync API can't be called from a thread different from the one
    that created the browser, so we marshal every browser interaction through a
    dedicated single-threaded executor.
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ozon-filter")
        self._client: Any = None
        self._init_error: str | None = None
        self._lock = threading.Lock()

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._init_error is not None:
            raise RuntimeError(self._init_error)
        if not OZON_CAMOUFOX_OK:
            self._init_error = "camoufox not available"
            raise RuntimeError(self._init_error)
        try:
            self._client = OzonCamoufoxClient(headless=True, warmup=True)
            return self._client
        except Exception as exc:
            self._init_error = f"camoufox init failed: {exc!r}"
            raise

    def fetch_html(self, url: str, *, timeout: float = 30.0) -> str:
        future = self._executor.submit(self._do_fetch, url, timeout)
        return future.result(timeout=timeout + 30)

    def _do_fetch(self, url: str, timeout: float) -> str:
        client = self._ensure_client()
        return client.fetch_page_html(url, timeout=timeout)

    def close(self) -> None:
        if self._client is not None:
            try:
                self._executor.submit(self._client.close).result(timeout=10)
            except Exception:
                pass
        self._executor.shutdown(wait=False, cancel_futures=True)


_ozon_filter_runner: _OzonFilterRunner | None = None
_ozon_filter_runner_lock = threading.Lock()


def _get_ozon_filter_runner() -> _OzonFilterRunner | None:
    global _ozon_filter_runner
    if not OZON_CAMOUFOX_OK:
        return None
    with _ozon_filter_runner_lock:
        if _ozon_filter_runner is None:
            _ozon_filter_runner = _OzonFilterRunner()
        return _ozon_filter_runner


class _WBFilterRunner:
    """Camoufox-backed WB SERP fetcher with side-panel expansion.

    WB renders the filter sidebar lazily — until the user clicks "Все
    фильтры", only sort + a couple of dropdowns are in DOM. We open the
    SERP, click that button, wait for `.filters-desktop__list` to populate,
    then return the rendered HTML for downstream parsing.
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wb-filter")
        self._client: Any = None
        self._init_error: str | None = None

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._init_error is not None:
            raise RuntimeError(self._init_error)
        if not OZON_CAMOUFOX_OK:
            self._init_error = "camoufox not available"
            raise RuntimeError(self._init_error)
        try:
            self._client = OzonCamoufoxClient(headless=True, warmup=False)
            return self._client
        except Exception as exc:
            self._init_error = f"camoufox init failed: {exc!r}"
            raise

    def fetch_html(self, url: str, *, timeout: float = 60.0) -> str:
        future = self._executor.submit(self._do_fetch, url, timeout)
        return future.result(timeout=timeout + 30)

    def fetch_serp_html(self, url: str, *, timeout: float = 60.0) -> str:
        """Fetch WB SERP HTML without clicking 'Все фильтры'.

        Used as a fallback for product search (not filters) when the JSON
        search API is rate-limited. Waits for `.product-card` tiles to render.
        """
        future = self._executor.submit(self._do_fetch_serp, url, timeout)
        return future.result(timeout=timeout + 30)

    def _do_fetch_serp(self, url: str, timeout: float) -> str:
        client = self._ensure_client()
        page = client._open_page()
        try:
            page.goto(url, timeout=int(timeout * 1000), wait_until="commit")
            # Wait for product tiles to render — these come from JS after page load.
            try:
                page.wait_for_selector(".product-card, .j-card-item", timeout=20_000)
            except Exception:
                pass
            # Light scroll to trigger lazy-loaded images / additional tiles.
            try:
                page.evaluate("window.scrollBy(0, 1500)")
                import time as _t
                _t.sleep(0.6)
                page.evaluate("window.scrollBy(0, -1500)")
                _t.sleep(0.3)
            except Exception:
                pass
            return page.content()
        finally:
            try:
                page.close()
            except Exception:
                pass

    def _do_fetch(self, url: str, timeout: float) -> str:
        client = self._ensure_client()
        page = client._open_page()
        try:
            # `commit` returns as soon as the first response byte arrives —
            # WB's full "load" or even "domcontentloaded" wait often blows
            # past 30s on rate-limited IPs because of slow ad/tracking scripts.
            page.goto(url, timeout=int(timeout * 1000), wait_until="commit")
            try:
                page.wait_for_selector(".dropdown-filter__btn, .filters-block", timeout=15_000)
            except Exception:
                pass
            # Click "Все фильтры" to open the side panel with the full filter list.
            opened = False
            for sel in (".j-show-all-filtres", ".dropdown-filter__btn--all"):
                try:
                    page.click(sel, timeout=3000)
                    opened = True
                    break
                except Exception:
                    continue
            if opened:
                try:
                    page.wait_for_selector(".filters-desktop__list", timeout=10_000)
                except Exception:
                    pass
                # Tiny settle pause so dynamic list items finish rendering.
                import time as _time
                _time.sleep(1.5)
            return page.content()
        finally:
            try:
                page.close()
            except Exception:
                pass

    def close(self) -> None:
        if self._client is not None:
            try:
                self._executor.submit(self._client.close).result(timeout=10)
            except Exception:
                pass
        self._executor.shutdown(wait=False, cancel_futures=True)


_wb_filter_runner: _WBFilterRunner | None = None
_wb_filter_runner_lock = threading.Lock()


def _get_wb_filter_runner() -> _WBFilterRunner | None:
    global _wb_filter_runner
    if not OZON_CAMOUFOX_OK:
        return None
    with _wb_filter_runner_lock:
        if _wb_filter_runner is None:
            _wb_filter_runner = _WBFilterRunner()
        return _wb_filter_runner


def _normalize_key(value: str) -> str:
    s = unicodedata.normalize("NFKC", value).strip().lower()
    s = s.replace("ё", "е")
    return s


# Static fixtures used when live access is unavailable (YM region block, Ozon antibot).
FIXTURES_YM: dict[str, Path] = {
    _normalize_key("холодильник"): ROOT / "YM.html",
}

FIXTURES_OZON: dict[str, Path] = {}


def _resolve_fixture(table: dict[str, Path], query: str) -> Path | None:
    key = _normalize_key(query)
    if key in table:
        path = table[key]
        return path if path.exists() else None
    return None


_ym_filter_client_lock = threading.Lock()
_ym_filter_client: Any = None


# Runtime override from aggregator's set_proxy_enabled toggle. Default True
# so the env-derived proxy is used out of the box (matches old behavior).
_proxy_enabled_runtime = True


def on_proxy_state_changed(enabled: bool) -> None:
    """Called by Aggregator when the user toggles proxy use. Drops the cached
    YM filter client so the next /api/filters call rebuilds it with/without
    proxy according to the new state.
    """
    global _proxy_enabled_runtime, _ym_filter_client
    with _ym_filter_client_lock:
        _proxy_enabled_runtime = enabled
        if _ym_filter_client is not None:
            try:
                _ym_filter_client.close()
            except Exception:
                pass
            _ym_filter_client = None


def _ym_filter_client_get():
    global _ym_filter_client
    if YandexMarketClient is None:
        return None
    with _ym_filter_client_lock:
        if _ym_filter_client is None:
            try:
                # Reuse the same proxy resolution as the search-time YM client
                # (see aggregator._resolve_ym_proxy). Without proxy YM returns
                # 403 "VPN/region block" from outside RU and filters never load.
                proxy_url = _resolve_ym_proxy_url() if _proxy_enabled_runtime else None
                _ym_filter_client = YandexMarketClient(proxy_url=proxy_url)
            except Exception:
                _ym_filter_client = None
        return _ym_filter_client


def _resolve_ym_proxy_url() -> str | None:
    # Mirrors aggregator._resolve_ym_proxy. Inlined here to avoid an import
    # cycle between filters_service and aggregator.
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


_HID_FROM_CARD_RE = __import__("re").compile(r"/card/[^\"]+hid=(\d+)")
_NID_FROM_CARD_RE = __import__("re").compile(r"/card/[^\"]+nid=(\d+)")


def _discover_ym_category(html_text: str) -> tuple[str | None, str | None]:
    """Pick the most frequent (hid, nid) pair from product card URLs in a SERP."""
    if not html_text:
        return None, None
    hids = _HID_FROM_CARD_RE.findall(html_text)
    nids = _NID_FROM_CARD_RE.findall(html_text)
    from collections import Counter
    top_hid = Counter(hids).most_common(1)[0][0] if hids else None
    top_nid = Counter(nids).most_common(1)[0][0] if nids else None
    return top_hid, top_nid


def _fetch_ym_filters_live(query: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Two-step fetch: SERP -> discover hid -> list-filters page with hid+rt=10."""
    client = _ym_filter_client_get()
    if client is None:
        return [], ["ym_client_unavailable"]
    from urllib.parse import quote_plus
    diagnostics: list[str] = []
    encoded = quote_plus(query)
    # Step 1: SERP to discover dominant category.
    try:
        serp_html, _ = client.fetch_page(f"/search?text={encoded}", endpoint_kind="search", timeout=20)
    except Exception as exc:
        return [], [f"ym_serp_fetch_error: {exc!r}"]
    hid, nid = _discover_ym_category(serp_html or "")
    diagnostics.append(f"ym_discovered_hid={hid} nid={nid}")
    # Step 2: list-filters page.
    list_url = f"/search?text={encoded}&rt=10"
    if hid:
        list_url += f"&hid={hid}"
    if nid:
        list_url += f"&nid={nid}"
    try:
        html, _ = client.fetch_page(list_url, endpoint_kind="search", timeout=20)
    except Exception as exc:
        return [], diagnostics + [f"ym_listfilters_fetch_error: {exc!r}"]
    try:
        filters = parse_yandex_market_filters_html(html or "")
    except Exception as exc:
        return [], diagnostics + [f"ym_parse_error: {exc!r}"]
    diagnostics.append(f"ym_list_filters_count={len(filters)}")
    # Fallback: if list-filters yielded few/no entries, also parse the SERP sidebar.
    if len(filters) < 8 and serp_html:
        try:
            serp_filters = parse_yandex_market_filters_html(serp_html)
        except Exception:
            serp_filters = []
        if len(serp_filters) > len(filters):
            diagnostics.append(f"ym_used_serp_fallback_count={len(serp_filters)}")
            return serp_filters, diagnostics
    return filters, diagnostics


def fetch_filters_ym(query: str) -> dict[str, Any]:
    started = time.time()
    fixture = _resolve_fixture(FIXTURES_YM, query)
    fixture_filters: list[dict[str, Any]] = []
    fixture_error: str | None = None
    if fixture:
        try:
            fixture_filters = parse_yandex_market_filters_file(fixture)
        except Exception as exc:
            fixture_error = repr(exc)

    live_filters, live_diags = _fetch_ym_filters_live(query)

    # Prefer whichever yields more filters: fixtures are richer for known queries,
    # but live works for arbitrary queries on VPN.
    if fixture_filters and len(fixture_filters) >= len(live_filters):
        return {
            "source": "yandex_market",
            "status": "ok",
            "from": "fixture",
            "fixture": str(fixture.relative_to(ROOT)) if fixture else None,
            "filters": fixture_filters,
            "diagnostics": live_diags,
            "took_seconds": round(time.time() - started, 3),
        }
    if live_filters:
        return {
            "source": "yandex_market",
            "status": "ok",
            "from": "live_html",
            "filters": live_filters,
            "diagnostics": live_diags + ([fixture_error] if fixture_error else []),
            "took_seconds": round(time.time() - started, 3),
        }
    return {
        "source": "yandex_market",
        "status": "skipped" if not fixture_error and not live_diags else "error",
        "reason": "no_live_fetch_and_no_fixture" if not fixture_error and not live_diags else None,
        "error": fixture_error,
        "diagnostics": live_diags + ([fixture_error] if fixture_error else []),
        "filters": [],
        "took_seconds": round(time.time() - started, 3),
    }


def fetch_filters_wb(query: str, *, wb_client: Any = None) -> dict[str, Any]:
    started = time.time()
    api_error: str | None = None
    api_filters: list[dict[str, Any]] = []
    try:
        api_filters, meta = fetch_wildberries_filters(query, wb_client=wb_client)
    except Exception as exc:
        api_error = repr(exc)
        meta = {}
    if not api_error and meta.get("error"):
        api_error = meta.get("error")

    if api_filters:
        return {
            "source": "wildberries",
            "status": "ok",
            "from": "live_api",
            "endpoint": meta.get("endpoint"),
            "filters": api_filters,
            "took_seconds": round(time.time() - started, 3),
        }

    # API empty/blocked — try Camoufox HTML fallback. WB SERP HTML carries
    # the filter sidebar with all category/range options even when the JSON
    # endpoint is rate-limited.
    diagnostics: list[Any] = []
    if api_error:
        diagnostics.append(f"api_error: {api_error[:160]}")
    runner = _get_wb_filter_runner()
    if runner is None:
        return {
            "source": "wildberries",
            "status": "empty",
            "from": "live_api",
            "endpoint": meta.get("endpoint"),
            "filters": [],
            "diagnostics": diagnostics + ["html_fallback_unavailable: camoufox not installed"],
            "took_seconds": round(time.time() - started, 3),
        }
    try:
        from urllib.parse import quote_plus
        serp_url = f"https://www.wildberries.ru/catalog/0/search.aspx?search={quote_plus(query)}"
        html = runner.fetch_html(serp_url, timeout=60)
        html_filters = parse_wb_filters_html(html or "")
    except Exception as exc:
        diagnostics.append(f"html_fallback_error: {exc!r}")
        html_filters = []
    if html_filters:
        return {
            "source": "wildberries",
            "status": "ok",
            "from": "live_html",
            "filters": html_filters,
            "diagnostics": diagnostics,
            "took_seconds": round(time.time() - started, 3),
        }
    return {
        "source": "wildberries",
        "status": "empty",
        "from": "live_api",
        "endpoint": meta.get("endpoint"),
        "filters": [],
        "diagnostics": diagnostics + ["html_fallback_empty: page parsed but no filters extracted"],
        "took_seconds": round(time.time() - started, 3),
    }


def fetch_filters_ozon(query: str, *, ozon_client: Any = None) -> dict[str, Any]:
    started = time.time()
    live_diag: list[str] = []
    runner = _get_ozon_filter_runner()
    if runner is not None and ozon_search_url is not None:
        try:
            url = ozon_search_url(query)
            html = runner.fetch_html(url, timeout=45)
            filters = parse_ozon_filters_html(html or "")
        except Exception as exc:
            filters = []
            live_diag.append(f"live_browser_error: {exc!r}")
        else:
            if not filters:
                live_diag.append("live_browser_empty: no filters parsed from HTML")
        if filters:
            return {
                "source": "ozon",
                "status": "ok",
                "from": "live_browser",
                "filters": filters,
                "took_seconds": round(time.time() - started, 3),
            }
    fixture = _resolve_fixture(FIXTURES_OZON, query)
    if not fixture:
        return {
            "source": "ozon",
            "status": "skipped",
            "reason": "no_live_fetch_and_no_fixture",
            "diagnostics": live_diag,
            "filters": [],
            "took_seconds": round(time.time() - started, 3),
        }
    try:
        filters = parse_ozon_filters_file(fixture)
    except Exception as exc:
        return {
            "source": "ozon",
            "status": "error",
            "error": repr(exc),
            "diagnostics": live_diag,
            "filters": [],
            "took_seconds": round(time.time() - started, 3),
        }
    return {
        "source": "ozon",
        "status": "ok" if filters else "empty",
        "from": "fixture",
        "fixture": str(fixture.relative_to(ROOT)),
        "diagnostics": live_diag,
        "filters": filters,
        "took_seconds": round(time.time() - started, 3),
    }


SOURCE_FETCHERS = {
    "yandex_market": fetch_filters_ym,
    "wildberries": fetch_filters_wb,
    "ozon": fetch_filters_ozon,
}


def collect_filters(
    query: str,
    *,
    sources: list[str] | None = None,
    ozon_client: Any = None,
    wb_client: Any = None,
) -> dict[str, Any]:
    if not query or not query.strip():
        raise ValueError("query must be a non-empty string")
    targets = sources or list(SOURCE_FETCHERS.keys())
    targets = [t for t in targets if t in SOURCE_FETCHERS]
    started = time.time()
    by_source: dict[str, dict[str, Any]] = {}

    def _invoke(name: str) -> dict[str, Any]:
        if name == "ozon":
            return fetch_filters_ozon(query, ozon_client=ozon_client)
        if name == "wildberries":
            return fetch_filters_wb(query, wb_client=wb_client)
        return SOURCE_FETCHERS[name](query)

    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        future_to_source = {pool.submit(_invoke, name): name for name in targets}
        for future, name in future_to_source.items():
            try:
                by_source[name] = future.result()
            except Exception as exc:
                by_source[name] = {
                    "source": name,
                    "status": "error",
                    "error": repr(exc),
                    "filters": [],
                }
    filters_by_source = {
        name: payload.get("filters") or []
        for name, payload in by_source.items()
    }
    use_llm = os.environ.get("USE_LLM_MATCHER", "").lower() in ("1", "true", "yes", "on")
    if use_llm and match_filters_llm is not None:
        match = match_filters_llm(filters_by_source)
    else:
        match = match_filters(filters_by_source)
        match.setdefault("matcher", "heuristic")
    return {
        "query": query,
        "sources": targets,
        "by_source": by_source,
        "match": match,
        "took_seconds": round(time.time() - started, 3),
    }


if __name__ == "__main__":
    import json

    q = sys.argv[1] if len(sys.argv) > 1 else "холодильник"
    print(json.dumps(collect_filters(q), ensure_ascii=False, indent=2))
