from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize saved Yandex Market HTML, embedded JSON, or browser HAR to basic_scheme.json shape."
    )
    parser.add_argument("input", help="Path to Market HTML/JSON response or .har file exported from browser Network tab.")
    parser.add_argument("--query", default="", help="Original search query, e.g. 'креатин моногидрат'.")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--region-name", default=DEFAULT_REGION_NAME)
    parser.add_argument("--output", help="Optional path to write normalized JSON result.")
    args = parser.parse_args()

    input_path = Path(args.input)
    payloads, source_urls = load_payloads(input_path, query=args.query)
    products = extract_yandex_market_products(payloads)

    items = [
        normalize_yandex_market_product(
            product,
            query=args.query or None,
            page_url=None,
            detail_payloads=[],
            region_name=args.region_name,
            source="saved_market_payload",
            endpoints=source_urls or [str(input_path)],
            diagnostics=[],
        )
        for product in products[: max(1, args.limit)]
    ]
    total = first_total(payloads)
    if total in (None, 0) and items:
        total = len(items)

    result = {
        "schema_version": "0.2",
        "marketplace": "yandex_market",
        "query": args.query or None,
        "page": max(1, args.page),
        "limit": max(1, args.limit),
        "total": total,
        "items": items,
        "source_meta": {
            "fetched_at": now_iso(),
            "adapter": "yandex_market",
            "adapter_version": ADAPTER_VERSION,
            "endpoints": source_urls or [str(input_path)],
            "region_params": {"region": args.region_name, "currency": DEFAULT_CURRENCY},
            "diagnostics": [],
        },
    }

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


def load_payloads(path: Path, *, query: str) -> tuple[list[Any], list[str]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".har":
        return load_from_har(json.loads(text), query=query)
    if text.lstrip().startswith("<"):
        return extract_payloads_from_html(text), [str(path)]
    payload = json.loads(text)
    if isinstance(payload, (dict, list)):
        return [payload], [str(path)]
    raise ValueError(f"Unsupported input in {path}")


def load_from_har(har: dict[str, Any], *, query: str) -> tuple[list[Any], list[str]]:
    entries = ((har.get("log") or {}).get("entries") or [])
    payloads: list[Any] = []
    urls: list[str] = []
    query_text = normalize_query(query)

    for entry in entries:
        request = entry.get("request") or {}
        response = entry.get("response") or {}
        url = request.get("url") or ""
        if "market.yandex" not in url:
            continue
        if query_text and query_text not in normalize_query(unquote(url)):
            continue
        text = response_text(response)
        if not text:
            continue
        if text.lstrip().startswith("<"):
            extracted = extract_payloads_from_html(text)
            if extracted:
                payloads.extend(extracted)
                urls.append(url)
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, (dict, list)):
            payloads.append(payload)
            urls.append(url)

    if not payloads:
        hint = " with matching query" if query else ""
        raise ValueError(f"No Yandex Market HTML/JSON responses{hint} found in HAR")
    return payloads, urls


def response_text(response: dict[str, Any]) -> str | None:
    content = response.get("content") or {}
    text = content.get("text")
    if not isinstance(text, str) or not text:
        return None
    if content.get("encoding") == "base64":
        try:
            return base64.b64decode(text).decode("utf-8")
        except ValueError:
            return None
    return text


def first_total(payloads: list[Any]) -> int | None:
    total = extract_total(payloads)
    return total


def normalize_query(value: str) -> str:
    parsed = urlparse(value)
    if parsed.query:
        params = parse_qs(parsed.query)
        value += " " + " ".join(item for values in params.values() for item in values)
    return " ".join(unquote(value).lower().replace("+", " ").split())


if __name__ == "__main__":
    raise SystemExit(main())
