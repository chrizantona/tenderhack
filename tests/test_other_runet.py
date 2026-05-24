from __future__ import annotations

import pytest

from scripts.other_runet.marketplace_exclusion import excluded_reason, is_excluded_marketplace
from scripts.other_runet.product_passport import build_passport
from scripts.other_runet.product_url_detector import score_html_for_product, url_only_product_likelihood
from scripts.other_runet.sold_object_resolver import resolve_sold_object
from scripts.other_runet.structured_extractor import Evidence, ExtractedOffer
from scripts.other_runet.evidence_fusion import compute_identity_confidence
from scripts.other_runet.price_anomaly import build_price_context, evaluate_price
from scripts.other_runet.ranker import GROUP_SUSPICIOUS, rank_offer
from scripts.other_runet.qwen_assist import QwenBudget, maybe_assist_sold_object
from scripts.other_runet.qwen_client import QwenClient


class _UnavailableQwen(QwenClient):
    def is_available(self, *, force: bool = False, ttl_seconds: float = 30.0) -> bool:
        return False


@pytest.mark.parametrize(
    "url",
    [
        "https://www.ozon.ru/product/abc",
        "https://m.wildberries.ru/cat/x",
        "https://global.wb.ru/x",
        "https://market.yandex.ru/abc",
        "https://pokupki.market.yandex.ru/abc",
        "https://market-delivery.yandex.ru/abc",
        "https://aliexpress.ru/item/1",
        "https://www.megamarket.ru/x",
        "https://www.avito.ru/x",
    ],
)
def test_exclusion_marketplaces_blocked(url: str) -> None:
    assert is_excluded_marketplace(url)
    assert excluded_reason(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://dns-shop.ru/product/123",
        "https://www.mvideo.ru/product/x",
        "https://example.ru/p/abc",
        "https://shop.local/x",
    ],
)
def test_exclusion_other_domains_pass(url: str) -> None:
    assert not is_excluded_marketplace(url)


@pytest.mark.parametrize(
    "query, expected_type, expected_brand_substr",
    [
        ("iphone 15 pro max 256 black", "smartphone", "apple"),
        ("macbook air m3 16 512 midnight", "laptop", "apple"),
        ("rtx 4070 super palit", "graphics_card", "palit"),
        ("215/55 r17 michelin pilot sport 4", "tires", "michelin"),
        ("hp laserjet pro mfp m428dw", "printer", "hp"),
        ("кроссовки nike air max 270 черные 42", "clothing", "nike"),
    ],
)
def test_passport_brand_and_type(query: str, expected_type: str, expected_brand_substr: str) -> None:
    passport = build_passport(query)
    assert passport.product_type == expected_type
    assert passport.brand is not None
    assert expected_brand_substr in passport.brand.lower()


def test_passport_iphone_attributes() -> None:
    p = build_passport("iphone 15 pro max 256 black")
    attrs = p.required_attributes
    assert attrs.get("model_number") == "15"
    assert attrs.get("model_modifier") == "pro max"
    assert attrs.get("storage_gb") == 256
    assert attrs.get("color") == "black"


def test_passport_tire_dimensions() -> None:
    p = build_passport("215/55 r17 michelin pilot sport 4")
    attrs = p.required_attributes
    assert attrs.get("tire_width") == 215
    assert attrs.get("tire_profile") == 55
    assert attrs.get("tire_diameter") == 17


def test_sold_object_rejects_iphone_accessories() -> None:
    passport = build_passport("iphone 15 pro max 256 black")
    for accessory in (
        "Чехол силиконовый для iPhone 15 Pro Max",
        "Защитное стекло для iPhone 15 Pro Max",
        "Ремешок для iPhone 15 Pro Max совместим",
        "Кабель USB-C для iPhone 15 Pro Max",
    ):
        result = resolve_sold_object(accessory, passport=passport)
        assert result.same_sold_object is False, accessory


def test_sold_object_accepts_real_iphone() -> None:
    passport = build_passport("iphone 15 pro max 256 black")
    for title in (
        "Смартфон Apple iPhone 15 Pro Max 256GB Black",
        "iPhone 15 Pro Max 256GB Black официальная гарантия",
    ):
        result = resolve_sold_object(title, passport=passport)
        assert result.same_sold_object or result.needs_review, title


def test_attribute_mismatch_lowers_identity() -> None:
    passport = build_passport("iphone 15 pro max 256 black")
    matching_offer = ExtractedOffer(
        url="https://shop.example.ru/p/iphone-15-pm-256-black",
        title="Apple iPhone 15 Pro Max 256GB Black",
        brand="Apple",
        price_current=129990,
    )
    mismatch_offer = ExtractedOffer(
        url="https://shop.example.ru/p/iphone-15-pm-128-black",
        title="Apple iPhone 15 Pro Max 128GB Black",
        brand="Apple",
        price_current=109990,
    )
    matching_identity = compute_identity_confidence(matching_offer, passport)
    mismatch_identity = compute_identity_confidence(mismatch_offer, passport)
    assert matching_identity.score > mismatch_identity.score


def test_price_anomaly_flags_suspicious_low() -> None:
    context = build_price_context([100000, 105000, 99000, 103000])
    score, flags = evaluate_price(55000, context)
    assert any(flag.flag == "suspicious_low_price" for flag in flags)
    assert score < 0.7


def test_ranker_groups_suspicious_low_price() -> None:
    passport = build_passport("iphone 15 pro max 256 black")
    offer = ExtractedOffer(
        url="https://shop.example.ru/p/cheap",
        title="Apple iPhone 15 Pro Max 256 Black",
        brand="Apple",
        price_current=55000,
        currency="RUB",
        availability="in_stock",
        extraction_path=["json_ld"],
    )
    offer.evidence.append(Evidence("price", 55000, "json_ld", 0.95))
    sold = resolve_sold_object(offer.title, passport=passport)
    identity = compute_identity_confidence(offer, passport)
    context = build_price_context([129990, 131990, 132990])
    price_score, flags = evaluate_price(offer.price_current, context)
    ranked = rank_offer(
        offer,
        passport=passport,
        identity=identity,
        sold_object=sold,
        trust_score=0.7,
        price_score=price_score,
        price_flags=flags,
    )
    assert ranked.group == GROUP_SUSPICIOUS


def test_ranker_rejects_excluded_marketplace_url() -> None:
    passport = build_passport("iphone 15 pro max 256 black")
    offer = ExtractedOffer(
        url="https://www.ozon.ru/product/abc",
        title="Apple iPhone 15 Pro Max 256 Black",
        brand="Apple",
        price_current=120000,
        currency="RUB",
        availability="in_stock",
        extraction_path=["json_ld"],
    )
    sold = resolve_sold_object(offer.title, passport=passport)
    identity = compute_identity_confidence(offer, passport)
    context = build_price_context([120000])
    price_score, flags = evaluate_price(offer.price_current, context)
    ranked = rank_offer(
        offer,
        passport=passport,
        identity=identity,
        sold_object=sold,
        trust_score=0.5,
        price_score=price_score,
        price_flags=flags,
        excluded=True,
    )
    assert ranked.rejected
    assert ranked.reject_reason == "excluded_marketplace"


def test_qwen_guardrail_keeps_base_when_unavailable() -> None:
    passport = build_passport("iphone 15 pro max 256 black")
    ambiguous_title = "Apple iPhone 15 Pro Max 256 Black комплектный набор для офисных задач"
    base = resolve_sold_object(ambiguous_title, passport=passport)
    base.needs_review = True
    client = _UnavailableQwen()
    budget = QwenBudget()
    result = maybe_assist_sold_object(
        base,
        candidate_title=ambiguous_title,
        breadcrumbs=None,
        description=None,
        passport=passport,
        client=client,
        budget=budget,
    )
    assert result is base
    assert budget.used == 0


def test_url_only_likelihood_distinguishes_product_pages() -> None:
    assert url_only_product_likelihood("https://shop.example.ru/catalog/item/123456") >= 0.65
    assert url_only_product_likelihood("https://shop.example.ru/cart") == 0.0
    assert url_only_product_likelihood("https://shop.example.ru/search?q=foo") < 0.4


def test_product_url_detector_scores_full_html_high() -> None:
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Product","name":"X","sku":"S1","offers":{"@type":"Offer","price":"100","priceCurrency":"RUB","availability":"https://schema.org/InStock"}}
    </script>
    </head><body>
    <h1>X</h1>
    <div>100 ₽</div>
    <nav class="breadcrumbs"><a>Home</a><a>Cat</a><a>Sub</a><a>X</a></nav>
    </body></html>
    """
    result = score_html_for_product(html, "https://example.ru/product/xyz")
    assert result["classification"] == "product"
    assert result["score"] >= 0.65
