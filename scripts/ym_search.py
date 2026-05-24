from __future__ import annotations

import argparse
import json

import ym_common
from ym_parser import YandexMarketClient, YandexMarketSearchParser


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Search Yandex Market products via public storefront HTML."
    )
    parser.add_argument("query", help="Search query, e.g. 'креатин моногидрат'")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--region-name", default="RU/Moscow")
    parser.add_argument("--region-id", type=int, help="Yandex Market lr region id")
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
        "--enrich-details",
        action="store_true",
        help="Fetch product pages for returned product URLs/IDs.",
    )
    parser.add_argument(
        "--enrich-limit",
        type=int,
        help="Max number of returned products to enrich with product pages.",
    )
    parser.add_argument("--output", help="Optional path to write JSON result")
    args = parser.parse_args()

    client = YandexMarketClient(
        transport=args.transport,
        impersonate=args.impersonate,
        fingerprint_profile=args.fingerprint_profile,
        cookie_header=args.cookie,
        cookie_file=args.cookie_file,
    )
    search_parser = YandexMarketSearchParser(client=client)
    result = search_parser.search(
        args.query,
        page=args.page,
        limit=args.limit,
        region_name=args.region_name,
        region_id=args.region_id,
        enrich=args.enrich_details,
        enrich_limit=args.enrich_limit,
    )

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as file:
            file.write(text + "\n")
    else:
        print(text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
