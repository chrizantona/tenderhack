from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import requests


LOGGER = logging.getLogger("other_runet.qwen")

DEFAULT_QWEN_HOST = "127.0.0.1"
DEFAULT_QWEN_PORT = 8765
DEFAULT_MODEL_TAG = "qwen2.5-coder-14b-instruct-4bit"


class QwenUnavailable(RuntimeError):
    pass


@dataclass
class QwenResponse:
    raw: str
    parsed: dict[str, Any] | None
    error: str | None
    elapsed_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "parsed": self.parsed,
            "error": self.error,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


class QwenClient:
    def __init__(
        self,
        *,
        host: str = DEFAULT_QWEN_HOST,
        port: int = DEFAULT_QWEN_PORT,
        model: str = DEFAULT_MODEL_TAG,
        timeout: float = 45.0,
        connect_timeout: float = 3.0,
    ) -> None:
        self.host = host
        self.port = port
        self.model = model
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.endpoint = f"http://{host}:{port}/v1/chat/completions"
        self.models_endpoint = f"http://{host}:{port}/v1/models"
        self.session = requests.Session()
        self._available: bool | None = None
        self._available_checked_at: float = 0.0

    def is_available(self, *, force: bool = False, ttl_seconds: float = 30.0) -> bool:
        now = time.monotonic()
        if not force and self._available is not None and (now - self._available_checked_at) < ttl_seconds:
            return self._available
        try:
            response = self.session.get(self.models_endpoint, timeout=self.connect_timeout)
            self._available = response.status_code == 200
        except requests.RequestException:
            self._available = False
        self._available_checked_at = now
        return self._available

    def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.1,
    ) -> QwenResponse:
        if not self.is_available():
            return QwenResponse(raw="", parsed=None, error="qwen_unavailable", elapsed_seconds=0.0)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt + "\n\nReturn ONLY a single JSON object. Do not include explanations."},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        started = time.monotonic()
        try:
            response = self.session.post(self.endpoint, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            self._available = False
            return QwenResponse(raw="", parsed=None, error=str(exc), elapsed_seconds=time.monotonic() - started)
        elapsed = time.monotonic() - started
        if response.status_code != 200:
            return QwenResponse(raw=response.text[:500], parsed=None, error=f"http_{response.status_code}", elapsed_seconds=elapsed)
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            return QwenResponse(raw=response.text[:500], parsed=None, error=f"bad_response_json: {exc}", elapsed_seconds=elapsed)
        choices = data.get("choices") or []
        if not choices:
            return QwenResponse(raw=json.dumps(data)[:500], parsed=None, error="no_choices", elapsed_seconds=elapsed)
        message = choices[0].get("message") or {}
        content = (message.get("content") or "").strip()
        if not content:
            return QwenResponse(raw="", parsed=None, error="empty_content", elapsed_seconds=elapsed)
        parsed = _extract_json(content)
        return QwenResponse(
            raw=content,
            parsed=parsed,
            error=None if parsed is not None else "json_parse_failed",
            elapsed_seconds=elapsed,
        )


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.S | re.I)
        if fence:
            text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    candidate = re.search(r"\{.*\}", text, flags=re.S)
    if not candidate:
        return None
    try:
        return json.loads(candidate.group(0))
    except json.JSONDecodeError:
        return None


SOLD_OBJECT_SYSTEM = (
    "You are a Russian e-commerce search judge. "
    "Given a target product passport and a candidate product title, decide if the candidate is the SAME sold object. "
    "Reject accessories like чехол/стекло/плёнка/ремешок/кабель when the relation is 'для', 'compatible with', 'fits'. "
    "Output JSON with fields: decision (one of 'accept','reject','review'), confidence (0..1), "
    "sold_object_resolution { same_sold_object, sold_object, reference_object, relation, reason }, "
    "failure_reason (nullable)."
)


def build_sold_object_prompt(*, passport: dict[str, Any], candidate_title: str, breadcrumbs: list[str] | None, description: str | None) -> str:
    breadcrumb_text = " > ".join(breadcrumbs) if breadcrumbs else ""
    description_text = (description or "")[:600]
    payload = {
        "passport": {
            "canonical_query": passport.get("canonical_query"),
            "brand": passport.get("brand"),
            "product_family": passport.get("product_family"),
            "model": passport.get("model"),
            "product_type": passport.get("product_type"),
            "required_attributes": passport.get("required_attributes"),
            "negative_constraints": passport.get("negative_constraints"),
        },
        "candidate": {
            "title": candidate_title,
            "breadcrumbs": breadcrumb_text,
            "description": description_text,
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


PARSER_RECIPE_SYSTEM = (
    "You are a deterministic web scraping recipe generator. "
    "Given the URL, domain, page HTML excerpts (head/body/scripts) and missing fields, propose a JSON recipe to extract price, title, availability. "
    "Allowed strategies: 'json_ld_then_html', 'internal_api', 'next_data', 'nuxt_state', 'html_selectors', 'playwright_required'. "
    "Output JSON: { domain, strategy, selectors: { title, price, availability, breadcrumbs }, json_paths: { title, price, availability, sku, brand }, "
    "requires_city, notes, confidence }. Selectors must be plain CSS selectors with no JavaScript."
)


def build_parser_recipe_prompt(*, url: str, domain: str, html_excerpt: str, missing_fields: list[str]) -> str:
    excerpt = html_excerpt[:6000]
    payload = {
        "url": url,
        "domain": domain,
        "missing_fields": missing_fields,
        "html_excerpt": excerpt,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
