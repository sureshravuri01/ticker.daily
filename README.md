# ticker.daily

Why stocks moved today, in 60 seconds 📈📉 Daily after-the-bell recap + earnings breakdowns.

An automated pipeline that collects market data, explains the day's moves, and renders an
Instagram carousel for [@ticker.daily](https://www.instagram.com/ticker.daily/). Every run is
saved, so the dataset (and the posts) get smarter over time.

## How it works

```
Finnhub (quotes, news, earnings) ─┐
SEC EDGAR (8-K filings) ──────────┼─► ingest ─► R2 data lake (Parquet, append-only)
                                  │
                                  └─► facts (numbers computed in code)
                                          │
                                          ▼
                                   Claude writes the words
                                          │
                                          ▼
                     validator: every number must exist in the facts,
                     no advice language, disclaimer present (auto-retry)
                                          │
                                          ▼
                   5-slide 1080×1350 carousel ─► R2 media bucket (public URLs)
```

| Slide | Content |
|---|---|
| 1 | Cover: hook, market tone, index changes |
| 2 | Scoreboard: S&P 500, Nasdaq 100, Dow, Russell, breadth of 50 big names |
| 3 | Biggest story: the top explained mover, with earnings beat/miss and sources |
| 4 | Top gainers and decliners |
| 5 | What to watch: next session's earnings |

## Schedules (GitHub Actions)

- **Hourly snapshot** (`snapshot.yml`): weekdays during market hours, stores everything.
- **Daily draft** (`daily-post.yml`): weekdays 2:12 PM PST. Builds the carousel and attaches the
  slides to the workflow run. Run it any time from **Actions → Daily after-the-bell draft → Run workflow**.

## Setup

**Secrets** (Settings → Secrets and variables → Actions → *Secrets*):
`ANTHROPIC_API_KEY`, `FINNHUB_API_KEY`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`

**Variables** (same page → *Variables* tab):
`R2_PUBLIC_URL` (e.g. `https://pub-xxxx.r2.dev`), `SEC_USER_AGENT` (e.g. `ticker.daily you@email.com`)

## Run locally

```bash
pip install -r requirements.txt && python -m playwright install chromium
python -m pytest -q
python -m tickerdaily draft --fixture tests/fixtures/sample_day.json --no-llm   # offline, sample data
cp .env.example .env   # add keys, then:
python -m tickerdaily draft
```

## Data lake layout (R2 bucket `ticker-daily-data`)

```
raw/quotes/dt=YYYY-MM-DD/<run>.parquet
raw/company_news/…   raw/market_news/…   raw/earnings/…   raw/filings/…
posts/dt=YYYY-MM-DD/<post_id>.json   # facts, copy, validator attempts, image URLs
```

Each raw row keeps the untouched API response (`payload`) so anything can be re-derived later.

## Roadmap

- [x] Phase 1: ingest, data lake, facts, writer, validator, carousel renderer
- [ ] Phase 2: Telegram approve/reject before posting
- [ ] Phase 3: Instagram Graph API publishing + Insights collection
- [ ] Phase 4: learning loop (engagement-driven scoring, retrieval over past events)

*Not financial advice. Market data from Finnhub and SEC EDGAR.*
