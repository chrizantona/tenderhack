from __future__ import annotations

OZON_BASE = "https://www.ozon.ru"

SEARCH_EXTRACT_JS = r"""
var seenUrls = {};
var results = [];
var anchors = document.querySelectorAll('a[href*="/product/"]');
for (var i = 0; i < anchors.length; i++) {
    var link = anchors[i];
    var href = link.href || "";
    var skuMatch = href.match(/-(\d{6,})(?:\/|\?|$)/);
    if (!skuMatch) continue;
    var sku = parseInt(skuMatch[1]);
    if (!sku || seenUrls[sku]) continue;
    seenUrls[sku] = true;
    var tile = link.closest('[class*="tile-root"], [data-widget*="searchResultsV2"] > div, article, [class*="-tile"]');
    if (!tile) tile = link.parentElement;
    var item = {url: href, sku: sku};
    var titleText = link.getAttribute("title") || "";
    function isBadgeText(t) {
        if (!t) return true;
        if (/[₽%]/.test(t)) return true;
        if (/^\d/.test(t)) return true;
        if (/^[А-ЯЁA-Z\s\d\-\.\,]+$/.test(t) && t.length < 40) return true;
        var lower = t.toLowerCase();
        var bad = ['осталось', 'осталась', 'осталось', 'остался', 'цена что надо', 'хит', 'хит продаж', 'до ', 'баллов', 'бесплатная доставка', 'premium', 'ozon', 'лучшая цена', 'выбор покупателей', 'распродажа', 'скидка', 'промокод'];
        for (var i = 0; i < bad.length; i++) if (lower.indexOf(bad[i]) === 0 || lower.indexOf(' ' + bad[i]) !== -1 && lower.length < 35) return true;
        return false;
    }
    if (!titleText || titleText.length < 12) {
        var spans = (tile || link).querySelectorAll("span");
        var candidates = [];
        for (var s = 0; s < spans.length; s++) {
            var txt = (spans[s].textContent || "").replace(/\s+/g, " ").trim();
            if (txt.length < 14 || txt.length > 240) continue;
            if (isBadgeText(txt)) continue;
            candidates.push(txt);
        }
        candidates.sort(function(a, b) { return b.length - a.length; });
        titleText = candidates[0] || (link.innerText || "").replace(/\s+/g, " ").trim();
    }
    titleText = titleText.replace(/\s+/g, " ").trim();
    if (titleText && titleText.length > 4 && !isBadgeText(titleText)) item.title = titleText.slice(0, 240);
    if (tile) {
        if (!item.title || item.title.length < 12) {
            var nameEls = tile.querySelectorAll('span[class*="tsBody500"], span[class*="tile-title"], [class*="tile-name"] span');
            for (var ni = 0; ni < nameEls.length; ni++) {
                var t = nameEls[ni].textContent.replace(/\s+/g, " ").trim();
                if (t.length > (item.title || "").length && !isBadgeText(t)) item.title = t;
            }
        }
        var priceEl = tile.querySelector('[class*="tsHeadline"], [class*="price-number"], span[class*="c3015-a"]');
        if (priceEl) {
            var priceText = priceEl.textContent.replace(/\s/g, "").replace(/[^\d]/g, "");
            if (priceText) item.finalPrice = parseInt(priceText);
        }
        var oldPriceEl = tile.querySelector('s, del, [class*="oldPrice"]');
        if (oldPriceEl) {
            var oldText = oldPriceEl.textContent.replace(/\s/g, "").replace(/[^\d]/g, "");
            if (oldText) item.priceBase = parseInt(oldText);
        }
        var ratingEl = tile.querySelector('[class*="stars-rating"], [class*="rating"]');
        if (ratingEl) {
            var rt = ratingEl.textContent.trim().match(/[0-9]+([.,][0-9]+)?/);
            if (rt) item.rating = parseFloat(rt[0].replace(",", "."));
        }
        var reviewEl = tile.querySelector('[class*="tsBodyControl"], [class*="reviews"]');
        if (reviewEl) {
            var rv = reviewEl.textContent.replace(/\s/g, "").match(/\d+/);
            if (rv) item.reviewsCount = parseInt(rv[0]);
        }
        var imgUrl = "";
        var imgEl = tile.querySelector('img[src*="ozon"], img[src*="cdn1"], img[src*="ir.ozone"], img[srcset*="ozon"]');
        if (imgEl) {
            imgUrl = imgEl.currentSrc || imgEl.src || "";
            if (!imgUrl) {
                var ss = imgEl.getAttribute("srcset") || "";
                if (ss) imgUrl = ss.split(",")[0].trim().split(" ")[0];
            }
        }
        if (!imgUrl) {
            var picSrc = tile.querySelector('picture source[srcset]');
            if (picSrc) {
                var ss = picSrc.getAttribute("srcset") || "";
                if (ss) imgUrl = ss.split(",")[0].trim().split(" ")[0];
            }
        }
        if (!imgUrl) {
            var anyImg = tile.querySelector('img[data-src*="ozon"], img[data-src*="cdn1"], img[data-src*="ir.ozone"]');
            if (anyImg) imgUrl = anyImg.getAttribute("data-src") || "";
        }
        if (!imgUrl) {
            var bgEl = tile.querySelector('[style*="background-image"]');
            if (bgEl) {
                var m = (bgEl.getAttribute("style") || "").match(/background-image:\s*url\(["']?([^)"']+)["']?\)/i);
                if (m) imgUrl = m[1];
            }
        }
        if (imgUrl) item.image = imgUrl;
        var deliveryText = "";
        var deliveryEl = tile.querySelector('[data-widget*="MiniDelivery"], [data-widget*="webDelivery"], [class*="delivery"], [class*="Delivery"]');
        if (deliveryEl) {
            deliveryText = (deliveryEl.textContent || "").replace(/\s+/g, " ").trim();
        }
        if (!deliveryText) {
            var tileText = (tile.innerText || "").replace(/\s+/g, " ");
            var dm = tileText.match(/(послезавтра|сегодня|завтра|за\s*\d+(?:\s*-\s*\d+)?\s*(?:час|мин)[а-я]*|через\s*\d+(?:\s*-\s*\d+)?\s*(?:час|мин|дн)[а-я]*|\d+(?:\s*-\s*\d+)?\s*(?:дн[\.яей]?|час[а-я]*|мин[а-я]*))/i);
            if (dm) deliveryText = dm[0];
        }
        if (deliveryText) item.delivery = deliveryText.slice(0, 160);
    }
    if (item.title || item.finalPrice) results.push(item);
}
return JSON.stringify({items: results, layout: [], url: window.location.href, title: document.title});
"""


PRODUCT_EXTRACT_JS = r"""
var item = {};
item.url = window.location.href;
var m = window.location.href.match(/-(\d{6,})(?:\/|\?|$)/);
if (m) item.sku = parseInt(m[1]);
var h1 = document.querySelector('h1');
if (h1) item.title = h1.textContent.trim();
var priceSelectors = [
    '[data-widget="webPrice"] [class*="tsHeadline"]',
    '[data-widget*="webPrice"] [class*="tsHeadline"]',
    '[data-widget="webPrice"] span',
    '[class*="tsHeadline500Medium"]',
    '[data-widget*="webPrice"] span'
];
for (var pi = 0; pi < priceSelectors.length; pi++) {
    var els = document.querySelectorAll(priceSelectors[pi]);
    for (var ei = 0; ei < els.length; ei++) {
        var raw = (els[ei].textContent || '').trim();
        if (!/₽/.test(raw)) continue;
        var n = parseInt(raw.replace(/[\s  ]/g, '').replace(/[^\d]/g, ''));
        if (n && n > 9 && n < 10000000) { item.finalPrice = n; break; }
    }
    if (item.finalPrice) break;
}
if (!item.finalPrice) {
    var og = document.querySelector('meta[property="product:price:amount"], meta[name="price"]');
    if (og && og.content) {
        var n2 = parseInt(String(og.content).replace(/[^\d]/g, ''));
        if (n2) item.finalPrice = n2;
    }
}
var ratingEl = document.querySelector('[class*="ga121-a"]');
if (ratingEl) {
    var rt = ratingEl.textContent.trim().match(/[0-9.]+/);
    if (rt) item.rating = parseFloat(rt[0]);
}
var brandEl = document.querySelector('a[href*="/brand/"], a[href*="/seller/"]');
if (brandEl) item.brand = brandEl.textContent.trim();
item.images = [];
function pushImg(src) {
    if (!src) return;
    var ok = src.indexOf('ozon') !== -1 || src.indexOf('cdn1') !== -1 || src.indexOf('ir.ozone') !== -1 || src.indexOf('ozonusercontent') !== -1;
    if (ok && item.images.indexOf(src) === -1) item.images.push(src);
}
var imgSelectors = [
    '[data-widget*="Gallery"] img',
    '[data-widget*="gallery"] img',
    '[data-widget="webGallery"] img',
    '[class*="gallery"] img',
    '[class*="photo"] img',
    '[data-widget*="webHeaderProductDetails"] img'
];
imgSelectors.forEach(function(sel) {
    document.querySelectorAll(sel).forEach(function(img) {
        var src = img.currentSrc || img.src || '';
        if (!src) {
            var ss = img.getAttribute('srcset') || '';
            if (ss) src = ss.split(',')[0].trim().split(' ')[0];
        }
        pushImg(src);
    });
});
if (!item.images.length) {
    document.querySelectorAll('picture source[srcset]').forEach(function(s) {
        var ss = s.getAttribute('srcset') || '';
        if (ss) pushImg(ss.split(',')[0].trim().split(' ')[0]);
    });
}
if (!item.images.length) {
    document.querySelectorAll('img').forEach(function(img) {
        var src = img.currentSrc || img.src || '';
        if (src && (img.naturalWidth > 200 || img.width > 200)) pushImg(src);
    });
}
var og = document.querySelector('meta[property="og:image"], meta[name="og:image"]');
if (og && og.content) pushImg(og.content);
item.images = item.images.slice(0, 10);
if (item.images.length) item.image = item.images[0];
var deliveryEl = document.querySelector('[data-widget="webDelivery"], [data-widget*="webDelivery"], [id^="state-webDelivery"]');
if (deliveryEl) {
    var dtxt = (deliveryEl.textContent || "").replace(/\s+/g, " ").trim();
    if (dtxt) item.delivery = dtxt.slice(0, 240);
}
if (!item.delivery) {
    var bodyText = (document.body && document.body.innerText || "").replace(/\s+/g, " ");
    var dm = bodyText.match(/Доставка\s*[^.]{0,80}?(послезавтра|сегодня|завтра|за\s*\d+(?:\s*-\s*\d+)?\s*(?:час|мин)[а-я]*|через\s*\d+(?:\s*-\s*\d+)?\s*(?:час|мин|дн)[а-я]*|\d+(?:\s*-\s*\d+)?\s*(?:дн[\.яей]?|час[а-я]*|мин[а-я]*))/i);
    if (dm) item.delivery = dm[0].slice(0, 240);
}
function extractCharsFrom(data, out) {
    if (!data) return;
    if (Array.isArray(data)) { data.forEach(function(x){ extractCharsFrom(x, out); }); return; }
    if (typeof data !== 'object') return;
    var key = data.key || data.name || data.title || data.attributeName;
    var vals = data.values;
    if (typeof key === 'string' && key && Array.isArray(vals) && vals.length) {
        var texts = vals.map(function(v) { return v && (v.text || v.title || v.value || v.name) || ''; }).filter(Boolean);
        if (texts.length) {
            out[key] = String(texts.join(', ')).slice(0, 320);
            return;
        }
    }
    if (typeof key === 'string' && key && (typeof data.text === 'string' || typeof data.value === 'string')) {
        out[key] = String(data.text || data.value).slice(0, 320);
        return;
    }
    Object.keys(data).forEach(function(k){ extractCharsFrom(data[k], out); });
}
var characteristics = {};
document.querySelectorAll('[id^="state-webShortCharacteristics"], [id^="state-webCharacteristics"], [id^="state-webDescription"]').forEach(function(el) {
    try {
        var raw = el.getAttribute('data-state');
        if (!raw) return;
        var data = JSON.parse(raw);
        extractCharsFrom(data, characteristics);
    } catch(e) {}
});
function scrapeDLs(root) {
    root.querySelectorAll('dl').forEach(function(dl) {
        var dts = dl.querySelectorAll('dt');
        var dds = dl.querySelectorAll('dd');
        var pairs = Math.min(dts.length, dds.length);
        for (var i = 0; i < pairs; i++) {
            var k = (dts[i].textContent || '').replace(/\s+/g, ' ').trim();
            var v = (dds[i].textContent || '').replace(/\s+/g, ' ').trim();
            if (k && v && !characteristics[k]) characteristics[k] = v.slice(0, 320);
        }
    });
}
var charsRoots = document.querySelectorAll('[data-widget*="Characteristic"], [data-widget*="characteristic"], [id*="state-webShort"], [id*="state-webCharacteristic"]');
charsRoots.forEach(function(root) { scrapeDLs(root); });
if (Object.keys(characteristics).length < 3) {
    scrapeDLs(document);
}
if (Object.keys(characteristics).length < 3) {
    var rxLine = /([А-ЯЁA-Z][а-яёa-zA-Z0-9 \-\/\(\)\.,]{1,50}?):\s+([^\n]{1,160})/g;
    var blocks = document.querySelectorAll('[data-widget*="Characteristic"], [data-widget*="characteristic"]');
    blocks.forEach(function(b) {
        var txt = (b.innerText || '').replace(/ /g, ' ');
        var m;
        while ((m = rxLine.exec(txt)) !== null) {
            var k = m[1].trim(); var v = m[2].trim();
            if (k && v && !characteristics[k]) characteristics[k] = v.slice(0, 320);
        }
    });
}
if (Object.keys(characteristics).length) item.attributes = characteristics;
var descEl = document.querySelector('[data-widget="webDescription"], [id^="state-webDescription"]');
if (descEl) {
    var dtext = (descEl.textContent || "").replace(/\s+/g, " ").trim();
    if (dtext) item.description = dtext.slice(0, 3000);
}
return JSON.stringify({items: [item], layout: [], url: window.location.href, title: document.title});
"""


WAIT_TILES_JS = r"""
return document.querySelectorAll('[class*="tile-root"]').length;
"""


def search_url(query: str, page: int = 1) -> str:
    from urllib.parse import quote_plus
    return f"{OZON_BASE}/search/?text={quote_plus(query)}&from_global=true&page={page}"


def product_url(sku_or_url: str) -> str:
    s = str(sku_or_url).strip()
    if s.startswith("http"):
        return s
    return f"{OZON_BASE}/product/{s}/"


def wrap_iife(js: str) -> str:
    return "(() => {\n" + js + "\n})()"


BLOCKED_TITLE_MARKERS = (
    "доступ ограничен",
    "antibot",
    "challenge",
    "сопоставьте пазл",
    "двигая ползунок",
    "проверка безопасности",
    "captcha",
    "капча",
)


def is_blocked(title: str, url: str) -> bool:
    if not title:
        return False
    t = title.lower()
    if any(marker in t for marker in BLOCKED_TITLE_MARKERS):
        return True
    if "__rr=1" in (url or ""):
        return True
    return False


def is_blocked_text(text: str | None) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in BLOCKED_TITLE_MARKERS)
