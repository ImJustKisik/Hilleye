
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
Shared fixtures in `tests/conftest.py` provide a local TCP Topic server,
`httpx.MockTransport` for Hub responses, and an ASGI client for web routes.
Run a focused module with `python -m pytest tests/test_protocol.py -v`.
The optional selfbot tests require `discord.py-self` and `aiohttp`, as documented
in `examples/selfbot_example.py`; they skip when those packages are absent.

### Collection and monitoring

`GET /live` checks the web process. `/health` and `/ready` return HTTP 503 while the
database is unavailable, the collector is stopped, no recent cycle has completed, or the
leader's clock is untrusted. Hub and Topic failures in a completed cycle return HTTP 200
with `status: degraded`, `ready: true`, and `collection_status: degraded`; diagnostics
remain in `tracker`. Monitor HTTP availability and collection quality separately.
Multiple web workers sharing
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
├── config.json              # Service configuration
├── pager_endpoints.json     # Configured pager-to-endpoint mappings
├── requirements.txt         # Production dependencies
├── requirements-dev.txt     # Test dependencies
├── run.py                   # Application entrypoint
├── core/
│   ├── assets/fonts/        # Bundled dossier-card fonts
│   ├── byond_topic.py       # Binary BYOND Topic protocol client
│   ├── config.py            # Settings loader & validator
│   ├── db/                  # Tracker SQLite schema, sessions & queries
│   ├── log_parser/          # SierraBay12 log parsing
│   ├── logs_db/             # Log indexing, search, uploads & access control
│   ├── map_renderer/        # Map parsing, rendering & cache management
│   ├── hub_scraper.py       # BYOND Hub scraper (JSON + text fallback)
│   └── tracker.py           # Background session tracking worker
├── web/
│   ├── app.py               # FastAPI application & lifespan
│   ├── routes/              # Tracker pages, API & health routes
│   ├── logs_routes/         # Log viewer, OAuth & admin routes
│   ├── static/              # CSS, JavaScript, images & audio
│   └── templates/           # Jinja2 pages & HTMX partials
├── deploy/                  # Nginx configuration
├── hilleye.service          # systemd service unit
├── backup.sh                # SQLite backup script
├── examples/                # Optional Discord selfbot & shared exports
├── tools/resolve_pager.py    # Windows BYOND pager endpoint resolver
└── tests/                   # Unit, integration & regression tests
```

`DESIGN_DOC.md` preserves the original design; `BUGS_AND_RELIABILITY.md` records
the reliability audit. Current startup and test instructions are maintained here.
Generated previews belong in `output/` and are ignored by Git. Python bytecode
and `.pytest_cache/` can be removed and regenerate as needed. SQLite databases,
their WAL/SHM files, collector locks, uploaded logs, and `map_cache/` are runtime
state; keep them when cleaning development artifacts.

---

### SierraBay12 Log Viewer (`/logs`)
- Protected by Discord OAuth2 + user ID whitelist (`discord` section in `config.json` or `HILLEYE_DISCORD_CLIENT_ID`, `HILLEYE_DISCORD_CLIENT_SECRET`, `HILLEYE_DISCORD_REDIRECT_URI`, `HILLEYE_SESSION_SECRET`, `HILLEYE_DISCORD_SUPERADMIN_IDS`).
- Configure `logs.root_dir` (`HILLEYE_LOGS_DIR`) to point to SierraBay12's `data/logs` directory (`game.log`, `qdel.log`), and upload `.log`/`.txt`/`.zip`/`.tar.gz` logs manually through the `/logs` web UI (`logs.uploads_dir`, `HILLEYE_LOGS_UPLOADS_DIR`).
- Uses a dedicated SQLite WAL + FTS5 database (`logs.db`, override with `HILLEYE_LOGS_DB_PATH`) for fast cross-round search, dynamic whitelist management (`/logs/admin`), and access audit logging.

---

## License
MIT License.
