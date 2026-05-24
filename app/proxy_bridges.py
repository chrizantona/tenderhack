"""Local HTTP→SOCKS5 bridges via `gost`.

Why: proxy6.net upstream is SOCKS5 with username/password. Firefox/Playwright
(which Camoufox wraps) doesn't support SOCKS5 auth — Mozilla bug 122752 has
been open since 2002. We spawn one gost process per upstream listening on
127.0.0.1, then expose those local plain-HTTP ports to Camoufox.

Each gost is invoked as:
    gost -L=http://127.0.0.1:PORT -F=socks5://USER:PASS@HOST:PORT

The bridges are started lazily on server startup (see :mod:`app.server`) and
torn down via `atexit`. Existing bridges on the same ports are reused without
spawning duplicates.
"""
from __future__ import annotations

import atexit
import logging
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Iterable


log = logging.getLogger("proxy_bridges")


DEFAULT_UPSTREAMS: tuple[str, ...] = (
    "195.208.170.23:8000",
    "195.208.170.209:8000",
    "195.208.171.192:8000",
    "195.208.170.156:8000",
    "195.208.170.151:8000",
    "195.208.170.252:8000",
    "195.208.170.20:8000",
    "195.208.170.194:8000",
    "195.208.170.31:8000",
    "185.126.87.151:8000",
)
DEFAULT_USER = "dNXQ6p"
DEFAULT_PASS = "39TqAh"
DEFAULT_BASE_PORT = 18000

LOG_DIR = Path("/tmp/easytender-proxy-bridge")


class GostMissingError(RuntimeError):
    pass


def gost_available() -> bool:
    return shutil.which("gost") is not None


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False


def _verify_bridge(port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_open(port):
            return True
        time.sleep(0.2)
    return False


class ProxyBridges:
    """Lifecycle manager for a pool of local HTTP→SOCKS5 bridges.

    Idempotent: calling :meth:`start` repeatedly does not respawn bridges
    whose port is already bound. ``stop()`` only kills processes we own —
    bridges started by `scripts/proxy_bridge.sh` are left alone.
    """

    def __init__(
        self,
        upstreams: Iterable[str] = DEFAULT_UPSTREAMS,
        *,
        user: str = DEFAULT_USER,
        password: str = DEFAULT_PASS,
        base_port: int = DEFAULT_BASE_PORT,
    ) -> None:
        self._upstreams = tuple(upstreams)
        self._user = user
        self._password = password
        self._base_port = base_port
        self._processes: list[subprocess.Popen] = []
        self._urls: list[str] = []
        self._started = False

    @property
    def urls(self) -> list[str]:
        return list(self._urls)

    def env_value(self) -> str:
        return ",".join(self._urls)

    def start(self) -> list[str]:
        if self._started:
            return list(self._urls)
        if not gost_available():
            raise GostMissingError(
                "`gost` binary not found in PATH. Install via `brew install gost` "
                "(or set OZON_PROXY_DISABLE=1 to skip the proxy pool)."
            )
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        urls: list[str] = []
        for idx, upstream in enumerate(self._upstreams):
            port = self._base_port + idx
            url = f"http://127.0.0.1:{port}"
            urls.append(url)
            if _port_open(port):
                # Someone else already bridges this port. Trust it.
                log.info("bridge port=%d already bound, reusing", port)
                continue
            forward = f"socks5://{self._user}:{self._password}@{upstream}"
            listen = f"http://127.0.0.1:{port}"
            log_path = LOG_DIR / f"bridge-{port}.log"
            try:
                proc = subprocess.Popen(
                    ["gost", f"-L={listen}", f"-F={forward}"],
                    stdout=log_path.open("ab"),
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise GostMissingError(str(exc)) from exc
            self._processes.append(proc)
            log.info("bridge port=%d → %s spawned pid=%d", port, upstream, proc.pid)
        self._urls = urls
        # Verify at least one bridge accepts connections.
        live = sum(1 for url in urls if _port_open(int(url.rsplit(":", 1)[1])))
        if live == 0:
            # Give gost a chance to bind.
            for url in urls:
                _verify_bridge(int(url.rsplit(":", 1)[1]), timeout=4)
            live = sum(1 for url in urls if _port_open(int(url.rsplit(":", 1)[1])))
        log.info("started %d/%d bridges", live, len(urls))
        self._started = True
        atexit.register(self.stop)
        return list(self._urls)

    def stop(self) -> None:
        if not self._processes:
            return
        for proc in self._processes:
            try:
                proc.terminate()
            except Exception:
                pass
        deadline = time.monotonic() + 3.0
        for proc in self._processes:
            try:
                remaining = max(0.0, deadline - time.monotonic())
                proc.wait(timeout=remaining)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._processes.clear()
        self._started = False


def autostart(env: dict[str, str] | None = None) -> ProxyBridges | None:
    """Spin up bridges if conditions are right; return manager or None.

    Skipped when ``OZON_PROXY_DISABLE`` is set, when the caller already
    provided ``OZON_PROXIES`` pointing somewhere other than 127.0.0.1, or when
    ``gost`` isn't installed (logged at WARNING).
    """
    env = env if env is not None else os.environ
    if (env.get("OZON_PROXY_DISABLE") or "").lower() in ("1", "true", "yes"):
        log.info("OZON_PROXY_DISABLE set — bridges skipped")
        return None
    existing = (env.get("OZON_PROXIES") or "").strip()
    if existing and "127.0.0.1" not in existing:
        log.info("OZON_PROXIES already provided by env — bridges skipped")
        return None
    if not gost_available():
        log.warning("gost not installed — Ozon will run without the proxy pool")
        return None
    manager = ProxyBridges()
    try:
        urls = manager.start()
    except GostMissingError as exc:
        log.warning("proxy bridges disabled: %s", exc)
        return None
    if not urls:
        return None
    env["OZON_PROXIES"] = ",".join(urls)
    env["OZON_PROXY_SCHEME"] = "http"
    # Auth is baked into the bridges; explicit creds would confuse Camoufox.
    env.pop("OZON_PROXY_USER", None)
    env.pop("OZON_PROXY_PASS", None)
    return manager
