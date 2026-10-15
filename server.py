#!/usr/bin/env python3
"""Serve SKHY spread monitor and proxy Binance Futures prices."""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8765"))
BINANCE_PRICE = "https://www.binance.com/fapi/v1/ticker/price"
BINANCE_KLINES = "https://www.binance.com/fapi/v1/klines"
ALERT_CONFIG_PATH = ROOT / "alert_config.json"

RANGES = {
    "15m": ("1m", 15),
    "1h": ("1m", 60),
    "4h": ("1m", 240),
    "24h": ("1m", 1440),
    "7d": ("15m", 672),  # 7 days × 24h × 4 = 672
}

PRICE_REFRESH_SEC = 30
HISTORY_REFRESH_SEC = 180

_cache_lock = threading.Lock()
_price_cache: dict = {"at": 0.0, "data": None, "error": None}
_history_cache: dict[str, dict] = {}
_alert_state: dict = {
    "latched_low": False,
    "latched_high": False,
    "last_sent_at": 0.0,
    "last_error": None,
    "last_ok_at": 0.0,
    "last_premium": None,
    "last_kind": None,
}
_stop = threading.Event()


def load_alert_config() -> dict:
    cfg: dict = {"enabled": False}
    if ALERT_CONFIG_PATH.exists():
        with ALERT_CONFIG_PATH.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
    # Render / cloud: secrets via env (never commit real alert_config.json)
    if os.environ.get("ALERT_ENABLED", "").strip().lower() in {"1", "true", "yes"}:
        cfg["enabled"] = True
    if os.environ.get("ALERT_TO"):
        cfg["to"] = os.environ["ALERT_TO"].strip()
    for key, env in (
        ("low_threshold", "ALERT_LOW"),
        ("high_threshold", "ALERT_HIGH"),
        ("low_reset", "ALERT_LOW_RESET"),
        ("high_reset", "ALERT_HIGH_RESET"),
        ("cooldown_minutes", "ALERT_COOLDOWN_MINUTES"),
    ):
        if os.environ.get(env):
            cfg[key] = float(os.environ[env]) if key != "cooldown_minutes" else int(float(os.environ[env]))
    smtp = dict(cfg.get("smtp") or {})
    if os.environ.get("SMTP_HOST"):
        smtp["host"] = os.environ["SMTP_HOST"].strip()
    if os.environ.get("SMTP_PORT"):
        smtp["port"] = int(os.environ["SMTP_PORT"])
    if os.environ.get("SMTP_USER"):
        smtp["user"] = os.environ["SMTP_USER"].strip()
    if os.environ.get("SMTP_PASSWORD"):
        smtp["password"] = os.environ["SMTP_PASSWORD"].strip()
    if os.environ.get("SMTP_SSL", "").strip().lower() in {"0", "false", "no"}:
        smtp["ssl"] = False
    elif os.environ.get("SMTP_SSL", "").strip().lower() in {"1", "true", "yes"}:
        smtp["ssl"] = True
    if smtp:
        cfg["smtp"] = smtp
    return cfg


def calc_premium(skhy: float, skhynix: float) -> float:
    return (skhy * 10 / skhynix - 1) * 100


def send_alert_email(
    cfg: dict, kind: str, premium: float, skhy: float, skhynix: float
) -> None:
    smtp = cfg.get("smtp") or {}
    user = (smtp.get("user") or "").strip()
    password = (smtp.get("password") or "").strip()
    to_addr = (cfg.get("to") or user).strip()
    if not user or not password or not to_addr:
        raise RuntimeError(
            "邮件未配置：请在 alert_config.json 填写 SMTP 授权码 (smtp.password)"
        )

    low = float(cfg.get("low_threshold", 40))
    high = float(cfg.get("high_threshold", 47))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ten = skhy * 10
    spread = skhynix - ten

    if kind == "low":
        title = f"[SKHY警报] 溢价偏低 {premium:.2f}%（<{low:g}%）"
        reason = f"ADR 溢价已跌破 {low:g}%（当前 {premium:.2f}%）"
    else:
        title = f"[SKHY警报] 溢价偏高 {premium:.2f}%（>{high:g}%）"
        reason = f"ADR 溢价已升破 {high:g}%（当前 {premium:.2f}%）"

    body = (
        f"时间：{now}\n"
        f"{reason}\n\n"
        f"SKHY：{skhy:.4f}\n"
        f"10×SKHY：{ten:.4f}\n"
        f"SKHYNIX：{skhynix:.4f}\n"
        f"差价（SKHYNIX−10×SKHY）：{spread:.4f} USDT\n\n"
        f"规则：溢价 < {low:g}% 或 > {high:g}% 报警\n"
        f"公式：溢价 = 10×SKHY / SKHYNIX − 1\n"
        f"来源：SKHY Spread Monitor\n"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = user
    msg["To"] = to_addr
    msg["Subject"] = Header(title, "utf-8")

    host = smtp.get("host") or "smtp.163.com"
    port = int(smtp.get("port") or 465)
    context = ssl.create_default_context()
    if smtp.get("ssl", True):
        with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as server:
            server.login(user, password)
            server.sendmail(user, [to_addr], msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.starttls(context=context)
            server.login(user, password)
            server.sendmail(user, [to_addr], msg.as_string())


def check_email_alert(skhy: float, skhynix: float) -> None:
    cfg = load_alert_config()
    if not cfg.get("enabled", False):
        return

    premium = calc_premium(skhy, skhynix)
    low = float(cfg.get("low_threshold", 40))
    high = float(cfg.get("high_threshold", 47))
    low_reset = float(cfg.get("low_reset", low + 0.5))
    high_reset = float(cfg.get("high_reset", high - 0.5))
    cooldown = float(cfg.get("cooldown_minutes", 30)) * 60

    with _cache_lock:
        _alert_state["last_premium"] = premium
        latched_low = bool(_alert_state["latched_low"])
        latched_high = bool(_alert_state["latched_high"])
        last_sent = float(_alert_state["last_sent_at"] or 0.0)

    # Clear latches once premium returns to the middle band.
    if premium >= low_reset:
        with _cache_lock:
            _alert_state["latched_low"] = False
        latched_low = False
    if premium <= high_reset:
        with _cache_lock:
            _alert_state["latched_high"] = False
        latched_high = False

    kind = None
    if premium < low and not latched_low:
        kind = "low"
    elif premium > high and not latched_high:
        kind = "high"
    else:
        return

    now = time.time()
    if now - last_sent < cooldown:
        return

    try:
        send_alert_email(cfg, kind, premium, skhy, skhynix)
        with _cache_lock:
            if kind == "low":
                _alert_state["latched_low"] = True
            else:
                _alert_state["latched_high"] = True
            _alert_state["last_sent_at"] = now
            _alert_state["last_ok_at"] = now
            _alert_state["last_kind"] = kind
            _alert_state["last_error"] = None
        print(f"[alert] email sent ({kind}): premium={premium:.2f}%", flush=True)
    except Exception as exc:
        with _cache_lock:
            _alert_state["last_error"] = str(exc)
        print(f"[alert] email failed: {exc}", flush=True)


def alert_status_payload() -> dict:
    cfg = load_alert_config()
    smtp = cfg.get("smtp") or {}
    with _cache_lock:
        state = dict(_alert_state)
    return {
        "enabled": bool(cfg.get("enabled")),
        "to": cfg.get("to"),
        "low_threshold": cfg.get("low_threshold", 40),
        "high_threshold": cfg.get("high_threshold", 47),
        "low_reset": cfg.get("low_reset", 40.5),
        "high_reset": cfg.get("high_reset", 46.5),
        "cooldown_minutes": cfg.get("cooldown_minutes", 30),
        "smtp_configured": bool((smtp.get("password") or "").strip()),
        "latched_low": state.get("latched_low"),
        "latched_high": state.get("latched_high"),
        "last_premium": state.get("last_premium"),
        "last_kind": state.get("last_kind"),
        "last_sent_at": state.get("last_sent_at"),
        "last_ok_at": state.get("last_ok_at"),
        "last_error": state.get("last_error"),
    }


def fetch_json_urllib(url: str):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/127.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_json_powershell(url: str):
    script = (
        "$ProgressPreference='SilentlyContinue'; "
        f"(Invoke-WebRequest -Uri '{url}' -TimeoutSec 15).Content"
    )
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "powershell fetch failed").strip()
        raise RuntimeError(err)
    body = completed.stdout.strip()
    if not body:
        raise RuntimeError("empty response from powershell fetch")
    return json.loads(body)


def fetch_json(url: str):
    if sys.platform.startswith("win"):
        try:
            return fetch_json_powershell(url)
        except Exception as ps_exc:
            try:
                return fetch_json_urllib(url)
            except Exception:
                raise ps_exc
    return fetch_json_urllib(url)


def fetch_price(symbol: str) -> float:
    data = fetch_json(f"{BINANCE_PRICE}?symbol={symbol}")
    return float(data["price"])


def fetch_klines(symbol: str, interval: str, limit: int):
    qs = urllib.parse.urlencode(
        {"symbol": symbol, "interval": interval, "limit": limit}
    )
    return fetch_json(f"{BINANCE_KLINES}?{qs}")


def build_history(range_key: str) -> dict:
    interval, limit = RANGES.get(range_key, RANGES["24h"])
    skhy = fetch_klines("SKHYUSDT", interval, limit)
    skhynix = fetch_klines("SKHYNIXUSDT", interval, limit)
    m1 = {int(c[0]): float(c[4]) for c in skhy}
    m2 = {int(c[0]): float(c[4]) for c in skhynix}
    times = sorted(set(m1) & set(m2))
    points = [
        {
            "t": t,
            "skhy": m1[t],
            "skhynix": m2[t],
            "premium": (m1[t] * 10 / m2[t] - 1) * 100,
            "spread": m2[t] - m1[t] * 10,
        }
        for t in times
    ]
    return {"range": range_key, "interval": interval, "points": points}


def refresh_prices() -> None:
    try:
        data = {
            "skhy": fetch_price("SKHYUSDT"),
            "skhynix": fetch_price("SKHYNIXUSDT"),
        }
        with _cache_lock:
            _price_cache["at"] = time.time()
            _price_cache["data"] = data
            _price_cache["error"] = None
        check_email_alert(data["skhy"], data["skhynix"])
    except Exception as exc:
        with _cache_lock:
            _price_cache["error"] = str(exc)


def refresh_history(range_key: str = "7d") -> None:
    try:
        data = build_history(range_key)
        with _cache_lock:
            _history_cache[range_key] = {"at": time.time(), "data": data, "error": None}
    except Exception as exc:
        with _cache_lock:
            prev = _history_cache.get(range_key) or {}
            _history_cache[range_key] = {
                "at": prev.get("at", 0.0),
                "data": prev.get("data"),
                "error": str(exc),
            }


def bg_loop() -> None:
    refresh_prices()
    refresh_history("7d")
    last_price = time.time()
    last_history = time.time()
    while not _stop.wait(2):
        now = time.time()
        if now - last_price >= PRICE_REFRESH_SEC:
            refresh_prices()
            last_price = now
        if now - last_history >= HISTORY_REFRESH_SEC:
            refresh_history("7d")
            refresh_history("24h")
            last_history = now


def send_json(handler: SimpleHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(SimpleHTTPRequestHandler):
    timeout = 10

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/api/prices":
            with _cache_lock:
                data = _price_cache.get("data")
                err = _price_cache.get("error")
                at = _price_cache.get("at", 0.0)
            if data:
                payload = dict(data)
                if err:
                    payload["warning"] = err
                payload["cached_at"] = at
                send_json(self, 200, payload)
            else:
                send_json(self, 503, {"error": err or "price cache warming up"})
            return

        if path == "/api/history":
            range_key = (qs.get("range") or ["7d"])[0]
            if range_key not in RANGES:
                range_key = "7d"
            with _cache_lock:
                hit = _history_cache.get(range_key)
            if hit and hit.get("data"):
                payload = dict(hit["data"])
                if hit.get("error"):
                    payload["warning"] = hit["error"]
                payload["cached_at"] = hit.get("at", 0.0)
                send_json(self, 200, payload)
            else:
                threading.Thread(
                    target=refresh_history, args=(range_key,), daemon=True
                ).start()
                send_json(
                    self,
                    503,
                    {"error": (hit or {}).get("error") or "history cache warming up"},
                )
            return

        if path == "/api/alert-status":
            send_json(self, 200, alert_status_payload())
            return

        return super().do_GET()

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}", flush=True)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    worker = threading.Thread(target=bg_loop, name="binance-refresh", daemon=True)
    worker.start()
    server = Server((HOST, PORT), Handler)
    cfg = load_alert_config()
    smtp_ok = bool(((cfg.get("smtp") or {}).get("password") or "").strip())
    print(f"SKHY spread monitor → http://{HOST}:{PORT}", flush=True)
    print(
        f"Email alert → {cfg.get('to')} @ <{cfg.get('low_threshold', 40)}% "
        f"or >{cfg.get('high_threshold', 47)}% "
        f"({'SMTP ready' if smtp_ok else '需要填写 SMTP 授权码'})",
        flush=True,
    )
    print("Ctrl+C to stop", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        _stop.set()
        print("\nStopped.", flush=True)


if __name__ == "__main__":
    main()
