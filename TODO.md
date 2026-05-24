# TODO

## Wildberries

Цель: на базе уже проверенного ресерча сделать два рабочих парсера/адаптера Wildberries и привести их выход к `basic_scheme.json`.

### 1. WildberriesSearchParser

- [X] Реализовать полноценный parser/service для поиска по текстовому запросу через `u-search.wb.ru/exactmatch/ru/common/v18/search`.
- [X] Входные параметры: `query`, `page`, `limit`, `sort`, `dest/region`.
- [X] Возвращать список товаров в универсальной схеме `basic_scheme.json`.
- [X] Маппить из search-ответа:`external_id` / `nm_id`;

  `group_id` / `root`;

  `variant_id`;

  `offer_id` из `sizes[].optionId`;

  `title`, `brand`, `category`;

   `price.current`, `price.old`, `currency`;

  `rating.value`, `rating.reviews_count`;

  `seller.id`, `seller.name`, `seller.rating`;

  `availability.status`, `availability.quantity`, `warehouse_id`;

  `delivery.time_min_hours`, `delivery.time_max_hours`;

  `marketplace_specific.wildberries`.
- [X] Не обогащать всю выдачу автоматически: только top N товаров или по отдельному флагу.
- [X] Добавить кэш поиска по ключу `query + page + sort + dest` на 10-30 минут.
- [X] Добавить rate limit: 3-5 RPS sustained, burst до 8 RPS, backoff при `429`.
- [ ] Добавить автоматический unit/smoke тест на запрос `шорты мужские adidas`.

### 2. WildberriesProductParser

- [X] Реализовать полноценный parser/service карточки товара по `nmID` или URL.
- [X] Из URL доставать `nmID`.
- [X] Получать карточку через `wbbasket.../info/ru/card.json`.
- [X] Получать fallback-данные через `card.wb.ru/cards/v4/detail`, если полезно.
- [X] Маппить из card-ответа:
  - [X] `description`;
  - [X] `attributes.raw`;
  - [X] `attributes.normalized`;
  - [X] `category`;
  - [X] `media.photo_count`, `media.has_video`, `media.image_urls`;
  - [X] `match_keys.vendor_code`;
  - [X] `colors_nm_ids`, `full_colors_nm_ids`;
  - [X] `chrt_ids`;
  - [X] `basket_host`.
- [X] Определять `basket_host` и кэшировать его по `nmID`.
- [X] Не падать, если enrichment недоступен: возвращать базовые поля и ошибку в diagnostics/source_meta.
- [X] Добавить кэш `card.json` на 12-24 часа.
- [X] Добавить rate limit: 3-5 RPS sustained, burst до 8 RPS, backoff при `429`.
- [ ] Добавить автоматический smoke тест на `nmID=273639353`.

### 3. Нормализация Wildberries

- [X] Обновить нормализацию под схему `basic_scheme.json` версии `0.2`.
- [X] Разделить нормализацию на:
  - [X] product-level поля;
  - [X] offer-level поля;
  - [X] variant-level поля;
  - [X] marketplace-specific поля.
- [X] Нормализовать базовые атрибуты:
  - [X] бренд;
  - [X] модель;
  - [X] цвет;
  - [X] размер;
  - [X] пол;
  - [X] материал;
  - [X] страна производства;
  - [X] память/объем/емкость для электроники.
- [X] Сохранять исходные характеристики в `attributes.raw`.
- [X] Формировать `match_keys.semantic_key`.
- [X] Сохранять полный raw-ответ в `raw_data`.

### 4. Wildberries HTTP server

- [X] Реализовать HTTP server на `scripts/wb_server.py`.
- [X] Добавить `GET /health`.
- [X] Добавить `GET /search`.
- [X] Добавить `GET /product`.
- [X] Проверить вручную `/health` и `/search?query=шорты мужские adidas&limit=1&enrich=1`.

## Ozon

- [X] Провести такой же research, как для WB.
- [X] Проверить официальные seller/partner API и публичные JSON-запросы сайта.
- [X] Проверить поиск по текстовому запросу.
- [X] Проверить карточку по URL/ID.
- [X] Описать доступные поля и ограничения.
- [X] Сделать PoC поиска и карточки.
- [X] Добавить controlled probe для `composer-api.bx`; текущая проверка из окружения упирается в repeated `307`, поэтому production RPS не утверждаем.
- [X] Реализовать `OzonSearchParser`.
- [X] Реализовать `OzonProductParser`.
- [X] Привести результат к `basic_scheme.json`.
- [X] Реализовать `scripts/ozon_server.py` с `GET /health`, `GET /search`, `GET /product`.

### Selenium-based решение (реализовано 2026-05-21)

Live `requests`-парсер блокировался `307/403` на `composer-api.bx`. Решено через
`undetected-chromedriver` + DOM-парсинг тайлов товаров.

- [X] Проверить browser-based сценарий через Selenium в headed-режиме.
- [X] Реализовать `scripts/ozon_selenium.py` — `OzonSeleniumClient`, drop-in замена `OzonClient`.
- [X] Парсить DOM тайлов `[class*="tile-root"]` — title, price, sku, image из href.
- [X] Проверить live поиск: `python scripts/ozon_selenium.py search "iphone 15 128gb" --limit 5` — возвращает 5 товаров с ценами и картинками.
- [ ] Подтянуть rating и reviews_count (CSS-классы обфусцированы, нужен дополнительный селектор).
- [ ] Подтянуть old_price / скидку из тайла.
- [ ] Проверить карточку товара: `python scripts/ozon_selenium.py product 397529235`.
- [ ] Добавить поддержку headless-режима (сейчас работает, но не тестировался на CI).

## Yandex Market

- [X] Провести такой же research, как для WB.
- [X] Проверить официальные partner API и публичные JSON-запросы сайта.
- [X] Проверить поиск по текстовому запросу.
- [X] Проверить карточку по URL/ID.
- [X] Описать доступные поля и ограничения.
- [X] Сделать PoC поиска и карточки.
- [X] Добавить controlled probe для storefront HTML; текущая live-проверка из окружения упирается в `403` VPN/region block, поэтому production RPS не утверждаем.
- [X] Реализовать `YandexMarketSearchParser`.
- [X] Реализовать `YandexMarketProductParser`.
- [X] Привести результат к `basic_scheme.json`.
- [X] Реализовать `scripts/ym_server.py` с `GET /health`, `GET /search`, `GET /product`.
- [X] Добавить offline fallback `scripts/ym_from_json.py` и smoke `scripts/ym_fixture_smoke.py`.
- [X] Проверить no-VPN HTML выдачи Яндекс Маркета: `status=200`, товары лежат в SSR/APIary `noframes data-apiary="patch"`.
- [X] Переделать extractor под APIary patch payload: доставать `productId`, `skuId`, `offerId`, title, price, picture.
- [X] Не считать служебные APIary виджеты, фильтры и JSON-LD search summary товарами.
- [X] Добавить поддержку ссылок `/card/.../{id}` вместе с `/product.../{id}`.
- [X] Обновить offline fallback `ym_from_json.py`, чтобы сохраненный HTML возвращал товары в `basic_scheme.json`.
- [X] Добавить smoke-тест на APIary HTML fixture.
- [X] Проверить live `scripts/ym_search.py` без VPN на 2-3 запросах и зафиксировать рабочий RPS/лимиты отдельно от старого VPN-block результата.
  Результат 2026-05-22: 3/3 live search-запроса и controlled probe на `0.2 RPS` получили `403 Yandex VPN/region block page`; рабочий RPS для requests-парсера не подтвержден, текущий `1 RPS, burst=2` остается только защитным лимитом.
- [X] Добавить experimental `curl_cffi` fingerprint profiles и cookie-input для YM live-проверок.
  Результат 2026-05-22: `chrome120_windows`, `chrome124_macos`, `chrome124_windows`, `safari180_macos` все получили `403`; без валидной browser session spoofing не помогает.

## Четвертый источник: Runet Open Web

Цель: добрать независимые магазины под категорию товара, чтобы четвертый источник не зависел от одного marketplace-антибота и давал дополнительные кандидаты для сравнения.

- [X] Выбрать подход: category-aware store registry + optional HTML search discovery вместо прямого парсинга еще одного крупного marketplace.
- [X] Добавить пул магазинов по категориям:
  - [X] оргтехника/память: `dns-shop.ru`, `citilink.ru`, `mvideo.ru`, `eldorado.ru`, `regard.ru`, `oldi.ru`;
  - [X] шины: `koleso.ru`, `mosautoshina.ru`, `4tochki.ru`, `shinservice.ru`;
  - [X] одежда: `sportmaster.ru`, `finn-flare.ru`, `gloria-jeans.ru`, `sela.ru`.
- [X] Исключать уже покрытые marketplace-домены: Ozon, Wildberries, Яндекс Маркет, AliExpress, Мегамаркет, Avito.
- [X] Реализовать `scripts/runet_parser.py`:
  - [X] `RunetOpenWebSearchParser`;
  - [X] TTL-cache страниц;
  - [X] per-domain delay;
  - [X] extraction через JSON-LD `Product`;
  - [X] fallback extraction через `og:title`, `product:price:amount`, `[itemprop=price]`, таблицы характеристик и картинки;
  - [X] нормализация в `basic_scheme.json` schema `0.2`;
  - [X] diagnostics по ошибкам отдельных магазинов.
- [X] Реализовать CLI `scripts/runet_search.py`.
- [X] Добавить офлайн smoke `scripts/runet_fixture_smoke.py`.
- [X] Подключить источник к `scripts/aggregate_search.py` как `runet`.
- [X] Сделать короткий live sanity на 3 категориях и зафиксировать нестабильные магазины.
  Результат 2026-05-23: офисная техника `0` валидных товаров (`401/429/503` и JS/static HTML), шины `0` после фильтрации category pages, одежда `1` валидная карточка Sela с ценой и фото; подробности в `docs/runet_open_web_research.md`.
- [ ] Зафиксировать рабочий RPS/лимиты полноценным controlled probe после уточнения store-specific search URLs.
- [ ] Расширить store registry под категории из тестового датасета после анализа частот запросов.

## Общая инфраструктура

- [ ] Описать общий интерфейс адаптера:
  - [ ] `search(query, page, limit, region)`;
  - [ ] `get_product(external_id_or_url, region)`;
  - [ ] `normalize(raw)`.
- [ ] Сделать общую модель результата по `basic_scheme.json`.
- [X] Добавить слой качества поиска: query relaxation `strict/core/broad`, correction/synonyms baseline, log-price/log-size features и scoring нормализованных кандидатов.
- [X] Добавить четвертый источник `runet_open_web` в общий агрегатор.
- [ ] Добавить общий rate limiter по marketplace и endpoint.
- [ ] Добавить кэширование search/card/basket_host.
- [ ] Добавить diagnostics для ошибок парсинга и частично заполненных товаров.
- [ ] Добавить интеграционные smoke-тесты для каждого маркетплейса.
- [ ] Добавить матчинг товаров между маркетплейсами по `match_keys`, `attributes.normalized`, `brand`, `category`.
