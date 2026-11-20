#!/usr/bin/env python3
"""Safe-band defaults for email alerts: 38%–44%, reset at 38.5 and 43.5.

Runs with alert_config.json and ALERT_*/SMTP_* environment absent. The sender
is replaced, and SMTP constructors refuse to connect.
"""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ENV_KEYS = (
    "ALERT_ENABLED",
    "ALERT_TO",
    "ALERT_LOW",
    "ALERT_HIGH",
    "ALERT_LOW_RESET",
    "ALERT_HIGH_RESET",
    "ALERT_COOLDOWN_MINUTES",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_SSL",
)

for _key in ENV_KEYS:
    os.environ.pop(_key, None)

import server  # noqa: E402

ROOT = Path(__file__).resolve().parent


class _SmtpForbidden:
    def __init__(self, *args, **kwargs):
        raise AssertionError("SMTP must not be contacted")


class AlertBandTests(unittest.TestCase):
    def setUp(self):
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        self._config_path = server.ALERT_CONFIG_PATH
        server.ALERT_CONFIG_PATH = ROOT / "alert_config.json.absent"
        self.assertFalse(server.ALERT_CONFIG_PATH.exists())
        self.sent: list[dict] = []
        self._real_send = server.send_alert_email
        server.send_alert_email = self._capture
        self._smtp_ssl = server.smtplib.SMTP_SSL
        self._smtp = server.smtplib.SMTP
        server.smtplib.SMTP_SSL = _SmtpForbidden
        server.smtplib.SMTP = _SmtpForbidden
        self._reset_state()

    def tearDown(self):
        server.send_alert_email = self._real_send
        server.smtplib.SMTP_SSL = self._smtp_ssl
        server.smtplib.SMTP = self._smtp
        server.ALERT_CONFIG_PATH = self._config_path
        self._reset_state()

    def _capture(self, cfg, kind, premium, skhy, skhynix):
        self.sent.append(
            {
                "kind": kind,
                "premium": premium,
                "low_threshold": "low_threshold" in cfg,
                "high_threshold": "high_threshold" in cfg,
                "low_reset": "low_reset" in cfg,
                "high_reset": "high_reset" in cfg,
            }
        )

    def _reset_state(self):
        with server._cache_lock:
            server._alert_state.update(
                {
                    "latched_low": False,
                    "latched_high": False,
                    "last_sent_at": 0.0,
                    "last_error": None,
                    "last_ok_at": 0.0,
                    "last_premium": None,
                    "last_kind": None,
                }
            )
        self.sent.clear()

    def _latches(self):
        with server._cache_lock:
            return (
                bool(server._alert_state["latched_low"]),
                bool(server._alert_state["latched_high"]),
                server._alert_state["last_error"],
            )

    def _expire_cooldown(self):
        with server._cache_lock:
            server._alert_state["last_sent_at"] = time.time() - 31 * 60

    def _check_premium(self, premium: float):
        """Drive check_email_alert with an exact premium (sender still mocked)."""
        before = len(self.sent)
        with patch.object(server, "calc_premium", lambda _skhy, _skhynix: premium):
            server.check_email_alert(1.0, 1.0)
        return self.sent[before:]

    def _check_quotes(self, skhy: float, skhynix: float):
        before = len(self.sent)
        premium = server.calc_premium(skhy, skhynix)
        server.check_email_alert(skhy, skhynix)
        return premium, self.sent[before:]

    def _enable_code_defaults(self):
        real = server.load_alert_config

        def _load():
            cfg = real()
            self.assertEqual(cfg, {"enabled": False})
            return {"enabled": True}

        return patch.object(server, "load_alert_config", _load)

    def test_effective_defaults_when_config_and_env_absent(self):
        cfg = server.load_alert_config()
        self.assertEqual(cfg, {"enabled": False})

        status = server.alert_status_payload()
        self.assertFalse(status["enabled"])
        self.assertIsNone(status["to"])
        self.assertEqual(status["low_threshold"], 38)
        self.assertEqual(status["high_threshold"], 44)
        self.assertEqual(status["low_reset"], 38.5)
        self.assertEqual(status["high_reset"], 43.5)
        self.assertEqual(status["cooldown_minutes"], 30)
        self.assertFalse(status["smtp_configured"])

        # Alerts stay off, so extreme premiums do not mail.
        low_premium, low_sent = self._check_quotes(130, 1000)
        high_premium, high_sent = self._check_quotes(200, 1000)
        self.assertLess(low_premium, 38)
        self.assertGreater(high_premium, 44)
        self.assertEqual(low_sent, [])
        self.assertEqual(high_sent, [])
        self.assertEqual(self._latches()[:2], (False, False))
        self.assertIsNone(self._latches()[2])

    def test_exact_boundaries_and_hysteresis(self):
        with self._enable_code_defaults():
            # Inclusive edges and the open middle do not alert.
            for premium in (38, 38.5, 41, 43.5, 44):
                self.assertEqual(self._check_premium(premium), [], premium)
                self.assertEqual(self._latches()[:2], (False, False), premium)

            sent = self._check_premium(37.999)
            self.assertEqual([row["kind"] for row in sent], ["low"])
            self.assertEqual(sent[0]["premium"], 37.999)
            self.assertFalse(any(sent[0][key] for key in (
                "low_threshold",
                "high_threshold",
                "low_reset",
                "high_reset",
            )))
            self.assertEqual(self._latches()[:2], (True, False))

            # Low latch holds until the premium reaches low + 0.5.
            self.assertEqual(self._check_premium(38), [])
            self.assertEqual(self._latches()[:2], (True, False))
            self.assertEqual(self._check_premium(38.499999), [])
            self.assertEqual(self._latches()[:2], (True, False))
            self.assertEqual(self._check_premium(38.5), [])
            self.assertEqual(self._latches()[:2], (False, False))

            # Cooldown still blocks the next breach; the latch is not set
            # until a send actually happens.
            self.assertEqual(self._check_premium(37.999), [])
            self.assertEqual(self._latches()[:2], (False, False))
            self._expire_cooldown()
            self.assertEqual(
                [row["kind"] for row in self._check_premium(37.999)],
                ["low"],
            )
            self.assertEqual(self._latches()[:2], (True, False))

            self.assertEqual(self._check_premium(41), [])
            self.assertEqual(self._latches()[:2], (False, False))
            self._expire_cooldown()

            sent = self._check_premium(44.001)
            self.assertEqual([row["kind"] for row in sent], ["high"])
            self.assertEqual(self._latches()[:2], (False, True))
            self.assertEqual(self._check_premium(44), [])
            self.assertEqual(self._latches()[:2], (False, True))
            self.assertEqual(self._check_premium(43.500001), [])
            self.assertEqual(self._latches()[:2], (False, True))
            self.assertEqual(self._check_premium(43.5), [])
            self.assertEqual(self._latches()[:2], (False, False))

            self.assertEqual(self._check_premium(44.001), [])
            self._expire_cooldown()
            self.assertEqual(
                [row["kind"] for row in self._check_premium(44.001)],
                ["high"],
            )
            self.assertEqual(self._latches()[:2], (False, True))
            self.assertIsNone(self._latches()[2])

    def test_real_quotes_use_the_same_band(self):
        with self._enable_code_defaults():
            premium, sent = self._check_quotes(130, 1000)
            self.assertLess(premium, 38)
            self.assertEqual([row["kind"] for row in sent], ["low"])

            premium, sent = self._check_quotes(2756, 19971)
            self.assertGreaterEqual(premium, 38)
            self.assertLess(premium, 38.5)
            self.assertEqual(sent, [])
            self.assertEqual(self._latches()[:2], (True, False))

            premium, sent = self._check_quotes(277, 2000)
            self.assertEqual(premium, 38.5)
            self.assertEqual(sent, [])
            self.assertEqual(self._latches()[:2], (False, False))

            premium, sent = self._check_quotes(141, 1000)
            self.assertGreater(premium, 38.5)
            self.assertLess(premium, 43.5)
            self.assertEqual(sent, [])

            premium, sent = self._check_quotes(18, 125)
            self.assertGreater(premium, 43.5)
            self.assertLessEqual(premium, 44)
            self.assertEqual(sent, [])
            self.assertEqual(self._latches()[:2], (False, False))

            self._expire_cooldown()
            premium, sent = self._check_quotes(1441, 10000)
            self.assertGreater(premium, 44)
            self.assertEqual([row["kind"] for row in sent], ["high"])
            self.assertEqual(self._latches()[:2], (False, True))

            premium, sent = self._check_quotes(287, 2000)
            self.assertGreater(premium, 43.5)
            self.assertLess(premium, 44)
            self.assertEqual(sent, [])
            self.assertEqual(self._latches()[:2], (False, True))

            premium, sent = self._check_quotes(2615, 18223)
            self.assertLessEqual(premium, 43.5)
            self.assertGreater(premium, 38.5)
            self.assertEqual(sent, [])
            self.assertEqual(self._latches()[:2], (False, False))

    def test_example_and_dashboard_copy_match_defaults(self):
        example = json.loads(
            (ROOT / "alert_config.example.json").read_text(encoding="utf-8")
        )
        self.assertEqual(example["low_threshold"], 38)
        self.assertEqual(example["high_threshold"], 44)
        self.assertEqual(example["low_reset"], 38.5)
        self.assertEqual(example["high_reset"], 43.5)
        self.assertTrue(example["enabled"])
        self.assertEqual(example["cooldown_minutes"], 30)
        self.assertEqual(example["smtp"]["host"], "smtp.163.com")
        self.assertEqual(example["smtp"]["port"], 465)
        self.assertTrue(example["smtp"]["ssl"])
        self.assertEqual(example["smtp"]["password"], "")

        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('<strong id="emailRuleLow">&lt; 38%</strong>', html)
        self.assertIn('<strong id="emailRuleMid">38% – 44%</strong>', html)
        self.assertIn('<strong id="emailRuleHigh">&gt; 44%</strong>', html)
        self.assertIn('src="app.js?v=20261120-38-44"', html)
        self.assertNotIn("44% – 49%", html)

        js = (ROOT / "app.js").read_text(encoding="utf-8")
        self.assertIn("s.low_threshold ?? 38", js)
        self.assertIn("s.high_threshold ?? 44", js)

        src = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("low_reset = float(cfg.get(\"low_reset\", low + 0.5))", src)
        self.assertIn("high_reset = float(cfg.get(\"high_reset\", high - 0.5))", src)
        self.assertNotIn("44.5", src)
        self.assertNotIn("48.5", src)


if __name__ == "__main__":
    unittest.main()
