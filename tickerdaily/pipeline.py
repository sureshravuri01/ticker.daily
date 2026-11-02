"""Orchestrator.

    python -m tickerdaily ingest            snapshot everything into the data lake
    python -m tickerdaily draft             ingest + build facts + write + validate + render
    python -m tickerdaily draft --fixture tests/fixtures/sample_day.json --no-llm   offline test
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from . import config, facts as factsmod, storage


def ingest(day: str, rid: str) -> dict:
    from .ingest import edgar, finnhub

    if not config.FINNHUB_API_KEY:
        sys.exit("FINNHUB_API_KEY is not set")
    symbols = list(config.INDEXES) + config.WATCHLIST
    data = {}
    print(f"[ingest] quotes for {len(symbols)} symbols…", flush=True)
    data["quotes"] = finnhub.quotes(symbols)
    print("[ingest] market news…", flush=True)
    data["market_news"] = finnhub.market_news()
    print("[ingest] company news…", flush=True)
    data["company_news"] = finnhub.company_news(config.WATCHLIST)
    print("[ingest] earnings calendar…", flush=True)
    data["earnings"] = finnhub.earnings_calendar()
    print("[ingest] SEC filings…", flush=True)
    try:
        data["filings"] = edgar.recent_filings(config.WATCHLIST)
    except Exception as e:  # EDGAR hiccups shouldn't kill the day's post
        print(f"[ingest] EDGAR failed: {e}", flush=True)
        data["filings"] = []
    for name, rows in data.items():
        storage.save_records(name, rows, rid, day)
        print(f"[ingest] saved {len(rows):>4} {name}", flush=True)
    return data


def _strip_payload(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if k != "payload"} for r in rows]


def stub_post(f: dict) -> dict:
    """Deterministic placeholder copy, used for offline rendering tests."""
    s = f["spotlight"]
    return {
        "hook": f"{s['name']} stole the show today",
        "cover_sub": "The moves, the reasons, and what's next.",
        "wrap_line": "A quick look at where the major indexes finished.",
        "spotlight_title": f"Why {s['symbol']} moved",
        "spotlight_why": "Placeholder text. In a real run, Claude writes this from the day's headlines and filings.",
        "movers_line": "Placeholder text for the movers slide.",
        "next_line": "Placeholder text for what to watch next.",
        "caption": "Placeholder caption.\nNot financial advice.",
        "hashtags": ["stocks", "stockmarket", "investing"],
    }


def draft(fixture: str | None, use_llm: bool) -> Path:
    rid = storage.run_id()
    if fixture:
        raw = json.loads(Path(fixture).read_text())
        today = date.fromisoformat(raw["date"])
        now = datetime.fromisoformat(raw["now"])
    else:
        today, now = date.today(), datetime.now(timezone.utc)
        print(f"[draft] synced {storage.sync_recent_posts()} recent post records", flush=True)
        raw = ingest(today.isoformat(), rid)
    day = today.isoformat()

    f = factsmod.build(
        quotes=raw["quotes"], company_news=_strip_payload(raw["company_news"]),
        market_news=_strip_payload(raw["market_news"]), filings=raw["filings"],
        earnings=raw["earnings"], today=today, now=now,
    )
    if not f["spotlight"] or not f["indexes"]:
        sys.exit("Not enough market data to build a post (market holiday or API problem).")
    public = factsmod.public_view(f)

    attempts: list[dict] = []
    if use_llm:
        from . import writer
        if not config.ANTHROPIC_API_KEY:
            sys.exit("ANTHROPIC_API_KEY is not set")
        post, attempts = writer.write(public)
    else:
        post = stub_post(public)

    from .render.render import render_pngs

    post_id = f"{day}-wrap-{rid[-7:-1]}"
    out_dir = config.OUTPUT_DIR / day / post_id
    pngs = render_pngs(f, post, out_dir)
    image_urls = [storage.upload(p, f"posts/{day}/{post_id}/{p.name}", config.R2_MEDIA_BUCKET) for p in pngs]

    full_caption = post["caption"] + "\n\n" + " ".join("#" + h for h in post.get("hashtags", []))
    (out_dir / "caption.txt").write_text(full_caption)
    record = {
        "post_id": post_id, "created_at": datetime.now(timezone.utc).isoformat(), "run_id": rid,
        "status": "draft", "format": "after_bell_carousel", "model": config.MODEL if use_llm else "stub",
        "schema_version": config.SCHEMA_VERSION,
        "facts": f, "post": post, "caption_full": full_caption, "attempts": attempts,
        "images": image_urls,
        # Filled in by later phases:
        "approval": None, "published": None, "metrics": [],
    }
    storage.save_post_record(record, day)
    print(f"[draft] {post_id}: {len(pngs)} slides → {out_dir}", flush=True)
    for u in image_urls:
        print("   ", u)
    return out_dir


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tickerdaily")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest")
    d = sub.add_parser("draft")
    d.add_argument("--fixture")
    d.add_argument("--no-llm", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "ingest":
        ingest(date.today().isoformat(), storage.run_id())
    else:
        draft(a.fixture, use_llm=not a.no_llm)


if __name__ == "__main__":
    main()
