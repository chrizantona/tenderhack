from __future__ import annotations

import argparse
import json

from ozon_parser import OzonProductParser


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Ozon product card by product ID/SKU or product URL."
    )
    parser.add_argument("product_id_or_url", help="Ozon product ID/SKU or product URL")
    parser.add_argument("--region-name", default="RU/Moscow")
    parser.add_argument(
        "--no-extra-layouts",
        action="store_true",
        help="Do not request extra product page layouts for description/seller/attributes.",
    )
    parser.add_argument("--output", help="Optional path to write JSON result")
    args = parser.parse_args()

    product_parser = OzonProductParser()
    result = product_parser.get_product(
        args.product_id_or_url,
        region_name=args.region_name,
        include_extra_layouts=not args.no_extra_layouts,
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
