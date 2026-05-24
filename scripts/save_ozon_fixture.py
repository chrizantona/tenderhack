from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import ozon_common
from ozon_camoufox import OzonCamoufoxClient
from ozon_headless_common import OZON_BASE, wrap_iife


DEFAULT_QUERIES = ("iphone 15", "ноутбук игровой")
DEFAULT_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def save_one(client, query: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = ozon_common.search_page_path(query, page=1)
    full_url = OZON_BASE + path
    page = client._open_page()
    try:
        print(f"[fixture] goto: {full_url}", file=sys.stderr, flush=True)
        page.goto(full_url, timeout=45_000)
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception as exc:
            print(f"[fixture] networkidle skip: {exc!r}", file=sys.stderr)
        for _ in range(3):
            try:
                page.evaluate(wrap_iife("window.scrollBy(0, 2000); return 0;"))
                time.sleep(1.5)
            except Exception:
                break
        html = page.content()
        title = page.title()
    finally:
        client._close_page(page)

    slug = "ozon_search_" + "".join(c if c.isalnum() else "_" for c in query.lower())[:40] + ".html"
    target = out_dir / slug
    target.write_text(html, encoding="utf-8")
    print(f"[fixture] saved: {target}  ({len(html)} bytes, title={title!r})", file=sys.stderr, flush=True)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Save Ozon search HTML fixtures via Camoufox.")
    parser.add_argument("--queries", nargs="*", default=list(DEFAULT_QUERIES))
    parser.add_argument("--out", default=str(DEFAULT_DIR))
    parser.add_argument("--no-headless", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out)
    print(f"[fixture] starting camoufox (headless={not args.no_headless})...", file=sys.stderr)
    with OzonCamoufoxClient(headless=not args.no_headless, warmup=True) as client:
        print("[fixture] camoufox ready, capturing pages...", file=sys.stderr)
        for q in args.queries:
            try:
                save_one(client, q, out_dir)
            except Exception as exc:
                print(f"[fixture] ERROR for {q!r}: {exc!r}", file=sys.stderr)
                continue
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
