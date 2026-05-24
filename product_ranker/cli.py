from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None

from .pipeline import rank_products
from .qwen import DEFAULT_HOST, DEFAULT_MODEL_PATH, DEFAULT_PORT, QwenClient, QwenServer


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    query = args.query
    payload = json.loads(Path(args.cards).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        query = query or payload.get("query")
        cards = payload.get("cards") or payload.get("products") or []
    else:
        cards = payload
    if not query:
        raise SystemExit("query is required via --query or input JSON")
    if not isinstance(cards, list):
        raise SystemExit("cards must be a JSON list")

    qwen = None
    server = None
    if not args.no_qwen:
        model_path = args.model_path or config.get("qwen_model_path") or os.environ.get("QWEN_MODEL_PATH") or str(DEFAULT_MODEL_PATH)
        host = args.host or config.get("qwen_host") or DEFAULT_HOST
        port = int(args.port or config.get("qwen_port") or DEFAULT_PORT)
        server = QwenServer(model_path=model_path, host=host, port=port)
        server.start()
        client = QwenClient(host=host, port=port)
        if client.is_available():
            qwen = client

    result = rank_products(query=query, cards=cards, qwen=qwen, top_n=args.top)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if server is not None and args.stop_server:
        server.stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate, group, de-outlier, and rank product cards.")
    parser.add_argument("--query", help="User search query.")
    parser.add_argument("--cards", required=True, help="Path to JSON file with cards or {query,cards}.")
    parser.add_argument("--model-path", help="Path to local Qwen2.5-Coder-14B-Instruct-4bit.")
    parser.add_argument("--config", default="config.toml", help="Optional TOML config path.")
    parser.add_argument("--host", default=None, help="Qwen server host.")
    parser.add_argument("--port", type=int, default=None, help="Qwen server port.")
    parser.add_argument("--top", type=int, default=10, help="How many best offers to return.")
    parser.add_argument("--no-qwen", action="store_true", help="Use deterministic heuristics only.")
    parser.add_argument("--stop-server", action="store_true", help="Stop Qwen server after processing.")
    return parser.parse_args()


def load_config(path: str) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists() or tomllib is None:
        return {}
    with config_path.open("rb") as fh:
        return tomllib.load(fh)


if __name__ == "__main__":
    main()

