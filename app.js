(() => {
  const WS_URL =
    "wss://fstream.binance.com/stream?streams=skhyusdt@ticker/skhynixusdt@ticker";
  const RANGE_MS = {
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "24h": 24 * 60 * 60 * 1000,
    "7d": 7 * 24 * 60 * 60 * 1000,
  };
  const LIVE_GAP_MS = {
    "15m": 2000,
    "1h": 5000,
    "4h": 15000,
    "24h": 60000,
    "7d": 5 * 60 * 1000,
  };

  const el = {
    skhy: document.getElementById("skhy"),
    tenSkhy: document.getElementById("tenSkhy"),
    skhynix: document.getElementById("skhynix"),
    premium: document.getElementById("premium"),
    spreadAbs: document.getElementById("spreadAbs"),
    statusDot: document.getElementById("statusDot"),
    statusText: document.getElementById("statusText"),
    updatedAt: document.getElementById("updatedAt"),
    alertThreshold: document.getElementById("alertThreshold"),
    alertSound: document.getElementById("alertSound"),
    alertHint: document.getElementById("alertHint"),
    chart: document.getElementById("chart"),
    tooltip: document.getElementById("chartTooltip"),
    chartMeta: document.getElementById("chartMeta"),
    emailAlertStatus: document.getElementById("emailAlertStatus"),
    emailAlertTo: document.getElementById("emailAlertTo"),
    emailRuleLow: document.getElementById("emailRuleLow"),
    emailRuleMid: document.getElementById("emailRuleMid"),
    emailRuleHigh: document.getElementById("emailRuleHigh"),
  };

  const state = {
    skhy: null,
    skhynix: null,
    history: [],
    range: "7d",
    ws: null,
    reconnectTimer: null,
    alertLatched: false,
    lastPushAt: 0,
    hoverIndex: -1,
    layout: null,
  };

  const fmt = (n, digits = 2) =>
    n == null || Number.isNaN(n)
      ? "—"
      : n.toLocaleString("en-US", {
          minimumFractionDigits: digits,
          maximumFractionDigits: digits,
        });

  const fmtPct = (n) =>
    n == null || Number.isNaN(n)
      ? "—"
      : `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`;

  const fmtTime = (ts, withDate = false) => {
    const d = new Date(ts);
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    if (!withDate) return `${hh}:${mm}`;
    const mo = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${mo}-${day} ${hh}:${mm}`;
  };

  function setStatus(kind, text) {
    el.statusDot.className = `dot ${kind}`;
    el.statusText.textContent = text;
  }

  function compute() {
    if (state.skhy == null || state.skhynix == null) return null;
    const ten = state.skhy * 10;
    const spread = state.skhynix - ten;
    const premium = (ten / state.skhynix - 1) * 100;
    return { ten, spread, premium };
  }

  function trimHistory(now = Date.now()) {
    const windowMs = RANGE_MS[state.range] || RANGE_MS["7d"];
    const cut = now - windowMs;
    while (state.history.length && state.history[0].t < cut) {
      state.history.shift();
    }
  }

  function pushHistory(point, force = false) {
    const now = point.t;
    const gap = LIVE_GAP_MS[state.range] || 5000;
    if (!force && now - state.lastPushAt < gap) return;
    state.lastPushAt = now;
    const last = state.history[state.history.length - 1];
    if (last && Math.abs(last.t - point.t) < 500) {
      state.history[state.history.length - 1] = point;
    } else {
      state.history.push(point);
    }
    trimHistory(now);
  }

  function renderQuotes() {
    const c = compute();
    el.skhy.textContent = fmt(state.skhy);
    el.tenSkhy.textContent = c ? fmt(c.ten) : "—";
    el.skhynix.textContent = fmt(state.skhynix);
    if (!c) return null;

    el.premium.textContent = fmtPct(c.premium);
    el.premium.classList.toggle("hot", c.premium >= 0);
    el.premium.classList.toggle("cold", c.premium < 0);
    el.spreadAbs.textContent = `差价 ${fmt(c.spread)} USDT（SKHYNIX − 10×SKHY）`;
    el.updatedAt.textContent = `更新 ${fmtTime(Date.now())}`;
    checkAlert(c.premium);
    return c;
  }

  function render() {
    const c = renderQuotes();
    if (!c) return;
    pushHistory({
      t: Date.now(),
      premium: c.premium,
      skhy: state.skhy,
      skhynix: state.skhynix,
      spread: c.spread,
    });
    drawChart();
  }

  function checkAlert(premium) {
    const threshold = Number(el.alertThreshold.value) || 0;
    const triggered = Math.abs(premium) >= threshold;
    if (triggered) {
      el.alertHint.textContent = `告警：|溢价| ${Math.abs(premium).toFixed(2)}% ≥ ${threshold}%`;
      el.alertHint.classList.add("alert");
      if (!state.alertLatched && el.alertSound.checked) beep();
      state.alertLatched = true;
    } else {
      el.alertHint.textContent = "未触发告警";
      el.alertHint.classList.remove("alert");
      state.alertLatched = false;
    }
  }

  function beep() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "square";
      osc.frequency.value = 880;
      gain.gain.value = 0.04;
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      setTimeout(() => {
        osc.stop();
        ctx.close();
      }, 180);
    } catch (_) {
      /* ignore */
    }
  }

  function niceTimeTicks(t0, t1, count = 5) {
    const span = Math.max(t1 - t0, 60_000);
    const stepCandidates = [
      60_000, 2 * 60_000, 5 * 60_000, 10 * 60_000, 15 * 60_000, 30 * 60_000,
      60 * 60_000, 2 * 60 * 60_000, 3 * 60 * 60_000, 4 * 60 * 60_000, 6 * 60 * 60_000,
    ];
    const target = span / Math.max(count - 1, 1);
    let step = stepCandidates[stepCandidates.length - 1];
    for (const s of stepCandidates) {
      if (s >= target) {
        step = s;
        break;
      }
    }
    const first = Math.ceil(t0 / step) * step;
    const ticks = [];
    for (let t = first; t <= t1; t += step) ticks.push(t);
    if (!ticks.length) ticks.push(t0, t1);
    return ticks;
  }

  function drawChart() {
    const canvas = el.chart;
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth || 960;
    const cssH = 320;
    canvas.width = Math.floor(cssW * dpr);
    canvas.height = Math.floor(cssH * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const w = cssW;
    const h = cssH;
    const pad = { t: 18, r: 18, b: 42, l: 54 };
    const plotW = w - pad.l - pad.r;
    const plotH = h - pad.t - pad.b;

    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#f7fafc";
    ctx.fillRect(0, 0, w, h);

    const pts = state.history;
    if (pts.length < 2) {
      state.layout = null;
      ctx.fillStyle = "#5f7385";
      ctx.font = "12px IBM Plex Mono, monospace";
      ctx.textAlign = "left";
      ctx.fillText("加载历史行情中…", pad.l, h / 2);
      return;
    }

    const t0 = pts[0].t;
    const t1 = pts[pts.length - 1].t;
    const tSpan = Math.max(t1 - t0, 1);

    let min = Math.min(...pts.map((p) => p.premium));
    let max = Math.max(...pts.map((p) => p.premium));
    if (min === max) {
      min -= 0.5;
      max += 0.5;
    }
    const padY = (max - min) * 0.12 || 0.2;
    min -= padY;
    max += padY;
    const ySpan = max - min;

    const xAt = (t) => pad.l + ((t - t0) / tSpan) * plotW;
    const yAt = (v) => pad.t + ((max - v) / ySpan) * plotH;

    state.layout = { pad, plotW, plotH, t0, t1, tSpan, min, max, ySpan, xAt, yAt, w, h };

    // Y grid + labels
    ctx.strokeStyle = "rgba(15,26,36,0.08)";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = pad.t + (plotH * i) / 4;
      ctx.beginPath();
      ctx.moveTo(pad.l, y);
      ctx.lineTo(w - pad.r, y);
      ctx.stroke();
      const val = max - (ySpan * i) / 4;
      ctx.fillStyle = "#5f7385";
      ctx.font = "11px IBM Plex Mono, monospace";
      ctx.textAlign = "right";
      ctx.fillText(`${val.toFixed(2)}%`, pad.l - 8, y + 4);
    }

    // X time grid + labels
    const showDate =
      state.range === "24h" || state.range === "7d" || tSpan > 6 * 60 * 60 * 1000;
    const ticks = niceTimeTicks(t0, t1, Math.max(4, Math.floor(plotW / 110)));
    ctx.textAlign = "center";
    ticks.forEach((t) => {
      const x = xAt(t);
      if (x < pad.l - 1 || x > w - pad.r + 1) return;
      ctx.strokeStyle = "rgba(15,26,36,0.06)";
      ctx.beginPath();
      ctx.moveTo(x, pad.t);
      ctx.lineTo(x, pad.t + plotH);
      ctx.stroke();
      ctx.fillStyle = "#5f7385";
      ctx.font = "11px IBM Plex Mono, monospace";
      ctx.fillText(fmtTime(t, showDate), x, pad.t + plotH + 22);
    });

    // Axis baseline
    ctx.strokeStyle = "rgba(15,26,36,0.22)";
    ctx.beginPath();
    ctx.moveTo(pad.l, pad.t + plotH);
    ctx.lineTo(w - pad.r, pad.t + plotH);
    ctx.stroke();

    // Area + line by time
    const grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + plotH);
    grad.addColorStop(0, "rgba(13,143,122,0.28)");
    grad.addColorStop(1, "rgba(13,143,122,0.02)");

    ctx.beginPath();
    pts.forEach((p, i) => {
      const x = xAt(p.t);
      const y = yAt(p.premium);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.lineTo(xAt(pts[pts.length - 1].t), pad.t + plotH);
    ctx.lineTo(xAt(pts[0].t), pad.t + plotH);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    ctx.beginPath();
    pts.forEach((p, i) => {
      const x = xAt(p.t);
      const y = yAt(p.premium);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = "#0d8f7a";
    ctx.lineWidth = 2;
    ctx.stroke();

    const last = pts[pts.length - 1];
    ctx.beginPath();
    ctx.arc(xAt(last.t), yAt(last.premium), 3.5, 0, Math.PI * 2);
    ctx.fillStyle = "#0f1a24";
    ctx.fill();

    // Hover marker
    if (state.hoverIndex >= 0 && state.hoverIndex < pts.length) {
      const hp = pts[state.hoverIndex];
      const hx = xAt(hp.t);
      const hy = yAt(hp.premium);
      ctx.strokeStyle = "rgba(15,26,36,0.35)";
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(hx, pad.t);
      ctx.lineTo(hx, pad.t + plotH);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.arc(hx, hy, 4.5, 0, Math.PI * 2);
      ctx.fillStyle = "#c45c1a";
      ctx.fill();
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }

    const firstLabel = fmtTime(t0, showDate);
    const lastLabel = fmtTime(t1, showDate);
    el.chartMeta.textContent = `X 轴：时间（${firstLabel} → ${lastLabel}） · Y 轴：ADR 溢价% · ${pts.length} 点`;
  }

  function nearestIndex(ts) {
    const pts = state.history;
    if (!pts.length) return -1;
    let best = 0;
    let bestDist = Math.abs(pts[0].t - ts);
    for (let i = 1; i < pts.length; i++) {
      const d = Math.abs(pts[i].t - ts);
      if (d < bestDist) {
        best = i;
        bestDist = d;
      }
    }
    return best;
  }

  function onChartMove(ev) {
    if (!state.layout || state.history.length < 2) return;
    const rect = el.chart.getBoundingClientRect();
    const x = ev.clientX - rect.left;
    const { pad, plotW, t0, tSpan, xAt } = state.layout;
    if (x < pad.l || x > pad.l + plotW) {
      hideTooltip();
      return;
    }
    const t = t0 + ((x - pad.l) / plotW) * tSpan;
    const idx = nearestIndex(t);
    state.hoverIndex = idx;
    const p = state.history[idx];
    const showDate = state.range === "24h" || state.range === "7d";
    el.tooltip.hidden = false;
    el.tooltip.innerHTML = `<div>${fmtTime(p.t, showDate)}</div><strong>${fmtPct(p.premium)}</strong>`;
    const tipX = Math.min(Math.max(xAt(p.t), 60), rect.width - 60);
    el.tooltip.style.left = `${tipX}px`;
    el.tooltip.style.top = `${state.layout.yAt(p.premium)}px`;
    drawChart();
  }

  function hideTooltip() {
    if (state.hoverIndex === -1 && el.tooltip.hidden) return;
    state.hoverIndex = -1;
    el.tooltip.hidden = true;
    drawChart();
  }

  function onTicker(symbol, price) {
    if (symbol === "SKHYUSDT") state.skhy = price;
    if (symbol === "SKHYNIXUSDT") state.skhynix = price;
    render();
  }

  function connect() {
    if (state.ws) {
      try {
        state.ws.close();
      } catch (_) {
        /* ignore */
      }
    }

    setStatus("", "连接中…");
    const ws = new WebSocket(WS_URL);
    state.ws = ws;

    ws.onopen = () => setStatus("live", "WebSocket 实时");
    ws.onclose = () => {
      setStatus("err", "WS 断开 · REST 兜底中");
      clearTimeout(state.reconnectTimer);
      state.reconnectTimer = setTimeout(connect, 2000);
    };
    ws.onerror = () => setStatus("err", "WS 异常 · REST 兜底中");
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        const payload = msg.data || msg;
        const symbol = payload.s;
        const price = Number(payload.c);
        if (symbol && Number.isFinite(price)) {
          onTicker(symbol, price);
          setStatus("live", "WebSocket 实时");
        }
      } catch (_) {
        /* ignore malformed */
      }
    };
  }

  async function seedFromRest() {
    try {
      const res = await fetch("/api/prices", { cache: "no-store" });
      if (!res.ok) return false;
      const data = await res.json();
      if (data.skhy != null) state.skhy = Number(data.skhy);
      if (data.skhynix != null) state.skhynix = Number(data.skhynix);
      const c = renderQuotes();
      if (c) {
        pushHistory(
          {
            t: Date.now(),
            premium: c.premium,
            skhy: state.skhy,
            skhynix: state.skhynix,
            spread: c.spread,
          },
          true
        );
        drawChart();
      }
      return state.skhy != null && state.skhynix != null;
    } catch (_) {
      return false;
    }
  }

  async function loadHistory(range) {
    state.range = range;
    document.querySelectorAll(".range-btn").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.range === range);
    });
    try {
      const res = await fetch(`/api/history?range=${encodeURIComponent(range)}`, {
        cache: "no-store",
      });
      if (!res.ok) throw new Error("history failed");
      const data = await res.json();
      state.history = (data.points || []).map((p) => ({
        t: Number(p.t),
        premium: Number(p.premium),
        skhy: Number(p.skhy),
        skhynix: Number(p.skhynix),
        spread: Number(p.spread),
      }));
      if (state.history.length) {
        state.lastPushAt = state.history[state.history.length - 1].t;
      }
      hideTooltip();
      drawChart();
      return true;
    } catch (_) {
      el.chartMeta.textContent = "历史行情加载失败，将仅显示实时点";
      return false;
    }
  }

  async function refreshEmailStatus() {
    if (!el.emailAlertStatus) return;
    try {
      const res = await fetch("/api/alert-status", { cache: "no-store" });
      if (!res.ok) throw new Error("status failed");
      const s = await res.json();
      const low = Number(s.low_threshold ?? 40);
      const high = Number(s.high_threshold ?? 47);
      const premNum = s.last_premium == null ? null : Number(s.last_premium);
      const prem = premNum == null ? "—" : `${premNum.toFixed(2)}%`;

      if (el.emailRuleLow) el.emailRuleLow.textContent = `< ${low}%`;
      if (el.emailRuleHigh) el.emailRuleHigh.textContent = `> ${high}%`;
      if (el.emailRuleMid) el.emailRuleMid.textContent = `${low}% – ${high}%`;
      if (el.emailAlertTo) {
        el.emailAlertTo.textContent = s.to
          ? `收件：${s.to}`
          : "收件人未配置";
      }

      document.querySelectorAll(".email-rule").forEach((node) => {
        node.classList.remove("is-active");
      });
      if (premNum != null) {
        if (premNum < low) {
          document.querySelector(".email-rule.low")?.classList.add("is-active");
        } else if (premNum > high) {
          document.querySelector(".email-rule.high")?.classList.add("is-active");
        } else {
          document.querySelector(".email-rule.mid")?.classList.add("is-active");
        }
      }

      if (!s.enabled) {
        el.emailAlertStatus.className = "email-alert-status warn";
        el.emailAlertStatus.textContent = "邮件警报已关闭";
        return;
      }
      if (!s.smtp_configured) {
        el.emailAlertStatus.className = "email-alert-status warn";
        el.emailAlertStatus.textContent =
          `规则已启用，但尚未填写 SMTP 授权码 · 当前溢价 ${prem}`;
        return;
      }
      el.emailAlertStatus.className = "email-alert-status ok";
      let zone = "安全区间";
      if (premNum != null) {
        if (premNum < low) zone = "偏低报警区";
        else if (premNum > high) zone = "偏高报警区";
      }
      el.emailAlertStatus.textContent =
        `监控中 · 当前溢价 ${prem}（${zone}）` +
        (s.last_error ? ` · 最近错误：${s.last_error}` : "");
    } catch (_) {
      el.emailAlertStatus.className = "email-alert-status warn";
      el.emailAlertStatus.textContent = "无法读取邮件警报状态";
    }
  }

  setInterval(() => {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) return;
    seedFromRest();
  }, 10000);

  // REST is only a fallback; keep it infrequent to avoid Binance IP bans.
  setInterval(seedFromRest, 60000);
  setInterval(refreshEmailStatus, 15000);

  window.addEventListener("resize", drawChart);
  el.chart.addEventListener("mousemove", onChartMove);
  el.chart.addEventListener("mouseleave", hideTooltip);

  document.querySelectorAll(".range-btn").forEach((btn) => {
    btn.addEventListener("click", () => loadHistory(btn.dataset.range));
  });

  el.alertThreshold.addEventListener("change", () => {
    state.alertLatched = false;
    const c = compute();
    if (c) checkAlert(c.premium);
  });

  Promise.all([loadHistory(state.range), seedFromRest(), refreshEmailStatus()]).then(
    ([histOk, priceOk]) => {
      if (priceOk && !(state.ws && state.ws.readyState === WebSocket.OPEN)) {
        setStatus("live", "REST 已同步");
      }
      if (!histOk) drawChart();
    }
  );
  connect();
})();
