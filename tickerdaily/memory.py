"""Memory: historical context for today's post, so it says more than "X moved Y%".

Two sources:
  * Finnhub (works from day one): the spotlight stock's earnings track record and 52-week range.
  * Our own archive (grows every day): streaks, and what happened the session after we last
    spotlighted the same stock.

Everything here is formatted in code. The writer may quote it, and the validator only accepts
numbers that appear in these facts. Each piece is optional: missing data just means less context.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from . import config, storage
from .facts import fmt_pct, fmt_price, next_business_day

INDEX_KEY = "state/spotlight_index.json"
QUOTES_PREFIX = "raw/quotes/"


# ---------- archive: one closing snapshot per trading day ----------
def _read_rows(key: str) -> list[dict]:
    path = storage.fetch(key)
    if path is None:
        return []
    import pandas as pd

    df = pd.read_parquet(path) if key.endswith(".parquet") else pd.read_json(path, lines=True)
    return df.drop(columns=["payload"], errors="ignore").to_dict("records")


def daily_closes(before: date, lookback_days: int = 45) -> dict[str, dict[str, dict]]:
    """{ 'YYYY-MM-DD': {symbol: {price, change_pct}} } using the last snapshot of each day."""
    oldest = (before - timedelta(days=lookback_days)).isoformat()
    latest_per_day: dict[str, str] = {}
    for key in storage.list_keys(QUOTES_PREFIX):
        part = key.split("/")[2] if key.count("/") >= 3 else ""
        if not part.startswith("dt="):
            continue
        day = part[3:]
        if oldest <= day < before.isoformat() and key > latest_per_day.get(day, ""):
            latest_per_day[day] = key  # run ids sort chronologically
    out = {}
    for day, key in sorted(latest_per_day.items()):
        rows = {r["symbol"]: {"price": r.get("price"), "change_pct": r.get("change_pct")}
                for r in _read_rows(key) if r.get("symbol") and r.get("change_pct") is not None}
        if rows:
            out[day] = rows
    return out


def streak(symbol: str, today_pct: float, today: date, closes: dict) -> int:
    """Consecutive sessions moving the same direction as today, including today.
    Stops at any gap in the archive, so it never overstates."""
    if not today_pct:
        return 0
    up, n, d = today_pct > 0, 1, today
    while True:
        prev = next((x for x in sorted(closes, reverse=True) if x < d.isoformat()), None)
        if prev is None or next_business_day(date.fromisoformat(prev)) != d:
            return n
        pct = (closes[prev].get(symbol) or {}).get("change_pct")
        if pct is None or pct == 0 or (pct > 0) != up:
            return n
        n, d = n + 1, date.fromisoformat(prev)


# ---------- spotlight index: compact log of what we covered each day ----------
def load_index() -> list[dict]:
    idx = storage.get_json(INDEX_KEY)
    if idx is not None:
        return idx.get("events", [])
    # First run: backfill from existing post records.
    events = []
    for key in storage.list_keys("posts/"):
        if not key.endswith(".json"):
            continue
        rec = storage.get_json(key) or {}
        s = (rec.get("facts") or {}).get("spotlight")
        if rec.get("status") == "draft" and s:
            events.append(_event(rec["facts"]["date"], rec["post_id"], s))
    return events


def _event(day: str, post_id: str, s: dict) -> dict:
    return {"date": day, "post_id": post_id, "symbol": s["symbol"],
            "change_pct": s["change_pct"], "raw_pct": s.get("_raw_pct")}


def record_spotlight(day: str, post_id: str, spotlight: dict) -> None:
    events = [e for e in load_index() if e["post_id"] != post_id]
    events.append(_event(day, post_id, spotlight))
    storage.put_json(INDEX_KEY, {"events": events[-2000:]})


def past_spotlights(symbol: str, today: date, closes: dict, events: list[dict], limit: int = 3) -> list[dict]:
    """Earlier days we spotlighted this stock, with the next session's move when we have it."""
    by_day = {}
    for e in events:
        if e["symbol"] != symbol or e["date"] >= today.isoformat():
            continue
        # Cross-check with that day's close: drops entries saved under the wrong date.
        actual = (closes.get(e["date"]) or {}).get(symbol, {}).get("change_pct")
        if actual is not None and e.get("raw_pct") is not None and abs(actual - e["raw_pct"]) > 0.05:
            continue
        by_day[e["date"]] = e  # latest draft of each day wins
    out = []
    for day in sorted(by_day, reverse=True)[:limit]:
        nxt = next_business_day(date.fromisoformat(day)).isoformat()
        follow = (closes.get(nxt) or {}).get(symbol, {}).get("change_pct")
        out.append({
            "date": date.fromisoformat(day).strftime("%b %-d"),
            "move_that_day": by_day[day]["change_pct"],
            "next_session_move": fmt_pct(follow) if follow is not None else None,
        })
    return out


# ---------- Finnhub context for the spotlight ----------
def earnings_record(history: list[dict]) -> dict | None:
    rows = [h for h in history if h.get("eps_estimate") is not None][:4]
    if not rows:
        return None
    quarters = [{
        "quarter": f"Q{h['quarter']} {h['year']}",
        "eps_actual": f"${h['eps_actual']:.2f}",
        "eps_estimate": f"${h['eps_estimate']:.2f}",
        "result": "beat" if h["eps_actual"] > h["eps_estimate"] else
                  "missed" if h["eps_actual"] < h["eps_estimate"] else "matched",
    } for h in rows]
    return {"last_quarters": quarters, "quarters_checked": len(quarters),
            "beats": sum(q["result"] == "beat" for q in quarters)}


def range_position(price: float, rng: dict) -> dict | None:
    hi, lo = rng.get("52WeekHigh"), rng.get("52WeekLow")
    if not hi or not lo or not price:
        return None
    out = {"label": "52-week range", "week52_high": fmt_price(hi), "week52_low": fmt_price(lo)}
    if price >= hi:
        out["status"] = "at or above its 52-week high"
    else:
        out["from_52w_high"] = fmt_pct((price / hi - 1) * 100)
        if price <= lo:
            out["status"] = "at or below its 52-week low"
        elif price >= hi * 0.97:
            out["status"] = "within 3% of its 52-week high"
    return out


# ---------- assemble ----------
def build(facts: dict, quotes: list[dict], use_api: bool = True, closes: dict | None = None,
          events: list[dict] | None = None) -> dict:
    today = date.fromisoformat(facts["date"])
    qmap = {q["symbol"]: q for q in quotes}
    s = facts.get("spotlight") or {}
    sym = s.get("symbol")
    mem: dict = {}

    try:
        closes = daily_closes(today) if closes is None else closes
    except Exception as e:  # memory must never block a post
        print(f"[memory] archive unavailable: {e}", flush=True)
        closes = {}
    mem["archive_days"] = len(closes)

    if sym and sym in qmap:
        n = streak(sym, qmap[sym]["change_pct"], today, closes)
        if n >= 2:
            mem["spotlight_streak"] = {"sessions": n, "direction": "up" if qmap[sym]["change_pct"] > 0 else "down"}
    if "SPY" in qmap:
        n = streak("SPY", qmap["SPY"]["change_pct"], today, closes)
        if n >= 2:
            mem["sp500_streak"] = {"sessions": n, "direction": "up" if qmap["SPY"]["change_pct"] > 0 else "down"}

    if sym:
        try:
            past = past_spotlights(sym, today, closes, load_index() if events is None else events)
        except Exception as e:
            print(f"[memory] index unavailable: {e}", flush=True)
            past = []
        if past:
            mem["previous_spotlights"] = past

    if sym and use_api:
        from .ingest import finnhub
        try:
            rec = earnings_record(finnhub.earnings_history(sym))
            if rec:
                mem["earnings_track_record"] = rec
            if sym in qmap:
                pos = range_position(qmap[sym]["price"], finnhub.price_range(sym))
                if pos:
                    mem["price_range"] = pos
        except Exception as e:
            print(f"[memory] Finnhub context failed: {e}", flush=True)

    print(f"[memory] {json.dumps({k: v for k, v in mem.items() if k != 'archive_days'})[:300]} "
          f"(archive: {mem['archive_days']} days)", flush=True)
    return mem
