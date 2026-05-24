from __future__ import annotations

import argparse
import json
import platform
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import requests as plain_requests

try:
    import curl_cffi
    from curl_cffi import requests as curl_requests
    CURL_CFFI_VERSION = getattr(curl_cffi, "__version__", "unknown")
except ImportError as exc:
    curl_cffi = None
    curl_requests = None
    CURL_CFFI_VERSION = f"not_installed: {exc!r}"

from ozon_parser import extract_ozon_products


TIMEOUT = 8

PROBE_MATRIX = [
    ("api_chrome120_cold",         "https://api.ozon.ru",  "/composer-api.bx/page/json/v2",       "chrome120",  "off"),
    ("api_chrome120_cold_apiwarm", "https://api.ozon.ru",  "/composer-api.bx/page/json/v2",       "chrome120",  "api"),
    ("api_chrome124_cold",         "https://api.ozon.ru",  "/composer-api.bx/page/json/v2",       "chrome124",  "off"),
    ("api_chrome131_cold",         "https://api.ozon.ru",  "/composer-api.bx/page/json/v2",       "chrome131",  "off"),
    ("api_safari17_cold",          "https://api.ozon.ru",  "/composer-api.bx/page/json/v2",       "safari17_0", "off"),
    ("www_composer_chrome",        "https://www.ozon.ru",  "/api/composer-api.bx/page/json/v2",   "chrome124",  "www"),
    ("www_entry_chrome",           "https://www.ozon.ru",  "/api/entrypoint-api.bx/page/json/v2", "chrome124",  "www"),
    ("www_composer_plain",         "https://www.ozon.ru",  "/api/composer-api.bx/page/json/v2",   None,         "www"),
]

PLAIN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://www.ozon.ru",
    "Referer": "https://www.ozon.ru/",
}

CURL_EXTRA_HEADERS = {
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.ozon.ru/",
}


def env_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "curl_cffi": CURL_CFFI_VERSION,
    }
    try:
        r = plain_requests.get("https://ipinfo.io/json", timeout=5)
        info["ipinfo"] = r.json()
    except Exception as exc:
        info["ipinfo"] = {"error": repr(exc)[:200]}
    return info


def make_session(impersonate: str | None):
    if impersonate is None:
        s = plain_requests.Session()
        s.headers.update(PLAIN_HEADERS)
        return s, "plain_requests"
    if curl_requests is None:
        return None, "curl_cffi_not_installed"
    s = curl_requests.Session(impersonate=impersonate)
    s.headers.update(CURL_EXTRA_HEADERS)
    return s, f"curl_cffi/{impersonate}"


def cookie_names_from(obj) -> list[str]:
    jar = getattr(obj, "cookies", None)
    if jar is None:
        return []
    try:
        return list(dict(jar).keys())
    except Exception:
        try:
            return [getattr(c, "name", str(c)) for c in jar]
        except Exception:
            return []


def warmup_session(session, target: str) -> dict[str, Any]:
    if target == "off":
        return {"skipped": True}
    url = "https://www.ozon.ru/" if target == "www" else "https://api.ozon.ru/"
    started = time.monotonic()
    try:
        r = session.get(url, timeout=TIMEOUT, allow_redirects=True)
        elapsed = round((time.monotonic() - started) * 1000)
        return {
            "url": url,
            "status": r.status_code,
            "elapsed_ms": elapsed,
            "session_cookies": cookie_names_from(session)[:10],
            "response_cookies": cookie_names_from(r)[:10],
        }
    except Exception as exc:
        return {"url": url, "error": repr(exc)[:200]}


def widget_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"has_widgetStates": False}
    states = payload.get("widgetStates")
    if not isinstance(states, dict):
        return {"has_widgetStates": False}
    by_prefix: dict[str, int] = {}
    for k in states.keys():
        prefix = re.split(r"-", str(k), 1)[0]
        by_prefix[prefix] = by_prefix.get(prefix, 0) + 1
    return {
        "has_widgetStates": True,
        "total_keys": len(states),
        "by_prefix": by_prefix,
    }


def probe_url(session, base: str, path_family: str, ozon_path: str, *, follow: bool) -> dict[str, Any]:
    url = f"{base}{path_family}"
    started = time.monotonic()
    hops: list[dict[str, Any]] = []
    current_url = url
    current_params: dict[str, Any] | None = {"url": ozon_path}
    final_response = None
    error: str | None = None

    for hop_index in range(6):
        try:
            r = session.get(
                current_url,
                params=current_params,
                timeout=TIMEOUT,
                allow_redirects=False,
            )
        except Exception as exc:
            error = repr(exc)[:300]
            break
        hop_info = {
            "url": current_url,
            "status": r.status_code,
            "len": len(r.content or b""),
            "location": (r.headers or {}).get("location"),
            "set_cookie_has_abt": "abt_data" in ((r.headers or {}).get("set-cookie") or ""),
        }
        hops.append(hop_info)
        if not follow or r.status_code not in (301, 302, 303, 307, 308):
            final_response = r
            break
        location = (r.headers or {}).get("location") or ""
        if not location:
            final_response = r
            break
        if "__rr=" in location:
            final_response = r
            hop_info["antibot_loop"] = True
            break
        if location.startswith("/"):
            from urllib.parse import urljoin
            current_url = urljoin(current_url, location)
        elif location.startswith("http"):
            current_url = location
        else:
            current_url = urljoin(current_url, location)
        current_params = None
    else:
        if final_response is None and hops:
            final_response = r

    elapsed = round((time.monotonic() - started) * 1000)
    info: dict[str, Any] = {
        "elapsed_ms": elapsed,
        "hops": len(hops),
        "hop_trace": [
            {"status": h["status"], "len": h["len"], "loc_sample": (h.get("location") or "")[:120]}
            for h in hops
        ],
        "session_cookies": cookie_names_from(session)[:10],
    }
    if error is not None:
        info["error"] = error
        info["status"] = None
        return info
    if final_response is None:
        info["status"] = None
        info["error"] = "no_response"
        return info

    r = final_response
    body = r.content or b""
    headers = r.headers or {}
    info.update({
        "status": r.status_code,
        "len": len(body),
        "cf_mitigated": headers.get("cf-mitigated"),
        "server_timing": (headers.get("server-timing") or "")[:200] or None,
        "location": headers.get("location"),
        "set_cookie_has_abt": "abt_data" in (headers.get("set-cookie") or ""),
        "content_type": (headers.get("content-type") or "")[:80] or None,
    })
    text_preview = body[:300].decode("utf-8", errors="ignore")
    if r.status_code == 200 and body:
        try:
            payload = json.loads(body)
            info["widgets"] = widget_summary(payload)
            try:
                products = extract_ozon_products([payload])
                info["items_extracted"] = len(products)
                if products:
                    sample = products[0]
                    info["first_item"] = {
                        "id": sample.get("id") or sample.get("sku") or sample.get("productId"),
                        "title": (sample.get("title") or sample.get("name") or "")[:120],
                    }
            except Exception as exc:
                info["items_extracted_error"] = repr(exc)[:200]
        except json.JSONDecodeError:
            info["body_preview"] = text_preview
    else:
        info["body_preview"] = text_preview
        if text_preview.lstrip().startswith("{"):
            try:
                payload = json.loads(text_preview)
                if isinstance(payload, dict):
                    info["incidentId"] = payload.get("incidentId") or payload.get("incident_id")
                    info["message"] = (payload.get("message") or payload.get("error") or "")[:200] or None
            except Exception:
                pass
    return info


def run_combo(
    name: str,
    base: str,
    path_family: str,
    impersonate: str | None,
    warmup: str,
    *,
    query: str,
    product_id: str,
    follow: bool,
) -> None:
    print(f"\n===== {name}  host={base}  path={path_family}  impersonate={impersonate}  warmup={warmup}  follow={follow} =====")
    session, label = make_session(impersonate)
    if session is None:
        print(json.dumps({"skip": label}, ensure_ascii=False))
        return
    print(json.dumps({"client": label}, ensure_ascii=False))
    w = warmup_session(session, warmup)
    print(json.dumps({"warmup": w}, ensure_ascii=False))

    search_path = f"/search/?text={quote_plus(query)}&from_global=true&page=1"
    product_path = f"/product/{product_id}/"
    for kind, ozon_path in (("search", search_path), ("product", product_path)):
        for attempt in range(1, 4):
            info = probe_url(session, base, path_family, ozon_path, follow=follow)
            info["kind"] = kind
            info["attempt"] = attempt
            print(json.dumps(info, ensure_ascii=False, sort_keys=True))
            time.sleep(0.5)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ozon HTTP/TLS probe matrix.")
    parser.add_argument("--query", default="iphone 15")
    parser.add_argument("--product-id", default="397529235")
    parser.add_argument("--only", help="run only combos whose name contains this substring")
    parser.add_argument("--follow", action="store_true", help="follow 3xx redirects (skip __rr= antibot loops)")
    args = parser.parse_args()

    print("=" * 80)
    print("OZON PROBE")
    print("=" * 80)
    print(json.dumps({"env": env_info()}, ensure_ascii=False, indent=2))

    for combo in PROBE_MATRIX:
        if args.only and args.only not in combo[0]:
            continue
        try:
            run_combo(*combo, query=args.query, product_id=args.product_id, follow=args.follow)
        except Exception as exc:
            print(json.dumps({"combo_error": repr(exc)[:300], "combo": combo[0]}, ensure_ascii=False))

    print("\n" + "=" * 80)
    print("DONE")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
