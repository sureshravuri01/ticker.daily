"""Publishing and insights against a fake Instagram API (no network)."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from tickerdaily import config, instagram, pipeline, storage


class FakeIG:
    def __init__(self, fail_metric=None):
        self.calls, self.n, self.fail_metric = [], 0, fail_metric
        self.publish_errors = []      # codes to answer media_publish with, in order
        self.live_after_error = False  # Meta's quirk: error, but the post went live
        self.live = []

    def __call__(self, method, url, params=None, data=None, timeout=None):
        p = params or data
        path = url.replace(instagram.GRAPH, "").replace(instagram.FB_GRAPH, "")
        self.calls.append((method, path, {k: v for k, v in p.items() if k != "access_token"}))
        if path.endswith("/media") and method == "POST":
            self.n += 1
            if p.get("caption"):
                self.last_caption = p["caption"]
            body = {"id": f"c{self.n}"}
        elif path.endswith("/media_publish"):
            if self.publish_errors:
                code = self.publish_errors.pop(0)
                if self.live_after_error:
                    self.live.append({"id": "M1", "caption": self.last_caption, "timestamp": "2026-11-02T15:00:00+0000",
                                      "permalink": "https://www.instagram.com/p/ABC/"})
                return _Resp(400, {"error": {"message": "Application request limit reached", "code": code}})
            self.live.append({"id": "M1", "caption": self.last_caption, "timestamp": "2026-11-02T15:00:00+0000",
                              "permalink": "https://www.instagram.com/p/ABC/"})
            body = {"id": "M1"}
        elif path.endswith("/media") and method == "GET":
            body = {"data": list(reversed(self.live))}
        elif "fields" in p and p["fields"] == "status_code":
            body = {"status_code": "FINISHED"}
        elif "fields" in p and p["fields"] == "permalink":
            body = {"permalink": "https://www.instagram.com/p/ABC/"}
        elif path.endswith("/insights"):
            if self.fail_metric and self.fail_metric in p["metric"]:
                return _Resp(400, {"error": {"message": f"metric[{self.fail_metric}] is not supported", "code": 100}})
            body = {"data": [{"name": m, "values": [{"value": 7}]} for m in p["metric"].split(",")]}
        elif path == "/refresh_access_token":
            body = {"access_token": "NEW", "expires_in": 5184000}
        else:
            body = {}
        return _Resp(200, body)


class _Resp:
    headers: dict = {}

    def __init__(self, code, body):
        self.status_code, self._b = code, body
        self.content, self.text = json.dumps(body).encode(), json.dumps(body)

    def json(self):
        return self._b


@pytest.fixture
def ig(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LOCAL_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "R2_ACCOUNT_ID", "")  # local storage only
    monkeypatch.setattr(config, "IG_USER_ID", "17841")
    monkeypatch.setattr(config, "IG_ACCESS_TOKEN", "TOKEN")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    fake = FakeIG()
    monkeypatch.setattr(instagram.requests, "request", fake)
    monkeypatch.setattr(instagram.time, "sleep", lambda s: None)
    return fake


URLS = [f"https://pub.r2.dev/posts/x/slide_{i}.jpg" for i in range(1, 6)]


def test_carousel_flow(ig):
    out = instagram.publish_carousel(URLS, "caption")
    assert out == {"media_id": "M1", "permalink": "https://www.instagram.com/p/ABC/",
                   "children": ["c1", "c2", "c3", "c4", "c5"], "container": "c6", "recovered": False}
    carousel = [c for c in ig.calls if c[2].get("media_type") == "CAROUSEL"][0]
    assert carousel[2]["children"] == "c1,c2,c3,c4,c5" and carousel[2]["caption"] == "caption"
    assert all(c[2]["is_carousel_item"] == "true" for c in ig.calls if "image_url" in c[2])


def test_rejects_png_and_single_image(ig):
    with pytest.raises(instagram.InstagramError):
        instagram.publish_carousel([u.replace(".jpg", ".png") for u in URLS], "c")
    with pytest.raises(instagram.InstagramError):
        instagram.publish_carousel(URLS[:1], "c")


def _record(day):
    return {"post_id": f"{day}-wrap-000001", "facts": {"date": day, "spotlight": {"symbol": "AMZN"}},
            "images": URLS, "caption_full": "hi\nNot financial advice.", "post": {"hook": "h"}}


def test_publish_once_only(ig, monkeypatch):
    monkeypatch.setattr(pipeline, "still_fresh", lambda d, now=None, **kw: True)
    monkeypatch.setattr(config, "market_date", lambda now=None: __import__("datetime").date(2026, 11, 2))
    rec = _record("2026-11-02")
    pipeline.publish(rec)
    first = len(ig.calls)
    pipeline.publish(rec)  # double tap
    assert len(ig.calls) == first
    assert storage.get_json("published/dt=2026-11-02/2026-11-02-wrap-000001.json")["media_id"] == "M1"


def test_stale_draft_blocked(ig):
    pipeline.publish(_record("2020-01-02"))
    assert ig.calls == []


def test_freshness_window():
    ny_close = datetime(2026, 11, 2, 21, 30, tzinfo=timezone.utc)  # Mon 4:30 PM ET
    assert pipeline.still_fresh("2026-11-02", ny_close)
    assert pipeline.still_fresh("2026-11-02", datetime(2026, 11, 3, 5, 0, tzinfo=timezone.utc))  # 9 PM PT Mon
    assert not pipeline.still_fresh("2026-11-02", datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc))  # Tue open
    assert pipeline.still_fresh("2026-10-30", datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc))  # Fri draft, Sunday


def test_insights_checkpoints_and_metric_fallback(ig, monkeypatch):
    monkeypatch.setattr(pipeline, "still_fresh", lambda d, now=None, **kw: True)
    monkeypatch.setattr(config, "market_date", lambda now=None: __import__("datetime").date(2026, 11, 2))
    pipeline.publish(_record("2026-11-02"))
    t0 = datetime.fromisoformat(storage.get_json("published/dt=2026-11-02/2026-11-02-wrap-000001.json")["published_at"])
    ig.fail_metric = "views"
    assert pipeline.collect_insights(t0 + timedelta(minutes=30)) == 0
    assert pipeline.collect_insights(t0 + timedelta(hours=2)) == 1
    assert pipeline.collect_insights(t0 + timedelta(hours=3)) == 0      # 1h already taken
    assert pipeline.collect_insights(t0 + timedelta(hours=25)) == 1     # 24h
    m = storage.get_json("metrics/2026-11-02-wrap-000001/24h.json")["metrics"]
    assert m["reach"] == 7 and "views" not in m


def test_token_refresh_saved(ig):
    instagram.refresh_token()
    assert storage.get_json(instagram.TOKEN_STATE_KEY)["access_token"] == "NEW"
    assert instagram.token() == "NEW"


def test_system_user_token_uses_facebook_host(ig, monkeypatch):
    monkeypatch.setattr(config, "IG_ACCESS_TOKEN", "EAAsystem")
    instagram.publish_carousel(URLS, "c")
    assert instagram.host("EAAx") == instagram.FB_GRAPH and instagram.host("IGAAx") == instagram.GRAPH
    assert instagram.refresh_token()["expires_at"].startswith("never")
    assert storage.get_json(instagram.TOKEN_STATE_KEY) is None  # nothing overwritten


def test_one_wrap_per_market_day(ig, monkeypatch):
    monkeypatch.setattr(pipeline, "still_fresh", lambda d, now=None, **kw: True)
    monkeypatch.setattr(config, "market_date", lambda now=None: __import__("datetime").date(2026, 11, 2))
    pipeline.publish(_record("2026-11-02"))
    first = len(ig.calls)
    other = dict(_record("2026-11-02"), post_id="2026-11-02-wrap-000002")
    pipeline.publish(other)  # a regenerated draft for the same day
    assert len(ig.calls) == first


def test_mislabelled_draft_blocked(ig, monkeypatch):
    monkeypatch.setattr(pipeline, "still_fresh", lambda d, now=None, **kw: True)
    monkeypatch.setattr(config, "market_date", lambda now=None: __import__("datetime").date(2026, 11, 2))
    pipeline.publish(_record("2026-11-03"))  # labelled tomorrow, built from today's data
    assert ig.calls == []


def test_publish_error_but_post_is_live_counts_as_success(ig):
    ig.publish_errors, ig.live_after_error = [4], True
    out = instagram.publish_carousel(URLS, "Big day\nNot financial advice.", delay=0)
    assert out["media_id"] == "M1" and out["recovered"] is True
    assert sum(1 for c in ig.calls if c[1].endswith("/media_publish")) == 1  # never re-published


def test_rate_limited_and_not_live_retries_safely(ig):
    ig.publish_errors = [4]
    out = instagram.publish_carousel(URLS, "Big day\nNot financial advice.", delay=0)
    assert out["media_id"] == "M1" and out["recovered"] is False
    assert sum(1 for c in ig.calls if c[1].endswith("/media_publish")) == 2


def test_other_errors_are_not_retried(ig):
    ig.publish_errors = [100]
    with pytest.raises(instagram.InstagramError):
        instagram.publish_carousel(URLS, "Big day", delay=0)
    assert sum(1 for c in ig.calls if c[1].endswith("/media_publish")) == 1


def test_reconcile_records_live_posts_we_missed(ig):
    storage.save_post_record({"post_id": "2026-11-02-dive-000001", "facts": {"date": "2026-11-02", "spotlight": {"symbol": "AMD"}},
                              "caption_full": "AMD fell hard\nNot financial advice.", "post": {"hook": "h"}}, "2026-11-02")
    ig.live = [{"id": "M9", "caption": "AMD fell hard\nNot financial advice.", "timestamp": "2026-11-02T19:45:00+0000",
                "permalink": "https://www.instagram.com/p/XYZ/"},
               {"id": "M8", "caption": "Something posted by hand", "timestamp": "2026-11-02T18:00:00+0000"}]
    assert pipeline.reconcile() == 1
    rec = storage.get_json("published/dt=2026-11-02/2026-11-02-dive-000001.json")
    assert rec["media_id"] == "M9" and rec["recovered"] is True
    assert pipeline.reconcile() == 0  # idempotent


def test_late_checkpoints_are_collected_and_flagged(ig, monkeypatch):
    monkeypatch.setattr(pipeline, "still_fresh", lambda d, now=None, **kw: True)
    monkeypatch.setattr(config, "market_date", lambda now=None: __import__("datetime").date(2026, 11, 2))
    pipeline.publish(_record("2026-11-02"))
    t0 = datetime.fromisoformat(storage.get_json("published/dt=2026-11-02/2026-11-02-wrap-000001.json")["published_at"])
    assert pipeline.collect_insights(t0 + timedelta(days=9)) == 3  # 1h, 24h and 7d all missed
    assert storage.get_json("metrics/2026-11-02-wrap-000001/1h.json")["late"] is True
    assert storage.get_json("metrics/2026-11-02-wrap-000001/7d.json")["late"] is False


def test_notify_never_raises(monkeypatch):
    from tickerdaily import telegram
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr(telegram.time, "sleep", lambda s: None)

    def boom(*a, **k):
        raise TimeoutError("Read timed out")
    monkeypatch.setattr(telegram, "_call", boom)
    telegram.notify("hello")  # must not raise
