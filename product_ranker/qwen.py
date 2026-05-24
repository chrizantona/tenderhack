from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover - dry runs can work without requests.
    requests = None


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MODEL = "qwen2.5-coder-14b-instruct-4bit"
DEFAULT_MODEL_PATH = Path(
    os.environ.get(
        "QWEN_MODEL_PATH",
        str(Path.home() / "models/Qwen2.5-Coder-14B-Instruct-4bit"),
    )
)


@dataclass
class QwenResult:
    raw: str
    parsed: dict[str, Any] | None
    error: str | None
    elapsed_seconds: float


class QwenClient:
    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        model: str = DEFAULT_MODEL,
        timeout: float = 45.0,
    ) -> None:
        self.endpoint = f"http://{host}:{port}/v1/chat/completions"
        self.models_endpoint = f"http://{host}:{port}/v1/models"
        self.model = model
        self.timeout = timeout
        if requests is None:
            raise RuntimeError("requests is required when QwenClient is used")
        self.session = requests.Session()

    def is_available(self) -> bool:
        try:
            return self.session.get(self.models_endpoint, timeout=2).status_code == 200
        except requests.RequestException:
            return False

    def json(
        self,
        *,
        system: str,
        user: dict[str, Any],
        max_tokens: int = 700,
        temperature: float = 0.1,
    ) -> QwenResult:
        started = time.monotonic()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(user, ensure_ascii=False, indent=2)
                    + "\n\nReturn ONLY one valid JSON object.",
                },
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        try:
            response = self.session.post(self.endpoint, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            return QwenResult("", None, str(exc), time.monotonic() - started)
        elapsed = time.monotonic() - started
        if response.status_code != 200:
            return QwenResult(response.text[:500], None, f"http_{response.status_code}", elapsed)
        try:
            content = (response.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            return QwenResult(response.text[:500], None, f"bad_response: {exc}", elapsed)
        parsed = extract_json(content)
        return QwenResult(content, parsed, None if parsed is not None else "json_parse_failed", elapsed)


class QwenServer:
    def __init__(
        self,
        *,
        model_path: str | Path = DEFAULT_MODEL_PATH,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        startup_timeout: float = 90.0,
    ) -> None:
        self.model_path = Path(model_path)
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


def extract_json(text: str) -> dict[str, Any] | None:
    text = str(text or "").strip()
    if text.startswith("```"):
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.S | re.I)
        if fenced:
            text = fenced.group(1)
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


PASSPORT_SYSTEM = (
    "You are a deterministic ecommerce query parser. Extract the exact product intent. "
    "Return JSON with canonical_query, product_type, brand, model, required_attributes, "
    "negative_constraints, category_hint."
)

VALIDATION_SYSTEM = (
    "You are an ecommerce product-card judge. Decide if a candidate card is the same sold object "
    "as the target query passport. Reject accessories, compatible items, wrong models, services, "
    "used/refurbished items unless requested. Return JSON: decision accept|reject|review, "
    "confidence 0..1, category, reason, flags."
)
