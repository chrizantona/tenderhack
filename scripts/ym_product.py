from __future__ import annotations

import argparse
import json

import ym_common
from ym_parser import YandexMarketClient, YandexMarketProductParser


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Yandex Market product by model ID/SKU or product URL."
    )
    parser.add_argument("product_id_or_url", help="Yandex Market model/product ID or product URL")
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
    parser.add_argument("--output", help="Optional path to write JSON result")
    args = parser.parse_args()

    client = YandexMarketClient(
        transport=args.transport,
        impersonate=args.impersonate,
        fingerprint_profile=args.fingerprint_profile,
        cookie_header=args.cookie,
        cookie_file=args.cookie_file,
    )
    product_parser = YandexMarketProductParser(client=client)
    result = product_parser.get_product(
        args.product_id_or_url,
        region_name=args.region_name,
        region_id=args.region_id,
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
