"""Claude writes the words. Facts (numbers) are supplied; the validator checks the result."""
from __future__ import annotations

import json
import re

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

Return ONLY a JSON object with these keys:
{
  "hook": "cover headline, max 55 characters, makes people swipe",
  "cover_sub": "one line under the hook, max 70 characters",
  "wrap_line": "one sentence on how the overall market did today",
  "spotlight_title": "max 40 characters, about the spotlight stock",
  "spotlight_why": "2-3 short sentences on why the spotlight stock moved",
  "movers_line": "one sentence tying today's biggest gainers and losers together",
  "next_line": "one sentence on what to watch next trading day",
  "caption": "Instagram caption, 3-6 short lines, ends with the disclaimer line",
  "hashtags": ["5 to 8 relevant hashtags without the # sign"]
}"""


def _parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("model did not return JSON")
    return json.loads(m.group(0))


def write(facts: dict, max_attempts: int = 3) -> tuple[dict, list[dict]]:
    """Returns (post, attempts). Each attempt is logged so we learn which prompts fail."""
    import anthropic

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    messages = [{"role": "user", "content": "FACTS:\n" + json.dumps(facts, indent=1)}]
    attempts = []
    for _ in range(max_attempts):
        resp = client.messages.create(model=config.MODEL, max_tokens=1500, system=SYSTEM, messages=messages)
        text = "".join(b.text for b in resp.content if b.type == "text")
        try:
            post = _parse(text)
        except ValueError as e:
            attempts.append({"raw": text, "errors": [str(e)]})
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": "Return only the JSON object."}]
            continue
        post["hashtags"] = [h.lstrip("#").replace(" ", "") for h in post.get("hashtags", [])][:8]
        result = validator.validate(post, facts)
        attempts.append({"raw": text, "errors": result.errors,
                         "usage": {"in": resp.usage.input_tokens, "out": resp.usage.output_tokens}})
        if result.ok:
            return post, attempts
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content": "Fix these problems and return the full JSON again:\n- "
                      + "\n- ".join(result.errors)}]
    raise RuntimeError(f"draft failed validation after {max_attempts} attempts: {attempts[-1]['errors']}")
