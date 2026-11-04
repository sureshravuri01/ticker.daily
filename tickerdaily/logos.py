"""Company logos for slides. Used only to identify the company next to its ticker.

Each logo is fetched once (Finnhub company profile), checked, normalised and cached in the
private data bucket (logos/). Misses are cached too, so a missing logo isn't re-requested daily.
Slides get logos as embedded data URIs, so rendering never depends on a network call.
Anything missing or broken falls back to a monogram tile in the template.
"""
from __future__ import annotations

import base64
import io
from datetime import datetime, timedelta, timezone

import requests

from . import config, storage

RETRY_AFTER = timedelta(days=30)        # the company has no usable logo
RETRY_AFTER_ERROR = timedelta(hours=20)  # network or API hiccup: try again next run
MAX_FETCH_PER_RUN = 25


def _png_key(sym: str) -> str:
    return f"logos/{sym}.png"


def _meta_key(sym: str) -> str:
    return f"logos/{sym}.json"


def normalise(data: bytes) -> bytes | None:
    """Return a clean PNG, or None if the image is unusable (tiny, blank or not an image)."""
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return None
    if min(img.size) < 48:
        return None
    img = img.convert("RGBA")
    if img.getchannel("A").getbbox() is None:  # fully transparent
        return None
    bbox = img.getchannel("A").getbbox()
    img = img.crop(bbox)
    lo, hi = img.convert("L").getextrema()
    alpha_lo, _ = img.getchannel("A").getextrema()
    if hi - lo < 12 and alpha_lo == 255:  # one flat colour: not a real logo
        return None
    img.thumbnail((256, 256))
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def icon_style(png: bytes) -> dict:
    """App-icon look: if the logo has its own solid background, the whole icon takes that colour and
    the logo fills it; transparent logos sit centred on white."""
    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("RGBA")
    w, h = img.size
    px = img.load()
    edge = [px[x, y] for x in range(w) for y in (0, 1, h - 2, h - 1)] + \
           [px[x, y] for y in range(h) for x in (0, 1, w - 2, w - 1)]
    opaque = [p for p in edge if p[3] > 230]
    if len(opaque) > 0.9 * len(edge):
        rgb = [sorted(p[i] for p in opaque)[len(opaque) // 2] for i in range(3)]
        spread = max(max(p[i] for p in opaque) - min(p[i] for p in opaque) for i in range(3))
        if spread < 40:  # one solid background colour around the mark
            return {"bg": "#%02X%02X%02X" % tuple(rgb), "scale": 100 if w == h else 88}
    return {"bg": "#FFFFFF", "scale": 70}


def _uri(png: bytes) -> dict:
    return {"src": "data:image/png;base64," + base64.b64encode(png).decode(), **icon_style(png)}


def _download(sym: str) -> tuple[bytes | None, str]:
    from .ingest import finnhub

    url = (finnhub.company_profile(sym) or {}).get("logo")
    if not url or not url.startswith("https://"):
        return None, "no logo in company profile"
    r = requests.get(url, timeout=20)
    if not r.ok:
        return None, f"download failed ({r.status_code})"
    png = normalise(r.content)
    return (png, "ok") if png else (None, "image unusable")


def get(sym: str, allow_fetch: bool = True) -> dict | None:
    """{"src": data URI, "bg": icon colour, "scale": %} or None (the template then draws a monogram)."""
    meta = storage.get_json(_meta_key(sym))
    if meta and meta.get("status") == "ok":
        path = storage.fetch(_png_key(sym))
        if path:
            return _uri(path.read_bytes())
    elif meta:
        wait = RETRY_AFTER if meta.get("detail") in ("no logo in company profile", "image unusable") else RETRY_AFTER_ERROR
        if datetime.now(timezone.utc) - datetime.fromisoformat(meta["checked_at"]) < wait:
            return None
    if not allow_fetch:
        return None
    try:
        png, status = _download(sym)
    except Exception as e:
        png, status = None, f"error: {e}"[:200]
    if png:
        local = config.LOCAL_DATA_DIR / _png_key(sym)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(png)
        storage.upload(local, _png_key(sym), config.R2_DATA_BUCKET)
    storage.put_json(_meta_key(sym), {"symbol": sym, "status": "ok" if png else "missing",
                                      "detail": status, "checked_at": datetime.now(timezone.utc).isoformat()})
    print(f"[logos] {sym}: {status}", flush=True)
    return _uri(png) if png else None


def symbols_in(facts: dict) -> list[str]:
    """Every company ticker shown on the slides (index ETFs excluded), in order of appearance."""
    seen: list[str] = []

    def walk(x):
        if isinstance(x, dict):
            s = x.get("symbol")
            if isinstance(s, str) and s not in config.INDEXES and s not in seen:
                seen.append(s)
            for k, v in x.items():
                if k != "memory":
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(facts)
    return seen


def for_facts(facts: dict, allow_fetch: bool = True) -> dict[str, dict]:
    out, fetched = {}, 0
    for sym in symbols_in(facts):
        cached_only = not allow_fetch or fetched >= MAX_FETCH_PER_RUN
        had_meta = storage.get_json(_meta_key(sym)) is not None
        uri = get(sym, allow_fetch=not cached_only)
        fetched += 0 if (had_meta or cached_only) else 1
        if uri:
            out[sym] = uri
    return out
