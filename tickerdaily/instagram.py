"""Instagram API with Instagram Login (graph.instagram.com): publish carousels, refresh the token,
read post insights. Docs: https://developers.facebook.com/docs/instagram-platform/content-publishing

The long-lived token starts in the IG_ACCESS_TOKEN secret. Each refresh writes the new token to
the private R2 data bucket (state/ig_token.json), which is then preferred over the secret.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import requests

from . import config, storage

GRAPH = "https://graph.instagram.com"
TOKEN_STATE_KEY = "state/ig_token.json"
INSIGHT_METRICS = ["reach", "views", "likes", "comments", "saved", "shares", "total_interactions"]


class InstagramError(RuntimeError):
    pass


def token() -> str:
    state = storage.get_json(TOKEN_STATE_KEY)
    t = (state or {}).get("access_token") or config.IG_ACCESS_TOKEN
    if not t:
        raise InstagramError("No Instagram token: set the IG_ACCESS_TOKEN secret")
    return t


def _req(method: str, path: str, tok: str | None = None, **params) -> dict:
    params["access_token"] = tok or token()
    r = requests.request(method, f"{GRAPH}{path}", params=params if method == "GET" else None,
                         data=params if method != "GET" else None, timeout=60)
    data = r.json() if r.content else {}
    if r.status_code >= 400 or "error" in data:
        err = data.get("error", {})
        raise InstagramError(f"{method} {path}: {err.get('message', r.text[:200])} (code {err.get('code')})")
    return data


def _wait_ready(container_id: str, tok: str, tries: int = 12, delay: float = 10) -> None:
    for _ in range(tries):
        status = _req("GET", f"/{container_id}", tok, fields="status_code").get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise InstagramError(f"container {container_id} is {status}")
        time.sleep(delay)
    raise InstagramError(f"container {container_id} not ready after {tries * delay:.0f}s")


def publish_carousel(image_urls: list[str], caption: str, delay: float = 10) -> dict:
    """Creates one container per image, a carousel container, then publishes it.
    Returns {"media_id", "permalink"}."""
    if not config.IG_USER_ID:
        raise InstagramError("IG_USER_ID variable is not set")
    if not 2 <= len(image_urls) <= 10:
        raise InstagramError(f"a carousel needs 2-10 images, got {len(image_urls)}")
    bad = [u for u in image_urls if not (u.startswith("https://") and u.lower().endswith((".jpg", ".jpeg")))]
    if bad:
        raise InstagramError(f"images must be public JPEG URLs: {bad[:2]}")
    tok, uid = token(), config.IG_USER_ID

    children = [_req("POST", f"/{uid}/media", tok, image_url=u, is_carousel_item="true")["id"]
                for u in image_urls]
    for c in children:
        _wait_ready(c, tok, delay=delay)
    carousel = _req("POST", f"/{uid}/media", tok, media_type="CAROUSEL",
                    children=",".join(children), caption=caption)["id"]
    _wait_ready(carousel, tok, delay=delay)
    media_id = _req("POST", f"/{uid}/media_publish", tok, creation_id=carousel)["id"]
    try:
        permalink = _req("GET", f"/{media_id}", tok, fields="permalink").get("permalink")
    except InstagramError:
        permalink = None
    return {"media_id": media_id, "permalink": permalink, "children": children, "container": carousel}


def refresh_token() -> dict:
    """Long-lived tokens last ~60 days and can be refreshed once they're at least a day old."""
    data = _req("GET", "/refresh_access_token", grant_type="ig_refresh_token")
    now = datetime.now(timezone.utc)
    state = {
        "access_token": data["access_token"],
        "refreshed_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=int(data.get("expires_in", 0)))).isoformat(),
    }
    storage.put_json(TOKEN_STATE_KEY, state)
    return {k: v for k, v in state.items() if k != "access_token"}


def insights(media_id: str) -> dict:
    """Post metrics. If Instagram rejects a metric name, drop it and retry with the rest."""
    metrics = list(INSIGHT_METRICS)
    while metrics:
        try:
            data = _req("GET", f"/{media_id}/insights", metric=",".join(metrics))
            return {m["name"]: (m.get("values") or [{}])[0].get("value", m.get("total_value", {}).get("value"))
                    for m in data.get("data", [])}
        except InstagramError as e:
            dropped = next((m for m in metrics if m in str(e)), None)
            if not dropped:
                raise
            metrics.remove(dropped)
    return {}
