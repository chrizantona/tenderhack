from __future__ import annotations

import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import requests

from wb_common import DEFAULT_HEADERS, basket_card_url, basket_host_candidates


SEARCH_ENDPOINT = "https://u-search.wb.ru/exactmatch/ru/common/v18/search"

DEFAULT_QUERIES = (
    "шорты мужские adidas",
    "iphone 15 128gb",
    "кроссовки nike",
    "рюкзак мужской",
    "наушники bluetooth",
)

DEFAULT_NM_IDS = (
    273639353,
    590952869,
    598952923,
    172050754,
    172049231,
    417699613,
    417699622,
    273639461,
    273639385,
    171627544,
)

DEFAULT_BASKET_HOSTS = {
    273639353: "basket-17.wbbasket.ru",
    590952869: "basket-30.wbbasket.ru",
    598952923: "basket-30.wbbasket.ru",
    172050754: "basket-12.wbbasket.ru",
    172049231: "basket-12.wbbasket.ru",
    417699613: "basket-24.wbbasket.ru",
    417699622: "basket-24.wbbasket.ru",
    273639461: "basket-17.wbbasket.ru",
    273639385: "basket-17.wbbasket.ru",
    171627544: "basket-12.wbbasket.ru",
}


@dataclass
class ProbeResult:
    status: int | None
    elapsed_ms: float
    ok: bool
    error: str | None = None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Small controlled RPS probe for public Wildberries JSON endpoints."
    )
    parser.add_argument(
        "--target",
        choices=("search", "basket"),
        required=True,
        help="Endpoint family to probe.",
    )
    parser.add_argument(
        "--levels",
        default="1,2,4,6",
        help="Comma-separated RPS levels.",
    )
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument("--timeout", type=float, default=8)
    parser.add_argument(
        "--stop-on-429",
        action="store_true",
        help="Stop after the first level that returns HTTP 429.",
    )
    args = parser.parse_args()

    levels = [float(item) for item in args.levels.split(",") if item.strip()]
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)

    urls = build_search_urls() if args.target == "search" else build_basket_urls(session)
    report: dict[str, Any] = {
        "target": args.target,
        "levels": [],
        "seconds_per_level": args.seconds,
    }

    with ThreadPoolExecutor(max_workers=max(2, int(max(levels) * 2))) as executor:
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

            if args.stop_on_429 and level["status_counts"].get("429"):
                break
            time.sleep(1)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def build_search_urls() -> list[tuple[str, dict[str, Any]]]:
    urls = []
    for query in DEFAULT_QUERIES:
        params = {
            "appType": 1,
            "curr": "rub",
            "dest": "-1257786",
            "lang": "ru",
            "page": 1,
            "query": query,
            "resultset": "catalog",
            "sort": "popular",
            "spp": 30,
            "suppressSpellcheck": "false",
        }
        urls.append((SEARCH_ENDPOINT, params))
    return urls


def build_basket_urls(session: requests.Session) -> list[tuple[str, dict[str, Any] | None]]:
    urls = []
    for nm_id in DEFAULT_NM_IDS:
        host = DEFAULT_BASKET_HOSTS.get(nm_id) or resolve_basket_host(session, nm_id)
        if host:
            urls.append((basket_card_url(nm_id, host), None))
    if not urls:
        raise RuntimeError("Cannot resolve any basket card URL")
    return urls


def resolve_basket_host(session: requests.Session, nm_id: int) -> str | None:
    for host in basket_host_candidates(nm_id):
        try:
            response = session.get(basket_card_url(nm_id, host), timeout=3)
        except requests.RequestException:
            continue
        if response.status_code == 200 and response.content:
            return host
        time.sleep(0.03)
    return None


def run_level(
    executor: ThreadPoolExecutor,
    session: requests.Session,
    urls: list[tuple[str, dict[str, Any] | None]],
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
        url, params = urls[index % len(urls)]
        futures.append(executor.submit(fetch_once, session, url, params, timeout))
        next_at = start + ((index + 1) * interval)
        sleep_for = next_at - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)

    results = [future.result() for future in as_completed(futures)]
    elapsed = time.monotonic() - start
    status_counts: dict[str, int] = {}
    latencies = []

    for result in results:
        key = str(result.status) if result.status is not None else "error"
        status_counts[key] = status_counts.get(key, 0) + 1
        if result.ok:
            latencies.append(result.elapsed_ms)

    return {
        "rps_target": rps,
        "requests": total,
        "wall_seconds": round(elapsed, 3),
        "actual_rps": round(total / elapsed, 2),
        "status_counts": status_counts,
        "latency_ms": latency_summary(latencies),
    }


def fetch_once(
    session: requests.Session,
    url: str,
    params: dict[str, Any] | None,
    timeout: float,
) -> ProbeResult:
    start = time.monotonic()
    try:
        response = session.get(url, params=params, timeout=timeout)
        elapsed_ms = (time.monotonic() - start) * 1000
        return ProbeResult(
            status=response.status_code,
            elapsed_ms=elapsed_ms,
            ok=response.status_code == 200,
        )
    except requests.RequestException as exc:
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
