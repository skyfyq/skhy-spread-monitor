#!/usr/bin/env python3
"""Temporary Render egress probe. Not part of the app."""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8765"))
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)

URLS = [
    "https://www.binance.com/fapi/v1/ticker/price?symbol=SKHYUSDT",
    "https://www.binance.com/fapi/v1/ticker/price?symbol=SKHYNIXUSDT",
    "https://fapi.binance.com/fapi/v1/ticker/price?symbol=SKHYUSDT",
    "https://fapi1.binance.com/fapi/v1/ticker/price?symbol=SKHYUSDT",
    "https://fapi2.binance.com/fapi/v1/ticker/price?symbol=SKHYUSDT",
    "https://fapi3.binance.com/fapi/v1/ticker/price?symbol=SKHYUSDT",
    "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT",
    "https://fapi.asterdex.com/fapi/v1/ticker/price?symbol=SKHYUSDT",
    "https://fapi.asterdex.com/fapi/v1/klines?symbol=SKHYUSDT&interval=1m&limit=1",
    "https://api.gateio.ws/api/v4/futures/usdt/tickers?contract=SKHY_USDT",
    "https://api.gateio.ws/api/v4/futures/usdt/tickers?contract=SKHYNIX_USDT",
    "https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=SKHY_USDT&interval=1m&limit=1",
    "https://api.bitget.com/api/v2/mix/market/ticker?productType=USDT-FUTURES&symbol=SKHYUSDT",
    "https://api.bitget.com/api/v2/mix/market/ticker?productType=USDT-FUTURES&symbol=SKHYNIXUSDT",
    "https://www.okx.com/api/v5/market/ticker?instId=SKHY-USDT-SWAP",
    "https://www.okx.com/api/v5/market/ticker?instId=SKHYNIX-USDT-SWAP",
    "https://contract.mexc.com/api/v1/contract/ticker?symbol=SKHYSTOCK_USDT",
    "https://contract.mexc.com/api/v1/contract/ticker?symbol=SKHYNIXSTOCK_USDT",
    "https://api.ipify.org?format=json",
    "https://cloudflare.com/cdn-cgi/trace",
]

STATE = {"started": time.time(), "done": False, "results": []}
LOCK = threading.Lock()


def clip(text: str, n: int = 180) -> str:
    text = " ".join(text.split())
    return text[:n]


def http_get(url: str, headers: dict | None = None, timeout: int = 8, label: str = "") -> dict:
    hdrs = {"User-Agent": UA, "Accept": "application/json,text/plain,*/*"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(240).decode("utf-8", "replace")
            return {
                "url": label or url,
                "ok": 200 <= resp.status < 300,
                "status": resp.status,
                "ms": int((time.time() - started) * 1000),
                "snippet": clip(body),
            }
    except Exception as exc:
        body = ""
        status = None
        if isinstance(exc, urllib.error.HTTPError):
            status = exc.code
            try:
                body = exc.read(180).decode("utf-8", "replace")
            except Exception:
                body = ""
        return {
            "url": label or url,
            "ok": False,
            "status": status,
            "ms": int((time.time() - started) * 1000),
            "error": clip(f"{type(exc).__name__}: {exc} {body}"),
        }


def cookie_then_fapi() -> dict:
    url = "https://www.binance.com/fapi/v1/ticker/price?symbol=SKHYUSDT"
    jar = urllib.request.HTTPCookieProcessor()
    opener = urllib.request.build_opener(jar)
    opener.addheaders = [("User-Agent", UA), ("Accept", "text/html,application/json")]
    started = time.time()
    try:
        with opener.open("https://www.binance.com/", timeout=8) as resp:
            home_status = resp.status
            resp.read(64)
        req = urllib.request.Request(
            url,
            headers={"User-Agent": UA, "Accept": "application/json"},
        )
        with opener.open(req, timeout=8) as resp:
            body = resp.read(180).decode("utf-8", "replace")
            return {
                "url": "cookie-warmup " + url,
                "ok": True,
                "status": resp.status,
                "home_status": home_status,
                "ms": int((time.time() - started) * 1000),
                "snippet": clip(body),
            }
    except Exception as exc:
        body = ""
        status = None
        if isinstance(exc, urllib.error.HTTPError):
            status = exc.code
            try:
                body = exc.read(160).decode("utf-8", "replace")
            except Exception:
                body = ""
        return {
            "url": "cookie-warmup " + url,
            "ok": False,
            "status": status,
            "ms": int((time.time() - started) * 1000),
            "error": clip(f"{type(exc).__name__}: {exc} {body}"),
        }


def ws_probe(path: str, timeout: int = 8) -> dict:
    host = "fstream.binance.com"
    started = time.time()
    key = base64.b64encode(os.urandom(16)).decode()
    try:
        raw = socket.create_connection((host, 443), timeout=timeout)
        ssock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        ssock.settimeout(timeout)
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"User-Agent: {UA}\r\n\r\n"
        )
        ssock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = ssock.recv(4096)
            if not chunk:
                break
            buf += chunk
        head = buf.split(b"\r\n\r\n", 1)[0].decode("latin1", "replace")
        status_line = head.split("\r\n", 1)[0]
        rest = buf.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in buf else b""
        deadline = time.time() + timeout
        frames = []
        data = rest
        while time.time() < deadline and len(frames) < 2:
            while len(data) < 2:
                ssock.settimeout(max(0.5, deadline - time.time()))
                chunk = ssock.recv(4096)
                if not chunk:
                    break
                data += chunk
            if len(data) < 2:
                break
            opcode = data[0] & 0x0F
            ln = data[1] & 0x7F
            i = 2
            if ln == 126:
                while len(data) < 4:
                    data += ssock.recv(4096)
                ln = int.from_bytes(data[2:4], "big")
                i = 4
            elif ln == 127:
                while len(data) < 10:
                    data += ssock.recv(4096)
                ln = int.from_bytes(data[2:10], "big")
                i = 10
            while len(data) < i + ln:
                data += ssock.recv(4096)
            payload = data[i : i + ln]
            data = data[i + ln :]
            if opcode == 9:  # ping
                mask = os.urandom(4)
                raw_payload = payload
                masked = bytes(b ^ mask[n % 4] for n, b in enumerate(raw_payload))
                frame = bytes([0x8A, 0x80 | len(raw_payload)]) + mask + masked
                ssock.sendall(frame)
                frames.append({"op": "ping"})
                continue
            frames.append({"op": opcode, "text": clip(payload.decode("utf-8", "replace"), 160)})
            if opcode == 1:
                break
        ssock.close()
        return {
            "url": f"wss://{host}{path}",
            "ok": any(f.get("op") == 1 for f in frames),
            "status": status_line,
            "ms": int((time.time() - started) * 1000),
            "frames": frames,
        }
    except Exception as exc:
        return {
            "url": f"wss://{host}{path}",
            "ok": False,
            "ms": int((time.time() - started) * 1000),
            "error": clip(f"{type(exc).__name__}: {exc}"),
        }


def run_probes() -> None:
    jobs = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futs = [pool.submit(http_get, url) for url in URLS]
        futs.append(
            pool.submit(
                http_get,
                "https://www.binance.com/fapi/v1/ticker/price?symbol=SKHYUSDT",
            )
        )
        futs.append(
            pool.submit(
                http_get,
                "https://fapi.binance.com/fapi/v1/ticker/price?symbol=SKHYUSDT",
                {
                    "Origin": "https://www.binance.com",
                    "Referer": "https://www.binance.com/",
                },
                8,
                "referer https://fapi.binance.com/fapi/v1/ticker/price?symbol=SKHYUSDT",
            )
        )
        futs.append(
            pool.submit(
                http_get,
                "https://www.binance.com/fapi/v1/ticker/price?symbol=SKHYUSDT",
                {
                    "Origin": "https://www.binance.com",
                    "Referer": "https://www.binance.com/",
                },
                8,
                "referer https://www.binance.com/fapi/v1/ticker/price?symbol=SKHYUSDT",
            )
        )
        futs.append(pool.submit(cookie_then_fapi))
        futs.append(pool.submit(ws_probe, "/ws/skhyusdt@miniTicker"))
        futs.append(pool.submit(ws_probe, "/stream?streams=skhyusdt@ticker/skhynixusdt@ticker"))
        for fut in as_completed(futs):
            try:
                jobs.append(fut.result())
            except Exception as exc:
                jobs.append({"ok": False, "error": clip(str(exc))})
    jobs.sort(key=lambda item: item.get("url") or "")
    with LOCK:
        STATE["results"] = jobs
        STATE["done"] = True
    ok = [j.get("url") for j in jobs if j.get("ok")]
    print("PROBE DONE ok=", ok, flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with LOCK:
            payload = {
                "done": STATE["done"],
                "elapsed_s": round(time.time() - STATE["started"], 1),
                "results": STATE["results"],
            }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[probe] {fmt % args}", flush=True)


def main() -> None:
    threading.Thread(target=run_probes, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"probe listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
