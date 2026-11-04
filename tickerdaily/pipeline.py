"""Orchestrator.

    python -m tickerdaily ingest            snapshot everything into the data lake
    python -m tickerdaily draft [--format wrap|morning|dive]   ingest + facts + write + validate + render
    python -m tickerdaily wait-open | autopublish --post-id ID   morning veto-window flow
    python -m tickerdaily decide --post-id ID --action approve|reject|regen
    python -m tickerdaily refresh-token | insights
    python -m tickerdaily draft --fixture tests/fixtures/sample_day.json --no-llm   offline test
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config, facts as factsmod, storage, telegram


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


FORMATS = {"wrap": "🔔 After the Bell", "morning": "☀️ Morning Brief", "dive": "🔎 Deep Dive"}


def stub_post(f: dict, fmt: str = "wrap") -> dict:
    """Deterministic placeholder copy, used for offline rendering tests."""
    common = {"context_line": "Placeholder context from the archive.",
              "caption": "Placeholder caption.\nNot financial advice.", "hashtags": ["stocks", "stockmarket"]}
    if fmt == "morning":
        return {"hook": "Markets wake up mixed", "cover_sub": "Earnings reactions, early movers and today's calendar.",
                "open_line": "Placeholder: how the market opened.", "earnings_line": "Placeholder: earnings reactions.",
                "movers_line": "Placeholder: early movers.", "watch_line": "Placeholder: what to watch today.", **common}
    s = f["spotlight"]
    if fmt == "dive":
        return {"hook": f"Inside {s['name']}'s big move", "cover_sub": "The numbers, the reasons and the context.",
                "numbers_line": "Placeholder: what the numbers mean.",
                "why_points": ["Placeholder point one.", "Placeholder point two.", "Placeholder point three."],
                "watch_line": "Placeholder: what investors will watch next.", **common}
    return {
        "hook": f"{s['name']} stole the show today",
        "cover_sub": "The moves, the reasons, and what's next.",
        "wrap_line": "A quick look at where the major indexes finished.",
        "spotlight_title": f"Why {s['symbol']} moved",
        "spotlight_why": "Placeholder text. In a real run, Claude writes this from the day's headlines and filings.",
        "movers_line": "Placeholder text for the movers slide.",
        "next_line": "Placeholder text for what to watch next.",
        **common,
    }


def _build_facts(fmt: str, raw: dict, today: date, now: datetime) -> dict:
    from . import formats
    args = dict(quotes=raw["quotes"], company_news=_strip_payload(raw["company_news"]),
                market_news=_strip_payload(raw["market_news"]), filings=raw["filings"],
                earnings=raw["earnings"], today=today, now=now)
    if fmt == "morning":
        return formats.build_morning(**args)
    if fmt == "dive":
        return formats.build_dive(**args)
    f = factsmod.build(**args)
    f["format"] = "wrap"
    f["_live"] = formats.live_session(raw["quotes"], today)
    return f


def _set_output(**kw) -> None:
    """Expose values to later GitHub Actions steps."""
    import os
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as fh:
            for k, v in kw.items():
                fh.write(f"{k}={v}\n")


def draft(fixture: str | None, use_llm: bool, fmt: str = "wrap") -> dict | None:
    rid = storage.run_id()
    if fixture:
        raw = json.loads(Path(fixture).read_text())
        today = date.fromisoformat(raw["date"])
        now = datetime.fromisoformat(raw["now"])
    else:
        today, now = config.market_date(), datetime.now(timezone.utc)
        print(f"[draft] synced {storage.sync_recent_posts()} recent post records", flush=True)
        raw = ingest(today.isoformat(), rid)
    day = today.isoformat()

    f = _build_facts(fmt, raw, today, now)
    if not fixture and not f.get("_live"):
        # Holiday or stale feed: quotes aren't from today's session, so there is nothing true to post.
        msg = f"{FORMATS[fmt]} skipped: no live market data for {f['date_label']} (holiday or data delay)."
        print(f"[draft] {msg}", flush=True)
        telegram.notify(f"💤 {msg}")
        _set_output(post_id="", auto="false")
        return None
    if not f["indexes"] or (fmt != "morning" and not f.get("spotlight")):
        sys.exit("Not enough market data to build a post (market holiday or API problem).")
    from . import memory
    f["memory"] = memory.build(f, raw["quotes"], use_api=not fixture) if f.get("spotlight") else {}
    public = factsmod.public_view(f)

    attempts: list[dict] = []
    if use_llm:
        from . import writer
        if not config.ANTHROPIC_API_KEY:
            sys.exit("ANTHROPIC_API_KEY is not set")
        try:
            post, attempts = writer.write(public, fmt=fmt)
        except writer.DraftRejected as e:
            # Keep failures too: they show which facts/prompts trip the validator.
            storage.save_post_record({
                "post_id": f"{day}-{fmt}-{rid[-7:-1]}-rejected", "created_at": datetime.now(timezone.utc).isoformat(),
                "run_id": rid, "status": "rejected", "format": fmt, "model": config.MODEL,
                "schema_version": config.SCHEMA_VERSION, "facts": f, "attempts": e.attempts,
            }, day)
            telegram.notify(f"⚠️ <b>{FORMATS[fmt]} failed validation</b> after "
                            f"{len(e.attempts)} tries. Nothing was sent for approval.\n"
                            + "\n".join("• " + html.escape(x) for x in e.attempts[-1]["errors"][:5]))
            sys.exit(f"Draft rejected by validator: {e.attempts[-1]['errors']}")
    else:
        post = stub_post(public, fmt)

    from .render.render import render_pngs

    post_id = f"{day}-{fmt}-{rid[-7:-1]}"
    out_dir = config.OUTPUT_DIR / day / post_id
    pngs = render_pngs(f, post, out_dir, fmt)
    image_urls = [storage.upload(p, f"posts/{day}/{post_id}/{p.name}", config.R2_MEDIA_BUCKET) for p in pngs]

    full_caption = post["caption"] + "\n\n" + " ".join("#" + h for h in post.get("hashtags", []))
    (out_dir / "caption.txt").write_text(full_caption)
    auto = fmt == "morning" and config.MORNING_AUTOPUBLISH and bool(f.get("_live"))
    record = {
        "post_id": post_id, "created_at": datetime.now(timezone.utc).isoformat(), "run_id": rid,
        "status": "draft", "format": fmt, "model": config.MODEL if use_llm else "stub",
        "schema_version": config.SCHEMA_VERSION, "auto_publish": auto,
        "facts": f, "post": post, "caption_full": full_caption, "attempts": attempts,
        "images": image_urls,
        "approval": None, "published": None, "metrics": [],
    }
    storage.save_post_record(record, day)
    if f.get("spotlight"):
        try:
            memory.record_spotlight(day, post_id, f["spotlight"])
        except Exception as e:
            print(f"[memory] could not update spotlight index: {e}", flush=True)
    if use_llm and telegram.enabled():
        try:
            mid = telegram.send_draft(record, auto=auto, veto_minutes=config.MORNING_VETO_MINUTES)
            print(f"[draft] sent to Telegram ({'auto-publish with veto' if auto else 'for approval'}, message {mid})", flush=True)
        except Exception as e:  # the draft is saved either way
            print(f"[draft] Telegram send failed: {e}", flush=True)
    print(f"[draft] {post_id}: {len(pngs)} slides → {out_dir}", flush=True)
    for u in image_urls:
        print("   ", u)
    _set_output(post_id=post_id, auto=str(auto).lower())
    return record


def decide(post_id: str, action: str, source: str) -> None:
    """Applies an owner decision from Telegram. Decisions are appended, never overwritten."""
    if action not in {"approve", "reject", "regen"}:
        sys.exit(f"unknown action {action!r}")
    record = storage.load_post_record(post_id)
    if record is None:
        telegram.notify(f"⚠️ Couldn't find draft <code>{html.escape(post_id)}</code>.")
        sys.exit(f"post {post_id} not found")
    day = record["facts"]["date"]
    storage.save_decision({
        "post_id": post_id, "action": action, "source": source,
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "spotlight": (record["facts"].get("spotlight") or {}).get("symbol"),
        "validator_attempts": len(record.get("attempts", [])),
    }, day)
    print(f"[decide] {action} {post_id}", flush=True)
    if action == "approve":
        publish(record)
    elif action == "reject":
        live = storage.get_json(f"{PUBLISHED_PREFIX}dt={day}/{post_id}.json")
        if live:
            telegram.notify(f"⚠️ <code>{html.escape(post_id)}</code> was already published before your tap: "
                            f"{live.get('permalink')}. Delete it in the Instagram app if needed.")
        else:
            telegram.notify(f"❌ Rejected <code>{html.escape(post_id)}</code>. It won't be posted; logged so we can learn from it.")
    else:
        telegram.notify("🔄 Regenerating with fresh data. New draft in about 3 minutes.")
        draft(None, use_llm=True, fmt=fmt_of(post_id))


PUBLISHED_PREFIX = "published/"
CHECKPOINTS = {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "7d": timedelta(days=7)}


def fmt_of(post_id: str) -> str:
    """Post ids look like YYYY-MM-DD-<format>-<suffix>."""
    return post_id[11:].split("-", 1)[0]


def still_fresh(day: str, now: datetime | None = None, fmt: str = "wrap") -> bool:
    """How long a draft's numbers stay current:
    wrap -> until the next session opens; morning -> until noon ET; dive -> until the close."""
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    d = date.fromisoformat(day)
    if fmt == "morning":
        until = datetime(d.year, d.month, d.day, 12, 0, tzinfo=ny)
    elif fmt == "dive":
        until = datetime(d.year, d.month, d.day, 16, 0, tzinfo=ny)
    else:
        nxt = factsmod.next_business_day(d)
        until = datetime(nxt.year, nxt.month, nxt.day, 9, 30, tzinfo=ny)
    return (now or datetime.now(timezone.utc)) < until


def publish(record: dict) -> None:
    from . import instagram

    post_id, day = record["post_id"], record["facts"]["date"]
    fmt = fmt_of(post_id)
    tag = f"<code>{html.escape(post_id)}</code>"
    if not (config.IG_USER_ID and (config.IG_ACCESS_TOKEN or storage.get_json(instagram.TOKEN_STATE_KEY))):
        telegram.notify(f"✅ Approved {tag}. Instagram isn't connected yet, so post the slides manually for now.")
        return
    key = f"{PUBLISHED_PREFIX}dt={day}/{post_id}.json"
    done = storage.get_json(key)
    if done:  # a double tap or a re-run must never post twice
        telegram.notify(f"ℹ️ {tag} is already live: {done.get('permalink') or done['media_id']}")
        return
    same_day = [k for k in storage.list_keys(f"{PUBLISHED_PREFIX}dt={day}/")
                if k.endswith(".json") and fmt_of(k.rsplit("/", 1)[-1]) == fmt]
    if same_day:  # one post of each format per market day
        other = storage.get_json(same_day[0]) or {}
        telegram.notify(f"ℹ️ Not publishing {tag}: today's {FORMATS.get(fmt, fmt)} is already live "
                        f"({other.get('permalink') or other.get('post_id', 'another draft')}).")
        return
    if day != config.market_date().isoformat() or not still_fresh(day, fmt=fmt):
        telegram.notify(f"⛔ Not publishing {tag}: it uses data from {day}, and stale market numbers "
                        "would mislead followers. Tap Regenerate on a fresh draft instead.")
        return
    telegram.notify(f"⏳ Publishing {tag} to Instagram…")
    try:
        result = instagram.publish_carousel(record["images"], record["caption_full"])
    except Exception as e:
        telegram.notify(f"⚠️ Publishing {tag} failed: {html.escape(str(e))[:500]}")
        raise
    storage.put_json(key, {
        "post_id": post_id, "media_id": result["media_id"], "permalink": result["permalink"],
        "published_at": datetime.now(timezone.utc).isoformat(), "market_date": day,
        "spotlight": (record["facts"].get("spotlight") or {}).get("symbol"),
        "market_tone": record["facts"].get("market_tone"), "format": fmt,
        "hook": record.get("post", {}).get("hook"),
    })
    print(f"[publish] {post_id} -> {result['permalink'] or result['media_id']}", flush=True)
    telegram.notify(f"🚀 {FORMATS.get(fmt, '')} live on Instagram: {result['permalink'] or result['media_id']}")


def decisions_for(post_id: str) -> set[str]:
    """Taps on a draft so far. Reads the GitHub runs the Telegram Worker started (they exist the
    instant you tap, even before they finish) plus decisions already logged in R2."""
    import os

    import requests

    found = set()
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if repo and token:
        r = requests.get(f"https://api.github.com/repos/{repo}/actions/workflows/decision.yml/runs",
                         params={"per_page": 50}, timeout=20,
                         headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        if r.ok:
            for run in r.json().get("workflow_runs", []):
                action, _, pid = (run.get("display_title") or "").partition(" ")
                if pid == post_id:
                    found.add(action)
        else:
            print(f"[veto] GitHub check failed ({r.status_code}); relying on R2 only", flush=True)
    for key in storage.list_keys(f"decisions/dt={post_id[:10]}/"):
        if key.rsplit("/", 1)[-1].startswith(post_id + "-"):
            found.add((storage.get_json(key) or {}).get("action", ""))
    return found - {""}


def autopublish(post_id: str, wait_seconds: int) -> None:
    """Veto window: wait, then publish unless the owner tapped Don't post."""
    import time

    record = storage.load_post_record(post_id)
    if not record or not record.get("auto_publish"):
        print(f"[auto] {post_id} is not set to auto-publish", flush=True)
        return
    print(f"[auto] veto window: waiting {wait_seconds}s", flush=True)
    time.sleep(wait_seconds)
    taps = decisions_for(post_id)
    if "reject" in taps:
        print(f"[auto] vetoed: {taps}", flush=True)
        return  # decide() already confirmed in Telegram
    if "approve" in taps:
        print("[auto] owner approved manually; that run publishes it", flush=True)
        return
    publish(record)


def wait_for_open(minutes_after: int = 15, max_wait_minutes: int = 60) -> None:
    """Sleep until a few minutes after the 9:30 ET open (cron runs in UTC and can fire early)."""
    import time
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    now = datetime.now(ny)
    target = now.replace(hour=9, minute=30, second=0, microsecond=0) + timedelta(minutes=minutes_after)
    wait = (target - now).total_seconds()
    if 0 < wait <= max_wait_minutes * 60:
        print(f"[wait] sleeping {wait / 60:.1f} min until {target:%H:%M} ET", flush=True)
        time.sleep(wait)


def collect_insights(now: datetime | None = None) -> int:
    """Snapshots each published post's metrics at 1h, 24h and 7d. These feed the learning loop."""
    from . import instagram

    now = now or datetime.now(timezone.utc)
    n = 0
    for key in storage.list_keys(PUBLISHED_PREFIX):
        pub = storage.get_json(key)
        if not pub:
            continue
        age = now - datetime.fromisoformat(pub["published_at"])
        have = {k.rsplit("/", 1)[-1].removesuffix(".json") for k in storage.list_keys(f"metrics/{pub['post_id']}/")}
        for name, delta in CHECKPOINTS.items():
            if name in have or age < delta or age > delta + timedelta(days=3):
                continue
            try:
                m = instagram.insights(pub["media_id"])
            except Exception as e:
                print(f"[insights] {pub['post_id']} {name}: {e}", flush=True)
                continue
            storage.put_json(f"metrics/{pub['post_id']}/{name}.json", {
                "post_id": pub["post_id"], "media_id": pub["media_id"], "checkpoint": name,
                "collected_at": now.isoformat(), "age_hours": round(age.total_seconds() / 3600, 2), "metrics": m,
            })
            print(f"[insights] {pub['post_id']} {name}: {m}", flush=True)
            n += 1
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tickerdaily")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest")
    d = sub.add_parser("draft")
    d.add_argument("--fixture")
    d.add_argument("--no-llm", action="store_true")
    d.add_argument("--format", default="wrap", choices=list(FORMATS))
    ap_ = sub.add_parser("autopublish")
    ap_.add_argument("--post-id", required=True)
    ap_.add_argument("--wait", type=int, default=None, help="seconds; default MORNING_VETO_MINUTES")
    w = sub.add_parser("wait-open")
    w.add_argument("--minutes-after", type=int, default=15)
    c = sub.add_parser("decide")
    c.add_argument("--post-id", required=True)
    c.add_argument("--action", required=True, choices=["approve", "reject", "regen"])
    c.add_argument("--source", default="telegram")
    sub.add_parser("refresh-token")
    sub.add_parser("insights")
    sub.add_parser("ig-check")
    a = ap.parse_args(argv)
    if a.cmd == "refresh-token":
        from . import instagram
        info = instagram.refresh_token()
        print(f"[token] refreshed, expires {info['expires_at']}", flush=True)
    elif a.cmd == "ig-check":
        from . import instagram
        me = instagram.whoami()
        print(f"[ig] connected as @{me.get('username')} ({me['id']}) via {me['host']}, token: {me['token_kind']}", flush=True)
        telegram.notify(f"🔗 Instagram connected as <b>@{html.escape(me.get('username') or '?')}</b>. "
                        "Approve is now live: tapping it publishes the carousel.")
    elif a.cmd == "insights":
        print(f"[insights] collected {collect_insights()} snapshot(s)", flush=True)
    elif a.cmd == "ingest":
        ingest(config.market_date().isoformat(), storage.run_id())
    elif a.cmd == "autopublish":
        autopublish(a.post_id, a.wait if a.wait is not None else config.MORNING_VETO_MINUTES * 60)
    elif a.cmd == "wait-open":
        wait_for_open(a.minutes_after)
    elif a.cmd == "decide":
        decide(a.post_id, a.action, a.source)
    else:
        draft(a.fixture, use_llm=not a.no_llm, fmt=a.format)


if __name__ == "__main__":
    main()
