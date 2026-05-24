from __future__ import annotations

import re
import time
from typing import Any

import requests


DEFAULT_DEST = "-1257786"
DEFAULT_REGION = "ru"
DEFAULT_CURRENCY = "rub"

SEARCH_ENDPOINTS = (
    "https://u-search.wb.ru/exactmatch/ru/common/v18/search",
    "https://search.wb.ru/exactmatch/ru/common/v18/search",
    "https://search.wb.ru/exactmatch/ru/common/v4/search",
)

CARD_ENDPOINTS = (
    "https://card.wb.ru/cards/v4/detail",
    "https://card.wb.ru/cards/detail",
)

DEFAULT_HEADERS = {
    # Mirror what a real Chrome 120 on macOS sends. Without the Sec-CH-UA /
    # Sec-Fetch-* triplet WB's edge (Angie) replies 403 from non-RU IPs
    # because Python's requests omits browser-only client hints. With them
    # we get the standard 429 rate-limit only — same surface as a real user.
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "ru,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Origin": "https://www.wildberries.ru",
    "Referer": "https://www.wildberries.ru/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "Sec-CH-UA": '"Not_A Brand";v="8", "Chromium";v="124", "Google Chrome";v="124"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"macOS"',
}


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    return session


def get_json(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = 15,
) -> Any:
    response = session.get(url, params=params, timeout=timeout)
    if response.status_code != 200:
        body = response.text[:300].replace("\n", " ")
        raise RuntimeError(f"GET {response.url} failed: {response.status_code} {body}")
    if not response.content:
        raise RuntimeError(f"GET {response.url} returned an empty response")
    return response.json()


def parse_nm_id(value: str) -> int:
    match = re.search(r"/catalog/(\d+)/", value)
    if match:
        return int(match.group(1))

    digits = re.search(r"\d{5,}", value)
    if not digits:
        raise ValueError(f"Cannot find Wildberries nmID in: {value!r}")
    return int(digits.group(0))


def search_products(
    query: str,
    *,
    page: int = 1,
    dest: str = DEFAULT_DEST,
    sort: str = "popular",
    timeout: float = 15,
    session: requests.Session | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    session = session or make_session()
    params = {
        "appType": 1,
        "curr": DEFAULT_CURRENCY,
        "dest": dest,
        "lang": DEFAULT_REGION,
        "page": page,
        "query": query,
        "resultset": "catalog",
        "sort": sort,
        "spp": 30,
        "suppressSpellcheck": "false",
    }

    errors: list[str] = []
    for endpoint in SEARCH_ENDPOINTS:
        try:
            payload = get_json(session, endpoint, params=params, timeout=timeout)
            products = extract_products(payload)
            return payload, products, endpoint
        except Exception as exc:
            errors.append(f"{endpoint}: {exc}")

    raise RuntimeError("All WB search endpoints failed:\n" + "\n".join(errors))


def fetch_card_product(
    nm_id: int,
    *,
    dest: str = DEFAULT_DEST,
    timeout: float = 15,
    session: requests.Session | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    session = session or make_session()
    params = {
        "appType": 1,
        "curr": DEFAULT_CURRENCY,
        "dest": dest,
        "spp": 30,
        "ab_testing": "false",
        "nm": nm_id,
    }

    for endpoint in CARD_ENDPOINTS:
        try:
            payload = get_json(session, endpoint, params=params, timeout=timeout)
        except Exception:
            continue

        products = extract_products(payload)
        if products:
            return products[0], endpoint

    return None, None


def extract_products(payload: dict[str, Any]) -> list[dict[str, Any]]:
    products = payload.get("products")
    if isinstance(products, list):
        return products

    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("products"), list):
        return data["products"]

    return []


def fetch_basket_card(
    nm_id: int,
    *,
    timeout: float = 6,
    sleep_seconds: float = 0.05,
    session: requests.Session | None = None,
) -> tuple[str, dict[str, Any]]:
    session = session or make_session()
    errors: list[str] = []

    for host in basket_host_candidates(nm_id):
        url = basket_card_url(nm_id, host)
        try:
            payload = get_json(session, url, timeout=timeout)
            return host, payload
        except Exception as exc:
            errors.append(f"{host}: {exc}")
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

    raise RuntimeError(
        f"Cannot resolve wbbasket host for nmID={nm_id}. Last errors: "
        + "; ".join(errors[-3:])
    )


def fetch_price_history(
    nm_id: int,
    host: str,
    *,
    timeout: float = 6,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    session = session or make_session()
    return get_json(session, basket_price_history_url(nm_id, host), timeout=timeout)


def basket_host_candidates(nm_id: int) -> list[str]:
    """Enumerate likely wbbasket-NN.wbbasket.ru hosts for an nmID.

    WB shards its image/card CDN across `basket-NN.wbbasket.ru`. NN grows
    over time (was capped at 45 in mid-2024, 60+ by 2026). We probe the
    most-likely bucket first (vol/divisor heuristic), then fall back to a
    full sweep across the live range.
    """
    vol = nm_id // 100000
    guesses: list[int] = []

    # Empirical divisors mapping vol -> bucket. Newer divisors (160-260) cover
    # higher vol numbers; old ones (150-160) cover legacy products.
    for divisor in (150, 155, 160, 170, 180, 190, 200, 210, 215, 220, 225, 230, 240, 250, 260):
        value = max(1, min(_BASKET_HOST_MAX, vol // divisor))
        guesses.extend((value, value - 1, value + 1))

    guesses.extend(range(1, _BASKET_HOST_MAX + 1))

    seen: set[int] = set()
    result: list[str] = []
    for value in guesses:
        if value < 1 or value > _BASKET_HOST_MAX or value in seen:
            continue
        seen.add(value)
        result.append(f"basket-{value:02d}.wbbasket.ru")
    return result


# Bumped from 45 to 60 in 2026 — WB keeps adding basket shards as the catalog
# grows. Tune this when 404s start cropping up for fresh products.
_BASKET_HOST_MAX = 60


def basket_card_url(nm_id: int, host: str) -> str:
    vol = nm_id // 100000
    part = nm_id // 1000
    return f"https://{host}/vol{vol}/part{part}/{nm_id}/info/ru/card.json"


def basket_price_history_url(nm_id: int, host: str) -> str:
    vol = nm_id // 100000
    part = nm_id // 1000
    return f"https://{host}/vol{vol}/part{part}/{nm_id}/info/price-history.json"


def image_url(nm_id: int, host: str | None, pic: int = 1) -> str | None:
    if not host:
        return None
    vol = nm_id // 100000
    part = nm_id // 1000
    return f"https://{host}/vol{vol}/part{part}/{nm_id}/images/big/{pic}.webp"


def normalize_product(
    product: dict[str, Any],
    *,
    query: str | None = None,
    basket_card: dict[str, Any] | None = None,
    basket_host: str | None = None,
    price_history: list[dict[str, Any]] | None = None,
    stock_reliable: bool = True,
) -> dict[str, Any]:
    nm_id = int(product.get("id") or product.get("nm_id") or basket_card.get("nm_id"))
    characteristics = extract_characteristics(product, basket_card)
    price = extract_price(product)
    old_price = extract_old_price(product)

    if price is None and price_history:
        price = latest_history_price(price_history)

    return {
        "marketplace": "wildberries",
        "query": query,
        "external_id": str(nm_id),
        "url": f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx",
        "title": title_from(product, basket_card),
        "brand": brand_from(product, basket_card),
        "price": price,
        "old_price": old_price,
        "currency": "RUB",
        "rating": number_or_none(
            product.get("reviewRating")
            or product.get("nmReviewRating")
            or product.get("rating")
        ),
        "reviews_count": int_or_none(product.get("feedbacks") or product.get("nmFeedbacks")),
        "seller": product.get("supplier"),
        "image_url": image_url(nm_id, basket_host),
        "characteristics": characteristics,
        "availability": availability_from(product, stock_reliable=stock_reliable),
        "raw": {
            "product": product,
            "basket_card": basket_card,
            "price_history": price_history,
        },
    }


def title_from(product: dict[str, Any], basket_card: dict[str, Any] | None) -> str | None:
    if basket_card:
        return basket_card.get("imt_name") or product.get("name")
    return product.get("name")


def brand_from(product: dict[str, Any], basket_card: dict[str, Any] | None) -> str | None:
    if product.get("brand"):
        return product["brand"]
    if basket_card:
        selling = basket_card.get("selling") or {}
        return selling.get("brand_name")
    return None


def extract_price(product: dict[str, Any]) -> float | None:
    price = first_price_object(product)
    if price:
        for key in ("product", "client", "sale"):
            if price.get(key) is not None:
                return money(price[key])

    for key in ("salePriceU", "salePrice", "clientPriceU"):
        if product.get(key) is not None:
            return money(product[key])

    return None


def extract_old_price(product: dict[str, Any]) -> float | None:
    price = first_price_object(product)
    if price and price.get("basic") is not None:
        return money(price["basic"])

    for key in ("priceU", "basicPriceU"):
        if product.get(key) is not None:
            return money(product[key])

    return None


def first_price_object(product: dict[str, Any]) -> dict[str, Any] | None:
    for size in product.get("sizes") or []:
        price = size.get("price")
        if isinstance(price, dict) and price:
            return price
    return None


def latest_history_price(history: list[dict[str, Any]]) -> float | None:
    if not history:
        return None
    latest = max(history, key=lambda item: item.get("dt", 0))
    price = latest.get("price") or {}
    value = price.get("RUB")
    return money(value) if value is not None else None


def money(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value) / 100, 2)
    except (TypeError, ValueError):
        return None


def extract_characteristics(
    product: dict[str, Any],
    basket_card: dict[str, Any] | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {}

    colors = [item.get("name") for item in product.get("colors") or [] if item.get("name")]
    if colors:
        result["colors"] = colors

    if product.get("entity"):
        result["entity"] = product["entity"]
    if product.get("subjectId"):
        result["subject_id"] = product["subjectId"]

    if basket_card:
        if basket_card.get("subj_name"):
            result["subject"] = basket_card["subj_name"]
        if basket_card.get("subj_root_name"):
            result["subject_root"] = basket_card["subj_root_name"]
        for option in basket_card.get("options") or []:
            name = option.get("name")
            value = option.get("value")
            if name and value is not None:
                result[name] = value

    return result


def availability_from(product: dict[str, Any], *, stock_reliable: bool) -> str:
    if not stock_reliable:
        return "unknown"

    quantity = product.get("totalQuantity")
    if isinstance(quantity, int):
        return "in_stock" if quantity > 0 else "out_of_stock"

    qty = 0
    for size in product.get("sizes") or []:
        for stock in size.get("stocks") or []:
            qty += int(stock.get("qty") or 0)

    if qty > 0:
        return "in_stock"
    return "unknown"


def int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def number_or_none(value: Any) -> float | int | None:
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return None
    return int(as_float) if as_float.is_integer() else as_float
