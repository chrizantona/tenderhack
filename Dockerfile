FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    NODE_VERSION=20.20.2

WORKDIR /app

# ---- System deps -----------------------------------------------------------
# camoufox (headless Firefox) + playwright need a slew of X/GTK libs.
# gost handles HTTP→SOCKS5 bridging for Ozon/YM proxies.
# Node 20 replaces Playwright's bundled v24 which has a crash bug on
# `pageError.location.url` undefined (see scripts/ozon_camoufox.py).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        wget \
        xz-utils \
        fonts-liberation \
        fonts-noto-core \
        fonts-noto-cjk \
        fonts-noto-color-emoji \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcairo2 \
        libcups2 \
        libdbus-1-3 \
        libdbus-glib-1-2 \
        libdrm2 \
        libgbm1 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libx11-xcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        libxss1 \
        libxt6 \
        libxtst6 \
        xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# ---- Node 20 ---------------------------------------------------------------
RUN ARCH=$(dpkg --print-architecture | sed 's/amd64/x64/; s/arm64/arm64/') \
    && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${ARCH}.tar.xz" -o /tmp/node.tar.xz \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
    && rm /tmp/node.tar.xz \
    && node --version

# ---- gost (HTTP↔SOCKS5 proxy bridge) --------------------------------------
RUN ARCH=$(dpkg --print-architecture | sed 's/amd64/amd64/; s/arm64/arm64/') \
    && curl -fsSL "https://github.com/go-gost/gost/releases/download/v3.0.0/gost_3.0.0_linux_${ARCH}.tar.gz" -o /tmp/gost.tgz \
    && tar -xzf /tmp/gost.tgz -C /usr/local/bin gost \
    && chmod +x /usr/local/bin/gost \
    && rm /tmp/gost.tgz \
    && gost -V || true

# ---- Python deps + browser engines ----------------------------------------
COPY requirements.txt .
RUN pip install -r requirements.txt

# Replace Playwright's bundled Node v24 with system Node 20 (crash fix).
# Python locates the bundled driver/node path; we just copy over it.
RUN python -c "import playwright, os; print(os.path.dirname(playwright.__file__))" > /tmp/pw_root \
    && cp /usr/local/bin/node "$(cat /tmp/pw_root)/driver/node" \
    && "$(cat /tmp/pw_root)/driver/node" --version \
    && rm /tmp/pw_root

# Install Firefox engine for both raw playwright and camoufox.
RUN python -m playwright install firefox
RUN python -m camoufox fetch

COPY . .

# 4th source "other_runet" uses mlx_lm/Qwen for AI assist. mlx is Apple-Silicon
# only — in Linux containers it can't be installed. The aggregator gracefully
# degrades: other_runet runs WITHOUT the Qwen helper. To enable Qwen locally,
# run outside Docker on Mac M1+: `pip install mlx-lm && download a Qwen model`.

EXPOSE 8770

# --no-ozon is safer in containers without OZON_PROXIES (Ozon needs RU IP);
# override via command if you have proxies. Server starts with WB + YM + other_runet.
CMD ["python3", "app/server.py", "--host", "0.0.0.0", "--port", "8770"]
