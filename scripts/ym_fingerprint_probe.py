from __future__ import annotations

import argparse
import json
import time
from typing import Any

import ym_common
from ym_parser import YandexMarketClient, YandexMarketSearchParser


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Controlled Yandex Market fingerprint profile probe."
    )
    parser.add_argument("query", help="Search query, e.g. 'креатин моногидрат'")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument(
        "--profiles",
        default=",".join(ym_common.fingerprint_profile_names()),
        help="Comma-separated fingerprint profiles.",
    )
    parser.add_argument(
        "--cookie-file",
        help="Optional Playwright storage_state.json, Netscape cookies.txt, or raw Cookie header file.",
    )
    parser.add_argument("--cookie", help="Optional raw Cookie header for controlled experiments.")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay between profile attempts.")
    args = parser.parse_args()

    profiles = [item.strip() for item in args.profiles.split(",") if item.strip()]
    if not profiles:
        raise ValueError("At least one fingerprint profile is required")

    report: dict[str, Any] = {
        "query": args.query,
        "page": args.page,
        "limit": args.limit,
        "profiles": [],
        "cookies": "provided" if args.cookie or args.cookie_file else None,
    }

    for index, profile in enumerate(profiles):
        started_at = time.monotonic()
        client = YandexMarketClient(
            fingerprint_profile=profile,
            cookie_header=args.cookie,
            cookie_file=args.cookie_file,
        )
        search_parser = YandexMarketSearchParser(client=client)
        result = search_parser.search(args.query, page=args.page, limit=args.limit)
        summary = summarize_result(profile, result, elapsed_ms=(time.monotonic() - started_at) * 1000)
        report["profiles"].append(summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)

        if index < len(profiles) - 1 and args.delay > 0:
            time.sleep(args.delay)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def summarize_result(profile: str, result: dict[str, Any], *, elapsed_ms: float) -> dict[str, Any]:
    source_meta = result.get("source_meta") or {}
    diagnostics = source_meta.get("diagnostics") or []
    first_message = diagnostics[0].get("message") if diagnostics else None
    return {
        "fingerprint_profile": profile,
        "elapsed_ms": round(elapsed_ms, 1),
        "items": len(result.get("items") or []),
        "total": result.get("total"),
        "transport": source_meta.get("transport"),
        "diagnostics": diagnostics,
        "first_message": first_message,
    }


if __name__ == "__main__":
    raise SystemExit(main())
