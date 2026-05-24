# EasyTender — запуск с нуля

Агрегатор поиска по 4 источникам: **Wildberries, Ozon, Яндекс Маркет, прочие источники Рунета**. Кластеризация результатов, сравнение фильтров, runtime-переключение прокси, табличный/групповой/сеточный вид, lazy-load характеристик в модалке.

## TL;DR

```bash
git clone https://github.com/rougenn/easytenderhack2026
cd easytenderhack2026

# Docker (проще):
docker compose up --build
# → http://localhost:8770

# ИЛИ локально (Mac):
bash scripts/setup_local.sh    # ставит всё что нужно
python3 app/server.py
```

---

## Системные требования

- **macOS** (Apple Silicon для Qwen LLM) или **Linux**
- Python 3.12+
- **≥ 8 GB RAM** (16 GB рекомендуется при limit=10 и 8 enrich-воркерах — пик ~6-8 GB)
- Свободные порты: `8770` (UI), `18000–18009` (proxy bridges, если нужны)

---

## Вариант A. Docker

```bash
git clone https://github.com/rougenn/easytenderhack2026
cd easytenderhack2026
docker compose up --build
```

Контейнер сам:
- ставит Python 3.12, Node 20 (заменяет багнутый v24 в Playwright),
- ставит Camoufox + Firefox engine,
- ставит gost для прокси-мостов,
- стартует сервер на `0.0.0.0:8770`.

Открой `http://localhost:8770`.

С прокси:
```bash
OZON_PROXIES="ip1:8000,ip2:8000" \
OZON_PROXY_USER=user OZON_PROXY_PASS=pass \
docker compose up --build
```

⚠️ Qwen LLM (для `other_runet` источника) **не работает в Linux Docker** — `mlx-lm` есть только под Apple Silicon. Other_runet работает без LLM-помощника (просто без AI-извлечения структурки).

---

## Вариант B. Локально (macOS / Apple Silicon)

### B.1 Системные deps (один раз)

```bash
# Homebrew (если нет)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Python 3.12+
python3 --version    # ожидаем 3.12 или выше

# Node 20 LTS (Playwright bundles v24 с крэш-багом → заменим)
brew install node@20
brew unlink node 2>/dev/null
brew link --overwrite --force node@20
node --version       # должно быть v20.x

# gost — HTTP↔SOCKS5 мосты для прокси
brew install gost
```

### B.2 Python deps

```bash
cd easytenderhack2026
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### B.3 Browser engines

```bash
python3 -m playwright install firefox
python3 -m camoufox fetch
```

### B.4 КРИТИЧНО: подмена Node в Playwright

Playwright тащит **собственный Node v24** в `.venv`, у которого баг крэшит Camoufox с `TypeError: Cannot read properties of undefined (reading 'url')`. Подменяем на Node 20:

```bash
PW_NODE=$(python3 -c "import os,playwright; print(os.path.join(os.path.dirname(playwright.__file__),'driver','node'))")
cp "$(brew --prefix node@20)/bin/node" "$PW_NODE"
"$PW_NODE" --version    # должно быть v20.x
```

### B.5 Запуск

```bash
python3 app/server.py
```

На старте видим:
```
Ozon proxy bridges: 10 local HTTP ports → SOCKS5 upstreams
EasyTender aggregator listening on http://127.0.0.1:8770
```

Открой `http://localhost:8770`.

### B.6 Опционально: Qwen LLM для `other_runet` (только Apple Silicon)

`other_runet` парсит товары из открытых источников. Без LLM-помощника просто отрабатывает без AI-извлечения. Чтобы включить:

```bash
pip install mlx mlx-lm
# Модель лежит тут:
# ~/models/Qwen2.5-Coder-14B-Instruct-4bit
```

---

## Вариант C. Локально (Linux)

То же что Mac, но без `brew`. Поставь руками:
- `node@20` через nvm или systemwide ([nodejs.org](https://nodejs.org))
- `gost` через GitHub releases ([go-gost/gost](https://github.com/go-gost/gost/releases))
- Системные либы для headless Firefox (см. список в [Dockerfile](Dockerfile))

Подмена Node в Playwright делается так же как в B.4. Qwen недоступен.

---

## Env vars

| Переменная | Что делает | Default |
|---|---|---|
| `OZON_PROXIES` | CSV `host:port,host:port,...` для пула прокси | proxy6.net pool (захардкожен) |
| `OZON_PROXY_USER` | basic-auth login для прокси | `dNXQ6p` |
| `OZON_PROXY_PASS` | basic-auth pass | `39TqAh` |
| `OZON_PROXY_SCHEME` | `http` или `socks5` | `http` |
| `OZON_PROXY_DISABLE=1` | Полностью выключить proxy для Ozon | (off) |
| `OZON_ENRICH_MAX` | Сколько товаров Ozon обогащать через Camoufox при поиске | `10` |
| `YM_PROXY` | Один прокси для Яндекс Маркета | first of `OZON_PROXIES` |
| `YM_PROXY_DISABLE=1` | YM без прокси | (off) |
| `QWEN_MODEL_PATH` | Путь к Qwen модели (для other_runet) | `~/models/Qwen2.5-Coder-14B-Instruct-4bit` |
| `QWEN_SERVER_LOG` | Куда писать лог Qwen-сервера | `qwen_server.log` |

См. `.env.example` для шаблона.

---

## Что в UI

- **Чекбоксы источников** (YM / WB / Ozon / Прочие источники Рунета) — можно отключать
- **Прокси: вкл/выкл** в шапке — пересоздаёт WB/YM клиентов и Camoufox с/без proxy. Если у тебя свой VPN на RU IP — direct (выкл) обычно быстрее
- **Поиск** → стрим карточек по мере обогащения. Ozon ждёт характеристики, появляется уже с ними (warmup ~13с, потом ~10с/товар × 8 параллельно)
- **Фильтры** — внизу панель "Сравнение фильтров" с матчингом между источниками и кнопкой "Применить" (рестарт поиска с выбранными значениями)
- **Кластеры** — кнопка "Группы" в тулбаре. Группирует похожие SKU (iPhone 16 vs iPhone 16 Pro, размеры шин 205/55 R16 vs 215/55 R16, и т.д.)
- **Таблица** — динамические колонки атрибутов (топ-4 по покрытию)
- **Модалка** — клик на карточку. Для Ozon и WB-fallback характеристики догружаются по клику (lazy enrich)

---

## Troubleshooting

| Симптом | Причина / фикс |
|---|---|
| `TypeError: Cannot read properties of undefined (reading 'url')` в логе → Camoufox крэшится | Не сделан Node-swap. Повтори шаг B.4 |
| Ozon: `"Доступ ограничен"` или `ozon_timeout: Camoufox завис >70с` | Твой IP/proxy в ban-листе Ozon. Подожди 15-30мин, или нажми "Прокси: выкл" в UI |
| WB: 0 товаров / 0 фильтров, `wb_rate_limited` | WB rate-limit (429). Подожди ~5 мин. При выкл прокси и VPN на RU обычно проходит |
| WB: картинки не грузятся | Транзиент. Перезагрузи страницу. Если стабильно — проверь что `basket-XX.wbbasket.ru` отвечает (CDN-edge может быть на бане) |
| `ozon_cooldown: Camoufox перегрелся, ждём ~Xс` | Circuit breaker. Не кликай быстро много карточек подряд — каждый клик это 1 Camoufox fetch через single thread. Подожди или открой реже |
| Порт 8770 занят | `lsof -i :8770` → kill. Или `python3 app/server.py --port 8771` |
| `mlx not found` / Qwen-сервер не стартует | Optional. Either install `pip install mlx mlx-lm` (Apple Silicon only) или игнорируй — runet работает без LLM |
| Server ест 8GB RAM в пике | Это нормально при 8 enrich-воркерах × Camoufox. Снизь через `OZON_ENRICH_MAX=4` |
| После клика на модалку Ozon-товара характеристики не появляются | Открой DevTools→Console, должно быть `[enrich] fetching` → `[enrich] applying`. Если нет — hard-reload (Cmd+Shift+R) |

---

## Архитектура (вкратце)

```
frontend/  (vanilla JS SPA)
├─ Поиск через NDJSON-стрим (/api/search/stream)
├─ Лениво обогащает Ozon/WB модалки через /api/product

app/
├─ server.py        Flask: HTTP routes
├─ aggregator.py    Оркестрация 4 источников, кластеризация, proxy toggle, circuit breaker
├─ clustering.py    Hybrid (brand+model)+TF-IDF+price-split, специфика по domain (шины, батарейки)
└─ filters_service.py  /api/filters: сбор+матчинг фильтров

scripts/
├─ wb_parser.py + wb_serp_html.py  WB: curl_cffi+proxy rotation, Camoufox fallback
├─ ozon_*.py        Ozon: Camoufox primary, curl_cffi/Selenium fallback
├─ ym_parser.py     YM: HTML+JSON
├─ other_runet/     4-й источник: общий рунет-парсер с опциональным Qwen LLM
└─ filters_*.py     По одному модулю на источник + общий матчер
```

Что критично понять:
- **Camoufox** (headless Firefox + stealth) — тяжёлый, но нужен для Ozon и WB-fallback. Pinned на dedicated thread из-за Playwright thread-affinity
- **curl_cffi** имитирует TLS-отпечаток Chrome — без него WB банит за 2-3 запроса
- **Proxy bridges** через `gost` превращают SOCKS5-only прокси в HTTP — нужно для совместимости с Camoufox
- **Circuit breaker** на Ozon: после 70-сек таймаута блокирует Ozon на 90с с авто-restart браузера

---

## Контакты

Issues / PRs на [github.com/rougenn/easytenderhack2026](https://github.com/rougenn/easytenderhack2026).
