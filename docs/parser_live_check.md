# Parser live check

Дата проверки: 2026-05-21.

Команды:

```bash
python scripts/wb_search.py "креатин моногидрат" --limit 5 --enrich-details --enrich-limit 2 --output out/wb.json
python scripts/ozon_search.py "креатин моногидрат" --limit 5 --enrich-details --enrich-limit 2 --output out/ozon.json
python scripts/ym_search.py "креатин моногидрат" --limit 5 --enrich-details --enrich-limit 2 --output out/ym.json
```

## Результат из `out`

| Файл | Marketplace | Items | Что произошло |
|---|---|---:|---|
| `out/wb.json` | `wildberries` | 5 | Работает нормально. WB search endpoint вернул товары по `креатин моногидрат`. |
| `out/ozon.json` | `ozon` | 0 | Ozon storefront endpoint заблокировал requests-клиент: `307` на `www.ozon.ru` и `403 challenge` на `api.ozon.ru`. |
| `out/ym.json` | `yandex_market` | 0 | Яндекс Маркет заблокировал live HTML-запрос: `403 Yandex VPN/region block page`. |

## Повторная no-VPN проверка Яндекс Маркета

Дата проверки: 2026-05-22.

Отдельно от результата 2026-05-21 live `scripts/ym_search.py` был повторно
запущен вне sandbox-сетевых ограничений на 3 запросах:

```bash
python3 scripts/ym_search.py "креатин моногидрат" --limit 3
python3 scripts/ym_search.py "iphone 15 128gb" --limit 3
python3 scripts/ym_search.py "наушники bluetooth" --limit 3
```

Все 3 запуска вернули `items=[]`, `endpoints=[]` и diagnostics
`403 Yandex VPN/region block page`.

Короткий controlled probe:

```bash
python3 scripts/ym_rate_probe.py --target search --levels 0.2,0.5,1 --seconds 5 --stop-on-block
```

Результат: на уровне `0.2 RPS` выполнен `1` запрос, `status_counts={"403": 1}`.
Probe остановлен сразу после block-ответа, поэтому рабочий RPS не подтвержден.
Для requests-парсера с текущего IP безопасный production-лимит не заявляем;
текущий `1 RPS, burst=2` в `ym_parser.py` остается только защитным ограничением
для редких попыток и backoff при `403/429`.

### Эксперимент с curl_cffi transport

Дата проверки: 2026-05-22.

Добавлен experimental transport `curl_cffi`, чтобы проверить browser-like TLS
impersonation без перехода на полноценный браузер:

```bash
python3 scripts/ym_search.py "креатин моногидрат" --limit 3 --transport curl_cffi
python3 scripts/ym_search.py "iphone 15 128gb" --limit 3 --transport curl_cffi --impersonate chrome124
python3 scripts/ym_search.py "наушники bluetooth" --limit 3 --transport curl_cffi --impersonate chrome120
```

Итог: все 3 запуска снова вернули `items=[]`, `endpoints=[]` и diagnostics
`403 Yandex VPN/region block page`. Probe через `curl_cffi/chrome124` на `0.2 RPS`
также получил `status_counts={"403": 1}`. Значит проблема не снимается простой
заменой headers/TLS transport; следующий практичный вариант для live HTML -
браузерный transport или официальный/Affiliate API.

### Эксперимент с согласованными fingerprint profiles

Дата проверки: 2026-05-22.

Добавлены profiles `chrome120_windows`, `chrome124_macos`, `chrome124_windows`,
`safari180_macos` и быстрый перебор:

```bash
python3 scripts/ym_fingerprint_probe.py "креатин моногидрат" --limit 3 --delay 2
```

Итог: все 4 profiles получили `403 Yandex VPN/region block page`; `items=0`.
Это подтверждает, что одного согласования TLS/UA/`sec-ch-ua` недостаточно. В коде
также добавлены `--cookie` и `--cookie-file`, чтобы отдельно проверить гипотезу
валидной browser session без логирования значений cookies.

## Почему нормально работает только WB

WB отдает публичный JSON endpoint поиска:

```text
https://search.wb.ru/exactmatch/ru/common/v4/search
```

В `out/wb.json` есть нормальный результат:

- `items=5`;
- `total=3683`;
- первый товар: `external_id=4219394`;
- title: `Креатин моногидрат Creatine Monohydrate, без вкуса 200 гр.`;
- price: `568 RUB`;
- diagnostics пустые.

То есть WB parser получает реальные товарные JSON-данные и нормально приводит их к `basic_scheme.json`.

## Что не так с Ozon

Ozon parser дошел до endpoint'ов, но не получил товарный JSON:

```text
https://www.ozon.ru/api/composer-api.bx/page/json/v2 -> 307 redirect with __rr=1
https://api.ozon.ru/composer-api.bx/page/json/v2 -> 403 challenge
https://www.ozon.ru/api/composer-api.bx/page/json/v1 -> 403 challenge page
https://api.ozon.ru/composer-api.bx/page/json/v1 -> 403 challenge
```

Это не ошибка нормализации. Проблема в доступе к storefront API: Ozon защищает `composer-api.bx` от non-browser requests/challenge. Парсер специально не обходит captcha/challenge, поэтому возвращает:

- `items=[]`;
- `endpoints=[]`;
- diagnostics с `307/403`.

Рабочий способ проверить Ozon-нормализацию при блокировке:

```bash
python scripts/ozon_from_json.py ozon-search.har --query "креатин моногидрат" --limit 10
```

## Что не так с Яндекс Маркетом

Сначала parser принял SEO title страницы поиска за товар:

```text
Креатин моногидрат — купить по низкой цене на Яндекс Маркете
```

Это был ложный кандидат без `external_id`, `url` и `price`. Исправлено: `ym_parser.py`
теперь отбрасывает кандидатов без ID/URL и без цены.

После фикса реальная проблема стала видна корректно:

```text
GET https://market.yandex.ru/search?... failed: 403 Yandex VPN/region block page
```

То есть Яндекс Маркет не отдает live HTML текущему IP/региону. Парсер теперь не падает и не возвращает ложный товар, а честно пишет diagnostics.

Рабочий способ проверить YM-нормализацию при блокировке:

```bash
python scripts/ym_from_json.py yandex-market-search.har --query "креатин моногидрат" --limit 10
```

## Что зафиксировано в коде

- `ym_parser.py`: добавлена фильтрация невалидных кандидатов, чтобы SEO/meta-блоки не попадали в `items`.
- `out/ym.json`: обновлен после фикса; теперь `items=0` и есть корректный diagnostics по `403`.

## Вывод

Текущая причина, почему “как WB” работает только WB: WB имеет доступный публичный JSON endpoint, а Ozon и Яндекс Маркет на live requests возвращают защитные страницы/challenge. Для production-нормального доступа нужны:

- Ozon: официальный/партнерский источник данных или браузерный HAR/JSON как fallback для MVP.
- Яндекс Маркет: Affiliate API/Partner API с ключом либо HTML/HAR из браузера без VPN/region block.
