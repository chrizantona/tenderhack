# Yandex Market MVP data access research

Дата проверки: 2026-05-21.

## Краткий вывод

Для хакатонного MVP ближайший аналог Wildberries-подхода — storefront HTML
`market.yandex.ru/search?text=...` и `market.yandex.ru/product...`, из которого можно
best-effort доставать embedded JSON, JSON-LD, `data-zone-data` и ссылки на карточки.

Официальные API Яндекс Маркета существуют, но не заменяют публичный покупательский
поиск без ключей:

- Seller/Partner API работает с товарами конкретного кабинета/магазина и требует
  `Api-Key` или OAuth.
- Affiliate API умеет искать товары и получать варианты по `modelId`, но требует
  ключ партнерской сети.

С текущего окружения live-запрос к `market.yandex.ru/search?text=креатин моногидрат`
вернул `403` со страницей “похоже, вы используете VPN”. Поэтому runtime parser не
обходит challenge, а возвращает понятную диагностику. Для проверки нормализации
добавлен офлайн-путь через сохраненный HTML/JSON/HAR.

## Повторная live-проверка без VPN

Дата проверки: 2026-05-22.

Проверка запускалась live через `scripts/ym_search.py` вне sandbox-сетевых
ограничений, отдельно от старого результата 2026-05-21:

```bash
python3 scripts/ym_search.py "креатин моногидрат" --limit 3
python3 scripts/ym_search.py "iphone 15 128gb" --limit 3
python3 scripts/ym_search.py "наушники bluetooth" --limit 3
```

Результат для всех 3 запросов одинаковый:

- `items=[]`;
- `endpoints=[]`;
- diagnostics: `GET https://market.yandex.ru/search?... failed: 403 Yandex VPN/region block page`.

Controlled probe:

```bash
python3 scripts/ym_rate_probe.py --target search --levels 0.2,0.5,1 --seconds 5 --stop-on-block
```

Результат: уже первый уровень `0.2 RPS` вернул `1/1` ответ `403`, поэтому probe
остановлен по `--stop-on-block`; уровни `0.5` и `1 RPS` не запускались. Рабочий
RPS для requests-парсера с текущего IP не подтвержден. Текущий лимит в коде
`1 RPS, burst=2` нужно считать защитным limiter'ом для редких попыток, а не
production-рекомендацией. Для стабильного доступа нужен официальный API,
Affiliate API key либо HTML/HAR из браузера, который реально получает выдачу.

### Эксперимент с TLS impersonation

Дата проверки: 2026-05-22.

В `scripts/ym_search.py`, `scripts/ym_product.py`, `scripts/ym_server.py` и
`scripts/ym_rate_probe.py` добавлен optional transport `curl_cffi`:

```bash
python3 scripts/ym_search.py "креатин моногидрат" --limit 3 --transport curl_cffi
python3 scripts/ym_search.py "iphone 15 128gb" --limit 3 --transport curl_cffi --impersonate chrome124
python3 scripts/ym_search.py "наушники bluetooth" --limit 3 --transport curl_cffi --impersonate chrome120
python3 scripts/ym_rate_probe.py --target search --levels 0.2,0.5,1 --seconds 5 --transport curl_cffi --impersonate chrome124 --stop-on-block
```

Результат не изменился: все search-запросы получили `403 Yandex VPN/region block page`,
а probe остановился на первом уровне `0.2 RPS` с `status_counts={"403": 1}`.
Вывод: в текущем окружении одной подмены HTTP-заголовков и TLS fingerprint
недостаточно. Блок, вероятно, завязан на IP/region reputation, cookies/JS-session
или более широкий browser fingerprint.

### Эксперимент с согласованными fingerprint profiles

Дата проверки: 2026-05-22.

Добавлены воспроизводимые профили, где `curl_cffi` TLS impersonation согласован с
`User-Agent`, `sec-ch-ua`, platform и document navigation headers:

- `chrome120_windows`;
- `chrome124_macos`;
- `chrome124_windows`;
- `safari180_macos`.

Команда:

```bash
python3 scripts/ym_fingerprint_probe.py "креатин моногидрат" --limit 3 --delay 2
```

Результат: все 4 профиля вернули `items=0` и `403 Yandex VPN/region block page`
за 324-425 мс. Также добавлена возможность передать cookies через `--cookie` или
`--cookie-file` (`Playwright storage_state.json`, Netscape cookies.txt или raw
Cookie header), но без валидной браузерной сессии текущий fingerprint spoofing не
снимает блок.

## Таблица способов

| Способ | Что получает | Сложность | Стабильность | Риски | Рекомендация |
|---|---|---:|---:|---|---|
| Storefront HTML `market.yandex.ru/search?text=...` | Поисковая страница; иногда embedded JSON/JSON-LD со списком товаров | Средняя | Низкая-средняя | `403`, VPN/region block, captcha, смена frontend-схемы | Использовать для MVP best-effort |
| Storefront HTML карточки `/product--.../{modelId}` | Карточка товара; иногда JSON-LD и embedded state | Средняя | Низкая-средняя | Неполные поля, блокировки, разные URL-формы | Использовать для ручного URL/ID и enrichment top N |
| Browser HAR/HTML fallback | То, что уже получил настоящий браузер пользователя | Средняя | Средняя | Ручной шаг, зависит от Network export | Лучший способ проверить parser при блокировке |
| Partner/Seller API | Товары, карточки, цены, остатки собственного кабинета | Средняя | Высокая | Нужен кабинет, campaignId/businessId и ключ; не глобальный каталог | Production для seller-интеграций |
| Affiliate API | Поиск товаров и варианты по `modelId` | Средняя | Высокая | Нужен affiliate key | Production для публичного каталога, если есть доступ |

## Доступные поля

Storefront/embedded JSON может отдавать:

- идентификаторы: `modelId`, `skuId`, `offerId`, `wareMd5`, `feedId`, `shopId`;
- витринные поля: название, бренд/vendor, категория, URL, изображения;
- коммерческие поля: цена, старая цена, рейтинг, отзывы, магазин/продавец;
- характеристики: `specs`, `parameters`, `characteristics`, `properties`;
- наличие/доставка: только если frontend state явно содержит эти блоки.

## Код PoC

Live search:

```bash
python3 scripts/ym_search.py "креатин моногидрат" --limit 10 --enrich-details --enrich-limit 3
```

Карточка по ID или URL:

```bash
python3 scripts/ym_product.py 987654321
python3 scripts/ym_product.py "https://market.yandex.ru/product--kreatin-monogidrat/987654321"
```

Офлайн fallback из HTML/JSON/HAR:

```bash
python3 scripts/ym_from_json.py yandex-market-search.har --query "креатин моногидрат" --limit 10
python3 scripts/ym_fixture_smoke.py
```

HTTP server:

```bash
python3 scripts/ym_server.py --host 127.0.0.1 --port 8767
curl "http://127.0.0.1:8767/health"
curl "http://127.0.0.1:8767/search?query=креатин%20моногидрат&limit=10&enrich=1&enrich_limit=3"
curl "http://127.0.0.1:8767/product?id=987654321"
```

Controlled probe:

```bash
python3 scripts/ym_rate_probe.py --target search --levels 0.2,0.5,1 --seconds 5 --stop-on-block
```

## Ограничения

- Storefront HTML не является официальным API.
- Парсер не обходит captcha, VPN/region block, challenge и не использует приватные cookie.
- Если live HTML заблокирован, используйте `ym_from_json.py` с HAR/HTML из браузера.
- Для production лучше получить Affiliate API key или использовать Partner API для собственных товаров.

## Источники

- Yandex Market Partner API: `https://yandex.ru/dev/market/partner-api/doc/ru/`
- Метод просмотра товаров магазина: `https://yandex.ru/dev/market/partner-api/doc/ru/reference/assortment/getCampaignOffers`
- Метод просмотра карточек кабинета: `https://yandex.com/dev/market/partner-api/doc/ru/reference/business-assortment/getOfferMappings`
- Affiliate API variants по `modelId`: `https://yandex.ru/dev/market/affiliate/ru/reference/get-models-id-offers`
