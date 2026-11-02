"""SEC EDGAR: official company filings (free, no key). 8-Ks are the market-moving ones:
earnings releases, CEO changes, deals, cyber incidents."""
from __future__ import annotations

import time
from datetime import date, timedelta

import requests

from .. import config

FORMS = {"8-K", "10-Q", "10-K", "S-1", "SC 13D"}
ITEM_LABELS = {
    "1.01": "material agreement", "1.02": "agreement terminated", "1.05": "cybersecurity incident",
    "2.01": "acquisition or sale completed", "2.02": "earnings results", "2.05": "restructuring costs",
    "2.06": "impairment", "3.01": "delisting notice", "4.02": "financials restated",
    "5.02": "executive or board change", "5.07": "shareholder vote", "7.01": "investor disclosure",
    "8.01": "other material event",
}


def _get(url: str):
    r = requests.get(url, headers={"User-Agent": config.SEC_USER_AGENT}, timeout=20)
    time.sleep(0.15)  # SEC fair-access limit is 10 requests/second
    r.raise_for_status()
    return r.json()


def cik_map(symbols: list[str]) -> dict[str, int]:
    data = _get("https://www.sec.gov/files/company_tickers.json")
    by_ticker = {v["ticker"].upper(): v["cik_str"] for v in data.values()}
    return {s: by_ticker[s.replace(".", "-")] for s in symbols if s.replace(".", "-") in by_ticker}


def recent_filings(symbols: list[str], days: int = 2) -> list[dict]:
    since = (config.market_date() - timedelta(days=days)).isoformat()
    out = []
    for sym, cik in cik_map(symbols).items():
        try:
            rec = _get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")["filings"]["recent"]
        except requests.RequestException:
            continue
        for i, form in enumerate(rec["form"]):
            if rec["filingDate"][i] < since:
                break  # newest first
            if form not in FORMS:
                continue
            acc = rec["accessionNumber"][i]
            items = [x.strip() for x in (rec.get("items", [""] * (i + 1))[i] or "").split(",") if x.strip()]
            out.append({
                "symbol": sym, "cik": cik, "form": form, "accession": acc,
                "filed_date": rec["filingDate"][i], "accepted_at": rec["acceptanceDateTime"][i],
                "items": ",".join(items),
                "item_labels": "; ".join(ITEM_LABELS.get(x, x) for x in items),
                "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{rec['primaryDocument'][i]}",
                "payload": {k: rec[k][i] for k in rec if isinstance(rec[k], list) and len(rec[k]) > i},
            })
    return out
