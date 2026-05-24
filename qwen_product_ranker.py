from __future__ import annotations

import argparse
import atexit
import json
import math
import re
import shutil
import socket
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    requests = None


QWEN_MODEL_PATH = Path("~/models/Qwen2.5-Coder-14B-Instruct-4bit").expanduser()
QWEN_HOST = "127.0.0.1"
QWEN_PORT = 8765
QWEN_MODEL_NAME = "qwen2.5-coder-14b-instruct-4bit"

ACCESSORY_WORDS = {
    "чехол",
    "стекло",
    "пленка",
    "плёнка",
    "кабель",
    "зарядка",
    "ремешок",
    "держатель",
    "адаптер",
}

PASSPORT_PROMPT = (
    "Parse the exact ecommerce product intent from the user query. "
    "Return JSON with canonical_query, product_type, brand, model, "
    "required_attributes, negative_constraints, category_hint."
)

VALIDATION_PROMPT = (
    "Decide if the candidate product card is the same sold object as the target query. "
    "Reject accessories, compatible items, wrong models, services, and refurbished/used goods "
    "unless they are explicitly requested. Return JSON: decision accept|reject|review, "
    "confidence 0..1, category, reason, flags."
)


@dataclass
class QwenAnswer:
    raw: str
    parsed: dict[str, Any] | None
    error: str | None
    elapsed_seconds: float


class LocalQwenServer:
    def __init__(
        self,
        model_path: Path = QWEN_MODEL_PATH,
        host: str = QWEN_HOST,
        port: int = QWEN_PORT,
        startup_timeout: float = 90.0,
    ) -> None:
        self.model_path = model_path
        self.host = host
        self.port = port
        self.startup_timeout = startup_timeout
        self.process: subprocess.Popen | None = None

    def is_listening(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            try:
                sock.connect((self.host, self.port))
                return True
            except OSError:
                return False

    def start(self) -> bool:
        if self.is_listening():
            return True
        if not self.model_path.exists():
            return False
        if not shutil.which(sys.executable):
            return False
        command = [
            sys.executable,
            "-m",
            "mlx_lm.server",
            "--model",
            str(self.model_path),
            "--host",
            self.host,
            "--port",
            str(self.port),
        ]
        try:
            self.process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except OSError:
            return False
        atexit.register(self.stop)

        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                return False
            if self.is_listening():
                return True
            time.sleep(0.5)
        return False

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None


class QwenClient:
    def __init__(
        self,
        host: str = QWEN_HOST,
        port: int = QWEN_PORT,
        model: str = QWEN_MODEL_NAME,
        timeout: float = 45.0,
    ) -> None:
        if requests is None:
            raise RuntimeError("requests is required for QwenClient")
        self.endpoint = f"http://{host}:{port}/v1/chat/completions"
        self.models_endpoint = f"http://{host}:{port}/v1/models"
        self.model = model
        self.timeout = timeout
        self.session = requests.Session()

    def is_available(self) -> bool:
        try:
            return self.session.get(self.models_endpoint, timeout=2).status_code == 200
        except requests.RequestException:
            return False

    def json(
        self,
        system_prompt: str,
        payload: dict[str, Any],
        *,
        max_tokens: int = 700,
        temperature: float = 0.1,
    ) -> QwenAnswer:
        started = time.monotonic()
        request_payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, indent=2)
                    + "\n\nReturn only one valid JSON object.",
                },
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        try:
            response = self.session.post(self.endpoint, json=request_payload, timeout=self.timeout)
        except requests.RequestException as exc:
            return QwenAnswer("", None, str(exc), time.monotonic() - started)

        elapsed = time.monotonic() - started
        if response.status_code != 200:
            return QwenAnswer(response.text[:500], None, f"http_{response.status_code}", elapsed)
        try:
            data = response.json()
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            return QwenAnswer(response.text[:500], None, f"bad_response: {exc}", elapsed)
        parsed = extract_json(content)
        return QwenAnswer(content, parsed, None if parsed is not None else "json_parse_failed", elapsed)


def rank_cards(query: str, cards: list[dict[str, Any]], *, qwen: QwenClient | None = None, top: int = 10) -> dict[str, Any]:
    passport = build_passport(query, qwen)
    validated = [validate_card(query, passport, card, qwen) for card in cards]
    accepted = [item for item in validated if not item["rejected"]]

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in accepted:
        groups[item["category"]].append(item)

    ranked_groups = []
    all_ranked = []
    for category, group_items in groups.items():
        ranked = mark_outliers_and_score(query, group_items)
        all_ranked.extend(ranked)
        ranked_groups.append(
            {
                "category": category,
                "count": len(ranked),
                "items": sorted(ranked, key=lambda item: item["final_score"], reverse=True),
            }
        )

    best = [
        item
        for item in sorted(all_ranked, key=lambda item: item["final_score"], reverse=True)
        if not item["hidden"]
    ][:top]

    return {
        "query": query,
        "passport": passport,
        "best": best,
        "groups": sorted(ranked_groups, key=lambda item: (-item["count"], item["category"])),
        "rejected": [item for item in validated if item["rejected"]],
        "diagnostics": {
            "cards_in": len(cards),
            "accepted": len(accepted),
            "rejected": len(cards) - len(accepted),
            "qwen_model_path": str(QWEN_MODEL_PATH),
            "qwen_used": bool(qwen and qwen.is_available()),
        },
    }


def build_passport(query: str, qwen: QwenClient | None) -> dict[str, Any]:
    if qwen and qwen.is_available():
        answer = qwen.json(PASSPORT_PROMPT, {"query": query}, max_tokens=500)
        if answer.parsed:
            return answer.parsed
    return heuristic_passport(query)


def heuristic_passport(query: str) -> dict[str, Any]:
    norm = normalize_text(query)
    words = norm.split()
    brand = None
    for candidate in ("apple", "samsung", "xiaomi", "huawei", "honor", "sony", "lg"):
        if candidate in words:
            brand = candidate
            break
    product_type = "unknown"
    if any(word in words for word in ("iphone", "смартфон", "телефон")):
        product_type = "smartphones"
    elif any(word in words for word in ("ноутбук", "laptop", "macbook")):
        product_type = "laptops"
    elif any(word in words for word in ("шины", "резина")):
        product_type = "tires"
    return {
        "canonical_query": query.strip(),
        "product_type": product_type,
        "brand": brand,
        "model": " ".join(words[:6]) or None,
        "required_attributes": {},
        "negative_constraints": sorted(ACCESSORY_WORDS),
        "category_hint": product_type,
    }


def validate_card(
    query: str,
    passport: dict[str, Any],
    card: dict[str, Any],
    qwen: QwenClient | None,
) -> dict[str, Any]:
    compact = compact_card(card)
    flags = []
    if not compact["title"]:
        flags.append("missing_title")
    if compact["price"] is None:
        flags.append("missing_price")
    if not compact["url"] and not compact["external_id"]:
        flags.append("missing_identity")

    validation = None
    if qwen and qwen.is_available() and compact["title"]:
        answer = qwen.json(
            VALIDATION_PROMPT,
            {"passport": passport, "candidate": compact},
            max_tokens=500,
        )
        validation = answer.parsed

    if validation:
        decision = str(validation.get("decision") or "review").lower()
        confidence = float_or(validation.get("confidence"), 0.5)
        reason = str(validation.get("reason") or "")
        category = str(validation.get("category") or compact["category"] or passport.get("category_hint") or "other")
        model_flags = validation.get("flags") if isinstance(validation.get("flags"), list) else []
    else:
        decision, confidence, reason, category, model_flags = heuristic_validation(query, passport, card)

    rejected = False
    reject_reason = None
    if "missing_title" in flags:
        rejected = True
        reject_reason = "missing_title"
    elif decision == "reject":
        rejected = True
        reject_reason = reason or "rejected"
    elif is_accessory_mismatch(query, compact["title"]):
        rejected = True
        reject_reason = "accessory_mismatch"

    return {
        "card": card,
        "compact": compact,
        "category": normalize_category(category),
        "validation": {
            "decision": decision,
            "confidence": round(confidence, 3),
            "reason": reason,
            "flags": list(model_flags) + flags,
        },
        "rejected": rejected,
        "reject_reason": reject_reason,
    }


def heuristic_validation(
    query: str,
    passport: dict[str, Any],
    card: dict[str, Any],
) -> tuple[str, float, str, str, list[str]]:
    query_tokens = tokens(query)
    title_tokens = tokens(" ".join([title_of(card), brand_of(card), category_of(card)]))
    overlap = len(query_tokens & title_tokens) / max(1, len(query_tokens))
    flags = []
    decision = "accept" if overlap >= 0.35 else "review"
    if overlap < 0.15:
        decision = "reject"
        flags.append("low_text_overlap")
    if is_accessory_mismatch(query, title_of(card)):
        decision = "reject"
        flags.append("accessory_mismatch")
    category = category_of(card) or str(passport.get("category_hint") or "other")
    return decision, min(1.0, max(0.1, overlap + 0.35)), f"text_overlap={overlap:.2f}", category, flags


def mark_outliers_and_score(query: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prices = [price_of(item["card"]) for item in items]
    clean = [price for price in prices if price is not None and price > 0]
    median_price = statistics.median(clean) if clean else None
    low_threshold = median_price * 0.65 if median_price else None
    high_threshold = median_price * 1.80 if median_price else None

    ranked = []
    for item in items:
        card = item["card"]
        price = price_of(card)
        price_flags = []
        hidden = False
        price_score = 0.7

        if price is None:
            price_flags.append("missing_price")
            price_score = 0.3
        elif median_price:
            if low_threshold and price <= low_threshold:
                price_flags.append("suspicious_low_price")
                hidden = True
                price_score = 0.15
            elif high_threshold and price >= high_threshold:
                price_flags.append("suspicious_high_price")
                price_score = 0.45
            else:
                deviation = abs(price - median_price) / median_price
                price_score = max(0.35, 1.0 - deviation)

        score = final_score(
            query=query,
            card=card,
            validation_confidence=item["validation"]["confidence"],
            price_score=price_score,
            hidden=hidden,
        )
        ranked.append(
            {
                **item,
                "price_context": {
                    "median": median_price,
                    "low_threshold": low_threshold,
                    "high_threshold": high_threshold,
                },
                "price_flags": price_flags,
                "hidden": hidden,
                "final_score": round(score, 4),
            }
        )
    return ranked


def final_score(
    *,
    query: str,
    card: dict[str, Any],
    validation_confidence: float,
    price_score: float,
    hidden: bool,
) -> float:
    score = (
        validation_confidence * 0.35
        + text_score(query, card) * 0.20
        + price_score * 0.15
        + availability_score(availability_of(card)) * 0.10
        + rating_score(card) * 0.10
        + seller_score(card) * 0.05
        + source_score(card) * 0.05
    )
    if hidden:
        score *= 0.35
    return max(0.0, min(1.0, score))


def compact_card(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "marketplace": card.get("marketplace"),
        "external_id": card.get("external_id"),
        "url": card.get("url"),
        "title": title_of(card),
        "brand": brand_of(card),
        "category": category_of(card),
        "price": price_of(card),
        "rating": get_path(card, "rating.value"),
        "reviews_count": get_path(card, "rating.reviews_count"),
        "availability": availability_of(card),
        "attributes": card.get("attributes") or {},
        "description": str(card.get("description") or "")[:800],
    }


def title_of(card: dict[str, Any]) -> str:
    return str(card.get("title") or "").strip()


def brand_of(card: dict[str, Any]) -> str:
    brand = card.get("brand")
    if isinstance(brand, dict):
        return str(brand.get("name") or "").strip()
    return str(brand or "").strip()


def category_of(card: dict[str, Any]) -> str:
    category = card.get("category")
    if isinstance(category, dict):
        return str(category.get("name") or "").strip()
    return str(category or "").strip()


def price_of(card: dict[str, Any]) -> float | None:
    value = get_path(card, "price.current")
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def availability_of(card: dict[str, Any]) -> str | None:
    value = get_path(card, "availability.status")
    return str(value).strip() if value else None


def get_path(data: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return default
        current = current.get(part)
        if current is None:
            return default
    return current


def normalize_text(value: str | None) -> str:
    text = str(value or "").lower().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokens(value: str | None) -> set[str]:
    return {part for part in normalize_text(value).split() if len(part) > 1}


def normalize_category(value: str) -> str:
    text = normalize_text(value)
    if not text or text == "unknown":
        return "other"
    mapping = {
        "smartphone": "smartphones",
        "смартфон": "smartphones",
        "смартфоны": "smartphones",
        "телефон": "smartphones",
        "телефоны": "smartphones",
        "мобильный телефон": "smartphones",
        "мобильные телефоны": "smartphones",
        "laptop": "laptops",
        "ноутбук": "laptops",
        "ноутбуки": "laptops",
        "tires": "tires",
        "шина": "tires",
        "шины": "tires",
    }
    return mapping.get(text, text)


def is_accessory_mismatch(query: str, title: str) -> bool:
    query_wants_accessory = bool(tokens(query) & ACCESSORY_WORDS)
    if query_wants_accessory:
        return False
    return bool(tokens(title) & ACCESSORY_WORDS)


def text_score(query: str, card: dict[str, Any]) -> float:
    query_tokens = tokens(query)
    card_tokens = tokens(" ".join([title_of(card), brand_of(card), category_of(card)]))
    if not query_tokens:
        return 0.0
    return len(query_tokens & card_tokens) / len(query_tokens)


def availability_score(status: str | None) -> float:
    if status == "in_stock":
        return 1.0
    if status == "limited":
        return 0.8
    if status == "preorder":
        return 0.5
    if status == "out_of_stock":
        return 0.1
    return 0.4


def rating_score(card: dict[str, Any]) -> float:
    rating = float_or(get_path(card, "rating.value"), None)
    reviews = float_or(get_path(card, "rating.reviews_count"), 0.0) or 0.0
    rating_part = 0.5 if rating is None else min(1.0, max(0.0, rating / 5.0))
    reviews_part = min(1.0, math.log10(reviews + 1) / 4.0)
    return rating_part * 0.7 + reviews_part * 0.3


def seller_score(card: dict[str, Any]) -> float:
    seller = card.get("seller")
    if isinstance(seller, dict) and seller.get("name"):
        return 0.7
    return 0.4


def source_score(card: dict[str, Any]) -> float:
    return 0.8 if card.get("marketplace") else 0.5


def float_or(value: Any, default: float | None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def extract_json(text: str) -> dict[str, Any] | None:
    text = str(text or "").strip()
    if text.startswith("```"):
        match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.S | re.I)
        if match:
            text = match.group(1)
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def load_cards(path: str) -> tuple[str | None, list[dict[str, Any]]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        cards = payload.get("cards") or payload.get("products") or []
        if not isinstance(cards, list):
            raise ValueError("cards/products must be a list")
        return payload.get("query"), cards
    if isinstance(payload, list):
        return None, payload
    raise ValueError("input JSON must be a list or an object with cards/products")


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate, group, de-outlier, and rank product cards.")
    parser.add_argument("--query", help="Search query.")
    parser.add_argument("--cards", required=True, help="JSON file with cards or {query,cards}.")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--no-qwen", action="store_true")
    parser.add_argument("--stop-server", action="store_true")
    args = parser.parse_args()

    input_query, cards = load_cards(args.cards)
    query = args.query or input_query
    if not query:
        raise SystemExit("query is required")

    server = None
    qwen = None
    if not args.no_qwen:
        server = LocalQwenServer()
        server.start()
        if requests is not None:
            client = QwenClient()
            if client.is_available():
                qwen = client

    result = rank_cards(query, cards, qwen=qwen, top=args.top)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if server and args.stop_server:
        server.stop()


if __name__ == "__main__":
    main()
