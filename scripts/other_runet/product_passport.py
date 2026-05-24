from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


FAMILY_TO_BRAND: dict[str, str] = {
    "iphone": "apple",
    "ipad": "apple",
    "macbook": "apple",
    "imac": "apple",
    "airpods": "apple",
    "galaxy": "samsung",
    "redmi": "xiaomi",
    "poco": "xiaomi",
    "mi ": "xiaomi",
    "honor": "honor",
    "pixel": "google",
    "thinkpad": "lenovo",
    "ideapad": "lenovo",
    "yoga": "lenovo",
    "legion": "lenovo",
    "rog": "asus",
    "zenbook": "asus",
    "vivobook": "asus",
    "matebook": "huawei",
    "magicbook": "huawei",
    "latitude": "dell",
    "xps": "dell",
    "vostro": "dell",
    "inspiron": "dell",
    "surface": "microsoft",
    "laserjet": "hp",
    "deskjet": "hp",
    "officejet": "hp",
    "pixma": "canon",
    "imageclass": "canon",
    "ecotank": "epson",
    "workforce": "epson",
}

BRAND_VOCABULARY: dict[str, list[str]] = {
    "office_equipment": [
        "hp", "canon", "epson", "brother", "kyocera", "ricoh", "xerox",
        "samsung", "pantum", "konica", "minolta", "oki", "lexmark",
        "panasonic", "sharp", "deli",
    ],
    "tires": [
        "michelin", "continental", "bridgestone", "goodyear", "pirelli",
        "nokian", "hankook", "yokohama", "dunlop", "toyo", "kumho",
        "nordman", "kama", "matador", "amtel", "viatti", "cordiant",
        "ikon", "barum", "tigar", "sava", "gislaved", "marshal",
        "marangoni", "formula", "premiorri", "westlake", "triangle",
    ],
    "clothing": [
        "nike", "adidas", "puma", "reebok", "asics", "new balance",
        "levis", "levi's", "lacoste", "tommy hilfiger", "calvin klein",
        "gap", "uniqlo", "zara", "h&m", "mango", "ostin", "befree",
        "sela", "gloria jeans", "finn flare", "finn-flare", "incity",
        "love republic", "concept club", "befree", "savage",
        "columbia", "the north face", "merrell", "salomon",
    ],
    "electronics": [
        "apple", "samsung", "xiaomi", "huawei", "honor", "oppo", "realme",
        "vivo", "oneplus", "google", "asus", "lenovo", "acer", "msi",
        "dell", "hp", "razer", "logitech", "sony", "lg", "philips",
        "panasonic", "jbl", "bose", "harman", "marshall", "microsoft",
    ],
    "graphics_card": [
        "palit", "gigabyte", "asus", "msi", "inno3d", "zotac", "colorful",
        "evga", "powercolor", "sapphire", "kfa2", "gainward", "biostar",
        "manli", "afox",
    ],
    "appliances": [
        "bosch", "siemens", "miele", "electrolux", "ariston", "indesit",
        "atlant", "beko", "candy", "haier", "lg", "samsung", "whirlpool",
        "tefal", "philips", "braun", "rowenta", "polaris", "redmond",
        "moulinex", "kenwood", "kitfort", "dyson", "kärcher", "karcher",
    ],
    "memory": [
        "kingston", "samsung", "crucial", "corsair", "g.skill", "gskill",
        "patriot", "adata", "transcend", "team", "teamgroup", "hyperx",
        "micron", "hynix", "sk hynix",
    ],
}

BRAND_TRANSLITERATIONS: dict[str, str] = {
    "айфон": "iphone",
    "айпад": "ipad",
    "айпэд": "ipad",
    "макбук": "macbook",
    "эпл": "apple",
    "эппл": "apple",
    "эйпл": "apple",
    "самсунг": "samsung",
    "сяоми": "xiaomi",
    "ксиаоми": "xiaomi",
    "хуавей": "huawei",
    "хонор": "honor",
    "сони": "sony",
    "сонька": "sony",
    "филипс": "philips",
    "панасоник": "panasonic",
    "леново": "lenovo",
    "хайер": "haier",
    "ксерокс": "xerox",
    "ксеркс": "xerox",
    "найк": "nike",
    "адидас": "adidas",
    "пума": "puma",
    "рибок": "reebok",
    "леви": "levis",
    "лакост": "lacoste",
    "бош": "bosch",
    "дайсон": "dyson",
    "керхер": "karcher",
    "кёрхер": "karcher",
    "филлипс": "philips",
    "мишлен": "michelin",
    "континенталь": "continental",
    "бриджстоун": "bridgestone",
    "гудиер": "goodyear",
    "пирелли": "pirelli",
    "нокиан": "nokian",
    "ханкук": "hankook",
    "иокогама": "yokohama",
    "йокохама": "yokohama",
    "данлоп": "dunlop",
    "тойо": "toyo",
    "кумхо": "kumho",
    "матадор": "matador",
    "вьятти": "viatti",
    "кордиант": "cordiant",
    "нордман": "nordman",
    "лазерджет": "laserjet",
    "лазер джет": "laserjet",
    "инк джет": "inkjet",
    "инкджет": "inkjet",
    "оперативка": "ddr",
    "оперативная память": "ram",
}

PRODUCT_TYPE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("smartphone", ("iphone", "смартфон", "телефон", "galaxy", "redmi", "pixel", "xperia", "phone")),
    ("laptop", ("macbook", "ноутбук", "laptop", "thinkpad", "ideapad", "zenbook", "rog", "matebook")),
    ("tablet", ("ipad", "планшет", "tab ", "galaxy tab", "matepad")),
    ("printer", ("принтер", "мфу", "printer", "laserjet", "inkjet", "deskjet", "pixma", "pantum")),
    ("scanner", ("сканер", "scanner")),
    ("cartridge", ("картридж", "тонер", "cartridge", "toner")),
    ("monitor", ("монитор", "display", "monitor")),
    ("tires", ("шина", "шины", "резина", "покрышка")),
    ("clothing", ("шорты", "футболка", "куртка", "брюки", "джинсы", "кроссовки", "пиджак", "блузка", "платье", "жакет", "толстовка", "худи", "свитер", "пальто")),
    ("vacuum", ("пылесос", "робот пылесос", "vacuum")),
    ("graphics_card", ("видеокарта", "rtx", "gtx", "radeon", "rx ")),
)

NEGATIVE_BY_TYPE: dict[str, tuple[str, ...]] = {
    "smartphone": ("чехол", "стекло", "плёнка", "пленка", "ремешок", "адаптер", "зарядное", "кабель", "наклейка", "магнит", "кронштейн", "стенд", "док-станция", "силикон", "бампер", "крышка", "стилус", "линза", "крепление"),
    "laptop": ("чехол", "наклейка", "плёнка", "пленка", "сумка", "подставка", "охлаждение", "зарядное", "адаптер", "док", "клавиатура", "коврик"),
    "tablet": ("чехол", "стекло", "плёнка", "пленка", "ремешок", "стилус", "кабель", "адаптер", "наклейка", "крепление", "обложка"),
    "printer": ("картридж", "тонер", "запчасть", "ремкомплект", "вал", "девелопер", "чип", "термопленка", "плёнка"),
    "monitor": ("кронштейн", "подставка", "крепление", "vesa", "адаптер", "кабель", "плёнка", "пленка", "защитная пленка"),
    "tires": ("колпак", "колпачок", "набор", "комплект колпачков", "ниппель", "грузик", "балансировочный", "крепление", "стенд"),
    "clothing": ("вешалка", "плечики", "чехол для одежды", "бирка", "наклейка"),
    "vacuum": ("мешок", "фильтр", "насадка", "щётка", "щетка", "запчасть"),
    "graphics_card": ("кронштейн", "крепление", "переходник", "удлинитель", "блок питания", "бэкплейт"),
}

ATTRIBUTE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("storage_gb", re.compile(r"\b(\d{2,4})\s*(?:gb|гб)\b", re.I)),
    ("storage_tb", re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(?:tb|тб)\b", re.I)),
    ("ram_gb", re.compile(r"\b(\d{1,3})\s*(?:gb|гб)\s*(?:ram|озу|оперативной|оператив)?", re.I)),
    ("screen_inch", re.compile(r"\b(\d{1,2}(?:[.,]\d+)?)[\"”']", re.I)),
    ("tire_width", re.compile(r"\b(\d{3})\s*[/x]\s*(\d{2})\s*r\s*(\d{2})\b", re.I)),
    ("tire_diameter", re.compile(r"\br(\d{2})\b", re.I)),
    ("apple_chip", re.compile(r"\bm[1-9](?:\s*(?:pro|max|ultra))?\b", re.I)),
    ("snapdragon", re.compile(r"\bsnapdragon\s*\d{3}", re.I)),
    ("ddr_version", re.compile(r"\bddr(\d)\b", re.I)),
    ("frequency_mhz", re.compile(r"\b(\d{3,5})\s*(?:mhz|мгц)\b", re.I)),
)

COLOR_VOCABULARY: dict[str, list[str]] = {
    "black": ["black", "чёрный", "черный", "чёрная", "чёрное", "чёрные", "черная", "черное", "черные", "midnight", "graphite", "space black", "spacegray", "space gray", "серый космос"],
    "white": ["white", "белый", "белая", "белое", "белые", "starlight", "silver", "серебристый", "серебряный"],
    "blue": ["blue", "синий", "синяя", "синие", "голубой", "navy", "темно-синий", "тёмно-синий", "deep blue", "sierra blue"],
    "red": ["red", "красный", "красная", "красные", "product red", "scarlet"],
    "green": ["green", "зелёный", "зеленый", "зелёная", "зелёные", "зеленые", "alpine green", "mint"],
    "purple": ["purple", "фиолетовый", "фиолетовая", "сиреневый", "lavender", "violet"],
    "gold": ["gold", "золотой", "золотистый", "золотая"],
    "yellow": ["yellow", "жёлтый", "желтый", "жёлтая", "желтая"],
    "pink": ["pink", "розовый", "розовая", "розовые"],
    "orange": ["orange", "оранжевый", "оранжевая"],
    "beige": ["beige", "бежевый", "бежевая"],
    "brown": ["brown", "коричневый", "коричневая", "шоколадный"],
}

IDENTIFIER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ean13", re.compile(r"\b\d{13}\b")),
    ("gtin12", re.compile(r"\b\d{12}\b")),
    ("mpn", re.compile(r"\b[a-z]{2,5}[-_/]?\d{3,6}[a-z0-9-]*\b", re.I)),
    ("sku", re.compile(r"\b[a-z0-9]{6,16}\b", re.I)),
)

OPTIONAL_TOKENS = {
    "новый", "оригинал", "оригинальный", "официальный", "ростест", "ru", "рст",
    "eu", "us", "global", "глобальная", "версия", "edition",
}

SECONDHAND_TOKENS = {"б/у", "б у", "бу", "восстановленный", "refurbished", "витринный", "уценка"}


@dataclass
class ProductPassport:
    raw_query: str
    canonical_query: str
    brand: str | None
    product_family: str | None
    model: str | None
    product_type: str | None
    category: str | None
    category_label: str | None
    required_attributes: dict[str, Any]
    optional_attributes: dict[str, Any]
    negative_constraints: list[str]
    identifier_candidates: dict[str, list[str]]
    synonyms: list[str]
    russian_variants: list[str]
    english_variants: list[str]
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_query": self.raw_query,
            "canonical_query": self.canonical_query,
            "brand": self.brand,
            "product_family": self.product_family,
            "model": self.model,
            "product_type": self.product_type,
            "category": self.category,
            "category_label": self.category_label,
            "required_attributes": self.required_attributes,
            "optional_attributes": self.optional_attributes,
            "negative_constraints": list(self.negative_constraints),
            "identifier_candidates": {key: list(values) for key, values in self.identifier_candidates.items()},
            "synonyms": list(self.synonyms),
            "russian_variants": list(self.russian_variants),
            "english_variants": list(self.english_variants),
            "confidence": round(self.confidence, 3),
        }


@dataclass
class _Resolved:
    canonical_text: str
    tokens: list[str]
    has_secondhand: bool
    optional_tokens: list[str]


def build_passport(raw_query: str) -> ProductPassport:
    raw = (raw_query or "").strip()
    resolved = _resolve_text(raw)
    canonical = resolved.canonical_text
    category, category_label = _detect_category(canonical)
    brand = _detect_brand(canonical, category)
    product_type = _detect_product_type(canonical)
    required_attrs = _extract_required_attributes(canonical, category, product_type)
    color = _detect_color(canonical)
    if color:
        required_attrs.setdefault("color", color)
    optional_attrs: dict[str, Any] = {}
    if resolved.optional_tokens:
        optional_attrs["modifiers"] = resolved.optional_tokens
    if resolved.has_secondhand:
        optional_attrs["secondhand"] = True
    identifiers = _extract_identifiers(canonical)
    model = _extract_model(canonical, brand)
    family = _detect_family(canonical, product_type)
    russian_variants, english_variants, synonyms = _build_variants(raw, canonical, brand, model, product_type)
    negatives = _build_negative_constraints(product_type)
    confidence = _confidence_score(brand, product_type, required_attrs, identifiers)
    return ProductPassport(
        raw_query=raw,
        canonical_query=canonical,
        brand=brand,
        product_family=family,
        model=model,
        product_type=product_type,
        category=category,
        category_label=category_label,
        required_attributes=required_attrs,
        optional_attributes=optional_attrs,
        negative_constraints=negatives,
        identifier_candidates=identifiers,
        synonyms=synonyms,
        russian_variants=russian_variants,
        english_variants=english_variants,
        confidence=confidence,
    )


def _resolve_text(raw: str) -> _Resolved:
    lowered = raw.lower().strip()
    lowered = re.sub(r"\s+", " ", lowered)
    transliterated = lowered
    for source, target in sorted(BRAND_TRANSLITERATIONS.items(), key=lambda item: -len(item[0])):
        if source in transliterated:
            transliterated = transliterated.replace(source, target)
    optional_tokens = [token for token in OPTIONAL_TOKENS if token in transliterated]
    secondhand = any(token in transliterated for token in SECONDHAND_TOKENS)
    tokens = [token for token in re.split(r"[^a-zа-я0-9./\-]+", transliterated, flags=re.I) if token]
    canonical = " ".join(tokens)
    return _Resolved(canonical_text=canonical, tokens=tokens, has_secondhand=secondhand, optional_tokens=optional_tokens)


def _detect_category(canonical: str) -> tuple[str | None, str | None]:
    for category, label, keywords in (
        ("clothing", "одежда", ("шорты", "футболка", "куртка", "брюки", "джинсы", "кроссовки", "толстовка", "худи", "пиджак", "блузка", "платье", "юбка", "свитер")),
        ("tires", "шины", ("шина", "шины", "резина", "покрышка")),
        ("office_equipment", "оргтехника", ("принтер", "мфу", "сканер", "картридж", "laserjet", "inkjet", "deskjet", "pixma", "pantum")),
        ("electronics", "электроника", ("iphone", "macbook", "ipad", "galaxy", "redmi", "ноутбук", "смартфон", "планшет")),
        ("appliances", "бытовая техника", ("пылесос", "холодильник", "стиральная", "утюг", "блендер")),
        ("memory", "оперативная память", ("ddr", "оператив", "sodimm", "dimm")),
    ):
        if any(keyword in canonical for keyword in keywords):
            return category, label
    if re.search(r"\d{3}/\d{2}\s*r\d{2}", canonical):
        return "tires", "шины"
    return None, None


def _detect_brand(canonical: str, category: str | None) -> str | None:
    candidates: list[str] = []
    if category and category in BRAND_VOCABULARY:
        candidates.extend(BRAND_VOCABULARY[category])
    for brands in BRAND_VOCABULARY.values():
        candidates.extend(brands)
    seen: set[str] = set()
    for brand in candidates:
        if brand in seen:
            continue
        seen.add(brand)
        if re.search(rf"\b{re.escape(brand)}\b", canonical, flags=re.I):
            return brand
    for family, brand in FAMILY_TO_BRAND.items():
        token = family.strip()
        if re.search(rf"\b{re.escape(token)}", canonical, flags=re.I):
            return brand
    return None


def _detect_product_type(canonical: str) -> str | None:
    for product_type, hints in PRODUCT_TYPE_HINTS:
        if any(hint in canonical for hint in hints):
            return product_type
    if re.search(r"\d{3}/\d{2}\s*r\d{2}", canonical):
        return "tires"
    return None


def _extract_required_attributes(canonical: str, category: str | None, product_type: str | None) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for key, pattern in ATTRIBUTE_PATTERNS:
        match = pattern.search(canonical)
        if not match:
            continue
        if key == "tire_width":
            attrs["tire_width"] = int(match.group(1))
            attrs["tire_profile"] = int(match.group(2))
            attrs["tire_diameter"] = int(match.group(3))
        elif key == "tire_diameter" and "tire_diameter" not in attrs:
            attrs["tire_diameter"] = int(match.group(1))
        elif key in {"storage_gb", "ram_gb", "frequency_mhz"}:
            attrs[key] = int(match.group(1))
        elif key == "storage_tb":
            attrs[key] = float(match.group(1).replace(",", "."))
        elif key == "screen_inch":
            attrs[key] = float(match.group(1).replace(",", "."))
        elif key == "apple_chip":
            attrs["chip"] = match.group(0).lower()
        elif key == "snapdragon":
            attrs["chipset"] = match.group(0).lower()
        elif key == "ddr_version":
            attrs["ddr_version"] = int(match.group(1))
    if "iphone" in canonical:
        iphone_match = re.search(r"iphone\s*(\d{1,2})\s*(pro\s*max|pro|plus|mini|max)?", canonical, flags=re.I)
        if iphone_match:
            attrs["model_number"] = iphone_match.group(1)
            modifier = iphone_match.group(2)
            if modifier:
                attrs["model_modifier"] = re.sub(r"\s+", " ", modifier).strip().lower()
        tail = canonical.split("iphone", 1)[1]
        bare = re.search(r"\b(64|128|256|512|1024)\b", tail)
        if bare and "storage_gb" not in attrs:
            attrs["storage_gb"] = int(bare.group(1))
    if "macbook" in canonical or "ipad" in canonical:
        numbers = [int(value) for value in re.findall(r"\b(\d{2,4})\b", canonical) if 4 <= int(value) <= 8192]
        rams = [value for value in numbers if value in {8, 16, 24, 32, 64, 96, 128}]
        storages = [value for value in numbers if value in {64, 128, 256, 512, 1024, 2048, 4096, 8192}]
        if rams and "ram_gb" not in attrs:
            attrs["ram_gb"] = rams[0]
        if storages and "storage_gb" not in attrs:
            attrs["storage_gb"] = storages[0]
    if "galaxy" in canonical or "redmi" in canonical or "pixel" in canonical:
        bare = re.search(r"\b(64|128|256|512|1024)\b", canonical)
        if bare and "storage_gb" not in attrs:
            attrs["storage_gb"] = int(bare.group(1))
    if product_type == "graphics_card":
        gpu_match = re.search(r"\b(rtx|gtx|rx)\s*(\d{3,4})(?:\s*(ti|super|xt|le))?", canonical, flags=re.I)
        if gpu_match:
            attrs["gpu_series"] = gpu_match.group(1).lower()
            attrs["gpu_model"] = gpu_match.group(2)
            if gpu_match.group(3):
                attrs["gpu_modifier"] = gpu_match.group(3).lower()
    return attrs


def _detect_color(canonical: str) -> str | None:
    for color, aliases in COLOR_VOCABULARY.items():
        for alias in aliases:
            if re.search(rf"\b{re.escape(alias)}\b", canonical, flags=re.I):
                return color
    return None


def _extract_identifiers(canonical: str) -> dict[str, list[str]]:
    identifiers: dict[str, list[str]] = {"mpn": [], "gtin": [], "ean": [], "sku": []}
    for key, pattern in IDENTIFIER_PATTERNS:
        for match in pattern.findall(canonical):
            value = match if isinstance(match, str) else match[0]
            value = value.strip()
            if not value:
                continue
            target = {
                "ean13": "ean",
                "gtin12": "gtin",
                "mpn": "mpn",
                "sku": "sku",
            }[key]
            if value.lower() in (existing.lower() for existing in identifiers[target]):
                continue
            if target == "sku" and value.isalpha():
                continue
            identifiers[target].append(value)
    return identifiers


def _extract_model(canonical: str, brand: str | None) -> str | None:
    if not canonical:
        return None
    color_aliases: set[str] = set()
    for aliases in COLOR_VOCABULARY.values():
        color_aliases.update(aliases)
    storage_tokens = {"64", "128", "256", "512", "1024", "2048"}
    ram_tokens = {"8", "16", "24", "32", "64", "96"}
    stop_tokens = {"купить", "цена", "наличии"}
    tokens = canonical.split()
    if brand:
        brand_token = brand.split()[0]
        try:
            index = next(i for i, token in enumerate(tokens) if token == brand_token)
            tail = tokens[index + len(brand.split()) :]
        except StopIteration:
            tail = tokens
    else:
        tail = tokens
    candidate_parts: list[str] = []
    saw_alpha = False
    for token in tail:
        if token in stop_tokens:
            break
        lowered = token.lower()
        if lowered in color_aliases:
            continue
        if lowered in storage_tokens and saw_alpha:
            continue
        if lowered in ram_tokens and saw_alpha and len(candidate_parts) >= 2:
            continue
        candidate_parts.append(token)
        if any(ch.isalpha() for ch in token):
            saw_alpha = True
    cleaned = " ".join(candidate_parts[:6])
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,/-")
    return cleaned or None


def _detect_family(canonical: str, product_type: str | None) -> str | None:
    if "iphone" in canonical:
        return "iphone"
    if "macbook" in canonical:
        if "air" in canonical:
            return "macbook air"
        if "pro" in canonical:
            return "macbook pro"
        return "macbook"
    if "galaxy" in canonical:
        return "galaxy"
    if "thinkpad" in canonical:
        return "thinkpad"
    if "laserjet" in canonical:
        return "laserjet"
    if "pixma" in canonical:
        return "pixma"
    if "rtx" in canonical:
        return "rtx"
    if product_type in {"clothing", "tires"}:
        return None
    return product_type


def _build_variants(
    raw: str,
    canonical: str,
    brand: str | None,
    model: str | None,
    product_type: str | None,
) -> tuple[list[str], list[str], list[str]]:
    russian: list[str] = []
    english: list[str] = []
    synonyms: list[str] = []
    if raw:
        synonyms.append(raw.strip())
    if canonical and canonical not in synonyms:
        synonyms.append(canonical)
    if brand and model:
        english.append(f"{brand} {model}".strip())
    if brand:
        english.append(brand)
        russian.append(_to_russian(brand))
    if product_type:
        russian.append(_product_type_label(product_type))
    english = _dedupe(english)
    russian = _dedupe(russian)
    synonyms = _dedupe(synonyms)
    return russian, english, synonyms


def _build_negative_constraints(product_type: str | None) -> list[str]:
    if not product_type:
        return []
    return list(NEGATIVE_BY_TYPE.get(product_type, ()))


def _confidence_score(
    brand: str | None,
    product_type: str | None,
    required_attrs: dict[str, Any],
    identifiers: dict[str, list[str]],
) -> float:
    score = 0.0
    if brand:
        score += 0.3
    if product_type:
        score += 0.25
    if required_attrs:
        score += min(0.25, 0.05 * len(required_attrs))
    if any(identifiers[key] for key in identifiers):
        score += 0.2
    return min(1.0, score)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value.strip())
    return result


def _to_russian(text: str) -> str:
    reverse = {value: key for key, value in BRAND_TRANSLITERATIONS.items()}
    return reverse.get(text.lower(), text)


def _product_type_label(product_type: str) -> str:
    return {
        "smartphone": "смартфон",
        "laptop": "ноутбук",
        "tablet": "планшет",
        "printer": "принтер",
        "scanner": "сканер",
        "cartridge": "картридж",
        "monitor": "монитор",
        "tires": "шины",
        "clothing": "одежда",
        "vacuum": "пылесос",
        "graphics_card": "видеокарта",
    }.get(product_type, product_type)
