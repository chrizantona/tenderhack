from __future__ import annotations

import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import ym_common
from ym_common import MARKET_HOST, product_page_path, search_page_path


DEFAULT_QUERIES = (
    "креатин моногидрат",
    "iphone 15 128gb",
    "наушники bluetooth",
)

DEFAULT_PRODUCT_REFS = (
    "987654321",
)


@dataclass
class ProbeResult:
    status: int | None
    elapsed_ms: float
    ok: bool
    error: str | None = None
    location: str | None = None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Small controlled RPS probe for public Yandex Market storefront pages."
    )
    parser.add_argument(
        "--target",
        choices=("search", "product"),
        required=True,
        help="Endpoint family to probe.",
    )
    parser.add_argument(
        "--levels",
        default="0.2,0.5,1",
        help="Comma-separated RPS levels.",
    )
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument("--timeout", type=float, default=8)
    parser.add_argument(
        "--transport",
        choices=("requests", "curl_cffi"),
        default=ym_common.DEFAULT_TRANSPORT,
        help="HTTP transport. curl_cffi enables browser-like TLS impersonation.",
    )
    parser.add_argument(
        "--impersonate",
        default=ym_common.DEFAULT_CURL_IMPERSONATE,
        help="curl_cffi browser profile, e.g. chrome, chrome120, chrome124.",
    )
    parser.add_argument(
        "--fingerprint-profile",
        choices=ym_common.fingerprint_profile_names(),
        help="Consistent preset for transport, TLS impersonation, User-Agent, and browser headers.",
    )
    parser.add_argument(
        "--cookie-file",
        help="Optional Playwright storage_state.json, Netscape cookies.txt, or raw Cookie header file.",
    )
    parser.add_argument("--cookie", help="Optional raw Cookie header for controlled experiments.")
    parser.add_argument(
        "--stop-on-block",
        action="store_true",
        help="Stop after first level that returns 403, 429, or redirect.",
    )
    args = parser.parse_args()

    levels = [float(item) for item in args.levels.split(",") if item.strip()]
    cookie_header = args.cookie
    if args.cookie_file:
        cookie_header = ym_common.cookie_header_from_file(args.cookie_file)
    config = ym_common.resolve_fingerprint_config(
        args.transport,
        impersonate=args.impersonate,
        fingerprint_profile=args.fingerprint_profile,
    )
    session = ym_common.make_session(
        args.transport,
        impersonate=args.impersonate,
        fingerprint_profile=args.fingerprint_profile,
        cookie_header=cookie_header,
    )

    urls = build_search_urls() if args.target == "search" else build_product_urls()
    report: dict[str, Any] = {
        "target": args.target,
        "levels": [],
        "seconds_per_level": args.seconds,
        "transport": transport_report(config, cookie_header=bool(cookie_header)),
        "note": "Do not use as a load test. Yandex Market may return VPN/captcha/region blocks.",
    }

    with ThreadPoolExecutor(max_workers=max(2, int(max(levels) * 2) + 1)) as executor:
        for rps in levels:
            level = run_level(
                executor,
                session,
                urls,
                rps=rps,
                seconds=args.seconds,
                timeout=args.timeout,
            )
            report["levels"].append(level)
            print(json.dumps(level, ensure_ascii=False), flush=True)

            if args.stop_on_block and any(level["status_counts"].get(code) for code in ("301", "302", "307", "403", "429")):
                break
            time.sleep(1)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def build_search_urls() -> list[str]:
    return [MARKET_HOST + search_page_path(query, 1) for query in DEFAULT_QUERIES]


def build_product_urls() -> list[str]:
    return [MARKET_HOST + product_page_path(ref) for ref in DEFAULT_PRODUCT_REFS]


def transport_report(config: dict[str, Any], *, cookie_header: bool) -> dict[str, Any]:
    report = {"name": config["transport"]}
    if config["transport"] == "curl_cffi":
        report["impersonate"] = config["impersonate"]
    if config["name"]:
        report["fingerprint_profile"] = config["name"]
    if cookie_header:
        report["cookies"] = "provided"
    return report


def run_level(
    executor: ThreadPoolExecutor,
    session: Any,
    urls: list[str],
    *,
    rps: float,
    seconds: float,
    timeout: float,
) -> dict[str, Any]:
    total = max(1, int(rps * seconds))
    interval = 1 / rps
    start = time.monotonic()
    futures = []

    for index in range(total):
        url = urls[index % len(urls)]
        futures.append(executor.submit(fetch_once, session, url, timeout))
        next_at = start + ((index + 1) * interval)
        sleep_for = next_at - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)

    results = [future.result() for future in as_completed(futures)]
    elapsed = time.monotonic() - start
    status_counts: dict[str, int] = {}
    latencies = []
    locations: dict[str, int] = {}

    for result in results:
        key = str(result.status) if result.status is not None else "error"
        status_counts[key] = status_counts.get(key, 0) + 1
        if result.location:
            locations[result.location] = locations.get(result.location, 0) + 1
        if result.ok:
            latencies.append(result.elapsed_ms)

    return {
        "rps_target": rps,
        "requests": total,
        "wall_seconds": round(elapsed, 3),
        "actual_rps": round(total / elapsed, 2),
        "status_counts": status_counts,
        "redirect_locations": locations,
        "latency_ms": latency_summary(latencies),
    }


def fetch_once(session: Any, url: str, timeout: float) -> ProbeResult:
    start = time.monotonic()
    try:
        response = session.get(url, timeout=timeout, allow_redirects=False)
        elapsed_ms = (time.monotonic() - start) * 1000
        return ProbeResult(
            status=response.status_code,
            elapsed_ms=elapsed_ms,
            ok=response.status_code == 200,
            location=response.headers.get("location"),
        )
    except Exception as exc:
        elapsed_ms = (time.monotonic() - start) * 1000
        return ProbeResult(status=None, elapsed_ms=elapsed_ms, ok=False, error=str(exc))


def latency_summary(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "p50": None, "p95": None, "max": None}

    ordered = sorted(values)
    p95_index = min(len(ordered) - 1, int(len(ordered) * 0.95))
    return {
        "min": round(min(ordered), 1),
        "p50": round(statistics.median(ordered), 1),
        "p95": round(ordered[p95_index], 1),
        "max": round(max(ordered), 1),
    }


if __name__ == "__main__":
    raise SystemExit(main())
