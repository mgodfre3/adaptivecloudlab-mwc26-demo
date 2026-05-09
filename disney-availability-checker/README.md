# Disney EPCOT Fireworks Cruise – Availability Checker

A Python polling tool that monitors Disney World's [Enchanting Extras Fireworks Cruise](https://disneyworld.disney.go.com/enchanting-extras-collection/epcot-fireworks-cruises/) for availability on your desired dates and alerts you immediately when a slot opens up.

> **Note:** Disney does not publish a public API. This tool uses the same internal endpoints and web pages that the Disney website uses. Disney may update their site at any time, which could require adjustments to the script.

---

## How it works

1. **Session priming** – visits the experience landing page to acquire cookies/CSRF tokens Disney requires.
2. **API query** – POSTs to Disney's internal `availability-dxp-extras` JSON endpoint with the experience offer ID and your target date.
3. **HTML scrape fallback** – if the JSON endpoint doesn't respond as expected, the tool falls back to scraping the booking calendar page for availability indicators.
4. **Alert** – when availability is found, sends notifications via any enabled channel (email, Teams, Slack, Discord).
5. **Schedules** – repeats on the configured interval until stopped.

---

## Setup

### Prerequisites

- Python 3.11+
- pip

### Install dependencies

```bash
cd disney-availability-checker
pip install -r requirements.txt
```

---

## Configuration

Copy or edit `config.yaml`:

```yaml
checker:
  experience: "epcot-fireworks-cruise"   # or "mk-fireworks-cruise"
  watch_dates:
    - "2026-07-04"
    - "2026-12-31"
  party_size: 2
  poll_interval_minutes: 10
  stop_on_first_alert: false
```

### Experience options

| Key | Description |
|-----|-------------|
| `epcot-fireworks-cruise` | EPCOT Luminous: The Symphony of Us Fireworks Cruise |
| `mk-fireworks-cruise` | Magic Kingdom Fireworks Cruise |

### Alerting

Enable one or more channels in `config.yaml` and supply credentials/URLs:

#### Email (Gmail / SMTP)

```yaml
alerts:
  email:
    enabled: true
    smtp_host: "smtp.gmail.com"
    smtp_port: 587
    smtp_use_tls: true
    sender: "you@gmail.com"
    recipients:
      - "you@gmail.com"
```

Set your app password as an environment variable (never commit it to source control):

```bash
export SMTP_PASSWORD="your-app-password"
```

#### Microsoft Teams

```bash
export TEAMS_WEBHOOK_URL="https://outlook.office.com/webhook/..."
```

Or set `alerts.teams.webhook_url` in `config.yaml`.

#### Slack

```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
```

#### Discord

```bash
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
```

---

## Usage

### Single check (great for testing)

```bash
python check_availability.py --once
```

### Override dates on the command line

```bash
python check_availability.py --once --dates 2026-07-04,2026-12-31
```

### Continuous polling (scheduler)

```bash
python check_availability.py
```

Runs immediately, then repeats every `poll_interval_minutes` (default: 10).

### Enable debug logging

```bash
python check_availability.py --debug
```

### Full usage

```
usage: check_availability.py [-h] [--config CONFIG] [--once] [--dates DATES] [--debug]

Disney EPCOT Fireworks Cruise Availability Checker

options:
  --config CONFIG  Path to YAML config file (default: config.yaml)
  --once           Run a single check then exit
  --dates DATES    Comma-separated dates to override config (YYYY-MM-DD)
  --debug          Enable verbose logging
```

---

## Running as a background service

### systemd (Linux)

```ini
[Unit]
Description=Disney Fireworks Cruise Availability Checker
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/disney-checker
ExecStart=/usr/bin/python3 check_availability.py
Environment=SMTP_PASSWORD=your-password
Environment=TEAMS_WEBHOOK_URL=https://...
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

### Docker

```bash
docker run -d \
  -v $(pwd)/config.yaml:/app/config.yaml \
  -e SMTP_PASSWORD=your-password \
  -e TEAMS_WEBHOOK_URL=https://... \
  python:3.11-slim sh -c "pip install -r /app/requirements.txt && python /app/check_availability.py"
```

### Azure Container Instance (one-liner)

```bash
az container create \
  --resource-group my-rg \
  --name disney-checker \
  --image python:3.11-slim \
  --environment-variables TEAMS_WEBHOOK_URL="https://..." \
  --secure-environment-variables SMTP_PASSWORD="..." \
  --command-line "sh -c 'pip install requests pyyaml schedule beautifulsoup4 lxml && python /app/check_availability.py'" \
  --restart-policy Always
```

---

## Tips

- **60-day booking window** – Disney opens reservations exactly 60 days before the experience date. Set your `watch_dates` for your trip and let the checker run automatically as the 60-day mark approaches.
- **Best opening times** – Availability often appears at midnight–6 AM ET on the 60-day mark. Consider running the checker around that time.
- **Respectful polling** – The default 10-minute interval is intentionally conservative to avoid triggering Disney's rate limiting.
- **Offer ID override** – If the built-in offer IDs become stale, capture the actual ID by opening the booking page in Chrome DevTools → Network → filter for "extras" or "availability" while browsing the cruise page. Update `disney.offer_id` in `config.yaml`.

---

## Disclaimer

This tool is a personal convenience utility for monitoring publicly visible reservation availability on the official Disney website. It is not affiliated with or endorsed by Walt Disney World or The Walt Disney Company. Use responsibly and in accordance with Disney's Terms of Service.
