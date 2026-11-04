"""Facts for the Morning Brief (~7 AM PT) and the Deep Dive (~11 AM PT).
The after-the-bell wrap lives in facts.build(). Same rule everywhere: every number is
computed and formatted here, never by the writer."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config
from .facts import (PREFERRED_SOURCES, _earnings_fact, _recent, dedupe_headlines, fmt_big, fmt_pct,
                    fmt_price, next_business_day, recently_featured)

NY = ZoneInfo("America/New_York")


def prev_business_day(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _row(q: dict) -> dict:
    return {"symbol": q["symbol"], "name": config.COMPANY_NAMES.get(q["symbol"], q["symbol"]),
            "price": fmt_price(q["price"]), "change_pct": fmt_pct(q["change_pct"]),
            "up": q["change_pct"] >= 0, "_raw_pct": q["change_pct"]}


def _indexes(qmap: dict) -> tuple[list[dict], str]:
    idx = [{"symbol": s, "name": n, "price": fmt_price(qmap[s]["price"]),
            "change_pct": fmt_pct(qmap[s]["change_pct"]), "up": qmap[s]["change_pct"] >= 0,
            "_raw_pct": qmap[s]["change_pct"]} for s, n in config.INDEXES.items() if s in qmap]
    ups = sum(i["up"] for i in idx)
    return idx, ("up" if idx and ups == len(idx) else "down" if ups == 0 else "mixed")


def live_session(quotes: list[dict], today: date) -> bool:
    """True when index quotes come from today's session after the open (not a holiday or stale feed)."""
    opened = datetime.combine(today, time(9, 30), NY)
    n = 0
    for q in quotes:
        if q["symbol"] in config.INDEXES and q.get("quote_time"):
            if datetime.fromisoformat(q["quote_time"]) >= opened:
                n += 1
    return n >= 3


def _headlines(items: list[dict], n: int) -> list[dict]:
    heads = sorted(items, key=lambda h: (h["source"].lower() not in PREFERRED_SOURCES, h.get("published_at") or ""))
    return [{"headline": h["headline"], "source": h["source"], "url": h.get("url")} for h in dedupe_headlines(heads)[:n]]


def _fresh_reports(earnings: list[dict], today: date) -> list[dict]:
    """Results that came out since yesterday's close: last night after the bell, or this morning."""
    prev = prev_business_day(today).isoformat()
    return [e for e in earnings if e.get("eps_actual") is not None and (
        (e["report_date"] == today.isoformat() and e.get("hour") != "amc")
        or (e["report_date"] == prev and e.get("hour") == "amc"))]


def in_session(today: date, now: datetime) -> bool:
    """Morning Brief and Deep Dive describe a market that is open right now."""
    t = now.astimezone(NY)
    return t.date() == today and time(9, 30) <= t.time() < time(16, 0)


# ---------- Morning Brief ----------
def build_morning(quotes, company_news, market_news, filings, earnings, today: date, now: datetime) -> dict:
    qmap = {q["symbol"]: q for q in quotes}
    indexes, tone = _indexes(qmap)
    stocks = sorted((q for q in quotes if q["symbol"] in config.WATCHLIST and q.get("change_pct") is not None),
                    key=lambda q: q["change_pct"], reverse=True)

    reactions = []
    for e in _fresh_reports(earnings, today):
        q = qmap.get(e["symbol"])
        if q and q.get("change_pct") is not None:
            news = [n for n in company_news if n.get("symbol") == e["symbol"]]
            reactions.append({**_row(q), "earnings": _earnings_fact(e),
                              "when": "last night after the close" if e.get("hour") == "amc" else "this morning",
                              "headlines": _headlines(_recent(news, 20, now), 2)})
    reactions.sort(key=lambda r: -abs(r["_raw_pct"]))

    on_deck = [e for e in earnings if e["report_date"] == today.isoformat() and e.get("eps_actual") is None]
    on_deck.sort(key=lambda e: (e["symbol"] not in config.WATCHLIST, -(e.get("revenue_estimate") or 0)))

    return {
        "format": "morning",
        "date": today.isoformat(),
        "date_label": today.strftime("%a, %b %-d, %Y"),
        "session": "early trading",
        "market_tone": tone,
        "indexes": indexes,
        "early_gainers": [_row(q) for q in stocks[:3] if q["change_pct"] > 0],
        "early_losers": [_row(q) for q in reversed(stocks[-3:]) if q["change_pct"] < 0],
        "earnings_reactions": reactions[:3],
        "reporting_today": [{
            "symbol": e["symbol"], "name": config.COMPANY_NAMES.get(e["symbol"], e["symbol"]),
            "when": "after the close" if e.get("hour") == "amc" else "during the session",
            "eps_estimate": f"${e['eps_estimate']:.2f}" if e.get("eps_estimate") is not None else None,
            "revenue_estimate": fmt_big(e.get("revenue_estimate")),
        } for e in on_deck[:4]],
        "overnight_headlines": [{"headline": n["headline"], "source": n["source"]} for n in dedupe_headlines(
            sorted(_recent(market_news, 16, now), key=lambda n: n.get("published_at") or "", reverse=True))[:4]],
        "spotlight": None,
        "_live": live_session(quotes, today) and in_session(today, now),
    }


# ---------- Deep Dive ----------
def build_dive(quotes, company_news, market_news, filings, earnings, today: date, now: datetime,
               avoid: set[str] | None = None) -> dict:
    avoid = recently_featured(days=2) if avoid is None else avoid
    qmap = {q["symbol"]: q for q in quotes}
    indexes, tone = _indexes(qmap)
    stocks = [q for q in quotes if q["symbol"] in config.WATCHLIST and q.get("change_pct") is not None]
    news_by = {}
    for n in _recent(company_news, 30, now):
        news_by.setdefault(n.get("symbol"), []).append(n)
    filings_by = {}
    for f in filings:
        filings_by.setdefault(f["symbol"], []).append(f)

    reports = {e["symbol"]: e for e in _fresh_reports(earnings, today) if e["symbol"] in qmap}
    if reports:  # earnings days: the biggest reaction among companies that just reported
        sym = max(reports, key=lambda s: abs(qmap[s]["change_pct"]) * (0.5 if s in avoid else 1))
        kind = "earnings"
    elif stocks:  # otherwise the biggest move that the news explains
        def score(q):
            s = q["symbol"]
            sc = abs(q["change_pct"]) * (1 + 0.7 * bool(news_by.get(s)) + 0.4 * bool(filings_by.get(s)))
            return sc * (0.4 if s in avoid else 1)
        sym, kind = max(stocks, key=score)["symbol"], "news"
    else:
        sym, kind = None, None

    subject = None
    if sym:
        q = qmap[sym]
        subject = {
            **_row(q),
            "kind": kind,
            "change": f"{q['change']:+,.2f}" if q.get("change") is not None else None,
            "day_range": f"{fmt_price(q['low'])} – {fmt_price(q['high'])}" if q.get("low") else None,
            "prev_close": fmt_price(q["prev_close"]) if q.get("prev_close") else None,
            "earnings": _earnings_fact(reports[sym]) if sym in reports else None,
            "headlines": _headlines(news_by.get(sym, []), 5),
            "filings": [{"form": f["form"], "what": f["item_labels"], "url": f["url"]} for f in filings_by.get(sym, [])[:3]],
        }
    return {
        "format": "dive",
        "date": today.isoformat(),
        "date_label": today.strftime("%a, %b %-d, %Y"),
        "session": "midday",
        "market_tone": tone,
        "indexes": indexes,
        "spotlight": subject,
        "_live": live_session(quotes, today) and in_session(today, now),
    }
