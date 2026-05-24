from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ozon_html import extract_payloads_from_html
from ozon_parser import (
    best_product_candidate,
    extract_ozon_products,
    hours_from_delivery_text,
    normalize_ozon_product,
)


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    basic_scheme = json.loads((ROOT / "basic_scheme.json").read_text(encoding="utf-8"))
    payload = fixture_composer_payload()
    products = extract_ozon_products([payload])
    assert len(products) == 1, f"expected 1 product, got {len(products)}"

    item = normalize_ozon_product(
        products[0],
        query="iphone 15 128gb",
        page_url=None,
        detail_payloads=[payload],
        region_name="RU/Moscow",
        source="fixture",
        endpoints=["fixture://composer-api.bx/page/json/v2"],
        diagnostics=[],
    )

    missing = missing_schema_keys(basic_scheme, item)
    assert not missing, "missing basic_scheme keys: " + ", ".join(missing)
    assert item["schema_version"] == "0.2"
    assert item["marketplace"] == "ozon"
    assert item["external_id"] == "397529235"
    assert item["title"] == "Apple iPhone 15 128 ГБ"
    assert item["brand"]["name"] == "Apple"
    assert item["price"]["current"] == 72990
    assert item["price"]["old"] == 89990
    assert item["rating"]["value"] == 4.8
    assert item["rating"]["reviews_count"] == 1245
    assert item["seller"]["name"] == "Apple Russia"
    assert item["attributes"]["normalized"]["color"] == "black"
    assert item["attributes"]["normalized"]["memory_gb"] == 128
    assert "ozon" in item["marketplace_specific"]

    delivery_cases = {
        "За 15 минут": (15 / 60, 15 / 60),
        "30-60 мин": (0.5, 1.0),
        "За 4 часа": (4.0, 4.0),
        "Сегодня": (1.0, 24.0),
        "Завтра": (24.0, 48.0),
        "Послезавтра": (48.0, 72.0),
        "2-3 дня": (48.0, 72.0),
    }
    for text, expected in delivery_cases.items():
        got = hours_from_delivery_text(text)
        assert got == expected, f"delivery text {text!r}: expected {expected}, got {got}"

    html_check = check_example_html()

    print(json.dumps({"status": "ok", "checked_product": item["external_id"], "example_html": html_check}, ensure_ascii=False))
    return 0


def check_example_html() -> dict[str, Any]:
    path = ROOT / "example.html"
    if not path.exists():
        return {"status": "skipped", "reason": "example.html not found"}
    text = path.read_text(encoding="utf-8")
    payloads = extract_payloads_from_html(text)
    products = extract_ozon_products(payloads)
    best = best_product_candidate(products)
    assert best is not None, "example.html: no product candidates"
    item = normalize_ozon_product(
        best,
        query="lenovo v15",
        page_url=None,
        detail_payloads=payloads,
        region_name="RU/Moscow",
        source="example_html",
        endpoints=["example.html"],
        diagnostics=[],
    )
    assert item["external_id"] == "3457445601", f"example.html sku mismatch: {item['external_id']}"
    assert item["title"].startswith("Lenovo V15"), f"example.html title: {item['title']!r}"
    assert item["brand"]["name"] == "Lenovo"
    assert item["price"]["current"] == 38890
    assert item["availability"]["status"] == "in_stock", f"availability: {item['availability']}"
    return {
        "status": "ok",
        "sku": item["external_id"],
        "availability": item["availability"]["status"],
        "delivery_hours": (item["delivery"].get("time_min_hours"), item["delivery"].get("time_max_hours")),
    }


def fixture_composer_payload() -> dict[str, Any]:
    product = {
        "id": 397529235,
        "sku": 397529235,
        "title": "Apple iPhone 15 128 ГБ",
        "finalPrice": "72 990 ₽",
        "price": "89 990 ₽",
        "rating": 4.8,
        "reviewsCount": 1245,
        "brand": "Apple",
        "sellerName": "Apple Russia",
        "url": "/product/apple-iphone-15-128gb-397529235/",
        "image": "https://cdn1.ozone.ru/s3/multimedia/iphone-15.webp",
        "attributes": [
            {"name": "Цвет", "value": "черный"},
            {"name": "Память", "value": "128 ГБ"},
        ],
    }
    return {
        "widgetStates": {
            "searchResultsV2-1": json.dumps(
                {
                    "items": [
                        {
                            "cellTrackingInfo": {
                                "product": product,
                            }
                        }
                    ],
                    "totalFound": 1,
                },
                ensure_ascii=False,
            )
        }
    }


def missing_schema_keys(expected: dict[str, Any], actual: dict[str, Any], path: str = "") -> list[str]:
    missing = []
    for key, expected_value in expected.items():
        current_path = f"{path}.{key}" if path else key
        if current_path in {"attributes.raw", "source_meta.region_params", "raw_data"}:
            if key not in actual:
                missing.append(current_path)
            continue
        if key == "marketplace_specific":
            if "marketplace_specific" not in actual:
                missing.append(current_path)
            continue
        if key not in actual:
            missing.append(current_path)
            continue
        actual_value = actual[key]
        if isinstance(expected_value, dict) and isinstance(actual_value, dict):
            missing.extend(missing_schema_keys(expected_value, actual_value, current_path))
    return missing


if __name__ == "__main__":
    raise SystemExit(main())
