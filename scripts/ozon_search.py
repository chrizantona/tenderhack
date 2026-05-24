from __future__ import annotations

import argparse
import json

from ozon_parser import OzonSearchParser


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Search Ozon products via public composer-api storefront JSON."
    )
    parser.add_argument("query", help="Search query, e.g. 'iphone 15 128gb'")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--region-name", default="RU/Moscow")
    parser.add_argument(
        "--enrich-details",
        action="store_true",
        help="Fetch Ozon product page JSON for returned product URLs/IDs.",
    )
    parser.add_argument(
        "--enrich-limit",
        type=int,
        help="Max number of returned products to enrich with product page JSON.",
    )
    parser.add_argument("--output", help="Optional path to write JSON result")
    args = parser.parse_args()

    search_parser = OzonSearchParser()
    result = search_parser.search(
        args.query,
        page=args.page,
        limit=args.limit,
        region_name=args.region_name,
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
