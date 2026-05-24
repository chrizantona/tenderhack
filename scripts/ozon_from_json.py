from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from ozon_parser import (
    ADAPTER_VERSION,
    DEFAULT_CURRENCY,
    DEFAULT_REGION_NAME,
    extract_ozon_products,
    extract_total,
    normalize_ozon_product,
    now_iso,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize saved Ozon composer-api JSON or browser HAR to basic_scheme.json shape."
    )
    parser.add_argument("input", help="Path to composer JSON response or .har file exported from browser Network tab.")
    parser.add_argument("--query", default="", help="Original search query, e.g. 'креатин моногидрат'.")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--region-name", default=DEFAULT_REGION_NAME)
    parser.add_argument("--output", help="Optional path to write normalized JSON result.")
    args = parser.parse_args()

    input_path = Path(args.input)
    payloads, source_urls = load_payloads(input_path, query=args.query)
    products = []
    for payload in payloads:
        products.extend(extract_ozon_products([payload]))

    items = [
        normalize_ozon_product(
            product,
            query=args.query or None,
            page_url=None,
            detail_payloads=[],
            region_name=args.region_name,
            source="saved_composer_json",
            endpoints=source_urls or [str(input_path)],
            diagnostics=[],
        )
        for product in products[: max(1, args.limit)]
    ]

    result = {
        "schema_version": "0.2",
        "marketplace": "ozon",
        "query": args.query or None,
        "page": max(1, args.page),
        "limit": max(1, args.limit),
        "total": first_total(payloads),
        "items": items,
        "source_meta": {
            "fetched_at": now_iso(),
            "adapter": "ozon",
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


def load_payloads(path: Path, *, query: str) -> tuple[list[dict[str, Any]], list[str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if is_har(payload):
        return load_from_har(payload, query=query)
    if isinstance(payload, dict):
        return [payload], [str(path)]
    raise ValueError(f"Unsupported JSON root in {path}")


def is_har(payload: Any) -> bool:
    return isinstance(payload, dict) and isinstance(payload.get("log"), dict)


def load_from_har(har: dict[str, Any], *, query: str) -> tuple[list[dict[str, Any]], list[str]]:
    entries = ((har.get("log") or {}).get("entries") or [])
    payloads: list[dict[str, Any]] = []
    urls: list[str] = []
    query_text = normalize_query(query)

    for entry in entries:
        request = entry.get("request") or {}
        response = entry.get("response") or {}
        url = request.get("url") or ""
        if "composer-api.bx/page/json" not in url:
            continue
        if query_text and query_text not in normalize_query(unquote(url)):
            continue
        text = response_text(response)
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
            urls.append(url)

    if not payloads:
        hint = " with matching query" if query else ""
        raise ValueError(f"No Ozon composer-api JSON responses{hint} found in HAR")
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


def first_total(payloads: list[dict[str, Any]]) -> int | None:
    for payload in payloads:
        total = extract_total(payload)
        if total is not None:
            return total
    return None


def normalize_query(value: str) -> str:
    parsed = urlparse(value)
    if parsed.query:
        params = parse_qs(parsed.query)
        inner_url = (params.get("url") or [""])[0]
        if inner_url:
            value += " " + inner_url
    return " ".join(unquote(value).lower().replace("+", " ").split())


if __name__ == "__main__":
    raise SystemExit(main())
