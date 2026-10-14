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

1. Connect this GitHub repo to Render (Blueprint or New Web Service).
2. Confirm start command `python server.py` and `HOST=0.0.0.0`.
3. Add secrets only in the dashboard (`ALERT_ENABLED`, `ALERT_TO`, `SMTP_USER`, `SMTP_PASSWORD`, and related `ALERT_*` / `SMTP_*` keys). Free tier sleeps after idle.
