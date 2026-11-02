"""Guards every draft before a human or Instagram sees it.

Rule 1: every number in the post must come from the facts (rounding allowed, inventing not).
Rule 2: no investment-advice language.
Rule 3: Instagram limits (caption length, hashtag count).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

NUM_RE = re.compile(
    r"(?<![A-Za-z0-9])[-+−]?\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s?(%|[KMBT](?![A-Za-z])|billion|million|trillion|thousand)?",
)
UNIT_ALIASES = {"billion": "B", "million": "M", "trillion": "T", "thousand": "K"}
SMALL_INT_OK = set(range(0, 11))  # "3 things", "Q4", "top 5" — never with % or $

ADVICE_RE = re.compile(
    r"\b(you should (buy|sell|invest)|buy now|sell now|strong buy|must[- ]buy|price target of|"
    r"guaranteed|can'?t lose|to the moon|time to (buy|sell)|load up|buy the dip|financial advice:)\b",
    re.I,
)


@dataclass(frozen=True)
class Num:
    value: float
    decimals: int
    unit: str
    money: bool
    raw: str


def extract_numbers(text: str) -> list[Num]:
    out = []
    for m in NUM_RE.finditer(text):
        intpart, frac, unit = m.group(1), m.group(2) or "", m.group(3) or ""
        unit = UNIT_ALIASES.get(unit.lower(), unit) if unit else ""
        out.append(Num(
            value=float(intpart.replace(",", "") + frac),
            decimals=len(frac) - 1 if frac else 0,
            unit=unit,
            money="$" in m.group(0),
            raw=m.group(0).strip(),
        ))
    return out


def _walk_strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _walk_strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk_strings(v)


def _walk_ints(x):
    if isinstance(x, bool):
        return
    if isinstance(x, int):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _walk_ints(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk_ints(v)


def allowed_numbers(facts: dict) -> list[Num]:
    nums = [n for s in _walk_strings(facts) for n in extract_numbers(s)]
    # Plain counts in the facts (e.g. breadth: 31 of 50 down) are fair game too.
    nums += [Num(float(i), 0, "", False, str(i)) for i in _walk_ints(facts)]
    return nums


def _matches(n: Num, allowed: list[Num]) -> bool:
    if n.unit == "" and not n.money and n.decimals == 0 and n.value in SMALL_INT_OK:
        return True
    for a in allowed:
        if a.unit != n.unit:
            continue
        # Same number, or the fact rounded to the precision the writer used.
        if abs(round(a.value, n.decimals) - n.value) < 1e-9:
            return True
    return False


@dataclass
class Result:
    ok: bool
    errors: list[str] = field(default_factory=list)


def validate(post: dict, facts: dict) -> Result:
    errors = []
    allowed = allowed_numbers(facts)
    texts = list(_walk_strings({k: v for k, v in post.items() if k != "hashtags"}))

    for t in texts:
        for n in extract_numbers(t):
            if not _matches(n, allowed):
                errors.append(f"unsupported number '{n.raw}' in: {t[:80]!r}")
        if ADVICE_RE.search(t):
            errors.append(f"advice language in: {t[:80]!r}")

    caption = post.get("caption", "")
    tags = post.get("hashtags", [])
    if len(caption) + sum(len(h) + 2 for h in tags) > 2200:
        errors.append("caption + hashtags exceed Instagram's 2200 characters")
    if len(tags) > 30:
        errors.append("more than 30 hashtags")
    if "not financial advice" not in caption.lower():
        errors.append("caption is missing the 'Not financial advice' disclaimer")
    return Result(ok=not errors, errors=errors)
