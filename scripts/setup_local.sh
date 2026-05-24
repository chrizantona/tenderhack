#!/usr/bin/env bash
# One-shot local setup for EasyTender (macOS / Apple Silicon).
# Re-runnable: each step checks current state and skips if already done.
#
# Usage:
#   bash scripts/setup_local.sh
# Then:
#   source .venv/bin/activate
#   python3 app/server.py

set -e

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

step() { echo ""; echo "▶ $1"; }
ok()   { echo "  ✓ $1"; }
fail() { echo "  ✗ $1"; exit 1; }

# 1. Homebrew
step "1/7 Checking Homebrew"
if ! command -v brew >/dev/null 2>&1; then
    fail "brew not found. Install: /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
fi
ok "brew $(brew --version | head -1)"

# 2. Python 3.12+
step "2/7 Checking Python"
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3,12) else 0)')
if [ "$PY_OK" != "1" ]; then
    fail "Need Python 3.12+, got $PY_VER. Install via brew: brew install python@3.12"
fi
ok "python $PY_VER"

# 3. Node 20 (replaces Playwright bundled v24 to fix Camoufox crash)
step "3/7 Installing Node 20 LTS"
if ! brew list --formula 2>/dev/null | grep -q "^node@20$"; then
    brew install node@20
fi
# Link node@20 if not linked
if [ "$(node --version 2>/dev/null | head -c 3)" != "v20" ]; then
    brew unlink node 2>/dev/null || true
    brew link --overwrite --force node@20
fi
NODE_VER=$(node --version)
ok "node $NODE_VER (must be v20.x)"

# 4. gost (proxy bridge)
step "4/7 Installing gost"
if ! command -v gost >/dev/null 2>&1; then
    brew install gost
fi
ok "gost $(gost -V 2>&1 | head -1)"

# 5. venv + Python deps
step "5/7 Creating .venv and installing Python deps"
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    ok "created .venv"
fi
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
ok "pip install done"

# 6. Browser engines
step "6/7 Installing Firefox + Camoufox engines"
python3 -m playwright install firefox --with-deps 2>/dev/null || python3 -m playwright install firefox
ok "playwright firefox ready"
python3 -m camoufox fetch
ok "camoufox ready"

# 7. Replace Playwright's bundled Node v24 → Node 20 (CRITICAL — fixes crash)
step "7/7 Patching Playwright bundled Node v24 → v20"
PW_NODE=$(python3 -c "import os,playwright; print(os.path.join(os.path.dirname(playwright.__file__),'driver','node'))")
NODE_20_BIN="$(brew --prefix node@20)/bin/node"
if [ ! -f "$PW_NODE" ]; then
    fail "Playwright driver/node not found at $PW_NODE"
fi
BUNDLED_VER=$("$PW_NODE" --version 2>/dev/null || echo "?")
if [ "${BUNDLED_VER:0:3}" = "v20" ]; then
    ok "already patched ($BUNDLED_VER)"
else
    cp "$NODE_20_BIN" "$PW_NODE"
    NEW_VER=$("$PW_NODE" --version)
    ok "patched: was $BUNDLED_VER → now $NEW_VER"
fi

# Done
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  Setup complete. To start the server:                ║"
echo "║                                                      ║"
echo "║    source .venv/bin/activate                         ║"
echo "║    python3 app/server.py                             ║"
echo "║                                                      ║"
echo "║  Then open http://localhost:8770                     ║"
echo "╚══════════════════════════════════════════════════════╝"
