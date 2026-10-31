# Hilleye // CyberSyn Intel

**Space Station 13 Player & Server Playtime Intelligence Network**

Hilleye (CyberSyn Intel) is an autonomous, high-performance web service and telemetry tracker for Space Station 13. It operates as a monolithic process, continuously monitoring the official BYOND hub, querying live game servers over the binary BYOND Topic protocol (`\x00\x83`), and tracking player playtime per server with SQLite WAL storage and a Syndicate Black Ops tactical terminal interface.

---

## Key Features

- **Syndicate Black Ops Tactical Terminal UI**:
  - Obsidian (`#0a0b0e`), gunmetal (`#12151b`), Syndicate carmine (`#bb0202`), and active crimson (`#e53935`) theme.
  - Target status indicator (🟢 Active on station / 🔴 Inactive with last seen timestamp).
  - Horizontal server playtime distribution bars with crimson gradient glow.
  - Recent incursion session history logs.
  - Interactive HTMX search with live URL bar synchronization (`HX-Push-Url`).
  - Standalone bundled HTMX for 100% offline autonomy without external CDN dependencies.
- **BYOND Topic Binary Protocol Client**:
  - Implements DreamDaemon `world/Topic()` binary protocol (`0x00 0x83`, big-endian payload length, 5 null padding bytes, null-terminated query string).
  - Unpacks both ASCII string (`0x06`) and 32-bit float (`0x2a`) payloads.
  - Asynchronous socket pool with semaphore concurrency control (default: 30) and 3.0s timeout protection against server hangs.
- **Hub Scraper & Custom Server Registry**:
  - Scrapes the official BYOND SS13 Hub (`format=json` with automatic regex fallback to `format=text`).
  - Merges unannounced or private custom servers configured in `config.json`.
- **SQLite WAL Session Engine**:
  - High concurrency using `aiosqlite` with `PRAGMA journal_mode=WAL` and `PRAGMA synchronous=NORMAL`.
  - Automatic `ckey` normalization (`re.sub(r'[^a-z0-9]', '', raw.lower())`).
  - **5-minute (300s) Grace Period**: Prevents broken sessions during transient disconnects, client crashes, or server restarts while accurately concluding absent sessions without phantom hours.
- **RESTful JSON API**:
  - `GET /health` — Operational telemetry and tracker engine metrics.
  - `GET /api/v1/servers` — Monitored sector frequencies and statuses.
  - `GET /api/v1/player/{ckey}` — Machine-readable player dossier with per-server breakdown and recent sessions.
  - `POST /search` — HTMX search fragment renderer.
  - `GET /player/{ckey}` — Direct target dossier permalink.

---

## Quickstart

### Prerequisites
- Python 3.10+ (tested on Python 3.10–3.14)
- `pip`

### Installation
```bash
git clone https://github.com/ImJustKisik/Hilleye.git
cd Hilleye
pip install -r requirements.txt
```

### Running the Service
```bash
python run.py
```
The terminal will launch on `http://localhost:8000`.

### Running Tests
```bash
pytest -v
```
All 220 automated unit, adversarial, concurrency, and E2E tests will execute.

---

## Project Structure
```
Hilleye/
├── config.json              # Service configuration (intervals, timeouts, custom servers)
├── requirements.txt         # Production dependencies
├── run.py                   # Application entrypoint (CLI launcher)
├── core/
│   ├── byond_topic.py       # Binary BYOND Topic protocol client
│   ├── config.py            # Settings loader & validator
│   ├── db.py                # SQLite WAL schema & query layer
│   ├── hub_scraper.py       # BYOND Hub scraper (JSON + text fallback)
│   └── tracker.py           # Background session tracking worker
├── web/
│   ├── app.py               # FastAPI application & REST routes
│   ├── static/
│   │   ├── css/style.css    # Syndicate Black Ops stylesheet
│   │   └── js/htmx.min.js   # Bundled HTMX
│   └── templates/
│       ├── base.html        # Tactical terminal frame
│       ├── index.html       # Search console & monitored sectors
│       └── partials/
│           ├── not_found.html      # Tactical "NOT FOUND" state
│           └── player_dossier.html # Player telemetry dossier
└── tests/                   # 220 automated test suite (Tiers 1-5)
```

---

## License
MIT License.
