"""Hybrid product clustering for cross-marketplace search results.

Pipeline (no sklearn / numpy):
1. Hard-merge by (brand, model) when both are populated and model is informative.
2. Run TF-IDF over a mixed feature bag (brand, model, accessory tag, words,
   char n-grams) and add cosine-similarity edges into the same union-find.
3. Split each cluster along log10(price) if the spread exceeds ~1 decade — that
   pulls accessories (cases, screen protectors) away from the main product.
4. Auto-label each cluster from the most descriptive phrase in member titles.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any, Iterable

_PRICE_LOG_SPLIT_DECADES = 0.55  # ~3.5x price spread triggers a sub-split
_SIMILARITY_THRESHOLD = 0.32    # cosine over the mixed feature bag
_SIMILARITY_THRESHOLD_ACC = 0.18  # looser for same-accessory items
_SIMILARITY_THRESHOLD_SPEC = 0.22  # looser when both share a form-factor/spec tag
_CHAR_NGRAM_MIN = 3
_CHAR_NGRAM_MAX = 5
_MIN_MODEL_LEN = 2
_MAX_MODEL_LEN = 20             # match_keys.model often holds the full title; ignore noisy ones
_MAX_MODEL_WORDS = 3
# A primary key is "trusted" (allowed to gate) only when ≥ this many products share it.
# Otherwise messy WB models would block legitimate merges between near-identical items.
_TRUSTED_KEY_MIN_COUNT = 2

# Feature weights — chosen so a single matching strong signal already pushes
# similarity past the threshold; weak char-gram noise alone never does.
_W_MODEL = 8.0
_W_BRAND = 2.5
_W_ACCESSORY = 5.0
_W_SPEC = 9.0
_W_WORD = 2.0
_W_CHARGRAM = 1.0

# Accessory keywords let us split phone-vs-case even if titles share a lot of
# tokens; they also gate residue merges (an accessory item never merges with a
# non-accessory one even at high similarity).
_ACCESSORY_WORDS = {
    "чехол", "чехлы", "case",
    "пленка", "плёнка", "стекло", "защитное", "защитная",
    "кабель", "провод", "зарядка", "зарядное", "адаптер", "блок",
    "наушники", "гарнитура", "переходник", "док", "подставка",
    "ремешок", "браслет", "держатель",
    "сумка", "рюкзак",
    "картридж", "тонер",
    "лампа", "лампочка",
    "запчасть", "запчасти", "ремкомплект",
}

_KNOWN_CASING = {
    "iphone": "iPhone", "ipad": "iPad", "ipod": "iPod", "imac": "iMac",
    "macbook": "MacBook", "airpods": "AirPods", "airtag": "AirTag",
    "homepod": "HomePod", "xiaomi": "Xiaomi", "samsung": "Samsung",
    "huawei": "Huawei", "honor": "Honor", "tecno": "Tecno", "infinix": "Infinix",
    "sony": "Sony", "lg": "LG", "hp": "HP", "lenovo": "Lenovo", "msi": "MSI",
    "asus": "ASUS", "acer": "Acer", "dell": "Dell", "intel": "Intel", "amd": "AMD",
    "nvidia": "NVIDIA", "ssd": "SSD", "hdd": "HDD", "usb": "USB", "hdmi": "HDMI",
    "playstation": "PlayStation", "xbox": "Xbox",
}


def _fix_word_casing(word: str) -> str:
    return _KNOWN_CASING.get(word.lower(), word)


def _fix_phrase_casing(phrase: str) -> str:
    return " ".join(_fix_word_casing(w) for w in phrase.split())


_STOP_WORDS = {
    "и", "в", "с", "на", "для", "по", "от", "до", "не", "к", "за",
    "the", "and", "for", "with", "of", "to",
    "шт", "штук", "набор", "комплект",
    "новый", "новая", "оригинал", "оригинальный",
    "купить", "доставка",
}


def _normalize(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text)).lower()
    s = s.replace("ё", "е")
    s = re.sub(r"[^\w\s\-\+/]+", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _tokens(text: str) -> list[str]:
    if not text:
        return []
    return [t for t in re.split(r"[\s\-/]+", text) if t]


def _char_ngrams(text: str, n_min: int = _CHAR_NGRAM_MIN, n_max: int = _CHAR_NGRAM_MAX) -> Counter:
    """Char n-grams over space-padded text so word boundaries matter."""
    grams: Counter = Counter()
    if not text:
        return grams
    padded = f" {text} "
    for n in range(n_min, n_max + 1):
        if len(padded) < n:
            continue
        for i in range(len(padded) - n + 1):
            grams[padded[i:i + n]] += 1
    return grams


def _display_title(product: dict[str, Any]) -> str:
    """Original-case title preferred over normalized, for human-readable labels."""
    return str(product.get("title") or product.get("normalized_title") or "").strip()


def _norm_title(product: dict[str, Any]) -> str:
    """Lowercased title for similarity. Prefer normalized_title (parser-cleaned)."""
    return _normalize(product.get("normalized_title") or product.get("title") or "")


def _extract_brand(product: dict[str, Any]) -> str:
    brand = product.get("brand")
    if isinstance(brand, dict):
        name = brand.get("name") or ""
    else:
        name = brand or ""
    return _normalize(name)


def _display_brand(product: dict[str, Any]) -> str:
    brand = product.get("brand")
    if isinstance(brand, dict):
        return str(brand.get("name") or "").strip()
    return str(brand or "").strip()


def _extract_model(product: dict[str, Any]) -> str:
    """Extract model with noise filtering.

    WB's parser sometimes fills match_keys.model with the entire title — that
    pollutes (brand, model) bucketing because every item gets a unique key.
    Reject models longer than a short SKU code or that look like a sentence.
    """
    keys = product.get("match_keys") or {}
    model = keys.get("model")
    if not model:
        attrs = product.get("attributes") or {}
        norm = attrs.get("normalized") or {}
        model = norm.get("model")
    model_norm = _normalize(model or "")
    if not model_norm:
        return ""
    if len(model_norm) > _MAX_MODEL_LEN:
        return ""
    if len(_tokens(model_norm)) > _MAX_MODEL_WORDS:
        return ""
    # If the "model" is just the title, treat as no model.
    title_norm = _norm_title(product)
    if model_norm == title_norm or (title_norm and model_norm in title_norm and len(model_norm) > 12):
        return ""
    return model_norm


def _extract_price(product: dict[str, Any]) -> float | None:
    price = product.get("price") or {}
    cur = price.get("current")
    if isinstance(cur, (int, float)) and cur > 0:
        return float(cur)
    return None


def _accessory_tag(title: str) -> str:
    for tok in _tokens(title):
        if tok in _ACCESSORY_WORDS:
            return tok
    return ""


# Form-factor / spec extraction — battery sizes are a clean win, similar logic
# helps with screen sizes / memory tiers / paper sizes in other categories.
# Cyrillic АА/ААА (U+0410..) collapses to latin a's via _normalize, but the
# digit-prefix patterns (CR2032, LR44, R6, R03) need direct matching.
_SPEC_PATTERNS: list[tuple[Any, Any]] = [
    # Tire size: "205/55 R16", "205/55R16", "205x55 R16" — common WB/Ozon/YM
    # title format. Anchored on /xX{2-3}/R\d notation so we don't snag random
    # 3-digit pairs. Critical for tire clustering: two tires with the same
    # size belong together regardless of brand/season; different sizes don't.
    (re.compile(r"\b(\d{3})\s*[/x]\s*(\d{2})\s*r\s*(\d{2})\b", re.IGNORECASE),
     lambda m: f"tire:{m.group(1)}/{m.group(2)}r{m.group(3)}"),
    (re.compile(r"\bcr\s*(\d{3,4})\b", re.IGNORECASE), lambda m: f"cr{m.group(1)}"),
    (re.compile(r"\blr\s*0*(\d{1,4})\b", re.IGNORECASE), lambda m: f"lr{int(m.group(1)):02d}"),
    (re.compile(r"\br\s*0*(\d{1,4})\b", re.IGNORECASE), lambda m: f"r{int(m.group(1)):02d}"),
    # Latin AA/AAA/AAAA — boundary chars exclude word continuations like "AAB".
    (re.compile(r"(?<![a-zа-я0-9])a{2,4}(?![a-zа-я0-9])", re.IGNORECASE | re.UNICODE),
     lambda m: f"a{len(m.group(0))}"),
    # Cyrillic А/АА/ААА/АААА — same idea, separate pattern because [a-z] and
    # [а-я] are disjoint Unicode ranges.
    (re.compile(r"(?<![a-zа-я0-9])а{2,4}(?![a-zа-я0-9])", re.IGNORECASE | re.UNICODE),
     lambda m: f"a{len(m.group(0))}"),
    (re.compile(r"\b9\s*v\b", re.IGNORECASE), lambda m: "9v"),
    (re.compile(r"\b(\d+)\s*(?:gb|gib|гб)\b", re.IGNORECASE), lambda m: f"mem{m.group(1)}"),
    (re.compile(r"\b(\d+)\s*(?:tb|тб)\b", re.IGNORECASE), lambda m: f"mem{int(m.group(1))*1024}"),
]

# Categorical "season" tag for tires, clothing, and similar — products that
# differ on season alone (summer vs winter tires of the same size) should
# split into separate clusters.
_SEASON_PATTERNS = [
    (re.compile(r"\bвсесезон", re.IGNORECASE | re.UNICODE), "all-season"),
    (re.compile(r"\bлетн", re.IGNORECASE | re.UNICODE), "summer"),
    (re.compile(r"\bзимн", re.IGNORECASE | re.UNICODE), "winter"),
    (re.compile(r"\bдемисезон", re.IGNORECASE | re.UNICODE), "demi"),
]


def _extract_season(title: str) -> str:
    if not title:
        return ""
    for pat, tag in _SEASON_PATTERNS:
        if pat.search(title):
            return tag
    return ""

# Format synonyms — R6/LR06 → AA, R03/LR03 → AAA, etc. Lets Defender R6 and
# Energizer AA land in the same bucket even with different brand naming.
_SPEC_SYNONYMS = {
    "r06": "a2", "lr06": "a2", "a2": "a2",   # AA
    "r03": "a3", "lr03": "a3", "a3": "a3",   # AAA
    "r14": "c", "lr14": "c",                  # C
    "r20": "d", "lr20": "d",                  # D
}


def _extract_specs(title: str) -> list[str]:
    if not title:
        return []
    found: set[str] = set()
    for pat, mapper in _SPEC_PATTERNS:
        for m in pat.finditer(title):
            tag = mapper(m).lower()
            tag = _SPEC_SYNONYMS.get(tag, tag)
            found.add(tag)
    return sorted(found)


def _extract_specs_from_product(product: dict[str, Any]) -> list[str]:
    """Run spec extraction over BOTH the original title and normalized_title.

    Some marketplaces (YM) strip slashes from normalized_title, which kills
    tire-size patterns like `205/55 R16` → `205 55 R16`. Original title keeps
    them. Union the two passes to catch specs regardless of source quirks.
    """
    found: set[str] = set()
    for text in (product.get("title") or "", product.get("normalized_title") or ""):
        found.update(_extract_specs(text.lower().replace("ё", "е")))
    return sorted(found)


def _semantic_key(product: dict[str, Any]) -> str:
    """Bucket key when brand+model are both present and model looks informative."""
    brand = _extract_brand(product)
    model = _extract_model(product)
    if not brand or not model or len(model) < _MIN_MODEL_LEN:
        return ""
    has_digit = any(c.isdigit() for c in model)
    has_alpha = any(c.isalpha() for c in model)
    # Require either alphanumeric mix or longish model so we don't merge on "1".
    if not ((has_digit and has_alpha) or len(model) >= 4):
        return ""
    return f"{brand}|{model}"


def _vector_norm(vec: dict[str, float]) -> float:
    return math.sqrt(sum(v * v for v in vec.values()))


def _cosine(a_vec: dict[str, float], a_norm: float, b_vec: dict[str, float], b_norm: float) -> float:
    if a_norm == 0 or b_norm == 0:
        return 0.0
    if len(a_vec) > len(b_vec):
        a_vec, b_vec = b_vec, a_vec
    s = 0.0
    for tok, w in a_vec.items():
        bw = b_vec.get(tok)
        if bw is not None:
            s += w * bw
    return s / (a_norm * b_norm)


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


def _build_feature_bags(products: list[dict[str, Any]]) -> list[Counter]:
    bags: list[Counter] = []
    for p in products:
        title = _norm_title(p)
        brand = _extract_brand(p)
        model = _extract_model(p)
        acc = _accessory_tag(title)
        specs = _extract_specs_from_product(p)
        season = _extract_season(title)
        feats: Counter = Counter()
        if brand:
            feats[f"brand:{brand}"] = _W_BRAND
        if model and len(model) >= _MIN_MODEL_LEN:
            scope = brand or "_"
            feats[f"model:{scope}|{model}"] = _W_MODEL
        if acc:
            feats[f"acc:{acc}"] = _W_ACCESSORY
        for spec in specs:
            feats[f"spec:{spec}"] = _W_SPEC
        if season:
            feats[f"season:{season}"] = _W_ACCESSORY  # same weight as acc tag
        for word in _tokens(title):
            if word and word not in _STOP_WORDS and len(word) >= 2:
                feats[f"w:{word}"] += _W_WORD
        for ng, c in _char_ngrams(title).items():
            feats[ng] += _W_CHARGRAM * c
        bags.append(feats)
    return bags


def _tfidf_vectors(bags: list[Counter]) -> tuple[list[dict[str, float]], list[float]]:
    df: Counter = Counter()
    for bag in bags:
        for f in bag:
            df[f] += 1
    n_docs = max(1, len(bags))
    vectors: list[dict[str, float]] = []
    norms: list[float] = []
    for bag in bags:
        vec: dict[str, float] = {}
        for f, tf in bag.items():
            idf = math.log((n_docs + 1) / (df[f] + 1)) + 1.0
            vec[f] = tf * idf
        vectors.append(vec)
        norms.append(_vector_norm(vec))
    return vectors, norms


def _split_by_price(indices: list[int], prices: list[float | None]) -> list[list[int]]:
    """Split a cluster along big log10 price gaps; unpriced join the largest sub."""
    priced = [(i, prices[i]) for i in indices if prices[i] and prices[i] > 0]
    unpriced = [i for i in indices if not prices[i] or prices[i] <= 0]
    if len(priced) < 2:
        return [indices] if indices else []
    priced.sort(key=lambda pair: pair[1])
    groups: list[list[int]] = [[priced[0][0]]]
    prev_log = math.log10(priced[0][1])
    for idx, p in priced[1:]:
        cur_log = math.log10(p)
        if cur_log - prev_log >= _PRICE_LOG_SPLIT_DECADES:
            groups.append([idx])
        else:
            groups[-1].append(idx)
        prev_log = cur_log
    if unpriced:
        biggest = max(groups, key=len)
        biggest.extend(unpriced)
    return groups


_SEASON_LABEL_RU = {
    "summer": "Летние",
    "winter": "Зимние",
    "all-season": "Всесезонные",
    "demi": "Демисезонные",
}


def _label_for_cluster(
    products: list[dict[str, Any]],
    indices: list[int],
    query_terms: set[str],
) -> str:
    """Pick a label that adds info beyond the query."""
    # Tire-style label: shared (season, tire-size) wins over generic word soup.
    titles_norm = [_norm_title(products[i]) for i in indices]
    cluster_seasons = {_extract_season(t) for t in titles_norm if _extract_season(t)}
    tire_sizes: list[set[str]] = [
        {s for s in _extract_specs_from_product(products[i]) if s.startswith("tire:")}
        for i in indices
    ]
    flat_tires = [next(iter(ts)) for ts in tire_sizes if ts]
    if flat_tires and len(set(flat_tires)) == 1:
        tire_tag = flat_tires[0].split(":", 1)[1]  # e.g. "205/55r16"
        # Display: "205/55 R16" — capitalize R and split tokens
        m = re.match(r"(\d+)/(\d+)r(\d+)", tire_tag)
        tire_display = f"{m.group(1)}/{m.group(2)} R{m.group(3)}" if m else tire_tag.upper()
        season_label = ""
        if len(cluster_seasons) == 1:
            season_label = _SEASON_LABEL_RU.get(next(iter(cluster_seasons)), "")
        if season_label:
            return f"{season_label} {tire_display}"
        return f"Шины {tire_display}"

    brands = {_display_brand(products[i]).strip() for i in indices if _display_brand(products[i])}
    models = {_extract_model(products[i]) for i in indices if _extract_model(products[i])}
    if len(brands) == 1 and len(models) == 1:
        brand_disp = _fix_word_casing(next(iter(brands)))
        model_lc = next(iter(models))
        # Prefer the original-case model from any member's match_keys.
        model_disp: str | None = None
        for i in indices:
            for cand in (
                (products[i].get("match_keys") or {}).get("model"),
                ((products[i].get("attributes") or {}).get("normalized") or {}).get("model"),
            ):
                if cand and _normalize(cand) == model_lc:
                    model_disp = str(cand).strip()
                    break
            if model_disp:
                break
        if not model_disp:
            model_disp = model_lc
        model_disp = _fix_phrase_casing(model_disp)
        # If still all-lowercase (no known casing matched), title-case the leftover bits.
        if model_disp == model_disp.lower():
            model_disp = " ".join(w if w in _KNOWN_CASING.values() else w.title() for w in model_disp.split())
        return f"{brand_disp} {model_disp}".strip()

    titles_display = [_display_title(products[i]) for i in indices]
    tri_counts: Counter = Counter()
    bi_counts: Counter = Counter()
    uni_counts: Counter = Counter()
    accessory_label: str | None = None

    for title_disp in titles_display:
        title_lc = _normalize(title_disp)
        toks_disp = _tokens(title_disp)
        toks_lc = _tokens(title_lc)
        if accessory_label is None:
            for t in toks_lc:
                if t in _ACCESSORY_WORDS:
                    accessory_label = t
                    break
        # Index by lowercase but keep parallel display tokens for the label.
        for i in range(len(toks_lc) - 2):
            tri_counts[tuple(toks_lc[i:i + 3])] += 1
        for i in range(len(toks_lc) - 1):
            bi_counts[tuple(toks_lc[i:i + 2])] += 1
        for t in toks_lc:
            uni_counts[t] += 1

    def _score(phrase: tuple[str, ...], count: int) -> float:
        novel = sum(1 for t in phrase if t not in query_terms)
        return count * (1.0 + 0.5 * novel) * len(phrase)

    candidates: list[tuple[float, tuple[str, ...]]] = []
    for phrase, c in tri_counts.items():
        if c >= 2:
            candidates.append((_score(phrase, c), phrase))
    for phrase, c in bi_counts.items():
        if c >= 2:
            candidates.append((_score(phrase, c), phrase))
    if not candidates:
        # No contiguous-phrase consensus. Glue the top-2 shared unigrams
        # (e.g. "стекло защитное" vs "защитное стекло" both count toward this).
        top = [(tok, c) for tok, c in uni_counts.most_common(8)
               if tok and tok not in query_terms]
        repeated = [t for t in top if t[1] >= 2]
        if len(repeated) >= 2:
            phrase = (repeated[0][0], repeated[1][0])
            candidates.append((float(repeated[0][1] + repeated[1][1]) * 2, phrase))
        elif top:
            candidates.append((float(top[0][1]), (top[0][0],)))
    if not candidates and uni_counts:
        candidates.append((1.0, (uni_counts.most_common(1)[0][0],)))

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        best_phrase_lc = candidates[0][1]
        label = _phrase_with_display_casing(best_phrase_lc, titles_display)
        label = _fix_phrase_casing(label)
        if accessory_label and accessory_label not in best_phrase_lc:
            label = f"{accessory_label.capitalize()} · {label}"
        return label.strip()

    if titles_display:
        return sorted(titles_display, key=len)[0][:80]
    return "Без названия"


def _phrase_with_display_casing(phrase_lc: tuple[str, ...], titles: list[str]) -> str:
    """Find the phrase in a member title to recover original case; fallback to lc."""
    target = " ".join(phrase_lc)
    for title in titles:
        title_lc = _normalize(title)
        idx = title_lc.find(target)
        if idx < 0:
            continue
        # Map back to the original via token alignment: scan tokens.
        disp_toks = _tokens(title)
        lc_toks = _tokens(title_lc)
        for i in range(len(lc_toks) - len(phrase_lc) + 1):
            if tuple(lc_toks[i:i + len(phrase_lc)]) == phrase_lc:
                return " ".join(disp_toks[i:i + len(phrase_lc)])
    return " ".join(w.capitalize() if w.isascii() else w for w in phrase_lc)


def cluster_products(
    products: list[dict[str, Any]],
    *,
    query: str = "",
) -> list[dict[str, Any]]:
    """Cluster a mixed list of products into labeled groups.

    Returns a list of dicts (see frontend renderer for the consumed fields):
        {id, label, size, sources, price_min, price_max, price_median, member_keys}
    """
    if not products:
        return []
    n = len(products)
    uf = _UnionFind(n)

    # Stage 1: hard-merge by primary (brand, model) key.
    primary: dict[str, list[int]] = defaultdict(list)
    for i, p in enumerate(products):
        key = _semantic_key(p)
        if key:
            primary[key].append(i)
    for ids in primary.values():
        for j in ids[1:]:
            uf.union(ids[0], j)

    # Stage 2: TF-IDF similarity edges, gated by accessory flag mismatch and
    # by trusted-key disagreement only (lets near-duplicate SKUs of the same
    # noisy-model brand merge while keeping clean model lines apart).
    bags = _build_feature_bags(products)
    vectors, norms = _tfidf_vectors(bags)
    titles_norm = [_norm_title(p) for p in products]
    acc_tags = [_accessory_tag(t) for t in titles_norm]
    specs_per = [set(_extract_specs_from_product(p)) for p in products]
    seasons = [_extract_season(t) for t in titles_norm]
    # Extract tire-size specs for stricter gating — two products tagged with
    # different tire sizes are unambiguously different SKUs even if titles
    # share lots of vocab ("шины летние ...").
    tire_sizes: list[set[str]] = [
        {s for s in specs if s.startswith("tire:")} for specs in specs_per
    ]
    # A primary key is "trusted" only if at least 2 products share it — singletons
    # are usually WB's mis-parsed full-title models and shouldn't block merging.
    key_counts: Counter = Counter(k for k in primary.keys() for _ in primary[k])
    trusted = {k for k, ids in primary.items() if len(ids) >= _TRUSTED_KEY_MIN_COUNT}
    primary_keys = [_semantic_key(p) for p in products]
    is_trusted = [k in trusted for k in primary_keys]

    for a in range(n):
        for b in range(a + 1, n):
            if bool(acc_tags[a]) != bool(acc_tags[b]):
                continue
            # Season mismatch: summer vs winter / all-season vs winter etc.
            # Both must be tagged AND tags must differ — if one product has no
            # season info, allow merge based on similarity.
            if seasons[a] and seasons[b] and seasons[a] != seasons[b]:
                continue
            # Tire-size mismatch: when both sides have an explicit tire-size
            # spec and they differ, they are physically different SKUs.
            if tire_sizes[a] and tire_sizes[b] and tire_sizes[a].isdisjoint(tire_sizes[b]):
                continue
            # Half-mismatch: one item has a tire-size, the other doesn't. If
            # we let them merge at the normal threshold, a no-size middle item
            # chains clusters of different sizes together. Require strong
            # cosine to bridge through.
            tire_half_mismatch = bool(tire_sizes[a]) != bool(tire_sizes[b])
            # Only gate distinct primary keys when BOTH sides are trusted — that
            # protects iPhone 16 vs iPhone 16 Pro (clean models, ≥2 each) while
            # still letting noisy battery SKUs merge.
            if (
                is_trusted[a]
                and is_trusted[b]
                and primary_keys[a] != primary_keys[b]
            ):
                continue
            common_spec = specs_per[a] & specs_per[b]
            if acc_tags[a] and acc_tags[a] == acc_tags[b]:
                threshold = _SIMILARITY_THRESHOLD_ACC
            elif common_spec:
                threshold = _SIMILARITY_THRESHOLD_SPEC
            else:
                threshold = _SIMILARITY_THRESHOLD
            if tire_half_mismatch:
                threshold = max(threshold, 0.55)
            sim = _cosine(vectors[a], norms[a], vectors[b], norms[b])
            if sim >= threshold:
                uf.union(a, b)

    # Collect raw clusters.
    buckets: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        buckets[uf.find(i)].append(i)
    raw_clusters = list(buckets.values())

    # Stage 3: price split inside each cluster.
    prices = [_extract_price(p) for p in products]
    split_clusters: list[list[int]] = []
    for cluster in raw_clusters:
        split_clusters.extend(_split_by_price(cluster, prices))

    # Stage 4: assemble metadata + labels.
    query_terms = {t for t in _tokens(_normalize(query)) if t and t not in _STOP_WORDS}
    out: list[dict[str, Any]] = []
    for c_idx, cluster in enumerate(split_clusters):
        if not cluster:
            continue
        c_prices = [prices[i] for i in cluster if prices[i] is not None]
        sources: Counter = Counter(products[i].get("marketplace") or "unknown" for i in cluster)
        label = _label_for_cluster(products, cluster, query_terms)
        member_keys = [_product_key(products[i]) for i in cluster]
        out.append({
            "id": f"c{c_idx}",
            "label": label,
            "size": len(cluster),
            "sources": dict(sources),
            "price_min": min(c_prices) if c_prices else None,
            "price_max": max(c_prices) if c_prices else None,
            "price_median": _median(c_prices) if c_prices else None,
            "member_keys": member_keys,
        })

    # Sort: bigger clusters first; among same-size, higher median price first
    # so flagship sits above accessories.
    out.sort(key=lambda c: (-c["size"], -(c.get("price_median") or 0)))
    return out


def _product_key(product: dict[str, Any]) -> str:
    """Match the frontend's productStableKey() so member_keys round-trip."""
    marketplace = product.get("marketplace") or ""
    ext = product.get("external_id")
    if ext:
        return f"{marketplace}:{ext}"
    specific = product.get("marketplace_specific") or {}
    for candidate in (
        product.get("variant_id"),
        product.get("group_id"),
        specific.get("nm_id"),
        specific.get("sku"),
        specific.get("product_id"),
        specific.get("model_id"),
    ):
        if candidate not in (None, ""):
            return f"{marketplace}:{candidate}"
    return product.get("url") or product.get("title") or ""


def _median(values: Iterable[float]) -> float | None:
    arr = sorted(v for v in values if v is not None)
    if not arr:
        return None
    mid = len(arr) // 2
    if len(arr) % 2:
        return arr[mid]
    return (arr[mid - 1] + arr[mid]) / 2


if __name__ == "__main__":
    import json
    import sys
    payload = json.load(sys.stdin)
    products = payload.get("products") or payload
    print(json.dumps(cluster_products(products, query=payload.get("query") or ""), ensure_ascii=False, indent=2))
