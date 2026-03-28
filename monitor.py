"""
Price Monitor + Email Alert
Scrapes URLs from a config file on a schedule, detects price drops,
and sends an email alert. Designed to run as a long-lived process or cron job.
"""

import csv
import json
import logging
import os
import re
import smtplib
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import httpx
from apscheduler.schedulers.blocking import BlockingScheduler
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TARGETS_FILE: str = os.getenv("TARGETS_FILE", "targets.json")
HISTORY_FILE: str = os.getenv("HISTORY_FILE", "price_history.csv")
CHECK_INTERVAL_HOURS: float = float(os.getenv("CHECK_INTERVAL_HOURS", "1"))

SMTP_HOST: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER: str = os.getenv("SMTP_USER", "")
SMTP_PASS: str = os.getenv("SMTP_PASS", "")
ALERT_TO: str = os.getenv("ALERT_TO", "")

REQUEST_TIMEOUT: float = float(os.getenv("REQUEST_TIMEOUT", "15"))

USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15",
]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class PriceTarget:
    name: str
    url: str
    price_selector: str           # CSS selector for the price element
    threshold_pct: float = 5.0   # Alert when drop >= X %
    last_price: Optional[float] = None


@dataclass
class PriceRecord:
    name: str
    url: str
    price: float
    currency: str
    checked_at: str = field(default_factory=lambda: datetime.now(tz=timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def fetch_price(target: PriceTarget) -> Optional[tuple[float, str]]:
    """Fetch current price from a URL using the configured CSS selector."""
    import random
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    try:
        resp = httpx.get(target.url, headers=headers, timeout=REQUEST_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("Request failed for %s: %s", target.name, exc)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    el = soup.select_one(target.price_selector)
    if not el:
        logger.warning("Selector '%s' not found on %s", target.price_selector, target.url)
        return None

    raw = el.get_text(strip=True)
    return _parse_price(raw)


def _parse_price(raw: str) -> Optional[tuple[float, str]]:
    """Extract numeric price and currency symbol from a raw string."""
    symbols = {"$": "USD", "€": "EUR", "£": "GBP", "₺": "TRY", "₹": "INR"}
    currency = "N/A"
    for sym, code in symbols.items():
        if sym in raw:
            currency = code
            break

    amount_str = re.sub(r"[^\d.,]", "", raw).replace(",", ".")
    # Handle "1.234.56" style — keep last dot as decimal separator
    parts = amount_str.rsplit(".", 1)
    if len(parts) == 2:
        amount_str = parts[0].replace(".", "") + "." + parts[1]

    try:
        return float(amount_str), currency
    except ValueError:
        logger.warning("Could not parse price from '%s'", raw)
        return None


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def append_history(record: PriceRecord) -> None:
    """Append a price record to the CSV history file."""
    path = Path(HISTORY_FILE)
    write_header = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(record).keys()))
        if write_header:
            writer.writeheader()
        writer.writerow(asdict(record))


def load_history(name: str) -> list[PriceRecord]:
    """Load price history for a specific target."""
    path = Path(HISTORY_FILE)
    if not path.exists():
        return []
    records: list[PriceRecord] = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["name"] == name:
                records.append(
                    PriceRecord(
                        name=row["name"],
                        url=row["url"],
                        price=float(row["price"]),
                        currency=row["currency"],
                        checked_at=row["checked_at"],
                    )
                )
    return records


# ---------------------------------------------------------------------------
# Email alert
# ---------------------------------------------------------------------------

def send_alert(target: PriceTarget, new_price: float, old_price: float, currency: str) -> None:
    """Send an email alert when a price drop is detected."""
    if not all([SMTP_USER, SMTP_PASS, ALERT_TO]):
        logger.warning("Email credentials not configured — skipping alert.")
        return

    drop_pct = ((old_price - new_price) / old_price) * 100
    subject = f"Price Drop Alert: {target.name} — {drop_pct:.1f}% off"
    body = (
        f"Price drop detected!\n\n"
        f"Product: {target.name}\n"
        f"URL: {target.url}\n\n"
        f"Previous price: {currency} {old_price:.2f}\n"
        f"Current price:  {currency} {new_price:.2f}\n"
        f"Drop: {drop_pct:.1f}%\n\n"
        f"Checked at: {datetime.now(tz=timezone.utc).isoformat()}"
    )

    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = ALERT_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        logger.info("Alert sent for %s (%.1f%% drop)", target.name, drop_pct)
    except Exception as exc:
        logger.error("Failed to send email: %s", exc)


# ---------------------------------------------------------------------------
# Main check loop
# ---------------------------------------------------------------------------

def load_targets(path: str = TARGETS_FILE) -> list[PriceTarget]:
    """Load price targets from JSON config file."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [PriceTarget(**item) for item in data]


def check_all_targets() -> None:
    """Check prices for all configured targets and trigger alerts on drops."""
    try:
        targets = load_targets()
    except FileNotFoundError:
        logger.error("Targets file not found: %s", TARGETS_FILE)
        return

    logger.info("Checking %d target(s)...", len(targets))

    for target in targets:
        result = fetch_price(target)
        if result is None:
            continue

        current_price, currency = result
        record = PriceRecord(name=target.name, url=target.url, price=current_price, currency=currency)
        append_history(record)

        history = load_history(target.name)
        if len(history) >= 2:
            previous_price = history[-2].price
            if current_price < previous_price:
                drop_pct = ((previous_price - current_price) / previous_price) * 100
                logger.info("%s: %.2f → %.2f (%.1f%% drop)", target.name, previous_price, current_price, drop_pct)
                if drop_pct >= target.threshold_pct:
                    send_alert(target, current_price, previous_price, currency)
            else:
                logger.info("%s: %.2f %s (no drop)", target.name, current_price, currency)
        else:
            logger.info("%s: first reading — %.2f %s", target.name, current_price, currency)

        time.sleep(1)  # Polite delay between requests


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info(
        "Price monitor starting. Interval: every %.1f hour(s). Targets: %s",
        CHECK_INTERVAL_HOURS,
        TARGETS_FILE,
    )

    # Run once immediately on start
    check_all_targets()

    scheduler = BlockingScheduler()
    scheduler.add_job(
        check_all_targets,
        "interval",
        hours=CHECK_INTERVAL_HOURS,
        id="price_check",
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
