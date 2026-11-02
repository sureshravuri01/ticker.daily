"""Turns raw ingested data into the day's FACTS: every number that can appear in a post.
Numbers are computed and formatted here, in code. The LLM never does arithmetic."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config

PREFERRED_SOURCES = {"reuters", "bloomberg", "cnbc", "marketwatch", "wsj", "the wall street journal",
                     "financial times", "barron's", "yahoo", "associated press", "seekingalpha"}


# ---------- formatting (the ONLY place numbers get their final shape) ----------
def fmt_pct(x: float) -> str:
    return f"{x:+.2f}%"


def fmt_price(x: float) -> str:
    return f"${x:,.2f}"


def fmt_big(x: float | None) -> str | None:
    if x is None:
        return None
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(x) >= div:
            return f"${x / div:,.1f}{suf}"
    return f"${x:,.0f}"


def next_business_day(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


# ---------- helpers ----------
def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", s.lower())) - {"the", "a", "to", "of", "in", "and", "on", "for", "as", "is"}


def dedupe_headlines(items: list[dict], threshold: float = 0.55) -> list[dict]:
    kept: list[dict] = []
    for it in items:
        t = _tokens(it["headline"])
        if not t:
            continue
        if any(len(t & _tokens(k["headline"])) / len(t | _tokens(k["headline"])) > threshold for k in kept):
            continue
        kept.append(it)
    return kept


def _recent(items: list[dict], hours: int, now: datetime) -> list[dict]:
    cutoff = (now - timedelta(hours=hours)).isoformat()
    return [i for i in items if (i.get("published_at") or "") >= cutoff]


def recently_featured(days: int = 3) -> set[str]:
    """Symbols we spotlighted recently, so the feed doesn't repeat itself."""
    seen = set()
    root = config.LOCAL_DATA_DIR / "posts"
    if not root.exists():
        return seen
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    for p in root.glob("dt=*/*.json"):
        if p.parent.name[3:] >= cutoff:
            try:
                s = json.loads(p.read_text()).get("facts", {}).get("spotlight", {}).get("symbol")
                if s:
                    seen.add(s)
            except (ValueError, OSError):
                pass
    return seen


# ---------- main ----------
def build(quotes: list[dict], company_news: list[dict], market_news: list[dict],
          filings: list[dict], earnings: list[dict], today: date | None = None,
          now: datetime | None = None, avoid: set[str] | None = None) -> dict:
    today = today or date.today()
    now = now or datetime.now(timezone.utc)
    avoid = recently_featured() if avoid is None else avoid
    qmap = {q["symbol"]: q for q in quotes}

    indexes = [{
        "symbol": s, "name": name, "price": fmt_price(qmap[s]["price"]),
        "change_pct": fmt_pct(qmap[s]["change_pct"]), "up": qmap[s]["change_pct"] >= 0,
        "_raw_pct": qmap[s]["change_pct"],
    } for s, name in config.INDEXES.items() if s in qmap]
    ups = sum(1 for i in indexes if i["up"])
    tone = "up" if ups == len(indexes) else "down" if ups == 0 else "mixed"

    stocks = [q for q in quotes if q["symbol"] in config.WATCHLIST and q.get("change_pct") is not None]
    stocks.sort(key=lambda q: q["change_pct"], reverse=True)

    def row(q):
        return {"symbol": q["symbol"], "name": config.COMPANY_NAMES.get(q["symbol"], q["symbol"]),
                "price": fmt_price(q["price"]), "change_pct": fmt_pct(q["change_pct"]),
                "up": q["change_pct"] >= 0, "_raw_pct": q["change_pct"]}

    gainers = [row(q) for q in stocks[:3] if q["change_pct"] > 0]
    losers = [row(q) for q in reversed(stocks[-3:]) if q["change_pct"] < 0]
    breadth = {"up": sum(1 for q in stocks if q["change_pct"] > 0),
               "down": sum(1 for q in stocks if q["change_pct"] < 0), "total": len(stocks)}

    news_by_sym: dict[str, list[dict]] = {}
    for n in _recent(company_news, 30, now):
        news_by_sym.setdefault(n["symbol"], []).append(n)
    filings_by_sym: dict[str, list[dict]] = {}
    for f in filings:
        filings_by_sym.setdefault(f["symbol"], []).append(f)
    reported_today = {e["symbol"] for e in earnings
                      if e["report_date"] in (today.isoformat(), (today - timedelta(days=1)).isoformat())
                      and e.get("eps_actual") is not None}

    # Spotlight score: big move, explained by news/filings/earnings, not featured recently.
    def score(q):
        s = q["symbol"]
        sc = abs(q["change_pct"])
        sc *= 1 + 0.5 * bool(news_by_sym.get(s)) + 0.5 * bool(filings_by_sym.get(s)) + 1.0 * (s in reported_today)
        return sc * (0.5 if s in avoid else 1.0)

    spotlight = None
    if stocks:
        q = max(stocks, key=score)
        s = q["symbol"]
        heads = sorted(news_by_sym.get(s, []),
                       key=lambda n: (n["source"].lower() not in PREFERRED_SOURCES, n.get("published_at") or ""),
                       )
        heads = dedupe_headlines(heads)[:4]
        earn = next((e for e in earnings if e["symbol"] == s and s in reported_today), None)
        spotlight = {
            **row(q),
            "change": f"{q['change']:+,.2f}" if q.get("change") is not None else None,
            "day_range": f"{fmt_price(q['low'])} – {fmt_price(q['high'])}" if q.get("low") else None,
            "headlines": [{"headline": h["headline"], "source": h["source"], "url": h["url"]} for h in heads],
            "filings": [{"form": f["form"], "what": f["item_labels"], "url": f["url"]}
                        for f in filings_by_sym.get(s, [])[:3]],
            "earnings": _earnings_fact(earn) if earn else None,
            "score": round(score(q), 3),
        }

    nxt = next_business_day(today)
    upcoming = [e for e in earnings
                if (e["report_date"] == today.isoformat() and e.get("hour") == "amc" and e.get("eps_actual") is None)
                or e["report_date"] == nxt.isoformat()]
    upcoming.sort(key=lambda e: (e["symbol"] not in config.WATCHLIST, -(e.get("revenue_estimate") or 0)))
    next_up = [{
        "symbol": e["symbol"], "name": config.COMPANY_NAMES.get(e["symbol"], e["symbol"]),
        "when": "tonight after the bell" if e["report_date"] == today.isoformat()
        else f"{nxt.strftime('%a')} {'before the open' if e.get('hour') == 'bmo' else 'after the close' if e.get('hour') == 'amc' else ''}".strip(),
        "eps_estimate": f"${e['eps_estimate']:.2f}" if e.get("eps_estimate") is not None else None,
        "revenue_estimate": fmt_big(e.get("revenue_estimate")),
    } for e in upcoming[:4]]

    top_market = dedupe_headlines(sorted(_recent(market_news, 12, now),
                                         key=lambda n: n.get("published_at") or "", reverse=True))[:5]

    return {
        "date": today.isoformat(),
        "date_label": today.strftime("%a, %b %-d, %Y"),
        "market_tone": tone,
        "indexes": indexes,
        "breadth": breadth,
        "gainers": gainers,
        "losers": losers,
        "spotlight": spotlight,
        "next_up": next_up,
        "market_headlines": [{"headline": n["headline"], "source": n["source"]} for n in top_market],
    }


def _earnings_fact(e: dict) -> dict:
    out = {"quarter": f"Q{e['quarter']} {e['year']}" if e.get("quarter") else None}
    if e.get("eps_actual") is not None and e.get("eps_estimate") is not None:
        out["eps_actual"] = f"${e['eps_actual']:.2f}"
        out["eps_estimate"] = f"${e['eps_estimate']:.2f}"
        out["eps_result"] = "beat" if e["eps_actual"] > e["eps_estimate"] else "missed" if e["eps_actual"] < e["eps_estimate"] else "matched"
    if e.get("revenue_actual") and e.get("revenue_estimate"):
        out["revenue_actual"] = fmt_big(e["revenue_actual"])
        out["revenue_estimate"] = fmt_big(e["revenue_estimate"])
        out["revenue_result"] = "beat" if e["revenue_actual"] > e["revenue_estimate"] else "missed"
    return out


def public_view(facts: dict) -> dict:
    """Facts as shown to the LLM: drop internal fields (raw floats, scores)."""
    def clean(x):
        if isinstance(x, dict):
            return {k: clean(v) for k, v in x.items() if not k.startswith("_") and k != "score"}
        if isinstance(x, list):
            return [clean(v) for v in x]
        return x
    return clean(facts)
