
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
