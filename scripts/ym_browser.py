from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import ym_common
from ym_parser import (
    ADAPTER_VERSION,
    DEFAULT_CURRENCY,
    DEFAULT_REGION_NAME,
    extract_payloads_from_html,
    extract_total,
    extract_yandex_market_products,
    normalize_yandex_market_product,
    now_iso,
)


DEFAULT_PROFILE_DIR = "out/ym_browser_profile"
DEFAULT_STORAGE = "out/ym_storage.json"
DEFAULT_ARTIFACTS_DIR = "out/ym_debug"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Yandex Market browser-mediated fallback using a persistent Playwright context."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    warmup = subparsers.add_parser("warmup", help="Open Market in a persistent browser profile and save storage state.")
    add_browser_args(warmup)
    warmup.add_argument("--url", default=ym_common.MARKET_HOST)
    warmup.add_argument(
        "--pause-seconds",
        type=float,
        default=0,
        help="Optional time to leave headed browser open for manual challenge/region handling.",
    )

    search = subparsers.add_parser("search", help="Fetch a search page from the persistent browser context.")
    add_browser_args(search)
    search.add_argument("query", help="Search query, e.g. 'креатин моногидрат'")
    search.add_argument("--page", type=int, default=1)
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--region-name", default=DEFAULT_REGION_NAME)
    search.add_argument(
        "--mode",
        choices=("navigate", "inpage-fetch"),
        default="inpage-fetch",
        help="navigate opens the search URL; inpage-fetch runs fetch() from market.yandex.ru page context.",
    )
    search.add_argument("--output", help="Optional path to write normalized JSON result.")

    args = parser.parse_args()
    if args.command == "warmup":
        payload = run_warmup(args)
    elif args.command == "search":
        payload = run_search(args)
    else:
        raise ValueError(f"Unsupported command: {args.command}")

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if getattr(args, "output", None):
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


def add_browser_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile-dir", default=DEFAULT_PROFILE_DIR)
    parser.add_argument("--storage", default=DEFAULT_STORAGE)
    parser.add_argument("--artifacts-dir", default=DEFAULT_ARTIFACTS_DIR)
    parser.add_argument("--headed", action="store_true", help="Run browser with a visible window.")
    parser.add_argument(
        "--channel",
        help="Optional browser channel, e.g. chrome. If omitted, Playwright bundled Chromium is used.",
    )
    parser.add_argument(
        "--executable-path",
        help="Optional path to an installed Chromium-based browser executable.",
    )
    parser.add_argument("--locale", default="ru-RU")
    parser.add_argument("--timezone-id", default="Europe/Moscow")
    parser.add_argument("--viewport", default="1365x768", help="Viewport as WIDTHxHEIGHT.")
    parser.add_argument("--launch-timeout-ms", type=float, default=30000)
    parser.add_argument("--timeout-ms", type=float, default=30000)
    parser.add_argument("--settle-ms", type=float, default=2000)
    parser.add_argument(
        "--save-artifacts",
        action="store_true",
        help="Save HTML and screenshot even when the page is not blocked.",
    )


def run_warmup(args: argparse.Namespace) -> dict[str, Any]:
    with open_context(args) as context:
        page = first_page(context)
        response = page.goto(args.url, wait_until="domcontentloaded", timeout=args.timeout_ms)
        wait_for_settle(page, args.settle_ms)
        html_text = page.content()
        status = response.status if response else None
        challenge = challenge_message(status, page.url, html_text)

        if challenge and args.headed and args.pause_seconds > 0:
            print(
                f"Challenge detected: {challenge}. Waiting {args.pause_seconds:g}s for manual handling...",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(args.pause_seconds)
            wait_for_settle(page, args.settle_ms)
            html_text = page.content()
            challenge = challenge_message(status, page.url, html_text)

        storage_path = save_storage(context, args.storage)
        artifacts = save_artifacts(page, html_text, args, label="warmup") if args.save_artifacts or challenge else {}
        return {
            "status": "ok" if not challenge else "challenge_required",
            "url": page.url,
            "http_status": status,
            "storage": storage_path,
            "profile_dir": str(Path(args.profile_dir)),
            "challenge": challenge,
            "artifacts": artifacts,
        }


def run_search(args: argparse.Namespace) -> dict[str, Any]:
    search_path = ym_common.search_page_path(args.query, args.page)
    search_url = ym_common.absolute_market_url(search_path) or ym_common.MARKET_HOST

    with open_context(args) as context:
        page = first_page(context)
        html_text, final_url, status = fetch_search_html(page, args, search_url)
        payloads = extract_payloads_from_html(html_text)
        products = extract_yandex_market_products(payloads, html_text=html_text)[: max(1, args.limit)]
        total = extract_total(payloads)
        if total in (None, 0) and products:
            total = len(products)

        transport_meta = {
            "name": "playwright",
            "mode": args.mode,
            "profile_dir": str(Path(args.profile_dir)),
            "headed": bool(args.headed),
        }
        if args.channel:
            transport_meta["channel"] = args.channel

        diagnostics = []
        challenge = challenge_message(status, final_url, html_text)
        if challenge:
            diagnostics.append({"stage": "browser_search", "message": challenge})

        item_diagnostics = diagnostics if challenge else []
        items = [
            normalize_yandex_market_product(
                product,
                query=args.query,
                page_url=final_url,
                detail_payloads=[],
                region_name=args.region_name,
                source=f"browser_{args.mode}",
                endpoints=[final_url],
                diagnostics=item_diagnostics,
                transport_meta=transport_meta,
            )
            for product in products
        ]

        artifacts = save_artifacts(page, html_text, args, label="search") if args.save_artifacts or challenge else {}
        save_storage(context, args.storage)
        return {
            "schema_version": "0.2",
            "marketplace": "yandex_market",
            "query": args.query,
            "page": max(1, args.page),
            "limit": max(1, args.limit),
            "total": total,
            "items": items,
            "source_meta": {
                "fetched_at": now_iso(),
                "adapter": "yandex_market",
                "adapter_version": ADAPTER_VERSION,
                "endpoints": [final_url],
                "region_params": {"region": args.region_name, "currency": DEFAULT_CURRENCY},
                "page_url": search_path,
                "http_status": status,
                "transport": transport_meta,
                "diagnostics": diagnostics,
                "artifacts": artifacts,
            },
        }


def open_context(args: argparse.Namespace) -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed in this Python environment. "
            "Install requirements or run: python3 -m pip install playwright"
        ) from exc

    playwright = sync_playwright().start()
    width, height = parse_viewport(args.viewport)
    options: dict[str, Any] = {
        "headless": not args.headed,
        "locale": args.locale,
        "timezone_id": args.timezone_id,
        "viewport": {"width": width, "height": height},
        "accept_downloads": False,
        "timeout": args.launch_timeout_ms,
    }
    if args.channel:
        options["channel"] = args.channel
    if args.executable_path:
        options["executable_path"] = args.executable_path

    profile_dir = Path(args.profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    try:
        context = playwright.chromium.launch_persistent_context(str(profile_dir), **options)
    except Exception:
        playwright.stop()
        raise
    return PlaywrightContext(playwright, context)


class PlaywrightContext:
    def __init__(self, playwright: Any, context: Any) -> None:
        self.playwright = playwright
        self.context = context

    def __enter__(self) -> Any:
        return self.context

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            self.context.close()
        finally:
            self.playwright.stop()


def first_page(context: Any) -> Any:
    if context.pages:
        return context.pages[0]
    return context.new_page()


def fetch_search_html(page: Any, args: argparse.Namespace, search_url: str) -> tuple[str, str, int | None]:
    if args.mode == "navigate":
        response = page.goto(search_url, wait_until="domcontentloaded", timeout=args.timeout_ms)
        wait_for_settle(page, args.settle_ms)
        return page.content(), page.url, response.status if response else None

    page.goto(ym_common.MARKET_HOST, wait_until="domcontentloaded", timeout=args.timeout_ms)
    wait_for_settle(page, args.settle_ms)
    result = page.evaluate(
        """async ({ url }) => {
            const response = await fetch(url, {
                credentials: 'include',
                headers: {
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
                }
            });
            return {
                status: response.status,
                url: response.url,
                text: await response.text()
            };
        }""",
        {"url": search_url},
    )
    return str(result.get("text") or ""), str(result.get("url") or search_url), result.get("status")


def wait_for_settle(page: Any, settle_ms: float) -> None:
    if settle_ms <= 0:
        return
    page.wait_for_timeout(settle_ms)


def save_storage(context: Any, path: str) -> str:
    storage_path = Path(path)
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(storage_path))
    return str(storage_path)


def save_artifacts(page: Any, html_text: str, args: argparse.Namespace, *, label: str) -> dict[str, str]:
    artifacts_dir = Path(args.artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    html_path = artifacts_dir / f"ym_{label}_{timestamp}.html"
    screenshot_path = artifacts_dir / f"ym_{label}_{timestamp}.png"
    html_path.write_text(html_text, encoding="utf-8")
    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception:
        return {"html": str(html_path)}
    return {"html": str(html_path), "screenshot": str(screenshot_path)}


def challenge_message(status: int | None, url: str, html_text: str) -> str | None:
    normalized = html_text[:5000].lower()
    if status in {403, 429}:
        return f"browser response status={status}: {challenge_reason(normalized) or 'blocked or rate limited'}"
    reason = challenge_reason(normalized)
    if reason:
        return f"browser challenge detected: {reason}"
    if "showcaptcha" in url.lower() or "captcha" in url.lower():
        return f"browser challenge URL: {url}"
    return None


def challenge_reason(normalized_html: str) -> str | None:
    if "captcha" in normalized_html or "showcaptcha" in normalized_html:
        return "captcha/challenge page"
    if "похоже" in normalized_html and "vpn" in normalized_html:
        return "Yandex VPN/region block page"
    if "доступ ограничен" in normalized_html:
        return "Yandex access restricted page"
    return None


def parse_viewport(value: str) -> tuple[int, int]:
    try:
        width, height = value.lower().split("x", 1)
        return max(320, int(width)), max(240, int(height))
    except ValueError as exc:
        raise ValueError(f"Invalid viewport value: {value!r}. Expected WIDTHxHEIGHT.") from exc


if __name__ == "__main__":
    raise SystemExit(main())
