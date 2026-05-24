const SOURCE_LABELS = {
  wildberries: "Wildberries",
  ozon: "Ozon",
  yandex_market: "Яндекс Маркет",
  other_runet: "Прочие источники Рунета",
};

const DEFAULT_PRODUCT_LIMIT = 10;
const MAX_PRODUCTS_PER_SOURCE = 100;
const PRODUCTS_VISIBLE_STEP = 10;

const form = document.getElementById("search-form");
const queryInput = document.getElementById("query");
const regionSelect = document.getElementById("region");
const geoBtn = document.getElementById("geo-btn");
const limitInput = document.getElementById("limit");
const searchBtn = document.getElementById("search-btn");
const stopBtn = document.getElementById("stop-btn");
const sortSelect = document.getElementById("sort-by");
const sourcesToggle = document.getElementById("sources-toggle");
const resultsEl = document.getElementById("results");
const metaEl = document.getElementById("meta");
const emptyState = document.getElementById("empty-state");
const filtersPanelEl = document.getElementById("filters-panel");
const filtersPanelStatusEl = document.getElementById("filters-panel-status");
const filtersPanelBodyEl = document.getElementById("filters-panel-body");
const filtersToggleBtn = document.getElementById("filters-toggle-btn");
const filtersScopeAll = document.getElementById("filters-scope-all");
let activeFiltersController = null;
let filterSelections = new Map(); // canonical_label -> Set<value_name>
let lastFiltersData = null;
let filtersPanelExpanded = false;
let filtersScopeAllOnly = true;
let healthState = null;
let activeSearchController = null;
let sourceSectionEls = new Map();
let lastSourcePayloads = new Map();
let currentSearchState = null;
let supportedRegions = [];
let selectedGeo = null;
let viewMode = "grid";
const viewGridBtn = document.getElementById("view-grid");
const viewTableBtn = document.getElementById("view-table");
const viewGroupsBtn = document.getElementById("view-groups");
let lastClusters = [];
let lastClustersDiag = null;
let lastDoneHadClustersField = false;

async function loadHealth() {
  try {
    const res = await fetch("/api/health");
    healthState = await res.json();
    applySourceHealth(healthState);
    applyProxyState(healthState.proxy_enabled);
  } catch (err) {
    console.error("Failed to load health", err);
  }
}

const proxyToggleBtn = document.getElementById("proxy-toggle");
let proxyEnabled = null;        // tri-state: null=unknown, true/false
let proxyToggleInflight = false;

function applyProxyState(enabled) {
  proxyEnabled = enabled === true;
  if (!proxyToggleBtn) return;
  proxyToggleBtn.classList.remove("is-loading");
  proxyToggleBtn.classList.toggle("is-on", proxyEnabled);
  proxyToggleBtn.classList.toggle("is-off", !proxyEnabled);
  const label = proxyToggleBtn.querySelector(".proxy-toggle-label");
  if (label) label.textContent = proxyEnabled ? "Прокси: вкл" : "Прокси: выкл";
  proxyToggleBtn.title = proxyEnabled
    ? "Прокси включён (Ozon/YM через RU-мосты). Клик — выключить."
    : "Прокси выключен (прямой коннект). Ozon/YM могут не работать вне РФ. Клик — включить.";
}

async function toggleProxy() {
  if (proxyToggleInflight || proxyEnabled == null) return;
  const next = !proxyEnabled;
  proxyToggleInflight = true;
  proxyToggleBtn.classList.add("is-loading");
  const label = proxyToggleBtn.querySelector(".proxy-toggle-label");
  if (label) label.textContent = next ? "Включаем…" : "Выключаем…";
  try {
    const res = await fetch("/api/proxy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: next }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => null);
      showMeta(`<strong>Прокси:</strong> ${escapeHtml((data && (data.message || data.error)) || res.statusText)}`);
      applyProxyState(proxyEnabled);
      return;
    }
    const data = await res.json();
    applyProxyState(data.proxy_enabled);
    showMeta(`<strong>Прокси:</strong> ${data.proxy_enabled ? "включён" : "выключен"} (Ozon перезапустит браузер на следующем поиске, +~10с)`);
  } catch (err) {
    showMeta(`<strong>Прокси:</strong> ошибка — ${escapeHtml(err.message || err)}`);
    applyProxyState(proxyEnabled);
  } finally {
    proxyToggleInflight = false;
    proxyToggleBtn.classList.remove("is-loading");
  }
}

if (proxyToggleBtn) proxyToggleBtn.addEventListener("click", toggleProxy);

async function loadRegions() {
  try {
    const res = await fetch("/api/regions");
    const data = await res.json();
    supportedRegions = data.regions || [];
    regionSelect.innerHTML = "";
    for (const region of supportedRegions) {
      const opt = document.createElement("option");
      opt.value = region.key;
      opt.textContent = region.name;
      if (typeof region.latitude === "number") opt.dataset.latitude = String(region.latitude);
      if (typeof region.longitude === "number") opt.dataset.longitude = String(region.longitude);
      regionSelect.appendChild(opt);
    }
    regionSelect.value = "moscow";
  } catch (err) {
    console.error("Failed to load regions", err);
  }
}

function selectedSources() {
  const checked = sourcesToggle.querySelectorAll("input[type='checkbox']:checked:not(:disabled)");
  return Array.from(checked).map((el) => el.value);
}

function applySourceHealth(health) {
  const ozonInput = sourcesToggle.querySelector('input[value="ozon"]');
  if (!ozonInput) return;
  const label = ozonInput.closest("label");
  if (!label) return;
  label.classList.remove("source-disabled", "source-warning");
  ozonInput.disabled = false;
  if (health.ozon_enabled === false) {
    ozonInput.checked = false;
    ozonInput.disabled = true;
    label.classList.add("source-disabled");
    label.title = health.ozon_disabled_reason || "Ozon временно отключён";
  } else if (health.ozon_browser_warning) {
    label.classList.add("source-warning");
    label.title = health.ozon_disabled_reason || health.ozon_browser_warning || "Ozon требует проверки";
  } else {
    label.removeAttribute("title");
  }
}

function showMeta(text) {
  metaEl.innerHTML = text;
  metaEl.classList.remove("hidden");
}

// Backend diagnostics we don't surface to the user — informational stages
// that aren't actual errors but were historically displayed as red banners.
const SILENT_DIAG_STAGES = new Set([
  "wb_dest_fallback",          // search succeeded via default WB region
  "basket_card",               // single missing basket card; item still rendered
]);

function isUserFacingDiagnostic(entry) {
  if (entry == null) return false;
  if (typeof entry === "string") {
    return ![...SILENT_DIAG_STAGES].some((s) => entry.startsWith(s));
  }
  if (typeof entry === "object") {
    return !SILENT_DIAG_STAGES.has(entry.stage);
  }
  return true;
}

function formatDiagnostic(entry) {
  // Server diagnostics come back as either bare strings or {stage, message, ...}
  // dicts. Plain Array.join() on dicts produces "[object Object]" — pluck the
  // human-readable field and fall back to JSON.
  if (entry == null) return "";
  if (typeof entry === "string") return entry;
  if (typeof entry !== "object") return String(entry);
  const message = entry.message || entry.error || entry.reason;
  const stage = entry.stage;
  if (stage && message) return `${stage}: ${message}`;
  if (message) return String(message);
  if (stage) return String(stage);
  try {
    return JSON.stringify(entry);
  } catch (err) {
    return "(нечитаемая диагностика)";
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  }[char]));
}

function productLimit() {
  const rawValue = Number.parseInt(limitInput.value, 10);
  const normalized = Number.isFinite(rawValue) ? rawValue : DEFAULT_PRODUCT_LIMIT;
  const clamped = Math.max(1, Math.min(MAX_PRODUCTS_PER_SOURCE, normalized));
  limitInput.value = String(clamped);
  return clamped;
}

function placeholderImage() {
  const span = document.createElement("span");
  span.className = "placeholder";
  span.textContent = "🖼";
  return span;
}

function priceText(price) {
  if (!price || typeof price.current !== "number") return null;
  const fmt = new Intl.NumberFormat("ru-RU").format(price.current);
  const currency = price.currency === "RUB" ? "₽" : price.currency || "";
  return `${fmt} ${currency}`.trim();
}

function numericPrice(product) {
  return typeof product?.price?.current === "number" ? product.price.current : null;
}

function deliveryHours(product) {
  const delivery = product.delivery || {};
  const min = typeof delivery.time_min_hours === "number" ? delivery.time_min_hours : null;
  const max = typeof delivery.time_max_hours === "number" ? delivery.time_max_hours : null;
  if (min != null && max != null) return (min + max) / 2;
  return min ?? max;
}

function deliveryText(product) {
  const delivery = product.delivery || {};
  const min = typeof delivery.time_min_hours === "number" ? delivery.time_min_hours : null;
  const max = typeof delivery.time_max_hours === "number" ? delivery.time_max_hours : null;
  const hours = deliveryHours(product);
  if (hours == null) return null;
  if (min != null && max != null && max < 1) {
    const minMinutes = Math.max(1, Math.round(min * 60));
    const maxMinutes = Math.max(minMinutes, Math.round(max * 60));
    return minMinutes === maxMinutes ? `${minMinutes} мин` : `${minMinutes}-${maxMinutes} мин`;
  }
  if (hours < 24) return `${Math.max(1, Math.round(hours))} ч`;
  return `${Math.round(hours / 24)} дн`;
}

function ratingValue(product) {
  return typeof product?.rating?.value === "number" ? product.rating.value : null;
}

function reviewsCount(product) {
  return typeof product?.rating?.reviews_count === "number" ? product.rating.reviews_count : null;
}

function discountPercent(product) {
  return typeof product?.price?.discount_percent === "number" ? product.price.discount_percent : null;
}

function availabilityScore(product) {
  const status = product?.availability?.status;
  if (status === "in_stock") return 2;
  if (status === "unknown") return 1;
  if (status === "out_of_stock") return 0;
  return null;
}

function sellerRating(product) {
  return typeof product?.seller?.rating === "number" ? product.seller.rating : null;
}

function productMainImage(product) {
  const media = product.media || {};
  if (media.main_image_url) return media.main_image_url;
  if (Array.isArray(media.image_urls) && media.image_urls.length) return media.image_urls[0];
  return null;
}

function productBrandName(product) {
  const b = product.brand;
  if (!b) return "";
  if (typeof b === "string") return b;
  return b.name || "";
}

function productSourceSubLabel(product) {
  return null;
}

function renderProductCard(product) {
  const card = document.createElement("div");
  card.className = "product-card";
  card.tabIndex = 0;
  card.setAttribute("role", "button");
  card.addEventListener("click", () => openProductDetail(product));
  card.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openProductDetail(product);
    }
  });

  const imgWrap = document.createElement("div");
  imgWrap.className = "product-image-wrap";
  const imgUrl = productMainImage(product);
  if (imgUrl) {
    const img = document.createElement("img");
    img.src = imgUrl;
    img.alt = product.title || "";
    img.loading = "lazy";
    img.onerror = () => { img.replaceWith(placeholderImage()); };
    imgWrap.appendChild(img);
  } else {
    imgWrap.appendChild(placeholderImage());
  }

  const body = document.createElement("div");
  body.className = "product-body";

  const title = document.createElement("div");
  title.className = "product-title";
  title.textContent = product.title || "(без названия)";
  body.appendChild(title);

  const brand = productBrandName(product);
  if (brand) {
    const brandEl = document.createElement("div");
    brandEl.className = "product-brand";
    brandEl.textContent = brand;
    body.appendChild(brandEl);
  }

  const rating = product.rating;
  if (rating && (rating.value || rating.reviews_count)) {
    const r = document.createElement("div");
    r.className = "product-rating";
    const parts = [];
    if (rating.value) parts.push(`★ ${rating.value}`);
    if (rating.reviews_count) parts.push(`${rating.reviews_count} отзывов`);
    r.textContent = parts.join(" · ");
    body.appendChild(r);
  }

  const delivery = deliveryText(product);
  if (delivery) {
    const d = document.createElement("div");
    d.className = "product-delivery";
    d.textContent = `Доставка: ${delivery}`;
    body.appendChild(d);
  }

  const availabilityStatus = product?.availability?.status;
  if (availabilityStatus === "in_stock" || availabilityStatus === "out_of_stock") {
    const badge = document.createElement("span");
    badge.className = `product-availability ${availabilityStatus}`;
    const qty = typeof product?.availability?.quantity === "number" ? product.availability.quantity : null;
    if (availabilityStatus === "in_stock") {
      badge.textContent = qty != null && qty > 0 ? `В наличии · ${qty}` : "В наличии";
    } else {
      badge.textContent = "Нет в наличии";
    }
    body.appendChild(badge);
  }

  const sub = productSourceSubLabel(product);
  if (sub) {
    const pill = document.createElement("span");
    pill.className = "product-source-pill";
    pill.textContent = sub;
    body.appendChild(pill);
  }

  const priceRow = document.createElement("div");
  priceRow.className = "product-price-row";
  const currentPrice = priceText(product.price);
  if (currentPrice) {
    const p = document.createElement("span");
    p.className = "product-price";
    p.textContent = currentPrice;
    priceRow.appendChild(p);
  }
  if (priceRow.childNodes.length) body.appendChild(priceRow);

  const chars = keyCharacteristics(product);
  if (chars.length) {
    const charsRow = document.createElement("div");
    charsRow.className = "product-chars";
    for (const { label, value } of chars) {
      const chip = document.createElement("span");
      chip.className = "product-char-chip";
      chip.title = label;
      chip.textContent = value;
      charsRow.appendChild(chip);
    }
    body.appendChild(charsRow);
  }

  card.appendChild(imgWrap);
  card.appendChild(body);
  return card;
}

function keyCharacteristics(product) {
  const normalized = product?.attributes?.normalized || {};
  const raw = product?.attributes?.raw || {};
  const order = [
    ["Цвет", normalizeColorRu(normalized.color) || raw["Цвет"]],
    ["Память", normalized.memory_gb ? `${normalized.memory_gb} ГБ` : null],
    ["Размер", normalized.size || raw["Размер"]],
    ["Страна", normalized.country || raw["Страна производства"]],
    ["Пол", normalizeGenderRu(normalized.gender)],
  ];
  const items = [];
  const seen = new Set();
  for (const [label, value] of order) {
    if (!value) continue;
    const text = String(value).trim();
    if (!text || seen.has(text.toLowerCase())) continue;
    seen.add(text.toLowerCase());
    items.push({ label, value: text });
    if (items.length >= 4) break;
  }
  return items;
}

function normalizeColorRu(value) {
  if (!value) return null;
  const map = {
    black: "чёрный",
    white: "белый",
    blue: "синий",
    gray: "серый",
    red: "красный",
    green: "зелёный",
    yellow: "жёлтый",
    pink: "розовый",
    brown: "коричневый",
    purple: "фиолетовый",
    orange: "оранжевый",
  };
  return map[String(value).toLowerCase()] || value;
}

function normalizeGenderRu(value) {
  if (!value) return null;
  const map = { male: "мужской", female: "женский", kids: "детский" };
  return map[String(value).toLowerCase()] || null;
}

let productModalEl = null;
let productModalKeyHandler = null;

function openProductDetail(product) {
  closeProductDetail();

  const overlay = document.createElement("div");
  overlay.className = "product-modal-overlay";

  const modal = document.createElement("div");
  modal.className = "product-modal";

  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "product-modal-close";
  closeBtn.setAttribute("aria-label", "Закрыть");
  closeBtn.textContent = "×";
  modal.appendChild(closeBtn);

  const galleryEl = buildProductGallery(product);
  modal.appendChild(galleryEl);

  const contentEl = buildProductContent(product);
  modal.appendChild(contentEl);

  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  document.body.classList.add("modal-open");
  productModalEl = overlay;

  const onKey = (event) => {
    if (event.key === "Escape") closeProductDetail();
  };
  productModalKeyHandler = onKey;
  document.addEventListener("keydown", onKey);

  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) closeProductDetail();
  });
  closeBtn.addEventListener("click", closeProductDetail);

  // Ozon search results land without product-page details (the initial fetch
  // is page-only — enrichment is skipped server-side for speed). Pull the full
  // product on-demand so the modal still shows characteristics + description.
  maybeEnrichProductModal(product, { modal, galleryEl, contentEl });
}

// Sources whose modal we lazy-enrich on open. Ozon search emits only base
// items (no characteristics), so the modal hits /api/product when opened.
// WB falls into the same boat when its JSON API is rate-limited and we land
// on the Camoufox SERP-HTML fallback path (basic_scheme with empty
// attributes.raw — basket-CDN enrichment still works via /api/product).
const ENRICHABLE_SOURCES = new Set(["ozon", "wildberries"]);

function productNeedsEnrichment(product) {
  if (!product) return false;
  if (!ENRICHABLE_SOURCES.has(product.marketplace)) return false;
  const rawAttrs = product?.attributes?.raw || {};
  if (Object.keys(rawAttrs).length > 0) return false;
  const desc = (product?.description || "").trim();
  return !desc;
}

function sourceDisplayName(source) {
  if (source === "ozon") return "Ozon";
  if (source === "wildberries") return "Wildberries";
  if (source === "yandex_market") return "Яндекс Маркет";
  return source;
}

async function maybeEnrichProductModal(product, refs) {
  if (!productNeedsEnrichment(product)) {
    console.debug("[enrich] skip — already populated", {
      source: product?.marketplace,
      attrs: Object.keys(product?.attributes?.raw || {}).length,
      desc_len: (product?.description || "").length,
    });
    return;
  }
  const url = product?.url;
  const externalId = product?.external_id;
  if (!url && !externalId) {
    console.warn("[enrich] no url/external_id", product);
    return;
  }

  const source = product.marketplace;
  console.debug("[enrich] fetching", { source, url, externalId });
  const notice = document.createElement("div");
  notice.className = "product-modal-enriching";
  notice.innerHTML = `<span class="loading-spinner dark"></span>загружаем детали с ${escapeHtml(sourceDisplayName(source))}…`;
  refs.contentEl.prepend(notice);

  const params = new URLSearchParams({ source });
  if (url) params.set("url", url);
  else params.set("id", externalId);

  try {
    const res = await fetch(`/api/product?${params.toString()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const enriched = await res.json();
    // Modal closed while we waited — skip silently.
    if (productModalEl !== refs.modal.parentNode) {
      console.debug("[enrich] modal closed before fetch returned");
      return;
    }
    if (!enriched || enriched.error) {
      const errMsg = (enriched && enriched.error) ? enriched.error : "пустой ответ";
      console.warn("[enrich] backend error", errMsg);
      notice.innerHTML = `<span class="product-modal-enrich-err">не удалось загрузить: ${escapeHtml(String(errMsg))}</span>`;
      return;
    }
    const newAttrs = Object.keys(enriched?.attributes?.raw || {}).length;
    console.debug("[enrich] applying", { source, attrs: newAttrs, desc_len: (enriched.description || "").length });
    // Server returns the same schema — re-render gallery and content with the
    // enriched copy; preserve user's open state by swapping nodes in place.
    const newGallery = buildProductGallery(enriched);
    const newContent = buildProductContent(enriched);
    refs.galleryEl.replaceWith(newGallery);
    refs.contentEl.replaceWith(newContent);
    refs.galleryEl = newGallery;
    refs.contentEl = newContent;
  } catch (err) {
    console.warn("[enrich] fetch failed", err);
    notice.innerHTML = `<span class="product-modal-enrich-err">не удалось дозагрузить детали: ${escapeHtml(err.message || err)}</span>`;
  }
}

function closeProductDetail() {
  if (!productModalEl) return;
  productModalEl.remove();
  productModalEl = null;
  document.body.classList.remove("modal-open");
  if (productModalKeyHandler) {
    document.removeEventListener("keydown", productModalKeyHandler);
    productModalKeyHandler = null;
  }
}

function buildProductGallery(product) {
  const wrap = document.createElement("div");
  wrap.className = "product-modal-gallery";

  const main = document.createElement("div");
  main.className = "product-modal-gallery-main";
  const mainImg = document.createElement("img");
  mainImg.alt = product.title || "";
  const firstUrl = productMainImage(product);
  if (firstUrl) {
    mainImg.src = firstUrl;
  } else {
    main.appendChild(placeholderImage());
  }
  if (firstUrl) main.appendChild(mainImg);
  mainImg.onerror = () => { mainImg.replaceWith(placeholderImage()); };
  wrap.appendChild(main);

  const urls = Array.from(new Set([
    ...(firstUrl ? [firstUrl] : []),
    ...((product?.media?.image_urls || []).filter(Boolean)),
  ]));
  if (urls.length > 1) {
    const thumbs = document.createElement("div");
    thumbs.className = "product-modal-thumbs";
    for (const url of urls.slice(0, 8)) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "product-modal-thumb" + (url === mainImg.src ? " active" : "");
      const img = document.createElement("img");
      img.src = url;
      img.alt = "";
      img.loading = "lazy";
      btn.appendChild(img);
      btn.addEventListener("click", () => {
        mainImg.src = url;
        thumbs.querySelectorAll(".product-modal-thumb").forEach((el) => el.classList.remove("active"));
        btn.classList.add("active");
      });
      thumbs.appendChild(btn);
    }
    wrap.appendChild(thumbs);
  }
  return wrap;
}

function buildProductContent(product) {
  const wrap = document.createElement("div");
  wrap.className = "product-modal-content";

  const sourcePill = document.createElement("div");
  sourcePill.className = "product-modal-source";
  sourcePill.textContent = SOURCE_LABELS[product.marketplace] || product.marketplace || "";
  wrap.appendChild(sourcePill);

  const title = document.createElement("h2");
  title.className = "product-modal-title";
  title.textContent = product.title || "(без названия)";
  wrap.appendChild(title);

  const brand = productBrandName(product);
  if (brand) {
    const b = document.createElement("div");
    b.className = "product-modal-brand";
    b.textContent = brand;
    wrap.appendChild(b);
  }

  const priceRow = document.createElement("div");
  priceRow.className = "product-modal-price-row";
  const currentPrice = priceText(product.price);
  if (currentPrice) {
    const p = document.createElement("span");
    p.className = "product-modal-price";
    p.textContent = currentPrice;
    priceRow.appendChild(p);
  }
  wrap.appendChild(priceRow);

  const meta = document.createElement("div");
  meta.className = "product-modal-meta";
  const status = product?.availability?.status;
  if (status === "in_stock" || status === "out_of_stock") {
    const badge = document.createElement("span");
    badge.className = `product-availability ${status}`;
    const qty = product?.availability?.quantity;
    badge.textContent = status === "in_stock"
      ? (qty != null && qty > 0 ? `В наличии · ${qty}` : "В наличии")
      : "Нет в наличии";
    meta.appendChild(badge);
  }
  const delivery = deliveryText(product);
  if (delivery) {
    const d = document.createElement("span");
    d.className = "product-modal-delivery";
    d.textContent = `Доставка: ${delivery}`;
    meta.appendChild(d);
  }
  const rating = product?.rating?.value;
  const reviews = product?.rating?.reviews_count;
  if (rating || reviews) {
    const r = document.createElement("span");
    r.className = "product-modal-rating";
    const parts = [];
    if (rating) parts.push(`★ ${rating}`);
    if (reviews) parts.push(`${reviews} отзывов`);
    r.textContent = parts.join(" · ");
    meta.appendChild(r);
  }
  if (meta.childNodes.length) wrap.appendChild(meta);

  if (product.url) {
    const open = document.createElement("a");
    open.className = "product-modal-open";
    open.href = product.url;
    open.target = "_blank";
    open.rel = "noopener noreferrer";
    open.textContent = "Открыть на " + (SOURCE_LABELS[product.marketplace] || "маркетплейсе");
    wrap.appendChild(open);
  }

  const rows = detailRows(product);
  if (rows.length) {
    const charsTitle = document.createElement("div");
    charsTitle.className = "product-modal-section-title";
    charsTitle.textContent = "Характеристики";
    wrap.appendChild(charsTitle);

    const table = document.createElement("table");
    table.className = "product-modal-table";
    const tbody = document.createElement("tbody");
    for (const [label, value] of rows) {
      const tr = document.createElement("tr");
      const th = document.createElement("th");
      th.textContent = label;
      const td = document.createElement("td");
      td.textContent = value;
      tr.appendChild(th);
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
  }

  const description = (product?.description || "").trim();
  if (description) {
    const descTitle = document.createElement("div");
    descTitle.className = "product-modal-section-title";
    descTitle.textContent = "Описание";
    wrap.appendChild(descTitle);
    const desc = document.createElement("div");
    desc.className = "product-modal-description";
    desc.textContent = description;
    wrap.appendChild(desc);
  }

  return wrap;
}

function detailRows(product) {
  const rows = [];
  const raw = product?.attributes?.raw || {};
  const normalized = product?.attributes?.normalized || {};
  const brand = productBrandName(product);
  if (brand) rows.push(["Бренд", brand]);
  const categoryName = product?.category?.name;
  if (categoryName) rows.push(["Категория", categoryName]);
  const seller = product?.seller?.name;
  if (seller) rows.push(["Продавец", seller]);

  for (const [key, value] of Object.entries(raw)) {
    if (!value || typeof value === "object") continue;
    const text = String(value).trim();
    if (!text) continue;
    if (rows.some(([k]) => k.toLowerCase() === key.toLowerCase())) continue;
    rows.push([key, text]);
    if (rows.length >= 18) break;
  }

  if (Array.isArray(normalized.material) && normalized.material.length) {
    const parts = normalized.material
      .filter((item) => item && item.name)
      .map((item) => (item.percent != null ? `${item.name} ${item.percent}%` : item.name));
    if (parts.length && !rows.some(([k]) => k.toLowerCase() === "состав")) {
      rows.push(["Состав", parts.join(", ")]);
    }
  }
  return rows;
}

function average(values) {
  const clean = values.filter((value) => typeof value === "number" && Number.isFinite(value));
  if (!clean.length) return null;
  return clean.reduce((sum, value) => sum + value, 0) / clean.length;
}

function median(values) {
  const clean = values
    .filter((value) => typeof value === "number" && Number.isFinite(value))
    .sort((a, b) => a - b);
  if (!clean.length) return null;
  const mid = Math.floor(clean.length / 2);
  return clean.length % 2 ? clean[mid] : (clean[mid - 1] + clean[mid]) / 2;
}

function formatMoney(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(value)} ₽`;
}

function formatNumber(value, digits = 1) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat("ru-RU", { maximumFractionDigits: digits }).format(value);
}

function formatDeliveryHours(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  if (value < 24) return `${Math.round(value)} ч`;
  return `${formatNumber(value / 24, 1)} дн`;
}

function productStats(products) {
  const prices = products.map(numericPrice);
  const deliveries = products.map(deliveryHours);
  const ratings = products.map(ratingValue);
  const reviews = products.map(reviewsCount);
  const validPrices = prices.filter((value) => typeof value === "number");
  const inStock = products.filter((product) => product.availability?.status === "in_stock").length;
  return {
    count: products.length,
    avgPrice: average(prices),
    medianPrice: median(prices),
    minPrice: validPrices.length ? Math.min(...validPrices) : null,
    maxPrice: validPrices.length ? Math.max(...validPrices) : null,
    avgDelivery: average(deliveries),
    avgRating: average(ratings),
    totalReviews: reviews.filter((value) => typeof value === "number").reduce((sum, value) => sum + value, 0),
    inStock,
  };
}

function renderStats(products) {
  const stats = productStats(products);
  const list = document.createElement("div");
  list.className = "source-stats";
  const items = [
    ["Товаров", stats.count || "—"],
    ["Средняя цена", formatMoney(stats.avgPrice)],
    ["Медиана", formatMoney(stats.medianPrice)],
    ["Диапазон", stats.minPrice != null && stats.maxPrice != null ? `${formatMoney(stats.minPrice)} - ${formatMoney(stats.maxPrice)}` : "—"],
    ["Средняя доставка", formatDeliveryHours(stats.avgDelivery)],
    ["Средняя оценка", formatNumber(stats.avgRating, 2)],
    ["Отзывы", new Intl.NumberFormat("ru-RU").format(stats.totalReviews || 0)],
    ["В наличии", stats.inStock ? `${stats.inStock}` : "—"],
  ];

  for (const [label, value] of items) {
    const item = document.createElement("div");
    item.className = "stat-item";
    const labelEl = document.createElement("span");
    labelEl.textContent = label;
    const valueEl = document.createElement("strong");
    valueEl.textContent = value;
    item.appendChild(labelEl);
    item.appendChild(valueEl);
    list.appendChild(item);
  }
  return list;
}

function sortableValue(product, sortBy) {
  if (sortBy.startsWith("price")) return numericPrice(product);
  if (sortBy.startsWith("delivery")) return deliveryHours(product);
  if (sortBy.startsWith("rating")) return ratingValue(product);
  if (sortBy.startsWith("reviews")) return reviewsCount(product);
  if (sortBy.startsWith("discount")) return discountPercent(product);
  if (sortBy.startsWith("availability")) return availabilityScore(product);
  if (sortBy.startsWith("seller_rating")) return sellerRating(product);
  if (sortBy.startsWith("title")) return (product.title || "").toLocaleLowerCase("ru");
  return null;
}

function sortedProducts(products) {
  const sortBy = sortSelect.value;
  if (sortBy === "source") return products.slice();

  const direction = sortBy.endsWith("_asc") ? 1 : -1;
  return products
    .map((product, index) => ({ product, index }))
    .sort((a, b) => {
      const av = sortableValue(a.product, sortBy);
      const bv = sortableValue(b.product, sortBy);
      const aMissing = av == null || av === "";
      const bMissing = bv == null || bv === "";
      if (aMissing && bMissing) return a.index - b.index;
      if (aMissing) return 1;
      if (bMissing) return -1;
      if (typeof av === "string" || typeof bv === "string") {
        return String(av).localeCompare(String(bv), "ru") * direction || a.index - b.index;
      }
      return ((av > bv) - (av < bv)) * direction || a.index - b.index;
    })
    .map((item) => item.product);
}

function productStableKey(product) {
  const marketplace = product?.marketplace || "";
  if (product?.external_id) return `${marketplace}:${product.external_id}`;
  const specific = product?.marketplace_specific || {};
  for (const candidate of [
    product?.variant_id,
    product?.group_id,
    specific.nm_id,
    specific.sku,
    specific.product_id,
    specific.model_id,
  ]) {
    if (candidate != null && candidate !== "") return `${marketplace}:${candidate}`;
  }
  return product?.url || product?.title || "";
}

function isRenderableProduct(product) {
  const title = (product?.title || "").trim();
  return title && !["распродажа", "скидка", "скидки", "акция", "акции"].includes(title.toLocaleLowerCase("ru"));
}

function payloadForRender(payload) {
  const products = (payload.products || []).filter(isRenderableProduct);
  return {
    ...payload,
    count: products.length,
    products: sortedProducts(products),
  };
}

function isProductEnriched(product) {
  const raw = product?.attributes?.raw || {};
  if (Object.keys(raw).length > 0) return true;
  const desc = (product?.description || "").trim();
  return desc.length > 0;
}

function enrichmentStats(products) {
  if (!products || !products.length) return { total: 0, enriched: 0 };
  let enriched = 0;
  for (const p of products) if (isProductEnriched(p)) enriched += 1;
  return { total: products.length, enriched };
}

function renderSourceSection(sourceKey, payload) {
  const section = document.createElement("section");
  section.className = "source-section";
  section.dataset.source = sourceKey;

  const header = document.createElement("div");
  header.className = "source-header";

  const titleWrap = document.createElement("div");
  titleWrap.className = "source-title";
  titleWrap.textContent = SOURCE_LABELS[sourceKey] || sourceKey;
  header.appendChild(titleWrap);

  const status = document.createElement("div");
  const statusKind = payload.status || "unknown";
  status.className = `source-status ${statusKind}`;
  const count = payload.count || 0;
  const took = payload.took_seconds;
  const stats = enrichmentStats(payload.products || []);
  const enrichBadge = (stats.total > 0 && stats.enriched < stats.total)
    ? ` · загрузка характеристик ${stats.enriched}/${stats.total}`
    : "";
  status.textContent =
    statusKind === "ok" ? `${count} товаров${took != null ? ` · ${took}с` : ""}${enrichBadge}` :
    statusKind === "loading" ? (count ? `${count} найдено · ищем...${enrichBadge}` : "ищем...") :
    statusKind === "partial" ? `${count} товаров (частично)${enrichBadge}` :
    statusKind === "empty" ? "ничего не найдено" :
    statusKind === "error" ? "ошибка" :
    statusKind === "timeout" ? "таймаут" :
    statusKind === "stopped" ? "остановлено" :
    statusKind === "disabled" ? "отключён" :
    statusKind;
  header.appendChild(status);

  section.appendChild(header);

  if (payload.error) {
    const err = document.createElement("div");
    err.className = "diagnostics";
    err.textContent = formatDiagnostic(payload.error);
    section.appendChild(err);
  }
  // Filter out informational-only diagnostics that aren't actual errors;
  // wb_dest_fallback means "we fell back to default region" which is fine.
  const diagnostics = (payload.diagnostics || []).filter(isUserFacingDiagnostic);
  if (!payload.error && diagnostics.length) {
    const diag = document.createElement("div");
    diag.className = "diagnostics";
    diag.textContent = diagnostics.slice(0, 3).map(formatDiagnostic).join(" · ");
    section.appendChild(diag);
  }

  const products = payload.products || [];
  if (products.length) {
    section.appendChild(renderStats(products));
  }

  if (statusKind === "loading") {
    const loading = document.createElement("div");
    loading.className = "source-loading";
    loading.innerHTML = '<span class="loading-spinner dark"></span><span>Поиск продолжается</span>';
    section.appendChild(loading);
  }

  if (products.length) {
    if (viewMode === "table") {
      const tableEl = renderProductsTable(products);
      section.appendChild(tableEl);
    } else {
      const grid = document.createElement("div");
      grid.className = "products-grid";
      let visibleCount = Math.min(PRODUCTS_VISIBLE_STEP, products.length);

      for (const [index, product] of products.entries()) {
        const card = renderProductCard(product);
        if (index >= visibleCount) {
          card.hidden = true;
        }
        grid.appendChild(card);
      }
      section.appendChild(grid);

      if (products.length > visibleCount) {
        const actions = document.createElement("div");
        actions.className = "source-actions";

        const visibleLabel = document.createElement("span");
        visibleLabel.className = "source-visible-count";

        const showMoreBtn = document.createElement("button");
        showMoreBtn.type = "button";
        showMoreBtn.className = "show-more-btn";
        showMoreBtn.textContent = "Показать ещё";

        const updateVisibleLabel = () => {
          visibleLabel.textContent = `Показано ${visibleCount} из ${products.length}`;
        };

        showMoreBtn.addEventListener("click", () => {
          const nextVisibleCount = Math.min(visibleCount + PRODUCTS_VISIBLE_STEP, products.length);
          const cards = grid.querySelectorAll(".product-card");
          for (let index = visibleCount; index < nextVisibleCount; index += 1) {
            cards[index].hidden = false;
          }
          visibleCount = nextVisibleCount;
          updateVisibleLabel();

          if (visibleCount >= products.length) {
            showMoreBtn.remove();
          }
        });

        updateVisibleLabel();
        actions.appendChild(visibleLabel);
        actions.appendChild(showMoreBtn);
        section.appendChild(actions);
      }
    }
  }

  return section;
}

function topAttributeColumns(products, maxCount) {
  // Count how many products have each (key, normalized-label) attribute and
  // return the most populated. Coalesces "Цвет товара" / "Цвет" / "color"
  // into one column.
  const labelAliases = new Map([
    ["цвет товара", "Цвет"],
    ["цвет", "Цвет"],
    ["color", "Цвет"],
    ["объем памяти", "Память"],
    ["встроенная память", "Память"],
    ["память", "Память"],
    ["оперативная память", "ОЗУ"],
    ["размер", "Размер"],
    ["страна производства", "Страна"],
    ["страна", "Страна"],
    ["country", "Страна"],
    ["пол", "Пол"],
    ["материал", "Материал"],
    ["состав", "Состав"],
    ["диагональ экрана", "Диагональ"],
    ["диагональ", "Диагональ"],
    ["вес", "Вес"],
    ["мощность", "Мощность"],
    ["объем", "Объём"],
    ["объём", "Объём"],
    ["тип", "Тип"],
  ]);

  const stats = new Map(); // canonical label -> {count, sourceKeys: Set, total len}
  for (const product of products) {
    const raw = product?.attributes?.raw || {};
    const seenInProduct = new Set();
    for (const [key, value] of Object.entries(raw)) {
      if (!value || typeof value === "object") continue;
      const text = String(value).trim();
      if (!text || text.length > 60) continue;
      const norm = key.toLowerCase().trim();
      const canonical = labelAliases.get(norm) || key;
      if (seenInProduct.has(canonical)) continue;
      seenInProduct.add(canonical);
      const entry = stats.get(canonical) || { label: canonical, count: 0, sourceKeys: new Set() };
      entry.count += 1;
      entry.sourceKeys.add(key);
      stats.set(canonical, entry);
    }
    // Normalized attributes provide structured data that's often more reliable.
    const norm = product?.attributes?.normalized || {};
    if (norm.memory_gb && !seenInProduct.has("Память")) {
      const e = stats.get("Память") || { label: "Память", count: 0, sourceKeys: new Set() };
      e.count += 1; e.sourceKeys.add("memory_gb"); stats.set("Память", e);
    }
  }
  if (stats.size === 0) return [];
  // Rank by coverage (count desc), prefer common columns first when tied.
  const PREFERRED = ["Цвет", "Память", "Размер", "Бренд", "Материал", "Страна"];
  const arr = [...stats.values()];
  arr.sort((a, b) => {
    if (b.count !== a.count) return b.count - a.count;
    const ai = PREFERRED.indexOf(a.label);
    const bi = PREFERRED.indexOf(b.label);
    if (ai !== bi) return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
    return a.label.localeCompare(b.label, "ru");
  });
  // Require at least 2 products covered to avoid showing one-off columns.
  return arr.filter((s) => s.count >= 2).slice(0, maxCount);
}

function attributeValueFor(product, column) {
  const raw = product?.attributes?.raw || {};
  for (const sourceKey of column.sourceKeys) {
    if (sourceKey === "memory_gb") {
      const v = product?.attributes?.normalized?.memory_gb;
      if (v) return `${v} ГБ`;
    } else if (raw[sourceKey]) {
      return String(raw[sourceKey]).trim();
    }
  }
  // Case-insensitive fallback across raw keys.
  const wanted = column.label.toLowerCase();
  for (const [k, v] of Object.entries(raw)) {
    if (!v || typeof v === "object") continue;
    if (k.toLowerCase().includes(wanted)) return String(v).trim();
  }
  return "";
}

function renderProductsTable(products) {
  const wrap = document.createElement("div");
  wrap.className = "products-table-wrap";
  const table = document.createElement("table");
  table.className = "products-table";

  // Per-source columns differ a lot (electronics need memory/color; clothes
  // need size/material). Pick the 4 most-populated attribute keys across the
  // product set and add them as dedicated columns — beats one cramped chip row.
  const dynamicCols = topAttributeColumns(products, 4);
  const headers = [
    "",
    "Название",
    "Цена",
    "Доставка",
    "Наличие",
    "Оценка",
    ...dynamicCols.map((c) => c.label),
    "",
  ];

  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const label of headers) {
    const th = document.createElement("th");
    th.textContent = label;
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const product of products) {
    const tr = document.createElement("tr");
    tr.className = "products-table-row";

    const imgTd = document.createElement("td");
    imgTd.className = "products-table-img";
    const url = productMainImage(product);
    if (url) {
      const img = document.createElement("img");
      img.src = url;
      img.alt = "";
      img.loading = "lazy";
      img.onerror = () => { img.replaceWith(placeholderImage()); };
      imgTd.appendChild(img);
    } else {
      imgTd.appendChild(placeholderImage());
    }
    tr.appendChild(imgTd);

    const titleTd = document.createElement("td");
    const link = document.createElement("a");
    link.href = product.url || "#";
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.className = "products-table-title";
    link.textContent = product.title || "(без названия)";
    titleTd.appendChild(link);
    const brand = productBrandName(product);
    if (brand) {
      const b = document.createElement("div");
      b.className = "products-table-brand";
      b.textContent = brand;
      titleTd.appendChild(b);
    }
    tr.appendChild(titleTd);

    const priceTd = document.createElement("td");
    priceTd.className = "products-table-price";
    priceTd.textContent = priceText(product.price) || "—";
    tr.appendChild(priceTd);

    const deliveryTd = document.createElement("td");
    deliveryTd.textContent = deliveryText(product) || "—";
    tr.appendChild(deliveryTd);

    const availTd = document.createElement("td");
    const status = product?.availability?.status;
    if (status === "in_stock") {
      const qty = product?.availability?.quantity;
      availTd.innerHTML = `<span class="product-availability in_stock">В наличии${qty ? ` · ${qty}` : ""}</span>`;
    } else if (status === "out_of_stock") {
      availTd.innerHTML = `<span class="product-availability out_of_stock">Нет</span>`;
    } else {
      availTd.textContent = "—";
    }
    tr.appendChild(availTd);

    const ratingTd = document.createElement("td");
    ratingTd.className = "products-table-rating";
    const ratingVal = ratingValue(product);
    const reviewsVal = reviewsCount(product);
    if (ratingVal || reviewsVal) {
      const parts = [];
      if (ratingVal) parts.push(`★ ${ratingVal}`);
      if (reviewsVal) parts.push(`<span class="products-table-reviews">${reviewsVal}</span>`);
      ratingTd.innerHTML = parts.join(" ");
    } else {
      ratingTd.textContent = "—";
    }
    tr.appendChild(ratingTd);

    for (const col of dynamicCols) {
      const td = document.createElement("td");
      td.className = "products-table-attr";
      const value = attributeValueFor(product, col);
      td.textContent = value || "—";
      if (!value) td.classList.add("is-empty");
      tr.appendChild(td);
    }

    const expandTd = document.createElement("td");
    expandTd.className = "products-table-expand";
    const openBtn = document.createElement("button");
    openBtn.type = "button";
    openBtn.className = "products-table-open";
    openBtn.textContent = "Открыть";
    openBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      openProductDetail(product);
    });
    expandTd.appendChild(openBtn);
    tr.appendChild(expandTd);

    tr.addEventListener("click", () => openProductDetail(product));
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}

function upsertSourceSection(sourceKey, payload) {
  lastSourcePayloads.set(sourceKey, payload);
  if (viewMode === "groups") {
    // Groups view renders one consolidated container after `done`; per-source
    // sections aren't drawn while streaming, just kept in cache for re-render.
    updateGroupsPlaceholder();
    return;
  }
  const nextSection = renderSourceSection(sourceKey, payloadForRender(payload));
  const prevSection = sourceSectionEls.get(sourceKey);
  if (prevSection && prevSection.parentNode) {
    prevSection.replaceWith(nextSection);
  } else {
    resultsEl.appendChild(nextSection);
  }
  sourceSectionEls.set(sourceKey, nextSection);
}

function upsertProductInSource(sourceKey, product, count) {
  if (!isRenderableProduct(product)) {
    return lastSourcePayloads.get(sourceKey) || { status: "loading", count: 0, products: [] };
  }
  const previous = lastSourcePayloads.get(sourceKey) || { status: "loading", count: 0, products: [] };
  const products = Array.isArray(previous.products) ? previous.products.slice() : [];
  const productKey = productStableKey(product);
  const existingIndex = productKey ? products.findIndex((item) => productStableKey(item) === productKey) : -1;
  if (existingIndex >= 0) {
    products[existingIndex] = product;
  } else {
    products.push(product);
  }

  const nextPayload = {
    ...previous,
    status: previous.status && !["empty", "stopped"].includes(previous.status) ? previous.status : "loading",
    count: Math.max(count || 0, products.length),
    products,
  };
  upsertSourceSection(sourceKey, nextPayload);
  return nextPayload;
}

function rerenderSources() {
  if (viewMode === "groups") {
    renderGroupsView();
    return;
  }
  for (const [sourceKey, payload] of lastSourcePayloads.entries()) {
    const nextSection = renderSourceSection(sourceKey, payloadForRender(payload));
    const prevSection = sourceSectionEls.get(sourceKey);
    if (prevSection && prevSection.parentNode) {
      prevSection.replaceWith(nextSection);
      sourceSectionEls.set(sourceKey, nextSection);
    }
  }
}

function buildProductIndex() {
  // member_keys from the server use the same productStableKey scheme as the
  // frontend, so a flat map is enough to resolve clusters back to products.
  const idx = new Map();
  for (const payload of lastSourcePayloads.values()) {
    for (const product of payload.products || []) {
      if (!isRenderableProduct(product)) continue;
      const key = productStableKey(product);
      if (key) idx.set(key, product);
    }
  }
  return idx;
}

function totalProductsLoaded() {
  let sum = 0;
  for (const payload of lastSourcePayloads.values()) {
    sum += (payload.products || []).filter(isRenderableProduct).length;
  }
  return sum;
}

function enrichmentProgressTotal() {
  // Sum enriched/total across all sources so the groups view can wait until
  // every card has full characteristics before clustering kicks in.
  let total = 0;
  let enriched = 0;
  for (const payload of lastSourcePayloads.values()) {
    const s = enrichmentStats((payload.products || []).filter(isRenderableProduct));
    total += s.total;
    enriched += s.enriched;
  }
  return { total, enriched };
}

function groupsPlaceholderText() {
  const searchActive = currentSearchState && !currentSearchState.done;
  if (searchActive) {
    return `Идёт поиск… найдено ${totalProductsLoaded()} товаров, группы появятся после завершения.`;
  }
  // After search done — check enrichment progress. Clustering on partially
  // enriched data gives bad cluster labels and wrong member counts, so we
  // wait for every card to carry its characteristics.
  const prog = enrichmentProgressTotal();
  if (prog.total > 0 && prog.enriched < prog.total) {
    return `Загрузка характеристик ${prog.enriched}/${prog.total}. Группировка появится когда все карточки догрузятся.`;
  }
  if (lastClusters.length) return null;
  const loaded = totalProductsLoaded();
  if (loaded === 0) return "Нет данных для группировки — поиск ничего не вернул.";
  if (!lastDoneHadClustersField) {
    return `Найдено ${loaded} товаров, но бекенд не вернул поле clusters. Скорее всего сервер запущен со старой версией кода — перезапусти python3 app/server.py.`;
  }
  if (lastClustersDiag && lastClustersDiag.error) {
    return `Найдено ${loaded} товаров. Кластеризация упала: ${lastClustersDiag.error}`;
  }
  return `Найдено ${loaded} товаров, но кластеризация вернула пусто. Открой /api/search?query=… в новой вкладке и проверь поле clusters.`;
}

function updateGroupsPlaceholder() {
  if (viewMode !== "groups") return;
  const placeholder = resultsEl.querySelector(".groups-placeholder");
  const prog = enrichmentProgressTotal();
  const stillLoading = prog.total > 0 && prog.enriched < prog.total;
  const readyForClusters = lastClusters.length && !stillLoading;
  // If we just transitioned from "waiting" to "ready to cluster", redraw.
  if (placeholder && readyForClusters) {
    renderGroupsView();
    return;
  }
  // If clusters are rendered but enrichment regressed (new search), redraw.
  if (!placeholder && stillLoading) {
    renderGroupsView();
    return;
  }
  // Just update the live "X/Y загружено" text inside the existing placeholder.
  if (placeholder) {
    const text = groupsPlaceholderText();
    if (text) placeholder.textContent = text;
  }
}

function renderGroupsView() {
  resultsEl.innerHTML = "";
  sourceSectionEls = new Map();

  const container = document.createElement("div");
  container.className = "groups-container";

  // Defer clusters until every card has characteristics — otherwise we'd
  // group on garbage and confuse the user.
  const prog = enrichmentProgressTotal();
  const stillLoading = prog.total > 0 && prog.enriched < prog.total;

  if (!lastClusters.length || stillLoading) {
    const placeholder = document.createElement("div");
    placeholder.className = "groups-placeholder";
    placeholder.textContent = groupsPlaceholderText() || "Нет данных для группировки.";
    container.appendChild(placeholder);
    resultsEl.appendChild(container);
    return;
  }

  const productIndex = buildProductIndex();
  let rendered = 0;
  for (const cluster of lastClusters) {
    const section = renderGroupSection(cluster, productIndex);
    if (section) {
      container.appendChild(section);
      rendered += 1;
    }
  }
  if (!rendered) {
    const placeholder = document.createElement("div");
    placeholder.className = "groups-placeholder";
    placeholder.textContent = "Группы пусты — не удалось сопоставить товары с кластерами.";
    container.appendChild(placeholder);
  }
  resultsEl.appendChild(container);
}

function renderGroupSection(cluster, productIndex) {
  const products = (cluster.member_keys || [])
    .map((key) => productIndex.get(key))
    .filter((product) => product && isRenderableProduct(product));
  if (!products.length) return null;

  const section = document.createElement("section");
  section.className = "group-section";

  const header = document.createElement("button");
  header.type = "button";
  header.className = "group-header";
  header.setAttribute("aria-expanded", "true");

  const titleWrap = document.createElement("div");
  titleWrap.className = "group-title-wrap";
  const title = document.createElement("div");
  title.className = "group-title";
  title.textContent = cluster.label || "Без названия";
  titleWrap.appendChild(title);

  const sub = document.createElement("div");
  sub.className = "group-sub";
  const subParts = [];
  subParts.push(`${products.length} товаров`);
  if (cluster.price_min != null && cluster.price_max != null) {
    subParts.push(
      cluster.price_min === cluster.price_max
        ? formatMoney(cluster.price_min)
        : `${formatMoney(cluster.price_min)} – ${formatMoney(cluster.price_max)}`,
    );
  }
  sub.textContent = subParts.join(" · ");
  titleWrap.appendChild(sub);
  header.appendChild(titleWrap);

  const pills = document.createElement("div");
  pills.className = "group-pills";
  for (const [src, n] of Object.entries(cluster.sources || {})) {
    const pill = document.createElement("span");
    pill.className = `group-source-pill source-${src}`;
    pill.textContent = `${sourceShort(src)} · ${n}`;
    pills.appendChild(pill);
  }
  const caret = document.createElement("span");
  caret.className = "group-caret";
  caret.textContent = "▾";
  pills.appendChild(caret);
  header.appendChild(pills);

  section.appendChild(header);

  const body = document.createElement("div");
  body.className = "group-body";
  const grid = document.createElement("div");
  grid.className = "products-grid";
  for (const product of products) {
    grid.appendChild(renderProductCard(product));
  }
  body.appendChild(grid);
  section.appendChild(body);

  let expanded = true;
  header.addEventListener("click", () => {
    expanded = !expanded;
    body.classList.toggle("collapsed", !expanded);
    caret.textContent = expanded ? "▾" : "▸";
    header.setAttribute("aria-expanded", expanded ? "true" : "false");
  });
  return section;
}

function markPendingSourcesStopped() {
  for (const [sourceKey, payload] of lastSourcePayloads.entries()) {
    if ((payload.status || "unknown") === "loading") {
      upsertSourceSection(sourceKey, { status: "stopped", count: 0, products: [] });
    }
  }
}

function clearFiltersPanel() {
  filtersPanelEl.classList.add("hidden");
  filtersPanelStatusEl.textContent = "—";
  filtersPanelBodyEl.innerHTML = "";
  filterSelections = new Map();
  lastFiltersData = null;
  setFiltersPanelExpanded(false);
}

function setFiltersPanelExpanded(expanded) {
  filtersPanelExpanded = expanded;
  filtersPanelEl.classList.toggle("collapsed", !expanded);
  if (filtersToggleBtn) {
    filtersToggleBtn.setAttribute("aria-expanded", expanded ? "true" : "false");
    const caret = filtersToggleBtn.querySelector(".filters-toggle-caret");
    if (caret) caret.textContent = expanded ? "▾" : "▸";
  }
}

function setFiltersPanelLoading(query) {
  filtersPanelEl.classList.remove("hidden");
  filtersPanelStatusEl.innerHTML = '<span class="loading-spinner dark"></span>загружаем фильтры…';
  filtersPanelBodyEl.innerHTML = `<div class="filters-panel-empty">Сравниваем фильтры маркетплейсов по запросу «${escapeHtml(query)}»…</div>`;
}

function setFiltersPanelError(message) {
  filtersPanelEl.classList.remove("hidden");
  filtersPanelStatusEl.textContent = "ошибка";
  filtersPanelBodyEl.innerHTML = `<div class="filters-panel-error">${escapeHtml(message)}</div>`;
}

function sourceShort(name) {
  if (name === "wildberries") return "WB";
  if (name === "yandex_market") return "Я.М";
  if (name === "ozon") return "Ozon";
  return name;
}

function sourceFull(name) {
  return SOURCE_LABELS[name] || name;
}

function renderFiltersPanel(data) {
  lastFiltersData = data;
  const match = data.match || {};
  const totals = match.totals || {};
  const coverage = match.coverage || {};
  const allGroups = match.matched_groups || [];
  const unmatched = match.unmatched || {};
  const bySource = data.by_source || {};
  const totalSources = (data.sources || []).length || Object.keys(bySource).length || 1;

  const groups = filtersScopeAllOnly
    ? allGroups.filter((g) => (g.sources || []).length >= totalSources)
    : allGroups;

  const ratioPercent = Math.round((totals.matched_ratio || 0) * 100);
  const matcher = match.matcher || "heuristic";
  filtersPanelEl.classList.remove("hidden");
  const scopeNote = filtersScopeAllOnly
    ? ` · показано ${groups.length} из ${allGroups.length} (только общие для ${totalSources})`
    : "";
  filtersPanelStatusEl.innerHTML =
    `<strong>${ratioPercent}%</strong> совпало · ` +
    `${totals.matched_groups || 0} групп · ` +
    `${totals.matched_filters || 0} из ${totals.total_filters || 0} фильтров · ` +
    `матчер: ${escapeHtml(matcher)}${scopeNote}`;

  filtersPanelBodyEl.innerHTML = "";

  // Source pills row
  const sourceRow = document.createElement("div");
  sourceRow.className = "filters-source-row";
  for (const sourceKey of data.sources || Object.keys(bySource)) {
    const payload = bySource[sourceKey] || {};
    const cov = coverage[sourceKey] || { total: 0, matched: 0, ratio: 0 };
    const card = document.createElement("div");
    card.className = `filters-source-card status-${payload.status || "unknown"}`;
    const meta = payload.from === "live_api"
      ? "live API"
      : payload.from === "fixture"
        ? `фикстура ${payload.fixture || ""}`
        : payload.status === "skipped"
          ? "источник недоступен"
          : payload.status || "—";
    card.innerHTML = `
      <div class="filters-source-name">${escapeHtml(sourceFull(sourceKey))}</div>
      <div class="filters-source-total"><strong>${cov.total}</strong> фильтров</div>
      <div class="filters-source-matched">${cov.matched} сметчено (${Math.round((cov.ratio || 0) * 100)}%)</div>
      <div class="filters-source-meta">${escapeHtml(meta)}</div>
    `;
    sourceRow.appendChild(card);
  }
  filtersPanelBodyEl.appendChild(sourceRow);

  // Pair stats
  const pairStats = match.pair_stats || [];
  if (pairStats.length) {
    const pairsRow = document.createElement("div");
    pairsRow.className = "filters-pairs-row";
    for (const ps of pairStats) {
      const pair = document.createElement("div");
      pair.className = "filters-pair-chip";
      pair.innerHTML = `<span>${escapeHtml(sourceFull(ps.sources[0]))} ↔ ${escapeHtml(sourceFull(ps.sources[1]))}</span><strong>${ps.matched_groups}</strong>`;
      pairsRow.appendChild(pair);
    }
    filtersPanelBodyEl.appendChild(pairsRow);
  }

  if (!groups.length) {
    const emptyEl = document.createElement("div");
    emptyEl.className = "filters-panel-empty";
    emptyEl.textContent = "Не нашли пересечений по фильтрам (или источники сейчас отдают пусто).";
    filtersPanelBodyEl.appendChild(emptyEl);
  } else {
    const applyBar = document.createElement("div");
    applyBar.className = "filters-apply-bar";
    applyBar.innerHTML = `
      <div class="filters-apply-hint">Выбери значения → «Применить фильтры» — поиск перезапустится с этими условиями.</div>
      <div class="filters-apply-actions">
        <span class="filters-apply-count" id="filters-apply-count">0 выбрано</span>
        <button type="button" class="filters-reset-btn" id="filters-reset-btn">Сбросить</button>
        <button type="button" class="filters-apply-btn" id="filters-apply-btn" disabled>Применить фильтры</button>
      </div>
    `;
    filtersPanelBodyEl.appendChild(applyBar);

    const list = document.createElement("div");
    list.className = "filters-groups-list";
    for (const group of groups) {
      list.appendChild(renderMatchedGroupCard(group));
    }
    filtersPanelBodyEl.appendChild(list);
    updateFilterApplyState();

    document.getElementById("filters-reset-btn").addEventListener("click", () => {
      filterSelections = new Map();
      renderFiltersPanel(lastFiltersData);
    });
    document.getElementById("filters-apply-btn").addEventListener("click", applyFiltersAndSearch);
  }

  // Unmatched (collapsible)
  const unmatchedKeys = Object.keys(unmatched).filter((k) => (unmatched[k] || []).length);
  if (unmatchedKeys.length) {
    const details = document.createElement("details");
    details.className = "filters-unmatched";
    details.innerHTML = `<summary>Несметченные фильтры по источникам</summary>`;
    const body = document.createElement("div");
    body.className = "filters-unmatched-body";
    for (const key of unmatchedKeys) {
      const items = unmatched[key] || [];
      const block = document.createElement("div");
      block.className = "filters-unmatched-block";
      const labels = items.map((f) => `<li>${escapeHtml(f.label)}</li>`).join("");
      block.innerHTML = `<div class="filters-unmatched-source">${escapeHtml(sourceFull(key))} <span>(${items.length})</span></div><ul>${labels}</ul>`;
      body.appendChild(block);
    }
    details.appendChild(body);
    filtersPanelBodyEl.appendChild(details);
  }
}

function unionGroupValues(group) {
  const seen = new Map();
  for (const member of group.members || []) {
    for (const value of member.values || []) {
      if (!value || !value.name) continue;
      const key = value.name.toLocaleLowerCase("ru");
      if (!seen.has(key)) {
        seen.set(key, { name: value.name, sources: new Set([member.source]) });
      } else {
        seen.get(key).sources.add(member.source);
      }
    }
  }
  return Array.from(seen.values()).slice(0, 20);
}

function renderMatchedGroupCard(group) {
  const item = document.createElement("div");
  item.className = "filters-group-item";
  const sourcesPills = (group.sources || [])
    .map((s) => `<span class="filters-source-pill src-${s}">${escapeHtml(sourceShort(s))}</span>`)
    .join("");
  const scorePercent = Math.round((group.score || 0) * 100);
  const head = document.createElement("div");
  head.className = "filters-group-head";
  head.innerHTML = `
    <div class="filters-group-label">${escapeHtml(group.canonical_label)}</div>
    <div class="filters-group-sources">${sourcesPills}</div>
    <div class="filters-group-score" title="Похожесть label-ов">${scorePercent}%</div>
  `;
  item.appendChild(head);

  const memberList = document.createElement("ul");
  memberList.className = "filters-group-members";
  for (const m of group.members || []) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="filters-source-tag src-${m.source}">${escapeHtml(sourceShort(m.source))}</span> ${escapeHtml(m.label)}${m.type ? `<span class="filters-type">${escapeHtml(m.type)}</span>` : ""}`;
    memberList.appendChild(li);
  }
  item.appendChild(memberList);

  const values = unionGroupValues(group);
  if (values.length) {
    const chipBox = document.createElement("div");
    chipBox.className = "filters-value-chips";
    const labelKey = group.canonical_label;
    if (!filterSelections.has(labelKey)) filterSelections.set(labelKey, new Set());
    const selected = filterSelections.get(labelKey);
    for (const value of values) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "filters-value-chip";
      if (selected.has(value.name)) chip.classList.add("active");
      chip.innerHTML = `${escapeHtml(value.name)} <span class="filters-value-srcs">${Array.from(value.sources).map((s) => sourceShort(s)).join("·")}</span>`;
      chip.addEventListener("click", () => {
        if (selected.has(value.name)) selected.delete(value.name);
        else selected.add(value.name);
        chip.classList.toggle("active");
        updateFilterApplyState();
      });
      chipBox.appendChild(chip);
    }
    item.appendChild(chipBox);
  } else {
    const hint = document.createElement("div");
    hint.className = "filters-no-values";
    hint.textContent = "Нет значений (range/boolean — фильтрация значениями не настроена).";
    item.appendChild(hint);
  }
  return item;
}

function updateFilterApplyState() {
  const counter = document.getElementById("filters-apply-count");
  const btn = document.getElementById("filters-apply-btn");
  if (!counter || !btn) return;
  let total = 0;
  for (const sel of filterSelections.values()) total += sel.size;
  counter.textContent = `${total} выбрано`;
  btn.disabled = total === 0;
}

function buildFilterSpecs(matchData) {
  const specs = [];
  if (!matchData || !matchData.match) return specs;
  const groups = matchData.match.matched_groups || [];
  for (const group of groups) {
    const selected = filterSelections.get(group.canonical_label);
    if (!selected || selected.size === 0) continue;
    const wanted = new Set(Array.from(selected, (s) => s.toLocaleLowerCase("ru")));
    const selections = [];
    for (const member of group.members || []) {
      const matchedIds = [];
      const matchedNames = [];
      for (const v of member.values || []) {
        if (!v || !v.name) continue;
        if (wanted.has(v.name.toLocaleLowerCase("ru"))) {
          matchedIds.push(v.id || v.name);
          matchedNames.push(v.name);
        }
      }
      if (matchedIds.length) {
        selections.push({
          source: member.source,
          raw_id: member.raw_id || member.id,
          value_ids: matchedIds,
          value_names: matchedNames,
        });
      }
    }
    // Only emit specs that have at least one source with matching values; if
    // no source carries the picked value names, leave all sources unfiltered.
    if (selections.length) {
      specs.push({ label: group.canonical_label, selections, values: Array.from(selected) });
    }
  }
  return specs;
}

function applyFiltersAndSearch() {
  const specs = buildFilterSpecs(lastFiltersData);
  if (!specs.length) return;
  const event = new Event("submit", { cancelable: true });
  form.dispatchEvent(event);
}

async function runFiltersCompare(query, sources) {
  if (activeFiltersController) activeFiltersController.abort();
  const controller = new AbortController();
  activeFiltersController = controller;
  setFiltersPanelLoading(query);
  const params = new URLSearchParams({ query });
  if (sources && sources.length) params.set("sources", sources.join(","));
  try {
    const res = await fetch(`/api/filters?${params.toString()}`, { signal: controller.signal });
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      setFiltersPanelError((data && (data.message || data.error)) || res.statusText);
      return;
    }
    renderFiltersPanel(data);
  } catch (err) {
    if (err.name === "AbortError") return;
    setFiltersPanelError(err.message || String(err));
  } finally {
    if (activeFiltersController === controller) activeFiltersController = null;
  }
}

function showSearchProgress(state) {
  const statusText = state.statusOverride || (state.done ? "готово" : `ищем: ${state.completed} из ${state.totalSources}`);
  const foundText = state.total ? ` · <strong>Найдено:</strong> ${state.total}` : "";
  const timeText = state.tookSeconds != null ? ` · <strong>Время:</strong> ${state.tookSeconds}с` : "";
  showMeta(
    `<strong>Запрос:</strong> ${escapeHtml(state.query)} · ` +
    `<strong>Регион:</strong> ${escapeHtml(state.regionLabel)} · ` +
    `<strong>Лимит:</strong> до ${state.limit} на источник · ` +
    `<strong>Статус:</strong> ${escapeHtml(statusText)}` +
    foundText +
    timeText
  );
}

function updateGeoButton() {
  if (!geoBtn) return;
  if (selectedGeo) {
    geoBtn.classList.add("active");
    geoBtn.textContent = "Гео";
    geoBtn.title = `Геопозиция: ${selectedGeo.lat.toFixed(4)}, ${selectedGeo.lon.toFixed(4)}`;
  } else {
    geoBtn.classList.remove("active");
    geoBtn.textContent = "Гео";
    geoBtn.title = "Определить геопозицию";
  }
}

function resetGeoSelection() {
  selectedGeo = null;
  updateGeoButton();
}

function applyGeoSelection(lat, lon) {
  selectedGeo = { lat, lon };
  const nearest = nearestRegion(lat, lon);
  if (nearest) {
    regionSelect.value = nearest.key;
  }
  updateGeoButton();
}

function nearestRegion(lat, lon) {
  let nearest = null;
  let nearestDistance = Infinity;
  for (const region of supportedRegions) {
    if (typeof region.latitude !== "number" || typeof region.longitude !== "number") continue;
    const distance = geoDistanceKm(lat, lon, region.latitude, region.longitude);
    if (distance < nearestDistance) {
      nearest = region;
      nearestDistance = distance;
    }
  }
  return nearest;
}

function geoDistanceKm(lat1, lon1, lat2, lon2) {
  const radiusKm = 6371;
  const toRad = (value) => value * Math.PI / 180;
  const deltaLat = toRad(lat2 - lat1);
  const deltaLon = toRad(lon2 - lon1);
  const a = Math.sin(deltaLat / 2) ** 2
    + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(deltaLon / 2) ** 2;
  return radiusKm * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function requestGeoPosition() {
  if (!navigator.geolocation) {
    showMeta("Браузер не отдал геопозицию");
    return;
  }
  geoBtn.disabled = true;
  navigator.geolocation.getCurrentPosition(
    (position) => {
      geoBtn.disabled = false;
      applyGeoSelection(position.coords.latitude, position.coords.longitude);
    },
    (err) => {
      geoBtn.disabled = false;
      showMeta(`<strong>Гео:</strong> ${escapeHtml(err.message || "не удалось получить координаты")}`);
    },
    { enableHighAccuracy: false, timeout: 8000, maximumAge: 300000 }
  );
}

function handleStreamEvent(message, state) {
  if (state.cancelled) return;
  const data = message.data || {};
  if (message.event === "start") {
    state.regionLabel = data.region || state.regionLabel;
    state.totalSources = (data.sources || state.sources).length;
    showSearchProgress(state);
    return;
  }

  if (message.event === "source_start") {
    upsertSourceSection(data.source, { status: "loading", count: 0, products: [] });
    return;
  }

  if (message.event === "source_result") {
    const payload = data.payload || { status: "empty", count: 0, products: [] };
    state.bySource[data.source] = payload;
    state.completed = data.completed || Object.keys(state.bySource).length;
    state.total = Object.values(state.bySource).reduce((sum, source) => sum + (source.count || 0), 0);
    state.tookSeconds = data.took_seconds;
    upsertSourceSection(data.source, payload);
    showSearchProgress(state);
    return;
  }

  if (message.event === "product") {
    if (!data.source || !data.product) return;
    const payload = upsertProductInSource(data.source, data.product, data.count);
    state.bySource[data.source] = payload;
    state.total = Object.values(state.bySource).reduce((sum, source) => sum + (source.count || 0), 0);
    state.tookSeconds = data.took_seconds;
    showSearchProgress(state);
    return;
  }

  if (message.event === "heartbeat") {
    state.completed = data.completed || state.completed;
    state.tookSeconds = data.took_seconds;
    showSearchProgress(state);
    return;
  }

  if (message.event === "done") {
    state.done = true;
    state.bySource = data.by_source || state.bySource;
    state.completed = state.totalSources;
    state.total = Object.values(state.bySource).reduce((sum, source) => sum + (source.count || 0), 0);
    state.tookSeconds = data.took_seconds;
    lastDoneHadClustersField = "clusters" in data;
    lastClustersDiag = (data.diagnostics && data.diagnostics.clusters) || null;
    if (Array.isArray(data.clusters)) {
      lastClusters = data.clusters;
    }
    if (viewMode === "groups") renderGroupsView();
    showSearchProgress(state);
    return;
  }

  if (message.event === "error") {
    throw new Error(data.message || data.error || "stream_error");
  }
}

async function readSearchStream(response, onMessage) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop() || "";
    for (const line of lines) {
      if (line.trim()) onMessage(JSON.parse(line));
    }
  }

  buffer += decoder.decode();
  if (buffer.trim()) onMessage(JSON.parse(buffer));
}

function setLoading(loading) {
  searchBtn.disabled = loading;
  stopBtn.disabled = !loading;
  searchBtn.innerHTML = loading
    ? '<span class="loading-spinner"></span>Ищем...'
    : "Найти";
}

async function runSearch(event) {
  event.preventDefault();
  if (activeSearchController) {
    activeSearchController.abort();
  }

  const query = queryInput.value.trim();
  if (!query) return;
  const region = regionSelect.value || "moscow";
  const limit = productLimit();
  const sources = selectedSources();
  if (!sources.length) {
    showMeta("Выберите хотя бы один источник");
    return;
  }
  // Capture current filter selections BEFORE clearing the panel; they get sent
  // with the new search request below.
  const pendingFilterSpecs = buildFilterSpecs(lastFiltersData);

  emptyState.style.display = "none";
  resultsEl.innerHTML = "";
  sourceSectionEls = new Map();
  lastSourcePayloads = new Map();
  lastClusters = [];
  lastClustersDiag = null;
  lastDoneHadClustersField = false;
  metaEl.classList.add("hidden");
  clearFiltersPanel();
  setLoading(true);
  if (viewMode === "groups") renderGroupsView();

  runFiltersCompare(query, sources);

  const controller = new AbortController();
  activeSearchController = controller;

  const params = new URLSearchParams({
    query,
    region,
    limit: String(limit),
    sources: sources.join(","),
    enrich: "1",
    enrich_limit: String(limit),
    task_timeout: "600",
  });
  if (selectedGeo) {
    params.set("lat", selectedGeo.lat.toFixed(6));
    params.set("lon", selectedGeo.lon.toFixed(6));
  }
  if (pendingFilterSpecs.length) {
    params.set("filter_specs", JSON.stringify(pendingFilterSpecs));
  }

  const state = {
    query,
    regionLabel: `${regionSelect.selectedOptions[0]?.textContent || region}${selectedGeo ? " · гео" : ""}`,
    limit,
    sources,
    totalSources: sources.length,
    completed: 0,
    total: 0,
    tookSeconds: null,
    done: false,
    bySource: {},
    statusOverride: null,
    cancelled: false,
  };
  currentSearchState = state;

  showSearchProgress(state);
  for (const sourceKey of sources) {
    upsertSourceSection(sourceKey, { status: "loading", count: 0, products: [] });
  }

  try {
    const res = await fetch(`/api/search/stream?${params.toString()}`, {
      signal: controller.signal,
    });
    if (!res.ok) {
      const data = await res.json().catch(() => null);
      showMeta(`<strong>Ошибка:</strong> ${escapeHtml((data && (data.message || data.error)) || res.statusText)}`);
      return;
    }
    if (!res.body) {
      throw new Error("stream_not_supported");
    }
    await readSearchStream(res, (message) => handleStreamEvent(message, state));
  } catch (err) {
    if (err.name === "AbortError") return;
    showMeta(`<strong>Сетевая ошибка:</strong> ${escapeHtml(err.message || err)}`);
  } finally {
    if (activeSearchController === controller) {
      setLoading(false);
      activeSearchController = null;
    }
  }
}

function setViewMode(mode) {
  if (!["grid", "table", "groups"].includes(mode)) return;
  if (viewMode === mode) return;
  viewMode = mode;
  if (viewGridBtn) viewGridBtn.classList.toggle("active", mode === "grid");
  if (viewTableBtn) viewTableBtn.classList.toggle("active", mode === "table");
  if (viewGroupsBtn) viewGroupsBtn.classList.toggle("active", mode === "groups");

  if (mode === "groups") {
    renderGroupsView();
    return;
  }
  // Re-mount per-source sections from cached state when leaving groups mode.
  resultsEl.innerHTML = "";
  sourceSectionEls = new Map();
  for (const [sourceKey, payload] of lastSourcePayloads.entries()) {
    upsertSourceSection(sourceKey, payload);
  }
}

if (viewGridBtn) viewGridBtn.addEventListener("click", () => setViewMode("grid"));
if (viewTableBtn) viewTableBtn.addEventListener("click", () => setViewMode("table"));
if (viewGroupsBtn) viewGroupsBtn.addEventListener("click", () => setViewMode("groups"));

function detectYandexOutliers(products) {
  return new Set();
}

form.addEventListener("submit", runSearch);
if (geoBtn) geoBtn.addEventListener("click", requestGeoPosition);
regionSelect.addEventListener("change", resetGeoSelection);
stopBtn.addEventListener("click", () => {
  if (activeSearchController) {
    activeSearchController.abort();
    activeSearchController = null;
  }
  markPendingSourcesStopped();
  setLoading(false);
  if (currentSearchState) {
    currentSearchState.cancelled = true;
    currentSearchState.statusOverride = "остановлено";
    showSearchProgress(currentSearchState);
  } else {
    showMeta("<strong>Статус:</strong> остановлено");
  }
});
sortSelect.addEventListener("change", rerenderSources);
limitInput.addEventListener("change", productLimit);
if (filtersToggleBtn) {
  filtersToggleBtn.addEventListener("click", () => setFiltersPanelExpanded(!filtersPanelExpanded));
}
if (filtersScopeAll) {
  filtersScopeAll.addEventListener("change", () => {
    filtersScopeAllOnly = filtersScopeAll.checked;
    if (lastFiltersData) renderFiltersPanel(lastFiltersData);
  });
}
loadHealth();
loadRegions();
