from __future__ import annotations

import argparse
import json
from pathlib import Path

from .pipeline import rank_products
from .qwen import DEFAULT_HOST, DEFAULT_MODEL_PATH, DEFAULT_PORT, QwenClient, QwenServer


def main() -> None:
    args = parse_args()
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
        server = QwenServer(model_path=DEFAULT_MODEL_PATH, host=DEFAULT_HOST, port=DEFAULT_PORT)
        server.start()
        client = QwenClient(host=DEFAULT_HOST, port=DEFAULT_PORT)
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
    parser.add_argument("--top", type=int, default=10, help="How many best offers to return.")
    parser.add_argument("--no-qwen", action="store_true", help="Use deterministic heuristics only.")
    parser.add_argument("--stop-server", action="store_true", help="Stop Qwen server after processing.")
    return parser.parse_args()


if __name__ == "__main__":
    main()
