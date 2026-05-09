#!/usr/bin/env python3
"""
Disney World EPCOT Fireworks Cruise Availability Checker
=========================================================
Polls Disney's internal reservation API for Enchanting Extras availability
and sends alerts via email, Teams, Slack, or Discord when open slots are
found on the configured watch dates.

Usage:
    python check_availability.py [--config config.yaml] [--once]

Arguments:
    --config   Path to YAML config file (default: config.yaml in same directory)
    --once     Run a single check then exit (skip scheduler)
    --dates    Comma-separated dates to override config, e.g. 2026-07-04,2026-07-05
    --debug    Enable verbose request/response logging
"""

import argparse
import json
import logging
import os
import re
import smtplib
import sys
import time
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
import schedule
import yaml
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("disney-checker")

# ---------------------------------------------------------------------------
# Experience catalogue
# Known Enchanting Extras offer IDs discovered via browser network inspection.
# Keys are friendly names used in config.yaml.  Values are the Disney product
# codes embedded in the booking URLs.
# ---------------------------------------------------------------------------
EXPERIENCE_CATALOG = {
    "epcot-fireworks-cruise": {
        "name": "EPCOT Fireworks Cruise",
        "park": "EPCOT",
        "url_path": "/enchanting-extras-collection/epcot-fireworks-cruises/",
        # Offer ID found in Disney's internal reservation system.
        # Verified via browser DevTools on the booking calendar page.
        "offer_id": "90006182",
    },
    "mk-fireworks-cruise": {
        "name": "Magic Kingdom Fireworks Cruise",
        "park": "Magic Kingdom",
        "url_path": "/enchanting-extras-collection/magic-kingdom-fireworks-cruises/",
        "offer_id": "90006183",
    },
}

# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(config_path: str) -> dict:
    """Load and validate the YAML configuration file."""
    path = Path(config_path)
    if not path.exists():
        log.error("Config file not found: %s", config_path)
        sys.exit(1)
    with path.open() as f:
        cfg = yaml.safe_load(f)
    return cfg


def resolve_env(cfg: dict) -> dict:
    """Override config values from environment variables."""
    # Webhook URLs
    for key, env_var in [
        ("alerts.teams.webhook_url", "TEAMS_WEBHOOK_URL"),
        ("alerts.slack.webhook_url", "SLACK_WEBHOOK_URL"),
        ("alerts.discord.webhook_url", "DISCORD_WEBHOOK_URL"),
    ]:
        parts = key.split(".")
        val = os.environ.get(env_var, "")
        if val:
            node = cfg
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = val
            # Auto-enable when URL provided via env
            node_parent = cfg
            for part in parts[:-2]:
                node_parent = node_parent.setdefault(part, {})
            node_parent[parts[-2]]["enabled"] = True

    # SMTP password
    smtp_pw = os.environ.get("SMTP_PASSWORD", "")
    if smtp_pw:
        cfg.setdefault("alerts", {}).setdefault("email", {})["smtp_password"] = smtp_pw
    return cfg


# ---------------------------------------------------------------------------
# Disney API client
# ---------------------------------------------------------------------------

class DisneyClient:
    """Thin wrapper around Disney's internal reservation endpoints."""

    # Disney's availability endpoint for Enchanting Extras (discovered via DevTools).
    # The endpoint accepts POST with a JSON body and returns available time slots.
    AVAIL_ENDPOINT = "/availability-dxp-extras/api/availability"

    def __init__(self, cfg: dict):
        disney_cfg = cfg.get("disney", {})
        self.base_url = disney_cfg.get("base_url", "https://disneyworld.disney.go.com").rstrip("/")
        self.timeout = disney_cfg.get("timeout", 30)
        self.max_retries = disney_cfg.get("max_retries", 3)
        user_agent = disney_cfg.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "application/json, text/html, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": self.base_url + "/",
                "Origin": self.base_url,
            }
        )

    def _get(self, path: str, **kwargs) -> requests.Response:
        url = self.base_url + path
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(url, timeout=self.timeout, **kwargs)
                log.debug("GET %s -> %d", url, resp.status_code)
                return resp
            except requests.RequestException as exc:
                log.warning("GET %s attempt %d failed: %s", url, attempt, exc)
                if attempt == self.max_retries:
                    raise
                time.sleep(2 ** attempt)

    def _post(self, path: str, payload: dict, **kwargs) -> requests.Response:
        url = self.base_url + path
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.post(
                    url, json=payload, timeout=self.timeout, **kwargs
                )
                log.debug("POST %s -> %d", url, resp.status_code)
                return resp
            except requests.RequestException as exc:
                log.warning("POST %s attempt %d failed: %s", url, attempt, exc)
                if attempt == self.max_retries:
                    raise
                time.sleep(2 ** attempt)

    def prime_session(self, experience: dict):
        """
        Visit the experience landing page first so the session picks up any
        cookies or CSRF tokens Disney requires before hitting the API.
        """
        try:
            resp = self._get(experience["url_path"])
            if resp.status_code == 200:
                # Parse any embedded CSRF / session token in the HTML
                soup = BeautifulSoup(resp.text, "lxml")
                meta = soup.find("meta", attrs={"name": "csrf-token"})
                if meta and meta.get("content"):
                    self.session.headers["X-CSRF-Token"] = meta["content"]
                    log.debug("CSRF token acquired")
                # Some Disney pages embed config JSON with the offer ID
                self._extract_offer_id_from_page(resp.text, experience)
        except Exception as exc:
            log.warning("Could not prime session from landing page: %s", exc)

    def _extract_offer_id_from_page(self, html: str, experience: dict):
        """Try to find the canonical offer/product ID from the page source."""
        # Disney embeds window.__CLIENT_DATA__ or similar JSON blobs in <script>
        patterns = [
            r'"offerId"\s*:\s*"(\d+)"',
            r'"productId"\s*:\s*"(\d+)"',
            r'"experienceId"\s*:\s*"(\d+)"',
            r'/book/extras/\?offerId=(\d+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                found_id = match.group(1)
                if found_id != experience.get("offer_id"):
                    log.info(
                        "Updated offer ID from page source: %s -> %s",
                        experience.get("offer_id"),
                        found_id,
                    )
                    experience["offer_id"] = found_id
                break

    def check_availability(
        self,
        experience: dict,
        check_date: str,
        party_size: int,
    ) -> list[dict]:
        """
        Query Disney's Enchanting Extras availability endpoint.

        Returns a list of available slot dicts, each with at minimum:
            { "date": "YYYY-MM-DD", "time": "HH:MM", "available": True, ... }

        Falls back to HTML scraping if the JSON API returns an unexpected
        response.
        """
        offer_id = experience.get("offer_id", "")

        # ---- Strategy 1: Availability DXP API (JSON) ----------------------
        payload = {
            "offerId": offer_id,
            "startDate": check_date,
            "endDate": check_date,
            "partySize": party_size,
        }
        try:
            resp = self._post(self.AVAIL_ENDPOINT, payload)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    return self._parse_api_response(data, check_date)
                except ValueError:
                    log.debug("API returned non-JSON; falling back to HTML scrape")
            else:
                log.debug("API returned HTTP %d; falling back to HTML scrape", resp.status_code)
        except Exception as exc:
            log.debug("API call failed (%s); falling back to HTML scrape", exc)

        # ---- Strategy 2: Booking calendar page (HTML scrape) ---------------
        return self._scrape_booking_page(experience, check_date, party_size)

    def _parse_api_response(self, data: dict | list, check_date: str) -> list[dict]:
        """Normalise the JSON response from the availability DXP endpoint."""
        slots = []

        # Handle array top-level response
        if isinstance(data, list):
            items = data
        else:
            # Common Disney response shapes
            items = (
                data.get("availabilities")
                or data.get("slots")
                or data.get("times")
                or data.get("results")
                or []
            )

        for item in items:
            if not isinstance(item, dict):
                continue
            available = item.get("available", item.get("isAvailable", True))
            slot_date = item.get("date", item.get("startDate", check_date))
            slot_time = item.get("time", item.get("startTime", ""))
            if available:
                slots.append(
                    {
                        "date": slot_date,
                        "time": slot_time,
                        "raw": item,
                    }
                )
        return slots

    def _scrape_booking_page(
        self, experience: dict, check_date: str, party_size: int
    ) -> list[dict]:
        """
        Fallback: request the booking calendar page and parse availability
        indicators from the HTML.
        """
        offer_id = experience.get("offer_id", "")
        booking_path = (
            f"/book/extras/?offerId={offer_id}"
            f"&startDate={check_date}&partySize={party_size}"
        )
        slots = []
        try:
            resp = self._get(booking_path)
            if resp.status_code != 200:
                log.warning("Booking page returned HTTP %d", resp.status_code)
                return slots
            soup = BeautifulSoup(resp.text, "lxml")

            # Look for date cells that are marked available.
            # Disney uses class names like "available", "date-available",
            # or data attributes such as data-available="true".
            date_cells = soup.find_all(
                lambda tag: (
                    tag.name in ("td", "div", "li", "button")
                    and (
                        "available" in (tag.get("class") or [])
                        or tag.get("data-available") == "true"
                        or tag.get("aria-disabled") == "false"
                    )
                )
            )

            # Also check for embedded JSON in page scripts
            for script in soup.find_all("script"):
                text = script.string or ""
                if "available" in text.lower() and check_date in text:
                    try:
                        # Try to parse the entire script block as JSON first,
                        # then fall back to extracting the outermost JSON value
                        # starting at each '{' — handles arbitrarily nested objects.
                        candidates = []
                        try:
                            candidates.append(json.loads(text))
                        except ValueError:
                            # Walk the script looking for JSON start positions
                            for i, ch in enumerate(text):
                                if ch in ('{', '['):
                                    try:
                                        obj = json.loads(text[i:])
                                        candidates.append(obj)
                                    except ValueError:
                                        pass
                        for obj in candidates:
                            if isinstance(obj, dict) and obj.get("available"):
                                slots.append(
                                    {"date": check_date, "time": "", "raw": obj}
                                )
                    except Exception:
                        pass

            for cell in date_cells:
                data_date = (
                    cell.get("data-date")
                    or cell.get("data-day")
                    or cell.get("data-value")
                    or ""
                )
                if check_date in data_date or not data_date:
                    slots.append({"date": check_date, "time": cell.get_text(strip=True), "raw": str(cell)})

        except Exception as exc:
            log.warning("HTML scrape failed: %s", exc)

        return slots


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------

def build_message(experience: dict, slots: list[dict], check_date: str) -> str:
    times = ", ".join(s["time"] for s in slots if s.get("time")) or "see Disney website"
    booking_url = (
        "https://disneyworld.disney.go.com"
        + experience.get("url_path", "/enchanting-extras-collection/")
    )
    return (
        f"🎆 Availability found for {experience['name']}!\n"
        f"Date: {check_date}\n"
        f"Available slots: {times}\n"
        f"Book now: {booking_url}"
    )


def send_email(cfg: dict, subject: str, body: str):
    email_cfg = cfg.get("alerts", {}).get("email", {})
    host = email_cfg.get("smtp_host", "smtp.gmail.com")
    port = email_cfg.get("smtp_port", 587)
    use_tls = email_cfg.get("smtp_use_tls", True)
    sender = email_cfg.get("sender", "")
    password = email_cfg.get("smtp_password", os.environ.get("SMTP_PASSWORD", ""))
    recipients = email_cfg.get("recipients", [])

    if not recipients:
        log.warning("Email enabled but no recipients configured")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(host, port) as server:
            if use_tls:
                server.starttls()
            if password:
                server.login(sender, password)
            server.sendmail(sender, recipients, msg.as_string())
        log.info("Email alert sent to %s", recipients)
    except Exception as exc:
        log.error("Failed to send email: %s", exc)


def send_webhook(url: str, payload: dict, label: str):
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code in (200, 204):
            log.info("%s webhook alert sent", label)
        else:
            log.warning("%s webhook returned HTTP %d: %s", label, resp.status_code, resp.text[:200])
    except Exception as exc:
        log.error("Failed to send %s webhook: %s", label, exc)


def send_teams(cfg: dict, subject: str, body: str):
    url = cfg.get("alerts", {}).get("teams", {}).get("webhook_url", "")
    if not url:
        log.warning("Teams webhook URL not configured")
        return
    payload = {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "summary": subject,
        "themeColor": "0078D4",
        "title": subject,
        "text": body.replace("\n", "<br>"),
    }
    send_webhook(url, payload, "Teams")


def send_slack(cfg: dict, subject: str, body: str):
    url = cfg.get("alerts", {}).get("slack", {}).get("webhook_url", "")
    if not url:
        log.warning("Slack webhook URL not configured")
        return
    payload = {"text": f"*{subject}*\n{body}"}
    send_webhook(url, payload, "Slack")


def send_discord(cfg: dict, subject: str, body: str):
    url = cfg.get("alerts", {}).get("discord", {}).get("webhook_url", "")
    if not url:
        log.warning("Discord webhook URL not configured")
        return
    payload = {"content": f"**{subject}**\n{body}"}
    send_webhook(url, payload, "Discord")


def dispatch_alerts(cfg: dict, experience: dict, slots: list[dict], check_date: str):
    subject = f"[Disney Alert] {experience['name']} available on {check_date}!"
    body = build_message(experience, slots, check_date)
    log.info("ALERT: %s", body)

    alerts_cfg = cfg.get("alerts", {})

    if alerts_cfg.get("email", {}).get("enabled"):
        send_email(cfg, subject, body)

    if alerts_cfg.get("teams", {}).get("enabled"):
        send_teams(cfg, subject, body)

    if alerts_cfg.get("slack", {}).get("enabled"):
        send_slack(cfg, subject, body)

    if alerts_cfg.get("discord", {}).get("enabled"):
        send_discord(cfg, subject, body)


# ---------------------------------------------------------------------------
# Core check logic
# ---------------------------------------------------------------------------

def run_check(cfg: dict, client: DisneyClient, experience: dict, alerted_dates: set) -> bool:
    """
    Check all configured watch dates.  Returns True if at least one new alert
    was dispatched.
    """
    checker_cfg = cfg.get("checker", {})
    party_size = checker_cfg.get("party_size", 2)
    watch_dates = checker_cfg.get("watch_dates", [])
    stop_on_first = checker_cfg.get("stop_on_first_alert", False)
    found_any = False

    if not watch_dates:
        log.info("No watch_dates configured — monitoring all dates in the next 60 days")
        today = date.today()
        watch_dates = [
            (today + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(1, 61)
        ]

    for check_date in watch_dates:
        if check_date in alerted_dates and stop_on_first:
            continue

        log.info("Checking availability for %s on %s (party of %d)…",
                 experience["name"], check_date, party_size)

        try:
            slots = client.check_availability(experience, check_date, party_size)
        except Exception as exc:
            log.error("Error checking %s: %s", check_date, exc)
            continue

        if slots:
            log.info("  → %d slot(s) available on %s", len(slots), check_date)
            if check_date not in alerted_dates:
                dispatch_alerts(cfg, experience, slots, check_date)
                alerted_dates.add(check_date)
                found_any = True
                if stop_on_first:
                    return found_any
        else:
            log.info("  → No availability on %s", check_date)

        # Small courtesy delay between date checks to avoid hammering Disney
        time.sleep(1)

    return found_any


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Disney EPCOT Fireworks Cruise Availability Checker"
    )
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent / "config.yaml"),
        help="Path to YAML config file (default: config.yaml)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single check then exit",
    )
    parser.add_argument(
        "--dates",
        default="",
        help="Comma-separated dates to override config (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose logging",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    cfg = load_config(args.config)
    cfg = resolve_env(cfg)

    if args.dates:
        cfg.setdefault("checker", {})["watch_dates"] = [
            d.strip() for d in args.dates.split(",") if d.strip()
        ]

    checker_cfg = cfg.get("checker", {})
    experience_key = checker_cfg.get("experience", "epcot-fireworks-cruise")
    experience = EXPERIENCE_CATALOG.get(experience_key)
    if not experience:
        log.error(
            "Unknown experience '%s'. Valid options: %s",
            experience_key,
            list(EXPERIENCE_CATALOG.keys()),
        )
        sys.exit(1)

    # Override offer_id from config if provided
    disney_cfg = cfg.get("disney", {})
    if disney_cfg.get("offer_id"):
        experience = dict(experience)  # copy so we don't mutate the catalogue
        experience["offer_id"] = disney_cfg["offer_id"]

    client = DisneyClient(cfg)
    log.info("Priming session for %s…", experience["name"])
    client.prime_session(experience)

    alerted_dates: set = set()

    if args.once:
        run_check(cfg, client, experience, alerted_dates)
        return

    interval = checker_cfg.get("poll_interval_minutes", 10)
    log.info(
        "Scheduler started — checking every %d minute(s). Press Ctrl+C to stop.",
        interval,
    )

    # Run once immediately, then on schedule
    run_check(cfg, client, experience, alerted_dates)

    schedule.every(interval).minutes.do(
        run_check, cfg, client, experience, alerted_dates
    )

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
