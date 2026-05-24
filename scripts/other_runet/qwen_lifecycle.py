from __future__ import annotations

import atexit
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .qwen_client import DEFAULT_QWEN_HOST, DEFAULT_QWEN_PORT


LOGGER = logging.getLogger("other_runet.qwen.lifecycle")

DEFAULT_MODEL_PATH = Path(os.environ.get("QWEN_MODEL_PATH", str(Path.home() / "models/Qwen2.5-Coder-14B-Instruct-4bit")))


class QwenServer:
    def __init__(
        self,
        *,
        model_path: Path | str = DEFAULT_MODEL_PATH,
        host: str = DEFAULT_QWEN_HOST,
        port: int = DEFAULT_QWEN_PORT,
        log_path: Path | None = None,
        startup_timeout: float = 90.0,
        python_executable: str | None = None,
    ) -> None:
        self.model_path = Path(model_path)
        self.host = host
        self.port = port
        self.log_path = log_path
        self.startup_timeout = startup_timeout
        self.python_executable = python_executable or sys.executable
        self.process: subprocess.Popen | None = None
        self._log_file = None

    def is_listening(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            try:
                sock.connect((self.host, self.port))
                return True
            except OSError:
                return False

    def start(self) -> bool:
        if self.is_listening():
            LOGGER.info("qwen server already listening on %s:%s", self.host, self.port)
            return True
        if not self.model_path.exists():
            LOGGER.warning("qwen model path missing: %s", self.model_path)
            return False
        if not _command_available(self.python_executable):
            LOGGER.warning("qwen python executable not found: %s", self.python_executable)
            return False
        command = [
            self.python_executable,
            "-m",
            "mlx_lm.server",
            "--model",
            str(self.model_path),
            "--host",
            self.host,
            "--port",
            str(self.port),
        ]
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = self.log_path.open("ab")
            stdout = self._log_file
            stderr = subprocess.STDOUT
        else:
            stdout = subprocess.DEVNULL
            stderr = subprocess.DEVNULL
        LOGGER.info("starting qwen server: %s", " ".join(command))
        try:
            self.process = subprocess.Popen(
                command,
                stdout=stdout,
                stderr=stderr,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            LOGGER.warning("qwen server failed to start: %s", exc)
            return False
        atexit.register(self.stop)
        return self._wait_until_listening()

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            except OSError:
                pass
        self.process = None
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None

    def _wait_until_listening(self) -> bool:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self.process is None or self.process.poll() is not None:
                LOGGER.warning("qwen server exited during startup (rc=%s)", self.process.returncode if self.process else "?")
                return False
            if self.is_listening():
                LOGGER.info("qwen server is listening on %s:%s", self.host, self.port)
                return True
            time.sleep(0.5)
        LOGGER.warning("qwen server did not become ready within %.0fs", self.startup_timeout)
        return False


def _command_available(executable: str) -> bool:
    if not executable:
        return False
    if Path(executable).exists():
        return True
    return shutil.which(executable) is not None
