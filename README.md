# EasyTender Product Search

Проект собирает товарные предложения из нескольких источников, приводит их к единому формату, сравнивает между собой и поднимает наверх наиболее релевантные карточки.

Поддерживаемые источники:

- Wildberries
- Ozon
- Яндекс Маркет
- открытые магазины Рунета

Основной сценарий: пользователь вводит товарный запрос, система параллельно опрашивает источники, нормализует найденные карточки, группирует похожие предложения и показывает лучшие варианты с учётом цены, наличия, рейтинга, продавца и совпадения с исходным запросом.

## Быстрый Запуск

```bash
python3 -m pip install -r requirements.txt
python3 app/server.py --host 127.0.0.1 --port 8770
```

После запуска интерфейс доступен по адресу:

```text
http://127.0.0.1:8770
```

Docker-запуск:

```bash
docker compose up --build
```

## Локальная Модель Для Проверки Карточек

Дополнительный слой обработки карточек вынесен в `qwen_product_ranker.py`.

Он принимает исходный запрос и список уже найденных карточек, после чего:

- проверяет, что карточка относится к тому же товару;
- отсекает аксессуары и нерелевантные позиции;
- группирует товары по категориям;
- считает ценовой контекст внутри каждой группы;
- помечает подозрительно дешёвые и дорогие предложения;
- сортирует карточки и отдаёт лучшие варианты.

Путь до локальной модели уже указан в коде:

```python
QWEN_MODEL_PATH = Path("/Users/chrizantona/qwen/Qwen2.5-Coder-14B-Instruct-4bit")
```

Запуск обработки карточек:

```bash
python3 qwen_product_ranker.py \
  --query "iphone 15 128gb" \
  --cards examples/cards.json
```

Для проверки без запуска локальной модели:

```bash
python3 qwen_product_ranker.py \
  --query "iphone 15 128gb" \
  --cards examples/cards.json \
  --no-qwen
```

## Поток Обработки

1. Запрос пользователя поступает в агрегатор.
2. Агрегатор запускает нужные источники параллельно.
3. Каждый источник возвращает карточки товаров.
4. Карточки приводятся к единому внутреннему формату.
5. `product_ranker` проверяет карточки на соответствие запросу.
6. Валидные карточки группируются по категории.
7. Внутри каждой группы считается медианная цена и диапазон нормальных значений.
8. Ценовые выбросы помечаются и понижаются в выдаче.
9. Итоговый скоринг выбирает лучшие предложения.

## Wildberries

Основной код:

- `scripts/wb_parser.py`
- `scripts/wb_search.py`
- `scripts/wb_product.py`
- `scripts/wb_server.py`

Wildberries используется через публичные JSON endpoints поиска и карточек. Клиент работает с browser-like headers, прогревом сессии, rate limit и TTL-cache. Для карточек дополнительно тянутся данные из `wbbasket`: характеристики, фото, описание, бренд, категория и продавец.

CLI:

```bash
python3 scripts/wb_search.py "шорты мужские adidas" --limit 10 --enrich-details --enrich-limit 10
python3 scripts/wb_product.py 273639353
```

## Ozon

Основной код:

- `scripts/ozon_camoufox.py`
- `scripts/ozon_parser.py`
- `scripts/ozon_search.py`
- `scripts/ozon_product.py`
- `scripts/ozon_server.py`
- `scripts/ozon_selenium.py`

Для Ozon основной runtime-движок использует браузерный рендеринг через Camoufox. Страница поиска или товара открывается как обычная storefront-страница, после чего данные извлекаются из DOM: название, цена, SKU, изображения, доставка, рейтинг и характеристики.

Selenium-клиент оставлен как резервный вариант для окружений, где Camoufox недоступен.

CLI:

```bash
python3 scripts/ozon_search.py "iphone 15 128gb" --limit 10 --enrich-details --enrich-limit 5
python3 scripts/ozon_product.py 397529235
```

## Яндекс Маркет

Основной код:

- `scripts/ym_parser.py`
- `scripts/ym_common.py`
- `scripts/ym_search.py`
- `scripts/ym_product.py`
- `scripts/ym_server.py`

Яндекс Маркет обрабатывается через storefront HTML и embedded payloads. Клиент поддерживает обычный HTTP-транспорт и `curl_cffi`-транспорт с согласованными browser-like профилями.

CLI:

```bash
python3 scripts/ym_search.py "креатин моногидрат" --limit 10 --enrich-details --enrich-limit 3
python3 scripts/ym_product.py 987654321
```

## Открытый Рунет

Основной код:

- `scripts/other_runet/`
- `scripts/runet_search.py`
- `scripts/runet_parser.py`

Источник добирает предложения из профильных магазинов и открытых страниц. Приоритет отдаётся структурированным данным: JSON-LD Product, Offer, OpenGraph и HTML-характеристикам. Ошибки отдельных доменов не останавливают весь поиск.

CLI:

```bash
python3 scripts/runet_search.py "принтер hp laserjet" --limit 5 --no-discovery
python3 scripts/runet_search.py "шины 205/55 r16" --limit 5
```

## API

Основной сервер:

```bash
python3 app/server.py --host 127.0.0.1 --port 8770
```

Ключевые endpoints:

- `GET /api/health`
- `GET /api/search`
- `GET /api/search/stream`
- `GET /api/product`
- `GET /api/proxy`

Потоковый поиск используется интерфейсом, чтобы карточки появлялись по мере готовности источников и enrichment-этапов.

## Структура

```text
app/                  backend server, aggregator, clustering
frontend/             browser UI
scripts/              marketplace parsers and CLI tools
scripts/other_runet/  open web source, ranking, local model helpers
qwen_product_ranker.py post-processing: validation, grouping, outliers, best offers
product_ranker/       same logic split into package modules
docs/                 technical notes and source research
tests/                tests
examples/             sample card payloads
```

## Проверки

```bash
python3 -m py_compile product_ranker/*.py
python3 -m py_compile qwen_product_ranker.py
python3 qwen_product_ranker.py --query "iphone 15 128gb" --cards examples/cards.json --no-qwen
pytest
```

## Переменные Окружения

```bash
QWEN_MODEL_PATH=/Users/chrizantona/qwen/Qwen2.5-Coder-14B-Instruct-4bit
OZON_PROXIES=host:port,host:port
OZON_PROXY_DISABLE=1
OZON_ENRICH_MAX=10
YM_PROXY=http://host:port
YM_PROXY_DISABLE=1
```

## Примечания

Система работает с публично доступными storefront-данными и уже полученными карточками товаров. Если источник возвращает ограничение доступа, rate limit или challenge-страницу, это фиксируется в diagnostics и не маскируется как успешный результат.
