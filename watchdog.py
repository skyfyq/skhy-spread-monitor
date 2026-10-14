#!/usr/bin/env python3
"""Watchdog: keep SKHY spread monitor alive on 127.0.0.1:8765.

Detects hung servers (port listening but HTTP resets/times out) and restarts them.
Logs to watchdog.log so it can run headless.
"""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVER_SCRIPT = ROOT / "server.py"
LOG_FILE = ROOT / "watchdog.log"
HOST = "127.0.0.1"
PORT = 8765
HEALTH_URLS = (
    f"http://{HOST}:{PORT}/api/alert-status",
    f"http://{HOST}:{PORT}/",
)
CHECK_EVERY_SEC = 10
REQUEST_TIMEOUT_SEC = 3
FAILS_BEFORE_RESTART = 2
RESTART_COOLDOWN_SEC = 15


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def probe(url: str) -> bool:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
            if resp.status != 200:
                return False
            return bool(resp.read(128))
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def health_ok() -> bool:
    return any(probe(url) for url in HEALTH_URLS)


def listening_pids() -> list[int]:
    try:
        out = subprocess.check_output(
            ["netstat", "-ano"],
            text=True,
            errors="ignore",
            timeout=10,
        )
    except Exception:
        return []

    pids: set[int] = set()
    needle = f"{HOST}:{PORT}"
    for line in out.splitlines():
        if needle not in line or "LISTENING" not in line:
            continue
        parts = line.split()
        if not parts:
            continue
        try:
            pids.add(int(parts[-1]))
        except ValueError:
            continue
    return sorted(pids)


def kill_pids(pids: list[int]) -> None:
    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            log(f"killed pid {pid}")
        except Exception as exc:
            log(f"failed to kill pid {pid}: {exc}")


def start_server() -> subprocess.Popen:
    log("starting server.py")
    creationflags = 0
    if sys.platform.startswith("win"):
        # Detach from console so closing a terminal is less likely to kill children.
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
            | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        )
    return subprocess.Popen(
        [sys.executable, str(SERVER_SCRIPT)],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )


def restart_server(proc: subprocess.Popen | None) -> subprocess.Popen:
    log("unhealthy → restarting server")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    kill_pids(listening_pids())
    # Also kill any stray server.py by command line match.
    try:
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "Where-Object { $_.CommandLine -match 'skhy-spread-monitor\\\\server\\.py' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception:
        pass
    time.sleep(1.5)
    return start_server()


def wait_until_healthy(timeout_sec: float = 40) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if health_ok():
            return True
        time.sleep(1)
    return False


def main() -> None:
    log(f"watchdog started · probe every {CHECK_EVERY_SEC}s · fails={FAILS_BEFORE_RESTART}")
    proc: subprocess.Popen | None = None
    last_restart = 0.0
    fail_streak = 0

    try:
        # Clean slate: take over whatever is on the port.
        existing = listening_pids()
        if existing:
            log(f"adopting/clearing listeners {existing}")
            kill_pids(existing)
            time.sleep(1)

        proc = start_server()
        if wait_until_healthy(40):
            log("server is healthy")
        else:
            log("server did not become healthy in time")

        while True:
            time.sleep(CHECK_EVERY_SEC)

            if proc is not None and proc.poll() is not None:
                log(f"server process exited code={proc.returncode}")
                fail_streak = FAILS_BEFORE_RESTART

            if health_ok():
                if fail_streak:
                    log("health restored")
                fail_streak = 0
                continue

            fail_streak += 1
            log(f"health check failed ({fail_streak}/{FAILS_BEFORE_RESTART})")
            if fail_streak < FAILS_BEFORE_RESTART:
                continue

            now = time.time()
            if now - last_restart < RESTART_COOLDOWN_SEC:
                log("restart cooldown active")
                continue

            proc = restart_server(proc)
            last_restart = time.time()
            fail_streak = 0
            if wait_until_healthy(40):
                log("server recovered")
            else:
                log("still unhealthy after restart")
    except KeyboardInterrupt:
        log("watchdog stopping")
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        log("bye")


if __name__ == "__main__":
    main()
