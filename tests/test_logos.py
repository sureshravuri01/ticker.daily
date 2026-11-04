"""Logos: image checks, caching (including misses), symbol collection and the monogram fallback."""
import io
import json
from datetime import date, datetime
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from tickerdaily import config, formats, logos, pipeline
from tickerdaily import facts as factsmod
from tickerdaily.render.render import render_html

FIX = json.loads((Path(__file__).parent / "fixtures" / "sample_day.json").read_text())


def _png(size=200, blank=False, transparent=False):
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0) if transparent else (255, 255, 255, 255))
    if not blank and not transparent:
        m = size // 10
        ImageDraw.Draw(im).ellipse((m, m, size - m, size - m), fill=(255, 120, 0, 255))
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def test_normalise_accepts_real_and_rejects_junk():
    assert logos.normalise(_png()) is not None
    assert logos.normalise(_png(size=20)) is None             # too small
    assert logos.normalise(_png(blank=True)) is None          # flat white square
    assert logos.normalise(_png(transparent=True)) is None    # fully transparent
    assert logos.normalise(b"<html>not an image</html>") is None


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LOCAL_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "R2_ACCOUNT_ID", "")
    calls = []

    def download(sym):
        calls.append(sym)
        return (logos.normalise(_png()), "ok") if sym == "AMZN" else (None, "no logo in company profile")
    monkeypatch.setattr(logos, "_download", download)
    return calls


def test_cache_hits_and_cached_misses(store):
    assert logos.get("AMZN").startswith("data:image/png;base64,")
    assert logos.get("AMZN")                     # second time from cache
    assert logos.get("XYZ") is None
    assert logos.get("XYZ") is None              # miss is cached: not re-requested
    assert store == ["AMZN", "XYZ"]


def test_symbols_exclude_indexes_and_offline_never_fetches(store):
    f = factsmod.build(FIX["quotes"], FIX["company_news"], FIX["market_news"], FIX["filings"], FIX["earnings"],
                       today=date(2026, 10, 30), now=datetime.fromisoformat(FIX["now"]), avoid=set())
    syms = logos.symbols_in(f)
    assert "AMZN" in syms and not set(syms) & set(config.INDEXES)
    assert logos.for_facts(f, allow_fetch=False) == {} and store == []


def test_template_logo_or_monogram():
    f = formats.build_dive(FIX["quotes"], FIX["company_news"], FIX["market_news"], FIX["filings"], FIX["earnings"],
                           today=date(2026, 10, 30), now=datetime.fromisoformat(FIX["now"]), avoid=set())
    f["memory"] = {}
    post = pipeline.stub_post(factsmod.public_view(f), "dive")
    with_logo = render_html(f, post, "dive", {"AMZN": "data:image/png;base64,AAAA"})
    assert 'src="data:image/png;base64,AAAA"' in with_logo
    assert 'class="lg mono"' in render_html(f, post, "dive", {})


def test_errors_retry_sooner_than_true_misses(store, monkeypatch):
    from datetime import timedelta, timezone
    logos.storage.put_json("logos/AMZN.json", {"symbol": "AMZN", "status": "missing", "detail": "error: timeout",
                                               "checked_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()})
    assert logos.get("AMZN")  # retried after a transient error
    logos.storage.put_json("logos/XYZ.json", {"symbol": "XYZ", "status": "missing", "detail": "no logo in company profile",
                                              "checked_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()})
    assert logos.get("XYZ") is None and store == ["AMZN"]
