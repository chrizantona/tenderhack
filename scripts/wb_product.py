from __future__ import annotations

import argparse
import json

from wb_common import DEFAULT_DEST
from wb_parser import WildberriesProductParser


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Wildberries product card by nmID or product URL."
    )
    parser.add_argument("nm_or_url", help="nmID or WB product URL")
    parser.add_argument("--dest", default=DEFAULT_DEST, help="WB destination region")
    parser.add_argument("--region-name", default="RU/Moscow")
    parser.add_argument(
        "--no-price-history",
        action="store_true",
        help="Do not request public wbbasket price-history.json.",
    )
    parser.add_argument("--output", help="Optional path to write JSON result")
    args = parser.parse_args()

    product_parser = WildberriesProductParser()
    result = product_parser.get_product(
        args.nm_or_url,
        dest=args.dest,
        region_name=args.region_name,
        include_price_history=not args.no_price_history,
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
