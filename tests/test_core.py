import json
from datetime import date, datetime
from pathlib import Path

from tickerdaily import facts as factsmod
from tickerdaily.render.render import render_html
from tickerdaily.validator import extract_numbers, validate

FIX = json.loads((Path(__file__).parent / "fixtures" / "sample_day.json").read_text())


def build():
    return factsmod.build(FIX["quotes"], FIX["company_news"], FIX["market_news"], FIX["filings"],
                          FIX["earnings"], today=date.fromisoformat(FIX["date"]),
                          now=datetime.fromisoformat(FIX["now"]), avoid=set())


def good_post(**over):
    p = {
        "hook": "Amazon's cloud just woke up",
        "cover_sub": "AMZN jumped +9.86% after earnings.",
        "wrap_line": "The Nasdaq 100 led with a 1.14% gain while the Dow slipped 0.21%.",
        "spotlight_title": "AWS did the heavy lifting",
        "spotlight_why": "Amazon earned $1.95 a share versus $1.58 expected, on $180.2B of revenue.",
        "movers_line": "Chips split: SMCI rose 7.1% while Intel fell 6.4%.",
        "next_line": "AMD and Palantir report Monday after the close.",
        "caption": "Amazon ripped +9.86% on a clean Q3 beat.\nNot financial advice.",
        "hashtags": ["stocks", "amazon"],
    }
    p.update(over)
    return p


def test_spotlight_picks_explained_big_mover():
    f = build()
    assert f["spotlight"]["symbol"] == "AMZN"
    assert f["spotlight"]["earnings"]["eps_result"] == "beat"
    assert f["market_tone"] == "mixed"


def test_duplicate_headlines_removed():
    heads = [h["headline"] for h in build()["spotlight"]["headlines"]]
    assert len(heads) == 2  # the Yahoo rewrite of the Reuters story is dropped


def test_next_up_prefers_watchlist():
    syms = [e["symbol"] for e in build()["next_up"]]
    assert syms[:2] == ["AMD", "PLTR"]


def test_valid_post_passes_including_rounding():
    f = factsmod.public_view(build())
    r = validate(good_post(), f)
    assert r.ok, r.errors


def test_invented_number_rejected():
    f = factsmod.public_view(build())
    r = validate(good_post(spotlight_why="Shares hit $250 intraday as volume tripled."), f)
    assert not r.ok and any("$250" in e for e in r.errors)


def test_wrong_percentage_rejected():
    f = factsmod.public_view(build())
    assert not validate(good_post(cover_sub="AMZN jumped 12% today."), f).ok


def test_advice_and_disclaimer_rules():
    f = factsmod.public_view(build())
    assert not validate(good_post(next_line="Time to buy the dip on Intel."), f).ok
    assert not validate(good_post(caption="Big day for Amazon."), f).ok


def test_number_parsing():
    nums = {n.raw: (n.value, n.unit) for n in extract_numbers("Up +9.86% to $1,234.50 on $180.2B (2 billion)")}
    assert nums["+9.86%"] == (9.86, "%")
    assert nums["$1,234.50"] == (1234.5, "")
    assert nums["$180.2B"] == (180.2, "B")
    assert nums["2 billion"] == (2.0, "B")


def test_template_renders():
    html = render_html(build(), good_post())
    assert html.count('class="slide"') == 5
    assert "AMZN" in html and "Not financial advice" in html


def test_counts_from_facts_allowed():
    f = factsmod.public_view(build())
    b = f["breadth"]
    r = validate(good_post(wrap_line=f"{b['down']} of the {b['total']} names we track fell."), f)
    assert r.ok, r.errors


def test_telegram_review_and_buttons():
    from tickerdaily import telegram

    rec = {"post_id": "2026-10-30-wrap-123456", "facts": build(), "attempts": [{}, {}],
           "caption_full": "Amazon <3 AWS\nNot financial advice.\n\n#stocks", "images": []}
    txt = telegram.review_text(rec)
    assert "AMZN" in txt and "after 2 tries" in txt and "&lt;3" in txt  # HTML-escaped
    for row in telegram.buttons(rec["post_id"])["inline_keyboard"]:
        for b in row:
            assert len(b["callback_data"].encode()) <= 64
