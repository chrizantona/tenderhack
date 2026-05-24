from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import ozon_common
import undetected_chromedriver as uc
from selenium.webdriver.support.ui import WebDriverWait

OZON_BASE = "https://www.ozon.ru"
WARMUP_TIMEOUT = 25
PAGE_LOAD_TIMEOUT = 30

CHROME_BINARY_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)


class OzonSeleniumError(RuntimeError):
    pass


class OzonSeleniumClient:
    """
    Drop-in замена OzonClient из ozon_parser.py.
    Парсит DOM тайлов товаров со страницы поиска/карточки Ozon.
    """

    def __init__(
        self,
        *,
        headless=True,
        warmup=True,
        profile_dir=None,
        version_main=None,
        browser_binary=None,
        driver_binary=None,
    ):
        browser_binary = browser_binary or discover_browser_binary()
        driver_binary = driver_binary or discover_driver_binary()
        if not browser_binary:
            raise OzonSeleniumError(
                "Chrome/Chromium browser binary not found. Install Google Chrome "
                "or pass --browser-binary /path/to/browser."
            )
        self._driver = _make_driver(
            headless=headless,
            profile_dir=profile_dir,
            version_main=version_main,
            browser_binary=browser_binary,
            driver_binary=driver_binary,
        )
        self._warmed_up = False
        self.search_limiter = _NoopLimiter()
        self.product_limiter = _NoopLimiter()
        if warmup:
            self._warmup()

    def fetch_page(self, page_url, *, endpoint_kind, timeout=None):
        normalized = ozon_common.ozon_page_path(page_url)
        full_url = OZON_BASE + normalized
        self._driver.get(full_url)
        try:
            WebDriverWait(self._driver, timeout or PAGE_LOAD_TIMEOUT).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            pass
        # Ждём рендер товаров
        self._wait_for_tiles(timeout=timeout or PAGE_LOAD_TIMEOUT)

        if endpoint_kind == "product":
            payload = self._extract_product_page()
        else:
            payload = self._extract_search_page()

        return payload, "dom"

    def get_json(self, url, *, params=None, endpoint_kind, timeout=None):
        page_url = params.get("url", "") if params else ""
        payload, _ = self.fetch_page(page_url, endpoint_kind=endpoint_kind, timeout=timeout)
        return payload

    def fetch_page_html(self, full_url, *, timeout=None):
        self._driver.get(full_url)
        try:
            WebDriverWait(self._driver, timeout or PAGE_LOAD_TIMEOUT).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            pass
        try:
            self._driver.execute_script("window.scrollBy(0, 1200);")
            time.sleep(0.8)
            self._driver.execute_script("window.scrollBy(0, -1200);")
            time.sleep(0.3)
        except Exception:
            pass
        return self._driver.page_source

    def close(self):
        try:
            self._driver.quit()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _warmup(self):
        if self._warmed_up:
            return
        try:
            self._driver.get(OZON_BASE)
            WebDriverWait(self._driver, WARMUP_TIMEOUT).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            time.sleep(3)
            self._warmed_up = True
        except Exception as exc:
            print(f"[ozon_selenium] warmup warning: {exc}", file=sys.stderr)

    def _wait_for_tiles(self, timeout=15):
        """Ждём появления тайлов товаров в DOM."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            count = self._driver.execute_script(
                "return document.querySelectorAll('[class*=\"tile-root\"]').length"
            )
            if count and count > 0:
                time.sleep(1)
                return
            time.sleep(0.5)

    def _extract_search_page(self):
        """Извлекает товары из тайлов страницы поиска."""
        items = self._driver.execute_script(r"""
            var tiles = document.querySelectorAll('[class*="tile-root"]');
            var results = [];
            for (var i = 0; i < tiles.length; i++) {
                var tile = tiles[i];
                var item = {};

                // URL и ID из ссылки
                var link = tile.querySelector('a[href*="/product/"]');
                if (link) {
                    item.url = link.href;
                    var m = link.href.match(/-(\d{6,})(?:\/|\?|$)/);
                    if (m) item.sku = parseInt(m[1]);
                }

                // Название
                var nameEl = tile.querySelector('[class*="tsBody500Medium"], [class*="tile-title"], span[class*="ts-"]');
                if (!nameEl) nameEl = tile.querySelector('a[href*="/product/"] span');
                if (nameEl) item.title = nameEl.textContent.trim();

                // Цена текущая
                var priceEl = tile.querySelector('[class*="tsHeadline500Medium"], [class*="price-number"]');
                if (!priceEl) priceEl = tile.querySelector('span[class*="c3015-a"]');
                if (priceEl) {
                    var priceText = priceEl.textContent.replace(/\s/g, '').replace(/[^\d]/g, '');
                    if (priceText) item.finalPrice = parseInt(priceText);
                }

                // Старая цена
                var oldPriceEl = tile.querySelector('[class*="tsBody400Small"] s, s[class*="price"], del');
                if (oldPriceEl) {
                    var oldText = oldPriceEl.textContent.replace(/\s/g, '').replace(/[^\d]/g, '');
                    if (oldText) item.priceBase = parseInt(oldText);
                }

                // Рейтинг
                var ratingEl = tile.querySelector('[class*="stars-rating"], [class*="rating"]');
                if (ratingEl) {
                    var ratingText = ratingEl.textContent.trim().match(/[0-9.]+/);
                    if (ratingText) item.rating = parseFloat(ratingText[0]);
                }

                // Отзывы
                var reviewEl = tile.querySelector('[class*="tsBodyControl400Small"]');
                if (reviewEl) {
                    var reviewText = reviewEl.textContent.replace(/\s/g, '').match(/\d+/);
                    if (reviewText) item.reviewsCount = parseInt(reviewText[0]);
                }

                // Картинка
                var imgEl = tile.querySelector('img[src*="ozon"], img[src*="cdn1"], img[src*="ir.ozone"]');
                if (imgEl) item.image = imgEl.src;

                if (item.sku || item.url) results.push(item);
            }
            return results;
        """)

        return {"items": items or [], "layout": []}

    def _extract_product_page(self):
        """Извлекает данные с карточки товара."""
        data = self._driver.execute_script(r"""
            var item = {};

            // URL и ID
            item.url = window.location.href;
            var m = window.location.href.match(/-(\d{6,})(?:\/|\?|$)/);
            if (m) item.sku = parseInt(m[1]);

            // Название
            var h1 = document.querySelector('h1');
            if (h1) item.title = h1.textContent.trim();

            // Цена
            var priceEl = document.querySelector('[class*="tsHeadline500Medium"]');
            if (priceEl) {
                var priceText = priceEl.textContent.replace(/\s/g, '').replace(/[^\d]/g, '');
                if (priceText) item.finalPrice = parseInt(priceText);
            }

            // Рейтинг
            var ratingEl = document.querySelector('[class*="ga121-a"]');
            if (ratingEl) {
                var rt = ratingEl.textContent.trim().match(/[0-9.]+/);
                if (rt) item.rating = parseFloat(rt[0]);
            }

            // Бренд
            var brandEl = document.querySelector('a[href*="/brand/"], a[href*="/seller/"]');
            if (brandEl) item.brand = brandEl.textContent.trim();

            // Картинки
            var imgs = document.querySelectorAll('[class*="gallery"] img, [class*="photo"] img');
            item.images = [];
            for (var i = 0; i < Math.min(imgs.length, 10); i++) {
                if (imgs[i].src && imgs[i].src.indexOf('ozon') !== -1) {
                    item.images.push(imgs[i].src);
                }
            }

            // Характеристики
            item.attributes = {};
            var rows = document.querySelectorAll('[class*="attribute"], dl dt, dl dd');
            for (var i = 0; i < rows.length - 1; i += 2) {
                var key = rows[i] && rows[i].textContent.trim();
                var val = rows[i+1] && rows[i+1].textContent.trim();
                if (key && val) item.attributes[key] = val;
            }

            return {items: [item], layout: []};
        """)
        return data or {"items": [], "layout": []}


class _NoopLimiter:
    def wait(self): pass
    def backoff(self): pass


def _make_driver(*, headless, profile_dir, version_main, browser_binary, driver_binary):
    options = uc.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1440,900")
    options.add_argument("--lang=ru-RU")
    if profile_dir:
        options.add_argument(f"--user-data-dir={profile_dir}")
    return uc.Chrome(
        options=options,
        use_subprocess=True,
        version_main=version_main,
        browser_executable_path=str(browser_binary),
        driver_executable_path=str(driver_binary) if driver_binary else None,
    )


def discover_browser_binary() -> str | None:
    for env_name in ("OZON_BROWSER_BINARY", "CHROME_BINARY", "GOOGLE_CHROME_BIN"):
        value = os.environ.get(env_name)
        if value and Path(value).exists():
            return value
    for candidate in CHROME_BINARY_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def discover_driver_binary() -> str | None:
    for env_name in ("OZON_CHROMEDRIVER", "CHROMEDRIVER", "CHROME_DRIVER"):
        value = os.environ.get(env_name)
        if value and Path(value).exists():
            return value
    for candidate in ("/usr/bin/chromedriver", "/usr/local/bin/chromedriver"):
        if Path(candidate).exists():
            return candidate
    return None


def main():
    parser = argparse.ArgumentParser(description="Ozon Selenium-based parser CLI.")
    parser.add_argument("command", choices=["search", "product"])
    parser.add_argument("query_or_id")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--region-name", default="RU/Moscow")
    parser.add_argument("--enrich", action="store_true")
    parser.add_argument("--enrich-limit", type=int)
    parser.add_argument("--no-headless", action="store_true")
    parser.add_argument("--profile-dir")
    parser.add_argument("--chrome-version", type=int)
    parser.add_argument("--browser-binary")
    parser.add_argument("--driver-binary")
    parser.add_argument("--output")
    args = parser.parse_args()

    from ozon_parser import OzonProductParser, OzonSearchParser

    print("[ozon_selenium] Запускаем браузер...", file=sys.stderr)
    with OzonSeleniumClient(
        headless=not args.no_headless,
        profile_dir=args.profile_dir,
        version_main=args.chrome_version,
        browser_binary=args.browser_binary,
        driver_binary=args.driver_binary,
    ) as client:
        print("[ozon_selenium] Браузер готов.", file=sys.stderr)
        if args.command == "search":
            result = OzonSearchParser(client=client).search(
                args.query_or_id,
                page=args.page,
                limit=args.limit,
                region_name=args.region_name,
                enrich=args.enrich,
                enrich_limit=args.enrich_limit,
            )
        else:
            result = OzonProductParser(client=client).get_product(
                args.query_or_id,
                region_name=args.region_name,
            )

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"[ozon_selenium] Сохранено в {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
