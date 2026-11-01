
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
The terminal will launch on `http://localhost:8080`; `web.port` in `config.json` controls the port.
The default config and relative database paths resolve from the project directory.
`HILLEYE_DB_PATH` overrides the configured database location. Invalid config files stop startup.

### Running Tests
```bash
pip install -r requirements-dev.txt
python -m pytest -v
```
The unit, adversarial, concurrency, and E2E tests use temporary databases and local network mocks.

### Collection and monitoring

`GET /live` checks the web process. `/health` and `/ready` return HTTP 503 until a healthy
collection cycle completes, and on stale collection, Hub failures, Topic failures, low disk
space, or clock changes. Monitor these endpoints externally. Multiple web workers sharing
one local SQLite file elect one collector through an OS file lock; a follower retries
ownership every second after the owner exits. SQLite and the lock file must be on the same
local filesystem, not separate per-container volumes or network filesystems.

Missing rosters do not confirm player departures. Observation windows expire after
`tracker.stale_server_seconds` (default 900); gaps beyond the session grace period are
excluded from playtime. Topic rosters and counts take precedence over Hub values.
Unknown telemetry is nullable; the page shows when telemetry was last received.
BYOND.world pager addresses support Hub discovery only: configure a direct host/port to
enable Topic metrics. To map a known pager to its direct endpoint, give the custom entry
its exact Hub `identifier` (for example `byond://BYOND.world.12345`) and explicit `ip` and
`port`. They are not automatically equated with another endpoint by title.

Hub endpoints may only connect to public IPs after DNS validation. Explicit `custom_servers`
are trusted and may use private addresses. Manifest requests share the socket limit,
coalesce simultaneous requests for one server, and reuse results for three seconds.
The Hub body is limited to 4 MiB and discovery to 2,000 records; the cycle deadline defaults
to 300 seconds. The chart uses timestamps, leaves gaps over ten minutes, samples dense
observations by minute maxima, and calculates summary statistics from the original samples.

### Service installation and data safety

Create the service account before installing `hilleye.service`:

```bash
sudo useradd --system --user-group --home /var/lib/hilleye --shell /usr/sbin/nologin hilleye
```

The unit stores data in `/var/lib/hilleye/tracker.db` and requires read access to the code
and virtual environment under `/opt/Hilleye`. To move an existing database, stop all writers,
make a SQLite backup including committed WAL contents, restore it into the state directory,
and grant `hilleye:hilleye` ownership before starting the unit. Do not copy only the `.db`
file while WAL writers are running. Validate the unit and permissions on the Linux host;
the repository's Windows tests do not exercise systemd. Retries are unlimited with a five
second delay; configure an external alert for persistent failures.

Schema changes are additive and transactional. On first upgraded startup, duplicate open
sessions are finalized at their last observation without deleting rows, and server/player
aggregates are backfilled. Session triggers maintain aggregates for inserts, updates and
deletes, including external SQL writers. Older application versions can read the expanded
schema; roll back by stopping all processes before replacing the code. Keep a verified
pre-upgrade SQLite backup to restore the exact pre-migration open-session state.

Players and session history are retained until a retention policy is chosen. Low free disk
space makes readiness fail (threshold `tracker.min_free_disk_mb`, default 100). Snapshot
retention is seven days, runs after at least 24 hours of stable process time, and pauses
after a clock jump. Backups, restoration drills, disk alerts and a history/archive policy
remain deployment responsibilities; this service does not silently delete session history.

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
