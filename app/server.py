from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

from aggregator import Aggregator, list_regions
from filters_service import collect_filters
from proxy_bridges import autostart as autostart_proxy_bridges


FRONTEND_DIR = ROOT / "frontend"
DEFAULT_SEARCH_LIMIT = 10
MAX_SEARCH_LIMIT = 100
DEFAULT_TASK_TIMEOUT = 600.0


def create_app(
    *,
    timeout: float = 15.0,
    ozon_headless: bool = True,
    ozon_enabled: bool = True,
    ozon_browser_binary: str | None = None,
    ozon_driver_binary: str | None = None,
    ozon_profile_dir: str | None = None,
    ozon_chrome_version: int | None = None,
    wb_default_enrich_limit: int = 10,
    other_runet_enabled: bool = True,
    qwen_enabled: bool = True,
    qwen_model_path: str | None = None,
    qwen_host: str = "127.0.0.1",
    qwen_port: int = 8765,
) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.logger.setLevel(logging.INFO)
    # Make all log lines show thread name + timestamp so parallel /api/product
    # calls don't blur together when debugging "не подгружается".
    for handler in logging.root.handlers + app.logger.handlers:
        handler.setFormatter(logging.Formatter(
            "[%(asctime)s][%(threadName)-14s][%(levelname).1s] %(message)s",
            datefmt="%H:%M:%S",
        ))
    aggregator = Aggregator(
        timeout=timeout,
        ozon_headless=ozon_headless,
        ozon_enabled=ozon_enabled,
        ozon_browser_binary=ozon_browser_binary,
        ozon_driver_binary=ozon_driver_binary,
        ozon_profile_dir=ozon_profile_dir,
        ozon_chrome_version=ozon_chrome_version,
        wb_default_enrich_limit=wb_default_enrich_limit,
        other_runet_enabled=other_runet_enabled,
        qwen_enabled=qwen_enabled,
        qwen_model_path=qwen_model_path,
        qwen_host=qwen_host,
        qwen_port=qwen_port,
    )
    app.config["AGGREGATOR"] = aggregator

    @app.route("/")
    def index():
        return send_from_directory(FRONTEND_DIR, "index.html")

    @app.route("/static/<path:filename>")
    def static_files(filename: str):
        return send_from_directory(FRONTEND_DIR, filename)

    @app.route("/api/health")
    def health():
        return jsonify(aggregator.health())

    @app.route("/api/proxy", methods=["GET", "POST"])
    def proxy_toggle():
        if request.method == "POST":
            try:
                payload = request.get_json(silent=True) or {}
            except Exception:
                payload = {}
            enabled_raw = payload.get("enabled")
            if not isinstance(enabled_raw, bool):
                return jsonify({"error": "bad_request", "message": "enabled must be boolean"}), 400
            try:
                result = aggregator.set_proxy_enabled(enabled_raw)
            except Exception as exc:
                return jsonify({"error": "internal_error", "message": repr(exc)}), 500
            return jsonify(result)
        return jsonify({"proxy_enabled": aggregator.get_proxy_enabled()})

    @app.route("/api/regions")
    def regions():
        return jsonify({"regions": list_regions()})

    @app.route("/api/search")
    def search():
        search_args, error = parse_search_args()
        if error is not None:
            return error
        try:
            result = aggregator.search(**search_args)
            app.logger.info("search summary: %s", summarize_search_result(result))
            return jsonify(result)
        except Exception as exc:
            return jsonify({"error": "internal_error", "message": repr(exc)}), 500

    @app.route("/api/search/stream")
    def search_stream():
        search_args, error = parse_search_args()
        if error is not None:
            return error

        def generate():
            try:
                for event in aggregator.search_events(**search_args):
                    if event.get("event") == "done":
                        app.logger.info("search summary: %s", summarize_search_result(event.get("data") or {}))
                    yield json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            except GeneratorExit:
                raise
            except Exception as exc:
                error_event = {
                    "event": "error",
                    "data": {"error": "internal_error", "message": repr(exc)},
                }
                yield json.dumps(error_event, ensure_ascii=False, separators=(",", ":")) + "\n"

        return Response(
            stream_with_context(generate()),
            mimetype="application/x-ndjson",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.route("/api/filters")
    def filters():
        query = request.args.get("query", "").strip()
        if not query:
            return jsonify({"error": "bad_request", "message": "Missing query"}), 400
        sources_param = request.args.get("sources", "").strip()
        sources = [s.strip() for s in sources_param.split(",") if s.strip()] or None
        wb_client = getattr(aggregator.wb_search, "client", None) if hasattr(aggregator, "wb_search") else None
        try:
            return jsonify(collect_filters(query, sources=sources, wb_client=wb_client))
        except ValueError as exc:
            return jsonify({"error": "bad_request", "message": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": "internal_error", "message": repr(exc)}), 500

    @app.route("/api/product")
    def product():
        source = request.args.get("source", "").strip()
        if not source:
            return jsonify({"error": "bad_request", "message": "Missing source"}), 400
        url_or_id = request.args.get("url") or request.args.get("id")
        if not url_or_id:
            return jsonify({"error": "bad_request", "message": "Missing url or id"}), 400
        region = request.args.get("region")
        import time as _t
        t0 = _t.time()
        log_ref = (url_or_id or "")[:80]
        app.logger.info("/api/product START source=%s url=%s", source, log_ref)
        try:
            result = aggregator.get_product(source, url_or_id, region=region)
        except ValueError as exc:
            app.logger.warning("/api/product BAD source=%s err=%s", source, exc)
            return jsonify({"error": "bad_request", "message": str(exc)}), 400
        except Exception as exc:
            app.logger.error("/api/product CRASH source=%s err=%r in %.1fs", source, exc, _t.time() - t0)
            return jsonify({"error": "internal_error", "message": repr(exc)}), 500
        # Log what came back so missing-characteristics issues are
        # diagnosable from the log alone.
        attrs_n = len(((result or {}).get("attributes") or {}).get("raw") or {})
        desc_n = len((result or {}).get("description") or "")
        title = ((result or {}).get("title") or "")[:60]
        err = (result or {}).get("error")
        elapsed = _t.time() - t0
        if err:
            app.logger.warning("/api/product DONE source=%s in %.1fs error=%s", source, elapsed, err)
        else:
            app.logger.info(
                "/api/product DONE source=%s in %.1fs attrs=%d desc=%d title=%r",
                source, elapsed, attrs_n, desc_n, title,
            )
        return jsonify(result)

    return app


def parse_search_args():
    query = request.args.get("query", "").strip()
    if not query:
        return None, (jsonify({"error": "bad_request", "message": "Missing query"}), 400)
    region = request.args.get("region")
    try:
        limit = int(request.args.get("limit", str(DEFAULT_SEARCH_LIMIT)))
    except ValueError:
        return None, (jsonify({"error": "bad_request", "message": "Invalid limit"}), 400)
    limit = max(1, min(limit, MAX_SEARCH_LIMIT))
    sources_param = request.args.get("sources", "").strip()
    sources = [s.strip() for s in sources_param.split(",") if s.strip()] or None
    enrich = request.args.get("enrich", "").lower() in {"1", "true", "yes", "on"}
    enrich_limit_raw = request.args.get("enrich_limit")
    try:
        enrich_limit = int(enrich_limit_raw) if enrich_limit_raw else None
    except ValueError:
        return None, (jsonify({"error": "bad_request", "message": "Invalid enrich_limit"}), 400)
    if enrich_limit is not None:
        enrich_limit = max(0, min(enrich_limit, limit))
    try:
        task_timeout = float(request.args.get("task_timeout", str(DEFAULT_TASK_TIMEOUT)))
    except ValueError:
        task_timeout = DEFAULT_TASK_TIMEOUT
    task_timeout = max(1.0, task_timeout)
    latitude, longitude, geo_error = parse_coordinates()
    if geo_error is not None:
        return None, geo_error
    filter_specs_raw = request.args.get("filter_specs", "").strip()
    filter_specs: list[dict] | None = None
    if filter_specs_raw:
        try:
            decoded = json.loads(filter_specs_raw)
        except json.JSONDecodeError:
            return None, (jsonify({"error": "bad_request", "message": "Invalid filter_specs"}), 400)
        if isinstance(decoded, list):
            filter_specs = decoded
    return {
        "query": query,
        "region": region,
        "latitude": latitude,
        "longitude": longitude,
        "limit": limit,
        "sources": sources,
        "enrich": enrich,
        "enrich_limit": enrich_limit,
        "task_timeout": task_timeout,
        "filter_specs": filter_specs,
    }, None


def parse_coordinates():
    lat_raw = request.args.get("lat")
    lon_raw = request.args.get("lon")
    if not lat_raw and not lon_raw:
        return None, None, None
    if not lat_raw or not lon_raw:
        return None, None, (jsonify({"error": "bad_request", "message": "Invalid coordinates"}), 400)
    try:
        latitude = float(lat_raw)
        longitude = float(lon_raw)
    except ValueError:
        return None, None, (jsonify({"error": "bad_request", "message": "Invalid coordinates"}), 400)
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None, None, (jsonify({"error": "bad_request", "message": "Invalid coordinates"}), 400)
    return latitude, longitude, None


def summarize_search_result(result: dict) -> dict:
    summary = {}
    for source, payload in (result.get("by_source") or {}).items():
        diagnostics = payload.get("diagnostics") or []
        summary[source] = {
            "status": payload.get("status"),
            "count": payload.get("count"),
            "error": payload.get("error"),
            "diagnostics": diagnostics[:3],
            "took_seconds": payload.get("took_seconds"),
        }
    return {
        "query": result.get("query"),
        "region": result.get("region_key"),
        "took_seconds": result.get("took_seconds"),
        "sources": summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="EasyTender aggregator HTTP server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--no-ozon", action="store_true")
    parser.add_argument("--ozon-headed", action="store_true", help="Run Ozon Selenium with visible Chrome window")
    parser.add_argument("--ozon-browser-binary", default=os.environ.get("OZON_BROWSER_BINARY"))
    parser.add_argument("--ozon-driver-binary", default=os.environ.get("OZON_CHROMEDRIVER"))
    parser.add_argument("--ozon-profile-dir", default=os.environ.get("OZON_PROFILE_DIR"))
    parser.add_argument("--ozon-chrome-version", type=int)
    parser.add_argument("--wb-enrich-limit", type=int, default=10)
    parser.add_argument(
        "--no-proxy-bridges",
        action="store_true",
        help="Skip autostarting local HTTP→SOCKS5 bridges for Ozon proxies.",
    )
    parser.add_argument("--no-other-runet", action="store_true", help="Disable other_runet source.")
    parser.add_argument("--no-qwen", action="store_true", help="Disable Qwen-assisted parser repair / ambiguous judge.")
    parser.add_argument("--qwen-model-path", default=os.environ.get("QWEN_MODEL_PATH"))
    parser.add_argument("--qwen-host", default="127.0.0.1")
    parser.add_argument("--qwen-port", type=int, default=8765)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    # Camoufox/Firefox can't auth against SOCKS5 directly. autostart_proxy_bridges
    # spawns one `gost` per upstream and rewrites OZON_PROXIES/OZON_PROXY_SCHEME
    # so the Aggregator below picks up plain-HTTP local bridges.
    bridges = None
    if not args.no_proxy_bridges and not args.no_ozon:
        bridges = autostart_proxy_bridges()
        if bridges is not None:
            print(
                f"Ozon proxy bridges: {len(bridges.urls)} local HTTP ports → SOCKS5 upstreams",
                flush=True,
            )

    app = create_app(
        timeout=args.timeout,
        ozon_headless=not args.ozon_headed,
        ozon_enabled=not args.no_ozon,
        ozon_browser_binary=args.ozon_browser_binary,
        ozon_driver_binary=args.ozon_driver_binary,
        ozon_profile_dir=args.ozon_profile_dir,
        ozon_chrome_version=args.ozon_chrome_version,
        wb_default_enrich_limit=args.wb_enrich_limit,
        other_runet_enabled=not args.no_other_runet,
        qwen_enabled=not args.no_qwen,
        qwen_model_path=args.qwen_model_path,
        qwen_host=args.qwen_host,
        qwen_port=args.qwen_port,
    )
    # NOTE: cannot pre-warm Ozon client here — Playwright's sync API binds
    # the browser to its creator thread, and Flask request handlers run in
    # a different worker thread, so cross-thread reuse blows up with
    # "Cannot switch to a different thread". First search pays the ~13s
    # warmup cost; subsequent searches reuse the same worker-thread client.

    print(f"EasyTender aggregator listening on http://{args.host}:{args.port}", flush=True)
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
