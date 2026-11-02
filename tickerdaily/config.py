"""All settings come from environment variables (GitHub Secrets in CI, a .env file locally).
Nothing secret is ever hard-coded here, because this repo is public."""
import os
from pathlib import Path


def _load_dotenv() -> None:
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def _env(name: str, default: str = "") -> str:
    # Strip stray spaces/newlines that sneak in when secrets are pasted.
    return (os.getenv(name) or default).strip()

ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY", "")
FINNHUB_API_KEY = _env("FINNHUB_API_KEY", "")
MODEL = _env("TICKER_MODEL", "claude-sonnet-5-5")

R2_ACCOUNT_ID = _env("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = _env("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = _env("R2_SECRET_ACCESS_KEY", "")
R2_DATA_BUCKET = _env("R2_DATA_BUCKET", "ticker-daily-data")
R2_MEDIA_BUCKET = _env("R2_MEDIA_BUCKET", "ticker-daily-media")
R2_PUBLIC_URL = _env("R2_PUBLIC_URL", "").rstrip("/")  # https://pub-xxxx.r2.dev

# SEC asks every client to identify itself with a contact email.
SEC_USER_AGENT = _env("SEC_USER_AGENT", "ticker.daily contact@example.com")

LOCAL_DATA_DIR = Path(_env("TICKER_DATA_DIR", "data"))
OUTPUT_DIR = Path(_env("TICKER_OUTPUT_DIR", "out"))

# Market barometers shown on the wrap slide (ETFs track the indices).
INDEXES = {"SPY": "S&P 500", "QQQ": "Nasdaq 100", "DIA": "Dow 30", "IWM": "Russell 2000"}

# Companies we track daily. Large, widely followed names = engaged audience.
WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "AMD", "NFLX",
    "ORCL", "CRM", "ADBE", "INTC", "QCOM", "PLTR", "UBER", "SHOP", "COIN", "SNOW",
    "JPM", "BAC", "GS", "V", "MA", "BRK.B", "WMT", "COST", "HD", "NKE",
    "DIS", "KO", "PEP", "MCD", "SBUX", "LLY", "UNH", "JNJ", "PFE", "XOM",
    "CVX", "BA", "CAT", "GE", "F", "GM", "RIVN", "ARM", "SMCI", "MU",
]

COMPANY_NAMES = {
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "Nvidia", "GOOGL": "Alphabet", "AMZN": "Amazon",
    "META": "Meta", "TSLA": "Tesla", "AVGO": "Broadcom", "AMD": "AMD", "NFLX": "Netflix",
    "ORCL": "Oracle", "CRM": "Salesforce", "ADBE": "Adobe", "INTC": "Intel", "QCOM": "Qualcomm",
    "PLTR": "Palantir", "UBER": "Uber", "SHOP": "Shopify", "COIN": "Coinbase", "SNOW": "Snowflake",
    "JPM": "JPMorgan", "BAC": "Bank of America", "GS": "Goldman Sachs", "V": "Visa", "MA": "Mastercard",
    "BRK.B": "Berkshire Hathaway", "WMT": "Walmart", "COST": "Costco", "HD": "Home Depot", "NKE": "Nike",
    "DIS": "Disney", "KO": "Coca-Cola", "PEP": "PepsiCo", "MCD": "McDonald's", "SBUX": "Starbucks",
    "LLY": "Eli Lilly", "UNH": "UnitedHealth", "JNJ": "Johnson & Johnson", "PFE": "Pfizer", "XOM": "Exxon Mobil",
    "CVX": "Chevron", "BA": "Boeing", "CAT": "Caterpillar", "GE": "GE Aerospace", "F": "Ford",
    "GM": "General Motors", "RIVN": "Rivian", "ARM": "Arm Holdings", "SMCI": "Super Micro", "MU": "Micron",
}

SCHEMA_VERSION = 1


def market_date():
    """Today's date in New York, rolled back to Friday on weekends (runners use UTC)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    d = datetime.now(ZoneInfo("America/New_York")).date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def r2_enabled() -> bool:
    return all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY])
