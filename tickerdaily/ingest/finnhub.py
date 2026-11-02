"""Finnhub: quotes, market news, company news, earnings calendar.
Free tier allows ~60 calls/minute, so every call is spaced out."""
from __future__ import annotations

import hashlib
import time
from datetime import date, datetime, timedelta, timezone

import requests

from .. import config

BASE = "https://finnhub.io/api/v1"
_MIN_INTERVAL = 1.05
_last_call = 0.0


def _get(path: str, **params):
    global _last_call
    params["token"] = config.FINNHUB_API_KEY
    for attempt in range(4):
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
        r = requests.get(f"{BASE}{path}", params=params, timeout=20)
        if r.status_code == 429:
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code == 403:  # endpoint not on this plan
            return None
        r.raise_for_status()
        return r.json()
    return None


def _news_id(url: str, headline: str) -> str:
    return hashlib.sha1((url or headline).encode()).hexdigest()[:16]


def quotes(symbols: list[str]) -> list[dict]:
    out = []
    for s in symbols:
        q = _get("/quote", symbol=s)
        if not q or not q.get("c"):  # c == 0 means unknown symbol
            continue
        out.append({
            "symbol": s,
            "price": q["c"], "change": q["d"], "change_pct": q["dp"],
            "high": q["h"], "low": q["l"], "open": q["o"], "prev_close": q["pc"],
            "quote_time": datetime.fromtimestamp(q["t"], timezone.utc).isoformat() if q.get("t") else None,
            "payload": q,
        })
    return out


def _news_row(n: dict, symbol: str | None) -> dict:
    return {
        "news_id": _news_id(n.get("url", ""), n.get("headline", "")),
        "symbol": symbol or n.get("related") or None,
        "headline": n.get("headline", ""),
        "summary": n.get("summary", ""),
        "source": n.get("source", ""),
        "url": n.get("url", ""),
        "category": n.get("category", ""),
        "published_at": datetime.fromtimestamp(n["datetime"], timezone.utc).isoformat() if n.get("datetime") else None,
        "payload": n,
    }


def market_news() -> list[dict]:
    return [_news_row(n, None) for n in (_get("/news", category="general") or [])]


def company_news(symbols: list[str], days: int = 2) -> list[dict]:
    today = config.market_date()
    frm = (today - timedelta(days=days)).isoformat()
    out, seen = [], set()
    for s in symbols:
        for n in (_get("/company-news", symbol=s, **{"from": frm, "to": today.isoformat()}) or [])[:15]:
            row = _news_row(n, s)
            if row["news_id"] not in seen:
                seen.add(row["news_id"])
                out.append(row)
    return out


def earnings_calendar(days_ahead: int = 7) -> list[dict]:
    today = config.market_date()
    data = _get("/calendar/earnings", **{"from": today.isoformat(),
                                         "to": (today + timedelta(days=days_ahead)).isoformat()}) or {}
    out = []
    for e in data.get("earningsCalendar", []):
        out.append({
            "symbol": e.get("symbol"), "report_date": e.get("date"), "hour": e.get("hour"),
            "eps_estimate": e.get("epsEstimate"), "eps_actual": e.get("epsActual"),
            "revenue_estimate": e.get("revenueEstimate"), "revenue_actual": e.get("revenueActual"),
            "quarter": e.get("quarter"), "year": e.get("year"), "payload": e,
        })
    return out
