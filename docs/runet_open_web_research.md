# Runet Open Web research

Дата: 2026-05-23

## Цель

Четвертый источник нужен не как еще один marketplace с теми же антибот-рисками, а как независимый open-web слой: профильные магазины под категорию товара, нормализованные в `basic_scheme.json` и подключенные к общему ранжированию.

## Подход

1. Query planner из `scripts/search_quality.py` определяет категорию и строит нормализованный запрос.
2. `RunetOpenWebSearchParser` выбирает магазины из category-aware registry.
3. Парсер открывает страницы поиска магазинов и собирает product-like ссылки.
4. Для карточек сначала извлекается JSON-LD `Product`, затем HTML fallback.
5. Результат нормализуется в schema `0.2` и скорится через search-quality layer.

## Текущий store registry

Оргтехника и память:

- `dns-shop.ru`
- `citilink.ru`
- `mvideo.ru`
- `eldorado.ru`
- `regard.ru`
- `oldi.ru`

Шины:

- `koleso.ru`
- `mosautoshina.ru`
- `4tochki.ru`
- `shinservice.ru`

Одежда:

- `sportmaster.ru`
- `finn-flare.ru`
- `gloria-jeans.ru`
- `sela.ru`

Уже покрытые маркетплейсы исключаются: `ozon.ru`, `wildberries.ru`, `market.yandex.ru`, `aliexpress.ru`, `megamarket.ru`, `sbermegamarket.ru`, `avito.ru`.

## Извлекаемые поля

Основной источник данных - JSON-LD `Product`:

- `name` -> `title`;
- `brand.name` -> `brand.name`;
- `offers.price` -> `price.current`;
- `offers.priceCurrency` -> `price.currency`;
- `image` -> `media.image_urls`;
- `description` -> `description`;
- `sku`, `mpn`, `gtin`, `color`, `size`, `material` -> `attributes.raw`.

HTML fallback:

- `og:title`, `twitter:title`, `h1`, `<title>` для названия;
- `product:price:amount`, `[itemprop=price]` и текстовые цены с `руб/₽`;
- `og:image`, `twitter:image`, первый `img`;
- таблицы `tr/th/td` и `dl/dt/dd` для характеристик.

## Ограничения

- HTML магазинов нестабилен, поэтому источник должен быть best-effort.
- Ошибки отдельных магазинов пишутся в `source_meta.diagnostics`, а не валят весь результат.
- Поиск через HTML search discovery опционален; базовый режим использует только заранее выбранные магазины.
- Для live режима нужен controlled probe по категориям, потому что разные магазины имеют разные лимиты и защиту.

## MVP limits

Защитные настройки в коде:

- per-domain delay: `0.4s`;
- default timeout: `10s`;
- `max_search_pages`: CLI default `8`, в агрегаторе default `4`;
- `max_product_pages`: CLI default `20`, в агрегаторе default `8`;
- кэш страниц: `30 минут`;
- кэш search: `15 минут`.

Рабочие live RPS/лимиты еще надо подтвердить отдельным коротким прогоном без агрессивного скрейпа.

## Live sanity 2026-05-23

Проверка выполнялась короткими запросами `max_search_pages=4`, `max_product_pages=4`, без HTML discovery.

Оргтехника, запрос `принтер hp laserjet`:

- `dns-shop.ru` вернул `401`;
- `citilink.ru` вернул `429` challenge page;
- `mvideo.ru` отдал HTML, но без валидных карточек в статической выдаче;
- `eldorado.ru` вернул `503`;
- итог: `0` валидных товаров.

Шины, запрос `шины 205/55 r16`:

- `koleso.ru`, `mosautoshina.ru`, `shinservice.ru` на текущих search URL вернули `404`;
- `4tochki.ru` отдал HTML, но прямые кандидаты были category/content pages;
- после фильтрации псевдотоваров итог: `0` валидных товаров.

Одежда, запрос `шорты мужские adidas`:

- `sportmaster.ru` вернул `401`;
- `finn-flare.ru` вернул `503`;
- `gloria-jeans.ru`/`sela.ru` доступны частично;
- `sela.ru` вернул 1 валидную карточку товара с ценой и изображением, но релевантность широкая, потому что бренд `adidas` был отброшен на broad-уровне.
- дополнительный прогон с HTML discovery не увеличил число валидных товаров на этом запросе.

Вывод: кодовый MVP четвертого источника рабочий, но store registry надо делать более прикладным: уточнять search URL магазинов, добавлять store-specific селекторы/API там, где generic HTML не хватает, и разделять "рабочий магазин" от "магазин с антибот/challenge".

## Команды

```bash
python3 scripts/runet_search.py "принтер hp laserjet" --limit 5 --no-discovery
python3 scripts/runet_search.py "шины 205/55 r16" --limit 5 --max-search-pages 4 --max-product-pages 10
python3 scripts/runet_fixture_smoke.py
python3 scripts/aggregate_search.py "принтер hp laserjet" --marketplaces wb,ozon,ym,runet --limit 10 --candidate-limit 5 --max-variants 2
```
