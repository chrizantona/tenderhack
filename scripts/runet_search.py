from __future__ import annotations

import argparse
import json

from other_runet.search_parser import OtherRunetSearchParser


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Search the open Russian-language web for product offers, excluding the big marketplaces."
    )
    parser.add_argument("query", help="Search query, e.g. 'iphone 15 pro max 256 black'")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--region-name", default="RU/Moscow")
    parser.add_argument("--max-discovery", type=int, default=24)
    parser.add_argument("--max-live", type=int, default=18)
    parser.add_argument("--max-per-domain", type=int, default=3)
    parser.add_argument("--time-budget", type=float, default=None)
    parser.add_argument("--output", help="Optional path to write JSON result.")
    args = parser.parse_args()

    search_parser = OtherRunetSearchParser()
    result = search_parser.search(
        args.query,
        region_name=args.region_name,
        limit=args.limit,
        max_discovery_candidates=args.max_discovery,
        max_live_candidates=args.max_live,
        max_per_domain=args.max_per_domain,
        time_budget=args.time_budget,
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
