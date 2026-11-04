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
| 3 | Biggest story: the top explained mover, earnings beat/miss, a **Context** line from memory, sources |
| 4 | Top gainers and decliners |
| 5 | What to watch: next session's earnings |

## Daily lineup (weekdays, Pacific time)

| Time | Post | Workflow | Approval |
|---|---|---|---|
| ~7:00 AM | ☀️ **Morning Brief**: the open, earnings reactions, early movers, today's calendar | `morning-brief.yml` | **Auto-posts** after a 10-minute veto window (tap ⛔ to stop it) |
| ~11:00 AM | 🔎 **Deep Dive**: one company in depth (earnings first, else the biggest news-driven move) | `deep-dive.yml` | Tap ✅ |
| ~2:15 PM | 🔔 **After the Bell**: the daily wrap | `daily-post.yml` | Tap ✅ |

Plus `snapshot.yml` (hourly market snapshots) and `instagram-upkeep.yml` (metrics, token).
Each format posts at most once per market day, never with stale data, and never on a closed market.
Turn Morning Brief auto-posting off with the repo variable `MORNING_AUTOPUBLISH=false`.

**Company logos** appear next to tickers, for identification only (from Finnhub company profiles,
cached in R2 under `logos/`). Missing or broken logos fall back to a monogram tile. After design
changes, run **Actions → Preview slide designs** to get sample slides in Telegram.

## Approvals (Telegram)

Each draft is sent to Telegram with **Approve / Reject / Regenerate** buttons. Taps go to a
Cloudflare Worker (`worker/bot.js`), which verifies they came from `TELEGRAM_CHAT_ID` and starts
`decision.yml`. Every decision is logged to `decisions/` in R2. Failed runs alert you in Telegram.

**Approve publishes** the carousel to Instagram (once only; drafts are blocked after the next
market open so stale numbers never go out). `instagram-upkeep.yml` collects metrics at 1h/24h/7d
and refreshes the Instagram token weekly.

## Setup

**Secrets** (Settings → Secrets and variables → Actions → *Secrets*):
`ANTHROPIC_API_KEY`, `FINNHUB_API_KEY`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_WEBHOOK_SECRET`, `IG_ACCESS_TOKEN`

**Variables** (same page → *Variables* tab):
`R2_PUBLIC_URL` (e.g. `https://pub-xxxx.r2.dev`), `SEC_USER_AGENT` (e.g. `ticker.daily you@email.com`), `IG_USER_ID`

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
decisions/dt=YYYY-MM-DD/<post_id>-<run>.json   # your approve/reject/regen taps
published/dt=YYYY-MM-DD/<post_id>.json         # Instagram media id + permalink
metrics/<post_id>/{1h,24h,7d}.json             # reach, likes, saves, shares…
state/spotlight_index.json                     # what we covered each day (feeds memory)
```

Each raw row keeps the untouched API response (`payload`) so anything can be re-derived later.

## Roadmap

- [x] Phase 1: ingest, data lake, facts, writer, validator, carousel renderer
- [x] Phase 2: Telegram approve/reject/regenerate (Cloudflare Worker in `worker/`)
- [x] Phase 3: Instagram publishing on Approve, token refresh, insights at 1h/24h/7d
- [x] Phase 4a: memory (earnings track record, 52-week range, streaks, past-spotlight follow-through)
- [ ] Phase 4b: learning loop (engagement-driven scoring and hook style)

*Not financial advice. Market data from Finnhub and SEC EDGAR.*
