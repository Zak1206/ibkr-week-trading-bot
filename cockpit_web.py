"""
Serveur web local pour le cockpit live (JSON mis a jour par main.py a chaque cycle).
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

COCKPIT_LIVE_FILE_DEFAULT = "cockpit_live.json"
# Incrémenter si le HTML/JS du dashboard change (force repérage cache navigateur).
COCKPIT_WEB_BUILD_ID = "2026-09-30-paliers"

_SERVER_STARTED = False
_SERVER_LOCK = threading.Lock()
_LIVE_API_CACHE: dict = {}
_LIVE_API_CACHE_AT = 0.0
_LIVE_API_CACHE_MTIME = 0.0
_LIVE_API_CACHE_LOCK = threading.Lock()
_PRICE_ENRICHER: Optional[Callable[[dict], dict]] = None
_ENRICH_IN_PROGRESS = threading.Lock()


def register_cockpit_price_enricher(fn: Callable[[dict], dict]) -> None:
    """Enregistre l'enrichissement prix (depuis main(), evite import main dans les threads HTTP)."""
    global _PRICE_ENRICHER
    _PRICE_ENRICHER = fn


def cockpit_web_enabled() -> bool:
    return os.getenv("COCKPIT_WEB_ENABLED", "0").strip().lower() in {"1", "true", "yes", "y"}


def cockpit_live_file() -> str:
    return (
        os.getenv("COCKPIT_LIVE_FILE", COCKPIT_LIVE_FILE_DEFAULT).strip()
        or COCKPIT_LIVE_FILE_DEFAULT
    )


def write_cockpit_state(data: dict) -> None:
    path = cockpit_live_file()
    tmp = f"{path}.tmp"
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
    os.replace(tmp, path)


def cockpit_live_prices_enabled() -> bool:
    return os.getenv("COCKPIT_WEB_LIVE_PRICES", "1").strip().lower() in {"1", "true", "yes", "y"}


def _cockpit_file_mtime() -> float:
    path = cockpit_live_file()
    try:
        return os.path.getmtime(path) if os.path.exists(path) else 0.0
    except OSError:
        return 0.0


def _read_cockpit_state() -> dict:
    path = cockpit_live_file()
    if not os.path.exists(path):
        return {"positions": [], "summary": {}, "updated_at_utc": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {"positions": [], "summary": {}, "error": "lecture cockpit_live.json impossible"}


def _api_cockpit_payload() -> dict:
    data = _read_cockpit_state()
    if not cockpit_live_prices_enabled():
        return data
    # Prix deja rafraichis par le thread principal (fichier a jour) ?
    prices_at = data.get("prices_updated_at_utc")
    if data.get("prices_live") and prices_at:
        try:
            refresh_sec = float(data.get("live_price_refresh_sec") or os.getenv("COCKPIT_WEB_PRICE_REFRESH_SEC", "5"))
        except (TypeError, ValueError):
            refresh_sec = 5.0
        refresh_sec = max(2.0, refresh_sec)
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            ts = datetime.strptime(str(prices_at), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=ZoneInfo("UTC"))
            if (datetime.now(ZoneInfo("UTC")) - ts).total_seconds() < refresh_sec + 1.0:
                return data
        except Exception:
            pass
    if _PRICE_ENRICHER is None:
        return data
    try:
        refresh_sec = float(data.get("live_price_refresh_sec") or os.getenv("COCKPIT_WEB_PRICE_REFRESH_SEC", "5"))
    except (TypeError, ValueError):
        refresh_sec = 5.0
    refresh_sec = max(2.0, refresh_sec)
    mtime = _cockpit_file_mtime()
    now = time.time()
    with _LIVE_API_CACHE_LOCK:
        global _LIVE_API_CACHE, _LIVE_API_CACHE_AT, _LIVE_API_CACHE_MTIME
        if (
            _LIVE_API_CACHE
            and _LIVE_API_CACHE_MTIME == mtime
            and (now - _LIVE_API_CACHE_AT) < refresh_sec
        ):
            return _LIVE_API_CACHE
    if not _ENRICH_IN_PROGRESS.acquire(blocking=False):
        return _LIVE_API_CACHE if _LIVE_API_CACHE else data
    try:
        enriched = _PRICE_ENRICHER(data)
    except Exception as exc:
        enriched = dict(data)
        enriched["prices_live_error"] = str(exc)[:160]
    finally:
        _ENRICH_IN_PROGRESS.release()
    with _LIVE_API_CACHE_LOCK:
        _LIVE_API_CACHE = enriched
        _LIVE_API_CACHE_AT = now
        _LIVE_API_CACHE_MTIME = mtime
    return enriched


def start_cockpit_web_server() -> None:
    global _SERVER_STARTED
    with _SERVER_LOCK:
        if _SERVER_STARTED:
            return
        try:
            preferred = int(os.getenv("COCKPIT_WEB_PORT", "8765"))
        except ValueError:
            preferred = 8765
        host = os.getenv("COCKPIT_WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
        # Ports de secours: WinError 10013 = port reserve / bloque (pas seulement "occupe").
        ports = [preferred]
        for alt in (8766, 8767, 8770, 18765):
            if alt not in ports:
                ports.append(alt)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args) -> None:
                if os.getenv("COCKPIT_WEB_QUIET", "1").strip().lower() in {"1", "true", "yes", "y"}:
                    return
                super().log_message(fmt, *args)

            def _send_json(self, payload: dict, status: int = 200) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                if path in {"/", "/index.html"}:
                    html = _build_cockpit_html().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                    self.send_header("Pragma", "no-cache")
                    self.send_header("Expires", "0")
                    self.send_header("X-Cockpit-Build", COCKPIT_WEB_BUILD_ID)
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html)
                    return
                if path in {"/api/cockpit", "/api/cockpit.json"}:
                    self._send_json(_api_cockpit_payload())
                    return
                self.send_error(404)

        last_err: Optional[BaseException] = None
        for port in ports:
            try:
                httpd = ThreadingHTTPServer((host, port), Handler)
                thread = threading.Thread(target=httpd.serve_forever, name="cockpit-web", daemon=True)
                thread.start()
                _SERVER_STARTED = True
                url = f"http://{host}:{port}/"
                if port != preferred:
                    print(
                        f"[Cockpit Web] Port {preferred} inaccessible — fallback {port}."
                    )
                print(f"[Cockpit Web] Live dashboard: {url} (build {COCKPIT_WEB_BUILD_ID})")
                return
            except OSError as exc:
                last_err = exc
                print(f"[Cockpit Web] Bind {host}:{port} refuse: {exc}")
        print(
            f"[Cockpit Web] Impossible de demarrer le dashboard ({last_err}). "
            "Le bot continue sans UI web — cockpit_live.json reste a jour."
        )


def _build_cockpit_html() -> str:
    return _COCKPIT_HTML_TEMPLATE.replace("__COCKPIT_BUILD__", COCKPIT_WEB_BUILD_ID)


_COCKPIT_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate"/>
  <meta http-equiv="Pragma" content="no-cache"/>
  <meta http-equiv="Expires" content="0"/>
  <title>Cockpit Live — Trading Bot</title>
  <style>
    :root {
      --bg: #0f1419;
      --card: #1a2332;
      --border: #2d3a4f;
      --text: #e7ecf3;
      --muted: #8b9cb3;
      --green: #3dd68c;
      --red: #f07178;
      --amber: #ffcc66;
      --accent: #61afef;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      padding: 1rem 1.25rem 2rem;
    }
    header {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: 0.75rem 1.5rem;
      margin-bottom: 1.25rem;
    }
    h1 { margin: 0; font-size: 1.35rem; font-weight: 600; }
    .meta { color: var(--muted); font-size: 0.85rem; }
    .meta strong { color: var(--text); }
    .badge {
      display: inline-block;
      padding: 0.15rem 0.5rem;
      border-radius: 4px;
      font-size: 0.75rem;
      font-weight: 600;
    }
    .badge-open { background: #1e3a2f; color: var(--green); }
    .badge-closed { background: #3a2a1e; color: var(--amber); }
    .badge-live { background: #1e2d4a; color: var(--accent); }
    .cards {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 0.75rem;
      margin-bottom: 1.25rem;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.75rem 1rem;
    }
    .card label { display: block; font-size: 0.72rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
    .card .val { font-size: 1.15rem; font-weight: 600; margin-top: 0.25rem; }
    .card .val.pos { color: var(--green); }
    .card .val.neg { color: var(--red); }
    table {
      width: 100%;
      border-collapse: collapse;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
      font-size: 0.9rem;
    }
    th, td { padding: 0.6rem 0.75rem; text-align: left; border-bottom: 1px solid var(--border); }
    th { background: #15202b; color: var(--muted); font-weight: 500; font-size: 0.75rem; text-transform: uppercase; }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: rgba(97, 175, 239, 0.06); }
    .tk { font-weight: 600; color: var(--accent); }
    .pnl-pos { color: var(--green); }
    .pnl-neg { color: var(--red); }
    .bar-wrap { min-width: 100px; }
    .bar-bg { height: 6px; background: #2d3a4f; border-radius: 3px; overflow: hidden; margin-top: 4px; }
    .bar-sl { height: 100%; background: var(--red); border-radius: 3px; }
    .bar-tp { height: 100%; background: var(--green); border-radius: 3px; }
    .tp-breach { color: var(--amber); font-weight: 600; }
    .sub { display: block; font-size: 0.72rem; color: var(--muted); margin-top: 2px; }
    .empty { text-align: center; padding: 2rem; color: var(--muted); }
    .err { color: var(--red); margin-top: 0.5rem; font-size: 0.85rem; }
    @media (max-width: 900px) {
      table { font-size: 0.8rem; }
      th:nth-child(n+6), td:nth-child(n+6) { display: none; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Cockpit Live <span class="meta" style="font-weight:400;font-size:0.85rem">(__COCKPIT_BUILD__)</span></h1>
    <span id="marketBadge" class="badge badge-open">—</span>
    <span id="liveBadge" class="badge badge-live" style="display:none">Prix live</span>
    <span class="meta">Cycle <strong id="cycleNum">—</strong> · portefeuille <strong id="updatedAt">—</strong></span>
    <span class="meta" id="pxMeta" style="display:none">Prix <strong id="pricesAt">—</strong></span>
    <span class="meta">Prochain cycle ~<strong id="scanMin">15</strong> min</span>
  </header>
  <div class="cards" id="summaryCards"></div>
  <table>
    <thead>
      <tr>
        <th>Ticker</th>
        <th>Prix</th>
        <th>Entrée</th>
        <th>Investi</th>
        <th>PnL latent</th>
        <th>SL</th>
        <th>TP</th>
        <th>→ SL</th>
        <th>→ TP</th>
        <th>IBKR</th>
      </tr>
    </thead>
    <tbody id="posBody"><tr><td colspan="10" class="empty">Chargement…</td></tr></tbody>
  </table>
  <p id="err" class="err"></p>
  <script>
    (function ensureFreshCockpitUi() {
      const build = "__COCKPIT_BUILD__";
      const key = "cockpit_build_seen";
      try {
        if (localStorage.getItem(key) !== build) {
          localStorage.setItem(key, build);
          if (location.search.indexOf("fresh=") < 0) {
            location.replace(location.pathname + "?fresh=" + encodeURIComponent(build) + location.hash);
          }
        }
      } catch (e) { /* private mode */ }
      const ths = document.querySelectorAll("thead th");
      let hasInvesti = false;
      ths.forEach(h => { if ((h.textContent || "").trim() === "Investi") hasInvesti = true; });
      if (!hasInvesti) {
        location.replace(location.pathname + "?reload=" + Date.now());
      }
    })();
    function fmtUsd(n, d=2) {
      if (n == null || isNaN(n)) return "—";
      const s = n >= 0 ? "+" : "";
      return s + n.toFixed(d) + " $";
    }
    function fmtPct(n) {
      if (n == null || isNaN(n)) return "—";
      const s = n >= 0 ? "+" : "";
      return s + n.toFixed(2) + "%";
    }
    function pnlClass(v) { return v >= 0 ? "pnl-pos" : "pnl-neg"; }
    function investHtml(p) {
      const n = p.notional_usd;
      if (n == null || isNaN(n)) return "—";
      let html = n.toFixed(2) + " $";
      const parts = [];
      if (p.qty != null && !isNaN(p.qty)) parts.push(Number(p.qty).toLocaleString("fr-FR", {maximumFractionDigits: 4}) + " act.");
      if (p.pct_budget != null && !isNaN(p.pct_budget)) parts.push(p.pct_budget.toFixed(1) + "% budget");
      if (parts.length) html += '<span class="sub">' + parts.join(" · ") + '</span>';
      return html;
    }
    function barHtml(pct, kind) {
      if (pct == null || isNaN(pct)) return "—";
      const w = Math.min(100, Math.max(0, Math.abs(pct)));
      const cls = kind === "tp" ? "bar-tp" : "bar-sl";
      return `<div class="bar-wrap">${fmtPct(pct)}<div class="bar-bg"><div class="${cls}" style="width:${w}%"></div></div></div>`;
    }
    function render(data) {
      const err = document.getElementById("err");
      err.textContent = data.error || "";
      document.getElementById("cycleNum").textContent = data.cycle_idx ?? "—";
      document.getElementById("updatedAt").textContent = data.updated_at_ny || "—";
      document.getElementById("scanMin").textContent = data.scan_interval_min ?? "15";
      const liveOn = data.live_prices_enabled !== false && data.prices_live;
      const liveBadge = document.getElementById("liveBadge");
      const pxMeta = document.getElementById("pxMeta");
      if (liveOn) {
        liveBadge.style.display = "inline-block";
        pxMeta.style.display = "inline";
        document.getElementById("pricesAt").textContent = data.prices_updated_at_ny || "—";
      } else {
        liveBadge.style.display = data.live_prices_enabled === false ? "none" : liveBadge.style.display;
        if (!data.prices_updated_at_ny) pxMeta.style.display = "none";
      }
      const mb = document.getElementById("marketBadge");
      if (data.market_open) {
        mb.textContent = "Marché ouvert";
        mb.className = "badge badge-open";
      } else {
        mb.textContent = "Marché fermé";
        mb.className = "badge badge-closed";
      }
      const s = data.summary || {};
      const pnl = s.realized_pnl_usd;
      const latent = s.unrealized_pnl_usd;
      const latentPct = s.unrealized_pnl_pct;
      let latentVal = "—";
      let latentCls = "";
      if (latent != null && !isNaN(latent)) {
        latentVal = fmtUsd(latent);
        if (latentPct != null && !isNaN(latentPct)) {
          latentVal += " (" + fmtPct(latentPct) + ")";
        }
        latentCls = latent >= 0 ? "pos" : "neg";
      }
      const cards = [];
      const sNet = s.strategy_realized_net_est_usd;
      if (sNet != null && !isNaN(sNet)) {
        const since = s.strategy_since ? String(s.strategy_since).slice(8, 10) + "/" + String(s.strategy_since).slice(5, 7) : "";
        cards.push(["PnL stratégie depuis " + since + " (net est.)",
          fmtUsd(sNet) + '<span class="sub">' + (s.strategy_trades ?? 0) + " trades · brut " + fmtUsd(s.strategy_realized_usd) + "</span>",
          sNet >= 0 ? "pos" : "neg"]);
      }
      cards.push(["PnL latent", latentVal, latentCls]);
      if (s.line_target_usd != null) {
        const k = s.line_step_k;
        cards.push(["Taille de ligne", s.line_target_usd.toFixed(0) + " $" +
          (k != null ? '<span class="sub">palier ' + (k > 0 ? "+" : "") + k + " (x1.25 par palier)</span>" : ""), ""]);
      }
      cards.push(["Slots", (s.slots_used ?? 0) + "/" + (s.max_slots ?? 0), ""]);
      cards.push(["Déployé", (s.deployed_usd ?? 0).toFixed(2) + " $", ""]);
      cards.push(["Budget bot", (s.budget_effective_usd ?? 0).toFixed(2) + " $" +
        '<span class="sub">1000 $ + cumul brut (≠ cash IBKR)</span>', ""]);
      cards.push(["Cumul brut depuis mai", fmtUsd(pnl) + '<span class="sub">toutes configs, hors frais</span>',
        pnl >= 0 ? "pos" : "neg"]);
      if (s.breaker_limit_pct != null) {
        const dd = s.breaker_dd_pct ?? 0;
        cards.push(["Coupe-circuit", dd.toFixed(1) + "% / -" + s.breaker_limit_pct + "%",
          dd <= -0.8 * s.breaker_limit_pct ? "neg" : ""]);
      }
      if (s.pdt_limit != null) {
        const n = s.pdt_day_trades_5d ?? 0;
        cards.push(["Day trades 5 j (PDT)", n + "/" + s.pdt_limit, n >= s.pdt_limit ? "neg" : ""]);
      }
      document.getElementById("summaryCards").innerHTML = cards.map(([l,v,c]) =>
        `<div class="card"><label>${l}</label><div class="val ${c}">${v}</div></div>`
      ).join("");
      const rows = data.positions || [];
      const tbody = document.getElementById("posBody");
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="10" class="empty">Aucune position ouverte</td></tr>';
        return;
      }
      tbody.innerHTML = rows.map(p => {
        const tpCls = (p.dist_tp_pct != null && p.dist_tp_pct < 0) ? "tp-breach" : "";
        const ibkr = p.ibkr_sl_tp_active ? "SL/TP" : "—";
        return `<tr>
          <td class="tk">${p.ticker}</td>
          <td>${p.price_usd != null ? p.price_usd.toFixed(2) : "—"}</td>
          <td>${p.entry_usd != null ? p.entry_usd.toFixed(2) : "—"}</td>
          <td>${investHtml(p)}</td>
          <td class="${pnlClass(p.pnl_pct||0)}">${fmtUsd(p.pnl_usd)} (${fmtPct(p.pnl_pct)})</td>
          <td>${p.stop_loss != null ? p.stop_loss.toFixed(2) : "—"}</td>
          <td>${p.take_profit != null ? p.take_profit.toFixed(2) : "—"}</td>
          <td>${barHtml(p.dist_sl_pct, "sl")}</td>
          <td class="${tpCls}">${p.dist_tp_pct != null && p.dist_tp_pct < 0 ? "TP dépassé " + fmtPct(p.dist_tp_pct) : barHtml(p.dist_tp_pct, "tp")}</td>
          <td>${ibkr}</td>
        </tr>`;
      }).join("");
    }
    let pollTimer = null;
    function schedulePoll(data) {
      if (pollTimer) clearInterval(pollTimer);
      const sec = (data && data.live_price_refresh_sec) ? Number(data.live_price_refresh_sec) : 5;
      const ms = Math.max(2000, Math.round(sec * 1000));
      pollTimer = setInterval(poll, ms);
    }
    async function poll() {
      try {
        const r = await fetch("/api/cockpit.json?_=" + Date.now());
        const data = await r.json();
        render(data);
        if (!pollTimer) schedulePoll(data);
      } catch (e) {
        document.getElementById("err").textContent = "Connexion API: " + e.message;
      }
    }
    poll();
  </script>
</body>
</html>
"""
