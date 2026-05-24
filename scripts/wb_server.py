from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from wb_common import DEFAULT_DEST
from wb_parser import WildberriesClient, WildberriesProductParser, WildberriesSearchParser


class WildberriesParserServer:
    def __init__(self) -> None:
        client = WildberriesClient()
        self.product_parser = WildberriesProductParser(client=client)
        self.search_parser = WildberriesSearchParser(
            client=client,
            product_parser=self.product_parser,
        )

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "marketplace": "wildberries"}

    def search(self, params: dict[str, list[str]]) -> dict[str, Any]:
        query = required_param(params, "query")
        limit = int_param(params, "limit", 10)
        page = int_param(params, "page", 1)
        sort = str_param(params, "sort", "popular")
        dest = str_param(params, "dest", DEFAULT_DEST)
        region_name = str_param(params, "region_name", "RU/Moscow")
        enrich = bool_param(params, "enrich", False)
        enrich_limit = optional_int_param(params, "enrich_limit")
        return self.search_parser.search(
            query,
            page=page,
            limit=limit,
            sort=sort,
            dest=dest,
            region_name=region_name,
            enrich=enrich,
            enrich_limit=enrich_limit,
        )

    def product(self, params: dict[str, list[str]]) -> dict[str, Any]:
        nm_or_url = first_present(params, ("id", "nm_id", "url", "nm_or_url"))
        if not nm_or_url:
            raise ValueError("Missing required query parameter: id, nm_id, url, or nm_or_url")
        dest = str_param(params, "dest", DEFAULT_DEST)
        region_name = str_param(params, "region_name", "RU/Moscow")
        include_price_history = not bool_param(params, "no_price_history", False)
        return self.product_parser.get_product(
            nm_or_url,
            dest=dest,
            region_name=region_name,
            include_price_history=include_price_history,
        )


def make_handler(app: WildberriesParserServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            try:
                if parsed.path == "/health":
                    send_json(self, app.health())
                elif parsed.path == "/search":
                    send_json(self, app.search(params))
                elif parsed.path == "/product":
                    send_json(self, app.product(params))
                else:
                    send_json(
                        self,
                        {"error": "not_found", "path": parsed.path},
                        status=HTTPStatus.NOT_FOUND,
                    )
            except ValueError as exc:
                send_json(self, {"error": "bad_request", "message": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                send_json(
                    self,
                    {"error": "internal_error", "message": str(exc)},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def send_json(
    handler: BaseHTTPRequestHandler,
    payload: dict[str, Any],
    *,
    status: HTTPStatus = HTTPStatus.OK,
) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def required_param(params: dict[str, list[str]], name: str) -> str:
    value = str_param(params, name, "")
    if not value:
        raise ValueError(f"Missing required query parameter: {name}")
    return value


def str_param(params: dict[str, list[str]], name: str, default: str) -> str:
    values = params.get(name)
    if not values:
        return default
    return values[0]


def int_param(params: dict[str, list[str]], name: str, default: int) -> int:
    value = str_param(params, name, str(default))
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Invalid integer query parameter {name}={value!r}") from exc


def optional_int_param(params: dict[str, list[str]], name: str) -> int | None:
    if name not in params:
        return None
    return int_param(params, name, 0)


def bool_param(params: dict[str, list[str]], name: str, default: bool) -> bool:
    value = str_param(params, name, str(default).lower()).lower()
    return value in {"1", "true", "yes", "y", "on"}


def first_present(params: dict[str, list[str]], names: tuple[str, ...]) -> str | None:
    for name in names:
        values = params.get(name)
        if values and values[0]:
            return values[0]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Wildberries parser HTTP server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    app = WildberriesParserServer()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    print(f"Wildberries parser server listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
