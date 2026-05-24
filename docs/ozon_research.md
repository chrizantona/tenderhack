# Ozon MVP data access research

Дата проверки: 2026-05-20.

## Краткий вывод

Для хакатонного MVP ближайший аналог Wildberries-подхода — публичный storefront JSON
`composer-api.bx/page/json/v2`, который использует сайт Ozon. Он может отдавать поиск,
категории и карточки как дерево виджетов, но это неофициальный endpoint: имена виджетов
и структура состояния меняются, а запросы легко попадают под антибот/гео-редиректы.

Официальный Seller API подходит для товаров конкретного продавца с API-ключом и
`Client-Id`, но не закрывает публичный покупательский поиск по всему каталогу.
Performance API относится к рекламе и тоже требует ключ.

## Таблица способов

| Способ | Что получает | Сложность | Стабильность | Риски | Рекомендация |
|---|---|---:|---:|---|---|
| `composer-api.bx/page/json/v2?url=/search/?text=...` | Публичная выдача как widget tree: товары, цены, рейтинг, отзывы, продавец, картинки при наличии в виджете | Средняя | Низкая-средняя | Неофициальный endpoint, `307/403/429`, зависимость от региона/IP и имен виджетов | Использовать для MVP с кэшем, лимитами и диагностикой |
| `composer-api.bx/page/json/v2?url=/product/.../` | Карточка товара как widget tree: заголовок, цена, медиа, часть seller/rating | Средняя | Низкая-средняя | Может требовать дополнительные layout-параметры, легко блокируется | Использовать для ручного URL/ID и enrichment top N |
| Дополнительные layout-запросы `pdppage2copy`, `appPdpPage3` | Описание, характеристики, seller/delivery в отдельных блоках | Средняя-высокая | Низкая | Названия layout и stateId нестабильны | Использовать best-effort, не валить весь результат |
| HTML-страницы `www.ozon.ru` | Теоретически все видимое на сайте | Высокая | Низкая | Antibot/challenge, тяжелый browser runtime | Не использовать как backend-парсер |
| Официальный Seller API | Товары, цены, остатки и отчеты продавца с ключом | Средняя | Высокая | Нужен seller account, нет глобального поиска | Не заменяет публичный каталог, годится для seller-интеграций |
| Performance API | Рекламные кампании и статистика | Средняя | Высокая | Нужен рекламный API-ключ, не каталог | Не подходит для парсера товаров |
| Коммерческие/партнерские фиды | Лицензированные данные | Средняя | Высокая | Договор/стоимость | Production-направление |

## Проверка endpoint'ов

Публичный endpoint:

```text
https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=/search/?text=iphone%2015&page=1
https://api.ozon.ru/composer-api.bx/page/json/v2?url=/product/397529235/
```

С текущего окружения короткая проверка вернула повторяющийся `307 Temporary Redirect`
с добавлением `__rr=1`, `__rr=2`, ... к URL. Это признак защитного слоя Ozon, а не
ошибки построения URL. Поэтому в реализации `OzonClient`:

- отключает автоматическое следование редиректам;
- возвращает понятную диагностику по `307/403/429`;
- держит осторожный rate limit `1 RPS`, `burst=2`;
- кэширует поиск на 15 минут и карточку на 12 часов.

## Доступные поля

Поиск `composer-api`:

- часто есть: `external_id`/`sku`/`id`, URL, название, цена, старая цена, рейтинг,
  отзывы, продавец, изображение;
- иногда есть: категория, бренд, badge, delivery, availability;
- структура может быть в `widgetStates`, `catalog.searchResultsV2` или nested JSON-строках.

Карточка `composer-api`:

- часто есть: title, цена, медиа, rating/reviews, seller;
- дополнительные layout-запросы могут отдавать description и characteristics;
- наличие и доставка сильно зависят от региона/IP/cookies, поэтому для MVP возвращаются
  как `unknown`, если нет надежного явного поля.

## Код PoC

Поиск:

```bash
python3 scripts/ozon_search.py "iphone 15 128gb" --limit 5 --enrich-details
```

Карточка по ID или URL:

```bash
python3 scripts/ozon_product.py 397529235
python3 scripts/ozon_product.py "https://www.ozon.ru/product/397529235/"
```

HTTP server:

```bash
python3 scripts/ozon_server.py --host 127.0.0.1 --port 8766
curl "http://127.0.0.1:8766/health"
curl "http://127.0.0.1:8766/search?query=iphone%2015&limit=5&enrich=1"
curl "http://127.0.0.1:8766/product?id=397529235"
```

Controlled probe:

```bash
python3 scripts/ozon_rate_probe.py --target search --levels 0.2,0.5,1 --seconds 5 --stop-on-block
python3 scripts/ozon_rate_probe.py --target product --levels 0.2,0.5,1 --seconds 5 --stop-on-block
```

## Ограничения

- Неофициальный endpoint может менять структуру без предупреждения.
- Ozon может блокировать не-browser и non-RU запросы. MVP-парсер не должен обходить
  captcha/challenge, использовать proxy pools, приватные cookie или аккаунты.
- Нормализация сделана best-effort: Ozon widget tree сильно менее стабилен, чем WB
  search/card JSON.
- Для production лучше искать официальный партнерский фид, договорной источник данных,
  Seller API для собственных товаров или отдельную лицензированную витрину.

## Источники

- Ozon Help: `docs.ozon.com/global/api/intro/` — Seller API требует API-ключ и Client ID,
  предназначен для загрузки товаров, цен, остатков, отчетов и заказов.
- Ozon Help: `docs.ozon.com/global/api/perfomance-api/` — Performance API относится к
  рекламному кабинету, требует отдельный ключ и использует новый host `api-performance.ozon.ru`.
- Apify Ozon Scraper README: `apify.com/ahaham_bytiz/ozon-scraper` — подтверждает,
  что storefront-парсеры используют `composer-api.bx`, а ответ приходит как widget tree.
- Public notes/gists по `composer-api.bx/page/json/v1/v2` — подтверждают layout-подход
  для seller/description/characteristics, но не являются официальной документацией.
