#!/usr/bin/env python3
"""Serve SKHY spread monitor and proxy Binance Futures prices."""

from __future__ import annotations

import base64
import json
import os
import smtplib
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.header import Header
from email.mime.text import MIMEText
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8765"))
# fapi.binance.com returns 418/451 from some datacenter IPs (Render Singapore).
# www.binance.com serves the same /fapi/v1 paths. Override with BINANCE_FAPI_BASE.
_raw_fapi_base = os.environ.get("BINANCE_FAPI_BASE", "").strip().rstrip("/")
BINANCE_FAPI_BASE = _raw_fapi_base or "https://www.binance.com"
# Equity-perp tickers are delivered on /market/stream. The older /stream path
# completes the handshake and then stays silent, which froze the UI on REST.
BINANCE_WS_STREAM = "skhyusdt@miniTicker/skhynixusdt@miniTicker"
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
_binance_rest_blocked_until = 0.0
_ws_quote = {"skhy": None, "skhynix": None, "at": 0.0, "source": ""}
_source_logged: str | None = None

_GATE_CONTRACT = {"SKHYUSDT": "SKHY_USDT", "SKHYNIXUSDT": "SKHYNIX_USDT"}
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)


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

    low = float(cfg.get("low_threshold", 37))
    high = float(cfg.get("high_threshold", 45))
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
    low = float(cfg.get("low_threshold", 37))
    high = float(cfg.get("high_threshold", 45))
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
        "low_threshold": cfg.get("low_threshold", 37),
        "high_threshold": cfg.get("high_threshold", 45),
        "low_reset": cfg.get("low_reset", 37.5),
        "high_reset": cfg.get("high_reset", 44.5),
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


def binance_ws_urls() -> list[str]:
    defaults = [
        f"wss://fstream.binance.com/market/stream?streams={BINANCE_WS_STREAM}",
        f"wss://dstream.binance.com/market/stream?streams={BINANCE_WS_STREAM}",
    ]
    custom = os.environ.get("BINANCE_WS_URL", "").strip()
    if not custom:
        return defaults
    return [custom, *[url for url in defaults if url != custom]]


def binance_rest_bases() -> list[str]:
    raw = [
        BINANCE_FAPI_BASE,
        "https://www.binance.com",
        "https://fapi.binance.com",
        "https://fapi1.binance.com",
        "https://fapi2.binance.com",
        "https://fapi3.binance.com",
    ]
    bases: list[str] = []
    for base in raw:
        base = base.strip().rstrip("/")
        if base and base not in bases:
            bases.append(base)
    return bases


def binance_rest_blocked() -> bool:
    return time.time() < _binance_rest_blocked_until


def note_binance_rest_failure(exc: BaseException) -> None:
    global _binance_rest_blocked_until
    text = str(exc)
    until_s = None
    marker = "banned until "
    idx = text.find(marker)
    if idx >= 0:
        digits: list[str] = []
        for ch in text[idx + len(marker) :]:
            if ch.isdigit():
                digits.append(ch)
            elif digits:
                break
        if digits:
            raw = int("".join(digits))
            until_s = raw / 1000.0 if raw > 10**12 else float(raw)
    if until_s is None and any(
        code in text
        for code in ("HTTP Error 418", "HTTP Error 429", "HTTP Error 451", "-1003")
    ):
        until_s = time.time() + 15 * 60
    if until_s is None or until_s <= _binance_rest_blocked_until:
        return
    _binance_rest_blocked_until = until_s
    when = datetime.fromtimestamp(until_s, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[prices] binance REST blocked until {when}", flush=True)


def _open_url(req: urllib.request.Request, timeout: int):
    proxy = os.environ.get("BINANCE_PROXY", "").strip()
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
        return opener.open(req, timeout=timeout)
    # urlopen already honors HTTP_PROXY / HTTPS_PROXY.
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_json_urllib(url: str):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _UA, "Accept": "application/json"},
    )
    try:
        with _open_url(req, 12) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(300).decode("utf-8", "replace")
        except Exception:
            body = ""
        raise RuntimeError(f"HTTP Error {exc.code}: {exc.reason} {body}".strip()) from exc


def fetch_json_powershell(url: str):
    proxy = os.environ.get("BINANCE_PROXY", "").strip().replace("'", "")
    proxy_arg = f" -Proxy '{proxy}'" if proxy else ""
    script = (
        "$ProgressPreference='SilentlyContinue'; "
        f"(Invoke-WebRequest -Uri '{url}' -TimeoutSec 15{proxy_arg}).Content"
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


def _pair_prices(skhy: float, skhynix: float, source: str) -> dict:
    if skhy <= 0 or skhynix <= 0:
        raise RuntimeError(f"{source} returned non-positive prices")
    return {"skhy": skhy, "skhynix": skhynix, "source": source}


def fetch_binance_rest_prices() -> dict:
    if binance_rest_blocked():
        raise RuntimeError("binance REST skipped while the IP ban is active")
    last: BaseException | None = None
    for base in binance_rest_bases():
        try:
            skhy = fetch_json(f"{base}/fapi/v1/ticker/price?symbol=SKHYUSDT")
            nix = fetch_json(f"{base}/fapi/v1/ticker/price?symbol=SKHYNIXUSDT")
            return _pair_prices(float(skhy["price"]), float(nix["price"]), f"binance:{base}")
        except Exception as exc:
            last = exc
            note_binance_rest_failure(exc)
            if binance_rest_blocked():
                break
    raise RuntimeError(f"binance REST failed: {last}")


def fetch_gate_prices() -> dict:
    skhy = fetch_json("https://api.gateio.ws/api/v4/futures/usdt/tickers?contract=SKHY_USDT")
    nix = fetch_json("https://api.gateio.ws/api/v4/futures/usdt/tickers?contract=SKHYNIX_USDT")
    return _pair_prices(float(skhy[0]["last"]), float(nix[0]["last"]), "gate")


def fetch_bitget_prices() -> dict:
    def last(symbol: str) -> float:
        data = fetch_json(
            "https://api.bitget.com/api/v2/mix/market/ticker"
            f"?productType=USDT-FUTURES&symbol={symbol}"
        )
        return float(data["data"][0]["lastPr"])

    return _pair_prices(last("SKHYUSDT"), last("SKHYNIXUSDT"), "bitget")


def fetch_okx_prices() -> dict:
    def last(inst: str) -> float:
        data = fetch_json(f"https://www.okx.com/api/v5/market/ticker?instId={inst}")
        return float(data["data"][0]["last"])

    return _pair_prices(last("SKHY-USDT-SWAP"), last("SKHYNIX-USDT-SWAP"), "okx")


def fetch_mexc_prices() -> dict:
    def last(symbol: str) -> float:
        data = fetch_json(f"https://contract.mexc.com/api/v1/contract/ticker?symbol={symbol}")
        if not data.get("success"):
            raise RuntimeError(data.get("message") or "mexc ticker failed")
        return float(data["data"]["lastPrice"])

    return _pair_prices(last("SKHYSTOCK_USDT"), last("SKHYNIXSTOCK_USDT"), "mexc")


def fetch_rest_prices() -> dict:
    errors = []
    for fetcher in (
        fetch_binance_rest_prices,
        fetch_gate_prices,
        fetch_bitget_prices,
        fetch_okx_prices,
        fetch_mexc_prices,
    ):
        try:
            return fetcher()
        except Exception as exc:
            errors.append(f"{fetcher.__name__}: {exc}")
    raise RuntimeError(" | ".join(errors))


def fetch_binance_klines(symbol: str, interval: str, limit: int):
    if binance_rest_blocked():
        raise RuntimeError("binance REST skipped while the IP ban is active")
    qs = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": limit})
    last: BaseException | None = None
    for base in binance_rest_bases():
        try:
            rows = fetch_json(f"{base}/fapi/v1/klines?{qs}")
            if not isinstance(rows, list) or not rows:
                raise RuntimeError("empty binance klines")
            return rows
        except Exception as exc:
            last = exc
            note_binance_rest_failure(exc)
            if binance_rest_blocked():
                break
    raise RuntimeError(f"binance klines failed: {last}")


def fetch_gate_klines(symbol: str, interval: str, limit: int):
    contract = _GATE_CONTRACT[symbol]
    qs = urllib.parse.urlencode(
        {"contract": contract, "interval": interval, "limit": limit}
    )
    rows = fetch_json(f"https://api.gateio.ws/api/v4/futures/usdt/candlesticks?{qs}")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("empty gate klines")
    out = [
        [int(row["t"]) * 1000, row["o"], row["h"], row["l"], row["c"]]
        for row in rows
    ]
    out.sort(key=lambda row: row[0])
    return out


def fetch_klines(symbol: str, interval: str, limit: int):
    errors = []
    for fetcher in (fetch_binance_klines, fetch_gate_klines):
        try:
            return fetcher(symbol, interval, limit)
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError("; ".join(errors))


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


def publish_prices(data: dict, *, alert: bool) -> None:
    global _source_logged
    source = str(data.get("source") or "")
    if source != _source_logged:
        print(
            f"[prices] source={source} skhy={data['skhy']} skhynix={data['skhynix']}",
            flush=True,
        )
        _source_logged = source
    with _cache_lock:
        _price_cache["at"] = time.time()
        _price_cache["data"] = {
            "skhy": data["skhy"],
            "skhynix": data["skhynix"],
            "source": source,
        }
        _price_cache["error"] = None
    if alert:
        check_email_alert(float(data["skhy"]), float(data["skhynix"]))


def refresh_prices() -> None:
    try:
        publish_prices(fetch_rest_prices(), alert=False)
    except Exception as exc:
        with _cache_lock:
            _price_cache["error"] = str(exc)
        print(f"[prices] REST failed: {exc}", flush=True)


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


def _ws_send_pong(ssock: ssl.SSLSocket, payload: bytes) -> None:
    if len(payload) > 125:
        payload = payload[:125]
    mask = os.urandom(4)
    masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    ssock.sendall(bytes([0x8A, 0x80 | len(payload)]) + mask + masked)


def _ws_recv_exact(ssock: ssl.SSLSocket, size: int, buf: bytearray) -> bytes:
    while len(buf) < size:
        chunk = ssock.recv(4096)
        if not chunk:
            raise ConnectionError("websocket closed")
        buf.extend(chunk)
    out = bytes(buf[:size])
    del buf[:size]
    return out


def _ws_read_frame(ssock: ssl.SSLSocket, buf: bytearray) -> tuple[int, bytes, bool]:
    header = _ws_recv_exact(ssock, 2, buf)
    fin = bool(header[0] & 0x80)
    opcode = header[0] & 0x0F
    length = header[1] & 0x7F
    if length == 126:
        length = int.from_bytes(_ws_recv_exact(ssock, 2, buf), "big")
    elif length == 127:
        length = int.from_bytes(_ws_recv_exact(ssock, 8, buf), "big")
    payload = _ws_recv_exact(ssock, length, buf) if length else b""
    return opcode, payload, fin


def apply_ws_message(text: str) -> None:
    msg = json.loads(text)
    payload = msg.get("data") if isinstance(msg, dict) and isinstance(msg.get("data"), dict) else msg
    if not isinstance(payload, dict):
        return
    symbol = payload.get("s")
    if symbol == "SKHYUSDT":
        key = "skhy"
    elif symbol == "SKHYNIXUSDT":
        key = "skhynix"
    else:
        return
    price = float(payload["c"])
    if price <= 0:
        return
    data = None
    with _cache_lock:
        _ws_quote[key] = price
        _ws_quote["at"] = time.time()
        _ws_quote["source"] = _ws_quote.get("source") or "binance-ws"
        if _ws_quote["skhy"] is not None and _ws_quote["skhynix"] is not None:
            data = {
                "skhy": float(_ws_quote["skhy"]),
                "skhynix": float(_ws_quote["skhynix"]),
                "source": _ws_quote["source"],
            }
    if data:
        publish_prices(data, alert=False)


def ws_quotes_fresh(max_age: float = 15.0) -> bool:
    with _cache_lock:
        at = float(_ws_quote["at"] or 0.0)
        ready = _ws_quote["skhy"] is not None and _ws_quote["skhynix"] is not None
    return ready and (time.time() - at) <= max_age


def run_ws_session(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "wss" or not parsed.hostname:
        raise RuntimeError(f"unsupported websocket url: {url}")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    host = parsed.hostname
    port = parsed.port or 443
    key = base64.b64encode(os.urandom(16)).decode()
    raw = socket.create_connection((host, port), timeout=10)
    ssock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    try:
        ssock.settimeout(20)
        ssock.sendall(
            (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                f"User-Agent: {_UA}\r\n\r\n"
            ).encode()
        )
        blob = b""
        while b"\r\n\r\n" not in blob:
            chunk = ssock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake closed")
            blob += chunk
        head, rest = blob.split(b"\r\n\r\n", 1)
        status_line = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
        if " 101 " not in status_line:
            raise ConnectionError(status_line)
        with _cache_lock:
            _ws_quote["source"] = f"binance-ws:{host}"
        print(f"[prices] binance websocket connected {host}", flush=True)
        buf = bytearray(rest)
        parts: list[bytes] = []
        while not _stop.is_set():
            opcode, payload, fin = _ws_read_frame(ssock, buf)
            if opcode == 9:
                _ws_send_pong(ssock, payload)
                continue
            if opcode == 8:
                raise ConnectionError("websocket closed by server")
            if opcode in (1, 2):
                parts = [payload]
            elif opcode == 0 and parts:
                parts.append(payload)
            else:
                continue
            if not fin:
                continue
            text = b"".join(parts).decode("utf-8", "replace")
            parts = []
            if not text:
                continue
            try:
                apply_ws_message(text)
            except Exception as exc:
                print(f"[prices] ignored websocket message: {exc}", flush=True)
    finally:
        ssock.close()


def binance_ws_loop() -> None:
    backoff = 2
    while not _stop.is_set():
        for url in binance_ws_urls():
            if _stop.is_set():
                return
            try:
                run_ws_session(url)
                backoff = 2
            except Exception as exc:
                print(f"[prices] binance websocket {url} failed: {exc}", flush=True)
                with _cache_lock:
                    _ws_quote["at"] = 0.0
        if _stop.is_set():
            return
        _stop.wait(backoff)
        backoff = min(backoff * 2, 30)


def bg_loop() -> None:
    threading.Thread(target=binance_ws_loop, name="binance-ws", daemon=True).start()
    got_ws = False
    for _ in range(8):
        if _stop.wait(1):
            return
        if ws_quotes_fresh(10):
            got_ws = True
            break
    if not got_ws:
        refresh_prices()
    refresh_history("7d")
    last_alert = 0.0
    last_rest = time.time()
    last_history = time.time()
    while not _stop.wait(2):
        now = time.time()
        if not ws_quotes_fresh() and now - last_rest >= PRICE_REFRESH_SEC:
            refresh_prices()
            last_rest = now
        with _cache_lock:
            cached = _price_cache.get("data")
        if cached and cached.get("skhy") and cached.get("skhynix"):
            if last_alert == 0.0 or now - last_alert >= PRICE_REFRESH_SEC:
                check_email_alert(float(cached["skhy"]), float(cached["skhynix"]))
                last_alert = now
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
    print(f"Binance futures base → {BINANCE_FAPI_BASE}", flush=True)
    print(f"Binance market stream → {binance_ws_urls()[0]}", flush=True)
    print(
        f"Email alert → {cfg.get('to')} @ <{cfg.get('low_threshold', 37)}% "
        f"or >{cfg.get('high_threshold', 45)}% "
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
