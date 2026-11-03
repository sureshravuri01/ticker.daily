"""Claude writes the words. Facts (numbers) are supplied; the validator checks the result."""
from __future__ import annotations

import json

from . import config, validator

SYSTEM = """You write the daily after-the-bell Instagram carousel for @ticker.daily, a stock market news page.
Voice: sharp, plain-English, curious, a little witty. Explain WHY things moved. No hype, no emojis in slide text.

Hard rules:
1. Use ONLY numbers that appear in FACTS, copied exactly as formatted (you may drop the sign or round, never invent).
   No other numbers: no counts, years, times or estimates that are not in FACTS.
2. Explain moves ONLY with the provided headlines, filings and earnings data. If nothing explains a move,
   say no clear catalyst was reported. Never speculate about causes.
3. Never give advice: no buy/sell/hold language, no predictions, no price targets.
4. Do not copy headline wording; summarize in your own words.
5. The caption must end with a line containing exactly: Not financial advice.
6. MEMORY holds history: earnings track record, 52-week range, streaks, past spotlights and what
   happened the session after. Use it to add perspective, and put the single most interesting
   point in context_line. History never predicts the future: never say or imply a pattern will
   repeat ("could rise again", "history suggests"). If nothing in MEMORY is interesting, leave
   context_line as an empty string.

Reply with the post as JSON matching the schema."""



class DraftRejected(RuntimeError):
    def __init__(self, attempts: list[dict]):
        super().__init__(f"draft failed validation after {len(attempts)} attempts: {attempts[-1]['errors']}")
        self.attempts = attempts


_STR = {"type": "string"}
SCHEMA = {
    "type": "object",
    "properties": {
        "hook": {**_STR, "description": "cover headline, max 55 characters, makes people swipe"},
        "cover_sub": {**_STR, "description": "one line under the hook, max 70 characters"},
        "wrap_line": {**_STR, "description": "one sentence on how the overall market did today"},
        "spotlight_title": {**_STR, "description": "max 40 characters, about the spotlight stock"},
        "spotlight_why": {**_STR, "description": "2-3 short sentences on why the spotlight stock moved"},
        "movers_line": {**_STR, "description": "one sentence tying today's biggest gainers and losers together"},
        "next_line": {**_STR, "description": "one sentence on what to watch next trading day"},
        "context_line": {**_STR, "description": "one sentence of historical context from MEMORY, max 110 characters, or empty"},
        "caption": {**_STR, "description": "Instagram caption, 3-6 short lines, ending with: Not financial advice."},
        "hashtags": {"type": "array", "items": _STR, "description": "5 to 8 hashtags without the # sign"},
    },
    "required": ["hook", "cover_sub", "wrap_line", "spotlight_title", "spotlight_why",
                 "movers_line", "next_line", "context_line", "caption", "hashtags"],
    "additionalProperties": False,
}


def write(facts: dict, max_attempts: int = 3, client=None) -> tuple[dict, list[dict]]:
    """Returns (post, attempts). Each attempt is logged so we learn which prompts fail."""
    if client is None:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    messages = [{"role": "user", "content": "FACTS:\n" + json.dumps(facts, indent=1)}]
    attempts = []
    for _ in range(max_attempts):
        resp = client.messages.create(
            model=config.MODEL, max_tokens=3000, system=SYSTEM, messages=messages,
            # Structured outputs: the reply is guaranteed to be JSON matching SCHEMA.
            extra_body={"output_config": {"format": {"type": "json_schema", "schema": SCHEMA}}},
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        usage = {"in": resp.usage.input_tokens, "out": resp.usage.output_tokens}
        try:
            post = json.loads(text)
        except ValueError:
            attempts.append({"raw": text[:2000], "stop_reason": resp.stop_reason, "usage": usage,
                             "errors": [f"unparseable reply (stop_reason={resp.stop_reason})"]})
            print(f"[writer] unparseable reply, stop_reason={resp.stop_reason}: {text[:200]!r}", flush=True)
            continue
        post["hashtags"] = [h.lstrip("#").replace(" ", "") for h in post.get("hashtags", [])][:8]
        result = validator.validate(post, facts)
        attempts.append({"raw": post, "errors": result.errors, "usage": usage})
        print(f"[writer] attempt {len(attempts)}: {len(result.errors)} problem(s)", flush=True)
        for e in result.errors:
            print("    -", e, flush=True)
        if result.ok:
            return post, attempts
        messages += [
            {"role": "assistant", "content": text},
            {"role": "user", "content": "Fix these problems and reply with the full JSON again:\n- "
             + "\n- ".join(result.errors)},
        ]
    raise DraftRejected(attempts)
