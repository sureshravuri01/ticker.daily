"""Telegram: sends drafts for approval and status updates to the owner.

Button presses don't come back here. They go to the Cloudflare Worker (worker/),
which checks they came from TELEGRAM_CHAT_ID and starts the decision workflow.
"""
from __future__ import annotations

import html

import requests

from . import config

API = "https://api.telegram.org/bot{token}/{method}"
CAPTION_LIMIT = 1024  # media captions
TEXT_LIMIT = 4096     # normal messages


def enabled() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def _call(method: str, **payload) -> dict:
    r = requests.post(API.format(token=config.TELEGRAM_BOT_TOKEN, method=method), json=payload, timeout=30)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
    return data["result"]


def notify(text: str) -> None:
    if enabled():
        _call("sendMessage", chat_id=config.TELEGRAM_CHAT_ID, text=text[:TEXT_LIMIT],
              parse_mode="HTML", disable_web_page_preview=True)


def buttons(post_id: str) -> dict:
    # callback_data is capped at 64 bytes: "<action>|<post_id>"
    return {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"approve|{post_id}"},
        {"text": "❌ Reject", "callback_data": f"reject|{post_id}"},
    ], [
        {"text": "🔄 Regenerate", "callback_data": f"regen|{post_id}"},
    ]]}


def review_text(record: dict) -> str:
    f, n = record["facts"], len(record.get("attempts", []))
    s = f.get("spotlight") or {}
    lines = [
        f"<b>📬 Draft ready: {html.escape(f['date_label'])}</b>",
        f"Spotlight: <b>{html.escape(s.get('symbol', '?'))}</b> {html.escape(s.get('change_pct', ''))}"
        f" · market {html.escape(f['market_tone'])}",
        f"Validator: passed{' after ' + str(n) + ' tries' if n > 1 else ''}",
        *([f"🧠 Context: {html.escape(record['post']['context_line'])}"]
          if (record.get("post") or {}).get("context_line") else []),
        "",
        "<b>Caption</b>",
        html.escape(record["caption_full"]),
        "",
        f"<code>{html.escape(record['post_id'])}</code>",
    ]
    return "\n".join(lines)


def send_draft(record: dict) -> int:
    """Sends the slides as an album, then the caption with decision buttons. Returns the message id."""
    imgs = [u for u in record["images"] if u.startswith("http")]
    if imgs:
        media = [{"type": "photo", "media": u} for u in imgs[:10]]
        media[0]["caption"] = f"{record['facts']['date_label']} · after the bell"[:CAPTION_LIMIT]
        _call("sendMediaGroup", chat_id=config.TELEGRAM_CHAT_ID, media=media)
    msg = _call("sendMessage", chat_id=config.TELEGRAM_CHAT_ID, text=review_text(record)[:TEXT_LIMIT],
                parse_mode="HTML", reply_markup=buttons(record["post_id"]), disable_web_page_preview=True)
    return msg["message_id"]
