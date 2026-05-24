"""Parse WB SERP cards from rendered HTML.

Used as a fallback when the JSON search API (search.wb.ru) is rate-limited
(HTTP 429) on our IP/proxy pool. Camoufox loads the catalog page, JS renders
~40 product tiles into `.product-card` elements, we extract title/price/url/
brand/image from the DOM and emit basic_scheme-0.2 product dicts.

Compared to the JSON API:
- No structured attributes (color, size, etc) — only what's visible.
- Brand is parsed from the `/`-separated name ("SAILUN/Шины ...").
- Image URLs are direct CDN links, no basket-card enrichment needed for display.
- nmID still extracted from `data-nm-id`, so downstream basket-card enrich
  through curl_cffi still works if user opens the modal.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


_PRICE_RE = re.compile(r"(\d[\d  ]*)\s*₽")
_DISCOUNT_RE = re.compile(r"−(\d{1,3})\s*%")


def parse_wb_serp_html(html: str, *, query: str = "", region_name: str = "RU/Moscow") -> list[dict[str, Any]]:
    if not html:
        return []
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return []
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(".product-card, .j-card-item")
    out: list[dict[str, Any]] = []
    seen_nm: set[str] = set()
    now = datetime.now(timezone.utc).isoformat()

    for card in cards:
        nm = (card.get("data-nm-id") or "").strip()
        if not nm:
            card_id = card.get("id") or ""
            m = re.match(r"c(\d+)", card_id)
            nm = m.group(1) if m else ""
        if not nm or nm in seen_nm:
            continue
        seen_nm.add(nm)

        # Title with brand prefix: "SAILUN/Шины летние 205 60R16..."
        name_el = card.select_one("[class*='product-card__name'], [class*='card__name'], .product-card__title")
        raw_name = name_el.get_text(strip=True) if name_el else ""
        brand, title = _split_brand_title(raw_name)
        if not title:
            continue

        price_block = card.select_one("[class*='card__price'], [class*='product-card__price']")
        price_text = price_block.get_text(" ", strip=True) if price_block else ""
        current, old = _parse_prices(price_text)
        discount = _parse_discount(price_text)

        img_el = card.select_one("img.j-thumbnail, img")
        # WB lazy-loads tile images: first paint puts a 1x1 stub in src and the
        # real CDN url in data-src / srcset until the tile scrolls into view.
        img_url = None
        if img_el is not None:
            for attr in ("data-src", "src", "data-original"):
                val = img_el.get(attr)
                if val and not val.startswith("data:") and "placeholder" not in val.lower():
                    img_url = val
                    break
            if not img_url:
                srcset = img_el.get("srcset") or ""
                first = srcset.split(",")[0].strip().split(" ")[0]
                if first and not first.startswith("data:"):
                    img_url = first
        if img_url and img_url.startswith("//"):
            img_url = "https:" + img_url
        # Last resort: derive the CDN URL from nm_id when DOM didn't expose it.
        if not img_url and nm:
            try:
                from wb_common import basket_host_candidates, image_url as wb_image_url
                for host in basket_host_candidates(int(nm))[:5]:
                    cand = wb_image_url(int(nm), host, pic=1)
                    if cand:
                        img_url = cand
                        break
            except Exception:
                pass

        rating_el = card.select_one("[class*='product-card__rating'], [class*='card__rating']")
        rating_val = _parse_rating(rating_el.get_text(strip=True) if rating_el else "")
        reviews_el = card.select_one("[class*='product-card__count'], [class*='reviews'], [class*='card__count']")
        reviews_count = _parse_reviews(reviews_el.get_text(strip=True) if reviews_el else "")

        link_el = card.select_one("a.product-card__link, a[href*='/catalog/']")
        href = link_el.get("href") if link_el else None
        product_url = href if href and href.startswith("http") else f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"

        out.append({
            "schema_version": "0.2",
            "marketplace": "wildberries",
            "query": query,
            "external_id": nm,
            "group_id": nm,
            "variant_id": nm,
            "offer_id": None,
            "url": product_url,
            "title": title,
            "normalized_title": _normalize(f"{brand} {title}" if brand else title),
            "description": None,
            "brand": {"name": brand, "external_id": None} if brand else {"name": None, "external_id": None},
            "category": {"name": None, "external_id": None, "parent_name": None, "parent_external_id": None, "path": []},
            "price": {
                "current": current,
                "old": old,
                "currency": "RUB",
                "discount_percent": discount,
                "source": "wb_serp_html",
                "updated_at": now,
            },
            "rating": {"value": rating_val, "reviews_count": reviews_count},
            "seller": {"id": None, "name": None, "rating": None},
            "availability": {"status": "unknown", "quantity": None, "region": region_name, "warehouse_id": None},
            "delivery": {"region": region_name, "time_min_hours": None, "time_max_hours": None, "price": None, "currency": "RUB"},
            "media": {
                "main_image_url": img_url,
                "image_urls": [img_url] if img_url else [],
                "photo_count": 1 if img_url else 0,
                "has_video": False,
            },
            "attributes": {"raw": {}, "normalized": {}},
            "match_keys": {
                "brand": (brand or "").lower(),
                "model": None,
                "manufacturer_part_number": None,
                "vendor_code": None,
                "barcode": None,
                "color": None,
                "size": None,
                "semantic_key": _normalize(f"{brand} {title}" if brand else title),
            },
            "marketplace_specific": {
                "wildberries": {"nm_id": int(nm) if nm.isdigit() else None},
            },
            "source_meta": {
                "fetched_at": now,
                "adapter": "wildberries_serp_html",
                "adapter_version": "0.1",
                "endpoints": ["wildberries.ru/catalog/0/search.aspx (camoufox)"],
                "region_params": {"region": region_name, "currency": "RUB"},
                "diagnostics": [],
            },
            "raw_data": {},
        })
    return out


def _split_brand_title(raw: str) -> tuple[str, str]:
    if not raw:
        return "", ""
    # WB renders brand and title concatenated with `/`. Handle both
    # "BRAND/TITLE" and "/TITLE" (no brand).
    if "/" not in raw:
        return "", raw.strip()
    brand, _, rest = raw.partition("/")
    brand = brand.strip()
    title = rest.strip()
    # Sometimes WB puts the brand twice (e.g., "SAILUN/SAILUN Шины ..."); collapse it.
    if brand and title.lower().startswith(brand.lower() + " "):
        title = title[len(brand):].strip()
    return brand, title


def _parse_prices(text: str) -> tuple[int | None, int | None]:
    """Pick the smallest price as current, the next-larger as old."""
    nums = []
    for m in _PRICE_RE.finditer(text):
        val = m.group(1).replace(" ", "").replace(" ", "")
        if val.isdigit():
            nums.append(int(val))
    if not nums:
        return None, None
    nums.sort()
    if len(nums) == 1:
        return nums[0], None
    return nums[0], nums[-1]


def _parse_discount(text: str) -> int | None:
    m = _DISCOUNT_RE.search(text)
    return int(m.group(1)) if m else None


def _parse_rating(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"([0-5][.,]\d)", text)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            return None
    return None


def _parse_reviews(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"(\d[\d\s]*)", text)
    if m:
        val = m.group(1).replace(" ", "")
        if val.isdigit():
            return int(val)
    return None


def _normalize(text: str) -> str:
    s = (text or "").lower().replace("ё", "е").strip()
    s = re.sub(r"\s+", " ", s)
    return s
