from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ym_parser import (
    extract_payloads_from_html,
    extract_yandex_market_products,
    hours_from_delivery_text,
    normalize_yandex_market_product,
)


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    basic_scheme = json.loads((ROOT / "basic_scheme.json").read_text(encoding="utf-8"))
    payload = fixture_market_payload()
    products = extract_yandex_market_products([payload])
    assert len(products) == 1, f"expected 1 product, got {len(products)}"

    item = normalize_yandex_market_product(
        products[0],
        query="креатин моногидрат",
        page_url=None,
        detail_payloads=[payload],
        region_name="RU/Moscow",
        source="fixture",
        endpoints=["fixture://market.yandex.ru/search"],
        diagnostics=[],
    )

    missing = missing_schema_keys(basic_scheme, item)
    assert not missing, "missing basic_scheme keys: " + ", ".join(missing)
    assert item["schema_version"] == "0.2"
    assert item["marketplace"] == "yandex_market"
    assert item["external_id"] == "987654321"
    assert item["title"] == "Креатин моногидрат 500 г"
    assert item["brand"]["name"] == "Maxler"
    assert item["price"]["current"] == 1299
    assert item["price"]["old"] == 1699
    assert item["rating"]["value"] == 4.8
    assert item["rating"]["reviews_count"] == 321
    assert item["seller"]["name"] == "Яндекс Маркет"
    assert item["delivery"]["time_min_hours"] == 2
    assert item["delivery"]["time_max_hours"] == 26
    assert item["attributes"]["normalized"]["memory_gb"] is None
    assert "yandex_market" in item["marketplace_specific"]
    assert hours_from_delivery_text("15-30 мин. курьером в удобный момент") == (0.25, 0.5)

    apiary_payloads = extract_payloads_from_html(fixture_apiary_html())
    apiary_products = extract_yandex_market_products(apiary_payloads, html_text=fixture_apiary_html())
    assert len(apiary_products) == 1, f"expected 1 apiary product, got {len(apiary_products)}"
    apiary_item = normalize_yandex_market_product(
        apiary_products[0],
        query="креатин моногидрат",
        page_url=None,
        detail_payloads=[],
        region_name="RU/Moscow",
        source="fixture_apiary",
        endpoints=["fixture://market.yandex.ru/search.html"],
        diagnostics=[],
    )
    assert apiary_item["external_id"] == "5271377695"
    assert apiary_item["variant_id"] == "5263462535"
    assert apiary_item["offer_id"] == "m7vgzteDLZ4dsLXMnLdltA"
    assert apiary_item["price"]["current"] == 720
    assert apiary_item["media"]["main_image_url"] == "https://avatars.mds.yandex.net/get-mpic/fixture-apiary/orig"

    promo_products = extract_yandex_market_products([fixture_promotional_payload()])
    assert promo_products == [], f"expected promotional payload to be filtered, got {len(promo_products)}"

    example_check = check_example_html()

    print(json.dumps({"status": "ok", "checked_product": item["external_id"], "example_html": example_check}, ensure_ascii=False))
    return 0


def check_example_html() -> dict[str, Any]:
    path = ROOT / "yandex_market_example.html"
    if not path.exists():
        return {"status": "skipped", "reason": "yandex_market_example.html not found"}
    text = path.read_text(encoding="utf-8")
    payloads = extract_payloads_from_html(text)
    products = extract_yandex_market_products(payloads, html_text=text)
    target = None
    for candidate in products:
        if str(candidate.get("oskuId") or candidate.get("skuId") or candidate.get("id")) == "5198483074":
            target = candidate
            break
    assert target is not None, "yandex_market_example.html: target oskuId=5198483074 not found"
    item = normalize_yandex_market_product(
        target,
        query="lenovo v15",
        page_url=None,
        detail_payloads=payloads,
        region_name="RU/Moscow",
        source="example_html",
        endpoints=["yandex_market_example.html"],
        diagnostics=[],
        transport_meta={},
    )
    assert item["availability"]["status"] == "in_stock", f"availability: {item['availability']}"
    assert item["availability"]["quantity"] == 2, f"quantity: {item['availability']['quantity']}"
    delivery = item["delivery"]
    assert delivery["time_min_hours"] == 4 and delivery["time_max_hours"] == 4, f"delivery: {delivery}"
    return {
        "status": "ok",
        "osku": str(target.get("oskuId")),
        "availability": item["availability"]["status"],
        "quantity": item["availability"]["quantity"],
        "delivery_hours": (delivery["time_min_hours"], delivery["time_max_hours"]),
    }


def fixture_market_payload() -> dict[str, Any]:
    return {
        "widgets": {
            "searchResults": {
                "items": [
                    {
                        "modelId": 987654321,
                        "skuId": 123456789,
                        "offerId": "creative-offer-1",
                        "wareMd5": "abc123def456",
                        "name": "Креатин моногидрат 500 г",
                        "vendor": "Maxler",
                        "category": {"id": 90519, "name": "Спортивное питание"},
                        "price": {"value": 1299, "currency": "RUR"},
                        "oldPrice": {"value": 1699, "currency": "RUR"},
                        "rating": {"value": 4.8},
                        "reviewsCount": 321,
                        "shop": {"id": 431782, "name": "Яндекс Маркет", "rating": 4.9},
                        "delivery": {"timeMinHours": 2, "timeMaxHours": 26},
                        "available": True,
                        "url": "/product--kreatin-monogidrat-500-g/987654321?sku=123456789",
                        "pictures": [
                            {"url": "https://avatars.mds.yandex.net/get-mpic/fixture/orig"}
                        ],
                        "specs": [
                            {"name": "Вкус", "value": "без вкуса"},
                            {"name": "Страна производства", "value": "Россия"},
                        ],
                    }
                ],
                "total": 1,
            }
        }
    }


def fixture_apiary_html() -> str:
    payload = {
        "widgets": {
            "@light/ToggleWishlist": {
                "/content/search/item/toggleWishlist": {
                    "productId": 5271377695,
                    "skuId": "5263462535",
                    "offerId": "m7vgzteDLZ4dsLXMnLdltA",
                    "businessId": "45132220",
                    "title": "Креатин моногидрат 300 гр",
                    "picture": "https://avatars.mds.yandex.net/get-mpic/fixture-apiary/orig",
                    "price": {"value": 720, "currency": "RUR"},
                    "place": {"page": "search_result", "widget": "search_result_snippet"},
                }
            }
        },
        "meta": {
            "/content/search/item/toggleWishlist": {"name": "@light/ToggleWishlist"}
        },
        "collections": {},
    }
    return '<html><body><noframes data-apiary="patch">' + json.dumps(payload, ensure_ascii=False) + "</noframes></body></html>"


def fixture_promotional_payload() -> dict[str, Any]:
    return {
        "widgets": {
            "searchResults": {
                "items": [
                    {
                        "modelId": 111222333,
                        "url": "/product--promo/111222333",
                        "picture": "https://avatars.mds.yandex.net/get-mpic/sale/orig",
                    },
                    {
                        "modelId": 444555666,
                        "title": "Распродажа",
                        "url": "/product--sale/444555666",
                        "picture": "https://avatars.mds.yandex.net/get-mpic/sale/orig",
                    },
                ],
            }
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
