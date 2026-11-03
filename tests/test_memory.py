"""Memory: archive streaks, past-spotlight follow-through, Finnhub context, and validator fit."""
import json
from datetime import date
from pathlib import Path

import pytest

from tickerdaily import config, facts as factsmod, memory, storage
from tickerdaily.render.render import render_html
from tickerdaily.validator import validate

FIX = json.loads((Path(__file__).parent / "fixtures" / "sample_day.json").read_text())


@pytest.fixture
def local(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LOCAL_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "R2_ACCOUNT_ID", "")
    return tmp_path


def _write_day(root, day, pcts, runs=("T150000Z", "T221500Z")):
    d = root / "raw" / "quotes" / f"dt={day}"
    d.mkdir(parents=True, exist_ok=True)
    for i, r in enumerate(runs):  # earlier run has junk values; the last run of the day must win
        rows = [{"symbol": s, "price": 100.0, "change_pct": (p if i == len(runs) - 1 else -99.0)}
                for s, p in pcts.items()]
        (d / f"{day.replace('-', '')}{r}.jsonl").write_text("\n".join(json.dumps(x) for x in rows))


def test_closes_use_last_run_and_streaks_stop_at_gaps(local):
    _write_day(local, "2026-10-26", {"SPY": -0.3, "AMZN": 1.0})   # Mon
    _write_day(local, "2026-10-27", {"SPY": 0.4, "AMZN": 2.0})    # Tue
    _write_day(local, "2026-10-28", {"SPY": 0.2, "AMZN": -1.0})   # Wed
    _write_day(local, "2026-10-29", {"SPY": 0.1, "AMZN": 3.0})    # Thu
    closes = memory.daily_closes(date(2026, 10, 30))
    assert closes["2026-10-29"]["SPY"]["change_pct"] == 0.1
    assert memory.streak("SPY", 0.6, date(2026, 10, 30), closes) == 4   # Tue..Fri up
    assert memory.streak("AMZN", 9.9, date(2026, 10, 30), closes) == 2  # Wed was down
    del closes["2026-10-28"]
    assert memory.streak("SPY", 0.6, date(2026, 10, 30), closes) == 2   # gap: don't overstate


def test_past_spotlight_follow_through(local):
    _write_day(local, "2026-10-21", {"AMZN": -2.31})
    events = [{"date": "2026-10-20", "post_id": "a", "symbol": "AMZN", "change_pct": "+4.10%"},
              {"date": "2026-10-20", "post_id": "b", "symbol": "AMZN", "change_pct": "+4.12%"},
              {"date": "2026-10-22", "post_id": "c", "symbol": "NVDA", "change_pct": "+1.00%"}]
    closes = memory.daily_closes(date(2026, 10, 30))
    past = memory.past_spotlights("AMZN", date(2026, 10, 30), closes, events)
    assert past == [{"date": "Oct 20", "move_that_day": "+4.12%", "next_session_move": "-2.31%"}]


def test_index_backfills_and_records(local):
    rec = {"post_id": "2026-10-29-wrap-1", "status": "draft",
           "facts": {"date": "2026-10-29", "spotlight": {"symbol": "INTC", "change_pct": "-6.41%", "_raw_pct": -6.41}}}
    storage.save_post_record(rec, "2026-10-29")
    storage.save_post_record({**rec, "post_id": "2026-10-29-wrap-1-rejected", "status": "rejected"}, "2026-10-29")
    assert [e["symbol"] for e in memory.load_index()] == ["INTC"]
    memory.record_spotlight("2026-10-30", "2026-10-30-wrap-1", {"symbol": "AMZN", "change_pct": "+9.86%"})
    assert [e["symbol"] for e in memory.load_index()] == ["INTC", "AMZN"]


def test_earnings_record_and_range():
    hist = [{"quarter": q, "year": 2026, "eps_actual": a, "eps_estimate": 1.0} for q, a in [(3, 1.2), (2, 0.9), (1, 1.1)]]
    rec = memory.earnings_record(hist)
    assert rec["beats"] == 2 and rec["quarters_checked"] == 3 and rec["last_quarters"][1]["result"] == "missed"
    assert memory.range_position(98.0, {"52WeekHigh": 100.0, "52WeekLow": 50.0})["status"].startswith("within 3%")
    assert memory.range_position(101.0, {"52WeekHigh": 100.0, "52WeekLow": 50.0})["status"].startswith("at or above")
    assert memory.range_position(80.0, {}) is None


def _facts_with_memory(local, monkeypatch):
    from tickerdaily.ingest import finnhub
    monkeypatch.setattr(finnhub, "earnings_history", lambda s: [
        {"quarter": q, "year": 2026, "eps_actual": 1.95 - q / 10, "eps_estimate": 1.5} for q in (3, 2, 1, 0)])
    monkeypatch.setattr(finnhub, "price_range", lambda s: {"52WeekHigh": 236.0, "52WeekLow": 161.4})
    _write_day(local, "2026-10-29", {"SPY": 0.3, "AMZN": 1.0})
    f = factsmod.build(FIX["quotes"], FIX["company_news"], FIX["market_news"], FIX["filings"],
                       FIX["earnings"], today=date(2026, 10, 30),
                       now=__import__("datetime").datetime.fromisoformat(FIX["now"]), avoid=set())
    f["memory"] = memory.build(f, FIX["quotes"], events=[])
    return f


def test_memory_build_and_validator_accepts_memory_numbers(local, monkeypatch):
    f = _facts_with_memory(local, monkeypatch)
    m = f["memory"]
    assert m["earnings_track_record"]["beats"] == 4
    assert m["price_range"]["status"] == "within 3% of its 52-week high"
    assert m["sp500_streak"] == {"sessions": 2, "direction": "up"} and m["spotlight_streak"]["sessions"] == 2
    public = factsmod.public_view(f)
    post = {"hook": "Amazon's cloud woke up", "cover_sub": "AMZN +9.86%", "wrap_line": "Mixed day.",
            "spotlight_title": "AWS did it", "spotlight_why": "EPS of $1.95 beat $1.58.",
            "movers_line": "Chips split.", "next_line": "AMD reports Monday.",
            "context_line": "Amazon has beaten EPS estimates 4 quarters running and sits within 3% of its 52-week high of $236.00.",
            "caption": "Big day.\nNot financial advice.", "hashtags": ["stocks"]}
    assert validate(post, public).ok, validate(post, public).errors
    bad = dict(post, context_line="Amazon has beaten estimates 7 straight quarters.")
    assert not validate(bad, public).ok


def test_memory_never_blocks(local, monkeypatch):
    from tickerdaily.ingest import finnhub

    def boom(*a):
        raise RuntimeError("api down")
    monkeypatch.setattr(finnhub, "earnings_history", boom)
    f = factsmod.build(FIX["quotes"], FIX["company_news"], FIX["market_news"], FIX["filings"],
                       FIX["earnings"], today=date(2026, 10, 30),
                       now=__import__("datetime").datetime.fromisoformat(FIX["now"]), avoid=set())
    assert memory.build(f, FIX["quotes"])["archive_days"] == 0


def test_context_line_renders_only_when_present(local, monkeypatch):
    f = _facts_with_memory(local, monkeypatch)
    base = {"hook": "h", "cover_sub": "c", "wrap_line": "w", "spotlight_title": "t", "spotlight_why": "y",
            "movers_line": "m", "next_line": "n", "caption": "c", "hashtags": []}
    assert 'class="ctx"' in render_html(f, dict(base, context_line="Beat 4 quarters running."))
    assert 'class="ctx"' not in render_html(f, dict(base, context_line=""))
