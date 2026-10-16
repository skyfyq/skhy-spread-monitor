# SKHY spread monitor

Local / cloud dashboard for SKHY vs SK Hynix futures premium (Binance).

## Local

```bash
python server.py
# http://127.0.0.1:8765/
```

Optional: copy `alert_config.example.json` → `alert_config.json` and fill SMTP auth.

## Render

Web Service (see `render.yaml` for the Blueprint):

- Runtime: Python
- Build command: `pip install -r requirements.txt` (stdlib only; the file exists so Render detects Python)
- Start command: `python server.py`
- Env: `HOST=0.0.0.0` (Render injects `PORT`)
- Optional email alerts: set `ALERT_*` and `SMTP_*` in the Render dashboard. Do not commit `alert_config.json` or SMTP passwords.

## Prices

Live quotes prefer the Binance market websocket:

`wss://fstream.binance.com/market/stream?streams=skhyusdt@miniTicker/skhynixusdt@miniTicker`

Render’s shared Singapore egress is often REST-banned by Binance (`HTTP 418`, code `-1003`, “use the websocket”). The older `/stream` URL connects and then sends no equity-perp ticks, so both the page and the server use `/market/stream`. `dstream.binance.com` is the second websocket host. Override with `BINANCE_WS_URL` (full `wss://` URL).

REST is still used for kline backfill, and for quotes if the websocket is quiet. The first host is `BINANCE_FAPI_BASE` (default `https://www.binance.com`, trailing slash ignored). A 418/429/451 backs off until the `banned until` timestamp in the body, so a shared IP ban is not extended every poll.

If Binance REST is blocked, quotes fall through to public futures tickers that list the same contracts: Gate.io (`SKHY_USDT` / `SKHYNIX_USDT`), then Bitget, OKX, and MEXC. Kline backfill falls through to Gate.io. `/api/prices` includes `source` (`binance-ws`, `binance:https://...`, `gate`, `bitget`, `okx`, or `mexc`).

Optional proxy for REST only: `BINANCE_PROXY` (wins) or the usual `HTTPS_PROXY` / `HTTP_PROXY`. Example:

```bash
BINANCE_FAPI_BASE=https://fapi.binance.com BINANCE_PROXY=http://127.0.0.1:8888 python server.py
```

1. Connect this GitHub repo to Render (Blueprint or New Web Service).
2. Confirm start command `python server.py` and `HOST=0.0.0.0`.
3. Add secrets only in the dashboard (`ALERT_ENABLED`, `ALERT_TO`, `SMTP_USER`, `SMTP_PASSWORD`, and related `ALERT_*` / `SMTP_*` keys). Free tier sleeps after idle.
