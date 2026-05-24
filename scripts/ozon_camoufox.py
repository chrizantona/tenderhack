from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Any

import ozon_common
from ozon_headless_common import (
    OZON_BASE,
    PRODUCT_EXTRACT_JS,
    SEARCH_EXTRACT_JS,
    WAIT_TILES_JS,
    is_blocked,
    product_url,
    search_url,
    wrap_iife,
)


WARMUP_TIMEOUT_MS = 25_000
PAGE_LOAD_TIMEOUT_MS = 15_000

log = logging.getLogger("ozon_camoufox")
if not log.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[ozon_camoufox %(asctime)s %(levelname)s] %(message)s", "%H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


class OzonCamoufoxError(RuntimeError):
    pass


class OzonCamoufoxClient:
    MIN_GAP_SECONDS = 1.5
    RESTART_AFTER_N_FETCHES = 25

    def __init__(
        self,
        *,
        headless: bool = True,
        warmup: bool = True,
        locale: str = "ru-RU",
        proxy: dict | list | None = None,
    ) -> None:
        try:
            from camoufox.sync_api import Camoufox
        except ImportError as exc:
            raise OzonCamoufoxError(
                "camoufox not installed. pip install camoufox && python -m camoufox fetch"
            ) from exc
        self._headless = headless
        self._locale = locale
        self._Camoufox = Camoufox
        self._camoufox_ctx = None
        self._browser = None
        self._warmup_page = None
        self._warmed_up = False
        self._fetch_count = 0
        self._fetches_since_restart = 0
        self._last_fetch_at = 0.0
        self._closed = False
        self.search_limiter = _NoopLimiter()
        self.product_limiter = _NoopLimiter()
        if isinstance(proxy, dict):
            self._proxy_pool = [proxy]
        elif isinstance(proxy, list):
            self._proxy_pool = [p for p in proxy if isinstance(p, dict)]
        else:
            self._proxy_pool = []
        self._proxy_index = 0
        self._proxy_rotate_required = False
        self._proxy_blocked_until: dict[str, float] = {}
        self._proxy_block_ttl_seconds: float = 300.0
        t0 = time.monotonic()
        self._launch_browser()
        log.info(
            "init: browser launched headless=%s in %.2fs proxy=%s",
            headless,
            time.monotonic() - t0,
            self._current_proxy_label(),
        )
        if warmup:
            self._warmup()

    def _next_proxy(self) -> dict | None:
        if not self._proxy_pool:
            return None
        now = time.monotonic()
        for _ in range(len(self._proxy_pool) * 2):
            proxy = self._proxy_pool[self._proxy_index % len(self._proxy_pool)]
            self._proxy_index += 1
            server = str(proxy.get("server", ""))
            blocked_until = self._proxy_blocked_until.get(server, 0.0)
            if blocked_until <= now:
                return proxy
        log.warning("all proxies blacklisted, picking least-recent")
        candidates = sorted(self._proxy_pool, key=lambda p: self._proxy_blocked_until.get(str(p.get("server", "")), 0.0))
        return candidates[0] if candidates else None

    def _mark_current_proxy_blocked(self) -> None:
        label = self._current_proxy_label()
        if label and label != "none":
            self._proxy_blocked_until[label] = time.monotonic() + self._proxy_block_ttl_seconds
            log.warning("proxy=%s blacklisted for %.0fs", label, self._proxy_block_ttl_seconds)

    def _current_proxy_label(self) -> str:
        if not self._proxy_pool:
            return "none"
        proxy = self._proxy_pool[(self._proxy_index - 1) % len(self._proxy_pool)] if self._proxy_index else self._proxy_pool[0]
        return str(proxy.get("server", "?"))

    def _launch_browser(self) -> None:
        kwargs: dict = dict(
            headless=self._headless,
            humanize=True,
            locale=[self._locale],
            os=["macos", "windows", "linux"],
            geoip=False,
        )
        proxy = self._next_proxy()
        if proxy is not None:
            kwargs["proxy"] = proxy
        self._camoufox_ctx = self._Camoufox(**kwargs)
        self._browser = self._camoufox_ctx.__enter__()
        self._warmup_page = self._browser.new_page()
        self._fetches_since_restart = 0

    def _restart_browser(self) -> None:
        log.warning("restart: closing current browser and re-launching")
        try:
            if self._camoufox_ctx is not None:
                self._camoufox_ctx.__exit__(None, None, None)
        except Exception as exc:
            log.warning("restart: close failed: %r", exc)
        self._camoufox_ctx = None
        self._browser = None
        self._warmup_page = None
        self._warmed_up = False
        t0 = time.monotonic()
        self._launch_browser()
        log.info("restart: re-launched in %.2fs", time.monotonic() - t0)
        self._warmup()

    def _open_page(self):
        return self._browser.new_page()

    def _close_page(self, page) -> None:
        try:
            page.close()
        except Exception as exc:
            log.info("close_page warn: %r", exc)

    def _gap(self) -> None:
        gap = self.MIN_GAP_SECONDS - (time.monotonic() - self._last_fetch_at)
        if gap > 0:
            log.info("rate-gap sleeping %.2fs", gap)
            time.sleep(gap)

    def fetch_page(self, page_url: str, *, endpoint_kind: str, timeout: float | None = None) -> tuple[dict, str]:
        if self._proxy_rotate_required and len(self._proxy_pool) > 1:
            self._mark_current_proxy_blocked()
            log.warning("rotating to next proxy")
            try:
                self._restart_browser()
            except Exception as exc:
                log.error("rotate restart failed: %r", exc)
            self._proxy_rotate_required = False
        self._gap()
        self._fetch_count += 1
        self._fetches_since_restart += 1
        if self._fetches_since_restart > self.RESTART_AFTER_N_FETCHES:
            log.info("preventive restart after %d fetches", self._fetches_since_restart - 1)
            try:
                self._restart_browser()
            except Exception as exc:
                log.error("preventive restart failed: %r", exc)
        attempt = 1
        while True:
            try:
                result, endpoint = self._fetch_once(page_url, endpoint_kind=endpoint_kind, timeout=timeout, attempt=attempt)
                self._last_fetch_at = time.monotonic()
                if isinstance(result, dict) and result.get("_blocked"):
                    self._proxy_rotate_required = True
                return result, endpoint
            except Exception as exc:
                log.warning("fetch_page attempt=%d kind=%s failed: %r", attempt, endpoint_kind, exc)
                if attempt >= 2:
                    self._last_fetch_at = time.monotonic()
                    raise
                attempt += 1
                try:
                    self._restart_browser()
                except Exception as restart_exc:
                    log.error("fetch_page: restart failed: %r", restart_exc)
                    self._last_fetch_at = time.monotonic()
                    raise

    def _fetch_once(self, page_url: str, *, endpoint_kind: str, timeout: float | None, attempt: int) -> tuple[dict, str]:
        normalized = ozon_common.ozon_page_path(page_url)
        full_url = OZON_BASE + normalized
        log.info(
            "fetch start kind=%s attempt=%d count=%d since_restart=%d url=%s",
            endpoint_kind, attempt, self._fetch_count, self._fetches_since_restart, full_url,
        )
        page = self._open_page()
        t0 = time.monotonic()
        try:
            page.goto(full_url, timeout=int((timeout or PAGE_LOAD_TIMEOUT_MS / 1000) * 1000))
            t_goto = time.monotonic() - t0
            try:
                # 6s is enough for typical Ozon SERP; the tile-wait below catches
                # the actual content. Lower bound keeps perceived latency down.
                page.wait_for_load_state("networkidle", timeout=6_000)
            except Exception as exc:
                log.info("fetch: networkidle timeout/skip: %r", exc)
            t_idle = time.monotonic() - t0
            # Tile-wait is for the SEARCH-page selector `.product-card` and
            # makes no sense on product pages — there's no such element so
            # we'd wait the full timeout for nothing. That single change cut
            # /api/product wall time from ~22s to ~10s per item, and let
            # limit=20 finish before the 70s Ozon-thread cap.
            if endpoint_kind == "product":
                tiles_seen = 0
            else:
                tiles_seen = self._wait_for_tiles(page, timeout=timeout or 25)
            t_tiles = time.monotonic() - t0
            if endpoint_kind != "product":
                try:
                    page.evaluate(wrap_iife("window.scrollBy(0, 1500); return 0;"))
                    time.sleep(1.2)
                    page.evaluate(wrap_iife("window.scrollBy(0, 1500); return 0;"))
                    time.sleep(0.8)
                except Exception as exc:
                    log.info("fetch: scroll failed: %r", exc)
            js = PRODUCT_EXTRACT_JS if endpoint_kind == "product" else SEARCH_EXTRACT_JS
            payload = self._extract(page, js)
            page_title = (payload.get("title") or "")[:80]
            items_n = len(payload.get("items") or [])
            log.info(
                "fetch done kind=%s tiles=%s items=%s blocked=%s goto=%.2fs idle=%.2fs tiles=%.2fs total=%.2fs title=%r",
                endpoint_kind,
                tiles_seen,
                items_n,
                payload.get("_blocked"),
                t_goto,
                t_idle,
                t_tiles,
                time.monotonic() - t0,
                page_title,
            )
            return payload, "dom"
        finally:
            self._close_page(page)

    def get_json(self, url: str, *, params: dict | None = None, endpoint_kind: str, timeout: float | None = None) -> dict:
        page_url = params.get("url", "") if params else ""
        payload, _ = self.fetch_page(page_url, endpoint_kind=endpoint_kind, timeout=timeout)
        return payload

    def fetch_page_html(
        self,
        full_url: str,
        *,
        timeout: float | None = None,
        wait_for_selector: str | None = None,
        selector_timeout: float = 15.0,
    ) -> str:
        """Fetch the rendered HTML of a page.

        For SPA shells (WB, Ozon catalog), the initial HTML is just a stub —
        the real content is rendered by JS post-load. Pass `wait_for_selector`
        (CSS selector) to block until the marker appears, then read content.
        """
        self._gap()
        self._fetch_count += 1
        self._fetches_since_restart += 1
        page = self._open_page()
        t0 = time.monotonic()
        try:
            page.goto(full_url, timeout=int((timeout or PAGE_LOAD_TIMEOUT_MS / 1000) * 1000))
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception as exc:
                log.info("fetch_page_html: networkidle skip: %r", exc)
            # Scroll to trigger lazy-loaded widgets, then scroll back so the
            # filter sidebar (usually at top) lands in viewport.
            try:
                page.evaluate(wrap_iife("window.scrollBy(0, 1200); return 0;"))
                time.sleep(0.8)
                page.evaluate(wrap_iife("window.scrollBy(0, -2000); return 0;"))
                time.sleep(0.4)
            except Exception as exc:
                log.info("fetch_page_html: scroll skip: %r", exc)
            if wait_for_selector:
                try:
                    page.wait_for_selector(wait_for_selector, timeout=int(selector_timeout * 1000))
                except Exception as exc:
                    log.info("fetch_page_html: selector %r skip: %r", wait_for_selector, exc)
            html = page.content()
            log.info(
                "fetch_page_html done size=%d in %.2fs url=%s",
                len(html), time.monotonic() - t0, full_url,
            )
            self._last_fetch_at = time.monotonic()
            return html
        finally:
            self._close_page(page)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._camoufox_ctx is not None:
                self._camoufox_ctx.__exit__(None, None, None)
        except Exception as exc:
            log.warning("close: error %r", exc)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _warmup(self) -> None:
        if self._warmed_up:
            return
        t0 = time.monotonic()
        page = self._warmup_page or self._browser.new_page()
        try:
            page.goto(OZON_BASE, timeout=WARMUP_TIMEOUT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            time.sleep(2)
            self._warmed_up = True
            log.info("warmup ok in %.2fs", time.monotonic() - t0)
        except Exception as exc:
            log.warning("warmup failed in %.2fs: %r", time.monotonic() - t0, exc)
        finally:
            self._warmup_page = page

    def _wait_for_tiles(self, page, timeout: float = 20) -> int:
        deadline = time.monotonic() + timeout
        last_count = 0
        while time.monotonic() < deadline:
            try:
                count = page.evaluate(wrap_iife(WAIT_TILES_JS))
            except Exception:
                count = 0
            last_count = count or 0
            if last_count > 0:
                time.sleep(1)
                return last_count
            time.sleep(0.4)
        return last_count

    def _extract(self, page, js: str) -> dict:
        raw = page.evaluate(wrap_iife(js))
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except Exception:
                data = {"items": [], "layout": []}
        elif isinstance(raw, dict):
            data = raw
        else:
            data = {"items": [], "layout": []}
        if is_blocked(data.get("title") or "", data.get("url") or ""):
            data["_blocked"] = True
        items = data.get("items") or []
        if items:
            data["searchResultsV2"] = {"items": items}
            data["catalog"] = {"items": items}
        return data


class _NoopLimiter:
    def wait(self) -> None: pass
    def backoff(self) -> None: pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Ozon Camoufox-based parser CLI.")
    parser.add_argument("command", choices=["search", "product"])
    parser.add_argument("query_or_id")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--region-name", default="RU/Moscow")
    parser.add_argument("--no-headless", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()

    from ozon_parser import OzonProductParser, OzonSearchParser

    log.info("CLI: starting client...")
    with OzonCamoufoxClient(headless=not args.no_headless) as client:
        log.info("CLI: client ready.")
        if args.command == "search":
            result = OzonSearchParser(client=client).search(
                args.query_or_id,
                page=args.page,
                limit=args.limit,
                region_name=args.region_name,
            )
        else:
            result = OzonProductParser(client=client).get_product(
                args.query_or_id,
                region_name=args.region_name,
            )

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
