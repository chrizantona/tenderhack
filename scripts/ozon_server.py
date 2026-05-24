from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ozon_parser import OzonClient, OzonProductParser, OzonSearchParser


class OzonParserServer:
    def __init__(self, *, use_selenium: bool = False, headless: bool = True, profile_dir: str | None = None) -> None:
        if use_selenium:
            from ozon_selenium import OzonSeleniumClient
            client: Any = OzonSeleniumClient(headless=headless, profile_dir=profile_dir)
            print("[ozon_server] Используем Selenium клиент.", flush=True)
        else:
            client = OzonClient()
        self.product_parser = OzonProductParser(client=client)
        self.search_parser = OzonSearchParser(
            client=client,
            product_parser=self.product_parser,
        )

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "marketplace": "ozon"}

    def search(self, params: dict[str, list[str]]) -> dict[str, Any]:
        query = required_param(params, "query")
        limit = int_param(params, "limit", 10)
        page = int_param(params, "page", 1)
        region_name = str_param(params, "region_name", "RU/Moscow")
        enrich = bool_param(params, "enrich", False)
        enrich_limit = optional_int_param(params, "enrich_limit")
        return self.search_parser.search(
            query,
            page=page,
            limit=limit,
            region_name=region_name,
            enrich=enrich,
            enrich_limit=enrich_limit,
        )

    def product(self, params: dict[str, list[str]]) -> dict[str, Any]:
        product_id_or_url = first_present(params, ("id", "sku", "product_id", "url", "product_id_or_url"))
        if not product_id_or_url:
            raise ValueError("Missing required query parameter: id, sku, product_id, url, or product_id_or_url")
        region_name = str_param(params, "region_name", "RU/Moscow")
        include_extra_layouts = not bool_param(params, "no_extra_layouts", False)
        return self.product_parser.get_product(
            product_id_or_url,
            region_name=region_name,
            include_extra_layouts=include_extra_layouts,
        )


def make_handler(app: OzonParserServer) -> type[BaseHTTPRequestHandler]:
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
    parser = argparse.ArgumentParser(description="Ozon parser HTTP server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--selenium", action="store_true", help="Использовать Selenium вместо requests")
    parser.add_argument("--no-headless", action="store_true", help="Показать браузер (только с --selenium)")
    parser.add_argument("--profile-dir", help="Путь к Chrome user profile (только с --selenium)")
    args = parser.parse_args()

    app = OzonParserServer(
        use_selenium=args.selenium,
        headless=not args.no_headless,
        profile_dir=args.profile_dir,
    )
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    print(f"Ozon parser server listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
