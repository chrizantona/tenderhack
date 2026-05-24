#!/usr/bin/env bash
# Локальный HTTP→SOCKS5 мост через gost.
#
# Зачем: proxy6.net отдаёт SOCKS5 с username/password, а Firefox/Playwright
# (Camoufox) не умеет SOCKS5-auth (Mozilla bug 122752). Поднимаем по одному
# gost на каждый upstream — каждый слушает HTTP на 127.0.0.1:1800N — и
# передаём Camoufox эти локальные HTTP-прокси через переменную OZON_PROXIES.
#
# Usage:
#   bash scripts/proxy_bridge.sh start    # запустить мосты
#   bash scripts/proxy_bridge.sh stop     # остановить
#   bash scripts/proxy_bridge.sh env      # вывести строку OZON_PROXIES=...
#   eval "$(bash scripts/proxy_bridge.sh env)"  # экспортнуть в shell

set -euo pipefail

OZON_PROXY_USER="${OZON_PROXY_USER:-dNXQ6p}"
OZON_PROXY_PASS="${OZON_PROXY_PASS:-39TqAh}"
UPSTREAMS=(
  "195.208.170.23:8000"
  "195.208.170.209:8000"
  "195.208.171.192:8000"
  "195.208.170.156:8000"
  "195.208.170.151:8000"
  "195.208.170.252:8000"
  "195.208.170.20:8000"
  "195.208.170.194:8000"
  "195.208.170.31:8000"
  "185.126.87.151:8000"
)
BASE_PORT=18000
PID_DIR="/tmp/easytender-proxy-bridge"
mkdir -p "$PID_DIR"

cmd="${1:-}"

start() {
  command -v gost >/dev/null 2>&1 || { echo "gost not found. Install: brew install gost" >&2; exit 1; }
  for idx in "${!UPSTREAMS[@]}"; do
    upstream="${UPSTREAMS[$idx]}"
    port=$((BASE_PORT + idx))
    pidfile="$PID_DIR/bridge-$port.pid"
    if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
      continue
    fi
    nohup gost \
      -L="http://127.0.0.1:$port" \
      -F="socks5://${OZON_PROXY_USER}:${OZON_PROXY_PASS}@${upstream}" \
      > "$PID_DIR/bridge-$port.log" 2>&1 &
    echo $! > "$pidfile"
  done
  sleep 1
  echo "started ${#UPSTREAMS[@]} bridges on ports $BASE_PORT-$((BASE_PORT + ${#UPSTREAMS[@]} - 1))" >&2
}

stop() {
  for f in "$PID_DIR"/*.pid; do
    [ -f "$f" ] || continue
    pid=$(cat "$f")
    kill "$pid" 2>/dev/null || true
    rm -f "$f"
  done
  echo "stopped" >&2
}

env_line() {
  proxies=()
  for idx in "${!UPSTREAMS[@]}"; do
    proxies+=("http://127.0.0.1:$((BASE_PORT + idx))")
  done
  joined=$(IFS=,; echo "${proxies[*]}")
  echo "export OZON_PROXIES='$joined'"
  echo "export OZON_PROXY_SCHEME='http'"
  echo "unset OZON_PROXY_USER OZON_PROXY_PASS  # auth уже зашит в мост"
}

case "$cmd" in
  start) start ;;
  stop) stop ;;
  env) env_line ;;
  *) echo "usage: $0 {start|stop|env}" >&2; exit 2 ;;
esac
