"""Morning Brief and Deep Dive: facts, rendering, guards and the morning veto window."""
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from tickerdaily import config, formats, pipeline, storage, writer
from tickerdaily import facts as factsmod
from tickerdaily.render.render import render_html
from tickerdaily.validator import validate

FIX = json.loads((Path(__file__).parent / "fixtures" / "sample_day.json").read_text())
TODAY, NOW = date(2026, 10, 30), datetime.fromisoformat(FIX["now"])
ARGS = dict(quotes=FIX["quotes"], company_news=FIX["company_news"], market_news=FIX["market_news"],
            filings=FIX["filings"], earnings=FIX["earnings"], today=TODAY, now=NOW)


def test_morning_facts():
    f = formats.build_morning(**ARGS)
    assert [r["symbol"] for r in f["earnings_reactions"]] == ["AMZN"]  # reported last night after the close
    assert f["earnings_reactions"][0]["when"] == "last night after the close"
    assert f["early_gainers"][0]["symbol"] == "AMZN" and f["early_losers"][0]["symbol"] == "INTC"
    assert f["_live"] is False  # fixture quotes carry no timestamp


def test_dive_prefers_fresh_earnings_then_news():
    f = formats.build_dive(**ARGS, avoid=set())
    assert f["spotlight"]["symbol"] == "AMZN" and f["spotlight"]["kind"] == "earnings"
    no_earn = formats.build_dive(**{**ARGS, "earnings": []}, avoid=set())
    assert no_earn["spotlight"]["kind"] == "news" and no_earn["spotlight"]["symbol"] == "AMZN"
    avoided = formats.build_dive(**{**ARGS, "earnings": []}, avoid={"AMZN"})
    assert avoided["spotlight"]["symbol"] != "AMZN"


def test_live_session_detection():
    t = lambda h, m: datetime(2026, 10, 30, h, m, tzinfo=timezone.utc).isoformat()
    q = [{"symbol": s, "quote_time": t(14, 5)} for s in config.INDEXES]   # 10:05 ET
    assert formats.live_session(q, TODAY)
    q = [{"symbol": s, "quote_time": t(13, 0)} for s in config.INDEXES]   # 9:00 ET, before the open
    assert not formats.live_session(q, TODAY)


def _post(fmt):
    return pipeline.stub_post(factsmod.public_view(
        formats.build_dive(**ARGS, avoid=set()) if fmt == "dive" else formats.build_morning(**ARGS)), fmt)


@pytest.mark.parametrize("fmt,ctx,slides", [("morning", "x", 4), ("dive", "x", 5), ("dive", "", 4)])
def test_templates_render(fmt, ctx, slides):
    f = formats.build_dive(**ARGS, avoid=set()) if fmt == "dive" else formats.build_morning(**ARGS)
    f["memory"] = {}
    html = render_html(f, dict(_post(fmt), context_line=ctx), fmt)
    assert html.count('class="slide') == slides and "Not financial advice" in html


def test_dive_context_slide_uses_memory():
    f = formats.build_dive(**ARGS, avoid=set())
    f["memory"] = {"earnings_track_record": {"last_quarters": [{"quarter": "Q3 2026", "result": "beat"}],
                                             "quarters_checked": 1, "beats": 1},
                   "price_range": {"week52_high": "$236.00", "week52_low": "$161.40", "_pos": 0.9,
                                   "from_52w_high": "-1.92%"}}
    html = render_html(f, _post("dive"), "dive")
    assert html.count('class="slide') == 5 and "52-week range" in html and "left:90.0%" in html


def test_schemas_differ_per_format_and_validator_works_on_morning():
    assert "why_points" in writer.SCHEMAS["dive"]["properties"]
    assert "open_line" in writer.SCHEMAS["morning"]["properties"]
    assert "early" in writer.system_prompt("morning").lower()
    f = factsmod.public_view(formats.build_morning(**ARGS))
    post = dict(_post("morning"), open_line="The Nasdaq 100 rose 1.14% early.", caption="Hi\nNot financial advice.")
    assert validate(post, f).ok
    assert not validate(dict(post, open_line="The Nasdaq 100 jumped 2.5% early."), f).ok


def test_freshness_windows_per_format():
    utc = lambda *a: datetime(*a, tzinfo=timezone.utc)
    assert pipeline.still_fresh("2026-11-02", utc(2026, 11, 2, 16, 59), fmt="morning")      # 11:59 ET
    assert not pipeline.still_fresh("2026-11-02", utc(2026, 11, 2, 17, 1), fmt="morning")
    assert pipeline.still_fresh("2026-11-02", utc(2026, 11, 2, 20, 59), fmt="dive")         # 3:59 PM ET
    assert not pipeline.still_fresh("2026-11-02", utc(2026, 11, 2, 21, 1), fmt="dive")
    assert pipeline.fmt_of("2026-11-02-morning-123456") == "morning" and pipeline.fmt_of("2026-11-02-wrap-1") == "wrap"


# ---------- guards and veto window against a fake Instagram ----------
from tests.test_instagram import URLS, FakeIG  # noqa: E402
from tickerdaily import instagram  # noqa: E402


@pytest.fixture
def ig(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LOCAL_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "R2_ACCOUNT_ID", "")
    monkeypatch.setattr(config, "IG_USER_ID", "1")
    monkeypatch.setattr(config, "IG_ACCESS_TOKEN", "TOKEN")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "market_date", lambda now=None: date(2026, 11, 2))
    monkeypatch.setattr(pipeline, "still_fresh", lambda d, now=None, **kw: True)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    fake = FakeIG()
    monkeypatch.setattr(instagram.requests, "request", fake)
    monkeypatch.setattr(instagram.time, "sleep", lambda s: None)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return fake


def _rec(fmt, n=1, auto=False):
    pid = f"2026-11-02-{fmt}-00000{n}"
    rec = {"post_id": pid, "format": fmt, "auto_publish": auto, "facts": {"date": "2026-11-02", "spotlight": None},
           "images": URLS, "caption_full": "x\nNot financial advice.", "post": {"hook": "h"}}
    storage.save_post_record(rec, "2026-11-02")
    return rec


def _publishes(ig):
    return sum(1 for c in ig.calls if c[1].endswith("/media_publish"))


def test_one_post_per_format_per_day(ig):
    pipeline.publish(_rec("wrap"))
    pipeline.publish(_rec("morning"))
    pipeline.publish(_rec("dive"))
    pipeline.publish(_rec("morning", 2))  # second morning draft: blocked
    assert _publishes(ig) == 3


def test_autopublish_without_veto(ig):
    _rec("morning", auto=True)
    pipeline.autopublish("2026-11-02-morning-000001", 0)
    assert _publishes(ig) == 1


def test_autopublish_vetoed(ig):
    _rec("morning", auto=True)
    storage.save_decision({"post_id": "2026-11-02-morning-000001", "action": "reject"}, "2026-11-02")
    pipeline.autopublish("2026-11-02-morning-000001", 0)
    assert _publishes(ig) == 0


def test_autopublish_respects_github_tap(ig, monkeypatch):
    _rec("morning", auto=True)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")

    class R:
        ok = True
        def json(self):
            return {"workflow_runs": [{"display_title": "reject 2026-11-02-morning-000001"}]}
    monkeypatch.setattr("requests.get", lambda *a, **k: R())
    pipeline.autopublish("2026-11-02-morning-000001", 0)
    assert _publishes(ig) == 0


def test_non_auto_draft_never_autopublishes(ig):
    _rec("morning", auto=False)
    pipeline.autopublish("2026-11-02-morning-000001", 0)
    assert _publishes(ig) == 0


def test_telegram_texts():
    from tickerdaily import telegram
    rec = {"post_id": "2026-11-02-morning-000001", "format": "morning", "attempts": [{}],
           "facts": formats.build_morning(**ARGS), "caption_full": "c", "post": {}}
    t = telegram.review_text(rec, auto=True, veto_minutes=10)
    assert "Morning Brief posts automatically in 10 min" in t and "Spotlight" not in t
    assert telegram.veto_button(rec["post_id"])["inline_keyboard"][0][0]["callback_data"].startswith("reject|")


def test_wrap_survives_morning_records(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOCAL_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "R2_ACCOUNT_ID", "")
    storage.save_post_record({"post_id": f"{date.today()}-morning-1", "facts": {"spotlight": None}}, str(date.today()))
    assert factsmod.recently_featured() == set()


def test_intraday_formats_need_an_open_market():
    t = lambda d, h, m: datetime(2026, 11, d, h, m, tzinfo=timezone.utc)
    assert formats.in_session(date(2026, 11, 2), t(2, 14, 50))       # Mon 9:50 AM ET
    assert not formats.in_session(date(2026, 11, 2), t(3, 4, 0))     # Mon 11 PM ET: closing data
    assert not formats.in_session(date(2026, 11, 2), t(2, 21, 30))   # after the close
