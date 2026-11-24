
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
pip install --require-hashes -r requirements.txt
```

### Running the Service
```bash
python run.py
```
The terminal will launch on `http://localhost:8080`; `web.port` in `config.json` controls the port.
The default config and relative database paths resolve from the project directory.
`HILLEYE_CONFIG_PATH` selects the configuration file; explicit environment variables override its values.
`HILLEYE_DB_PATH` overrides the tracker database location. Invalid config files or unwritable runtime directories stop startup.

Dependencies are locked in `uv.lock`; `requirements*.txt` are generated exports. An alternative setup is
`uv sync --locked` followed by `uv run --locked python run.py`. See [operations and rollout](docs/OPERATIONS.md).

### Running Tests
```bash
pip install --require-hashes -r requirements-dev.txt
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
leader's clock is untrusted. A completed cycle with Hub/collection errors returns HTTP 200
with `status: degraded`, `ready: true`, and `collection_status: degraded`. Unreachable Topic
targets alone do not degrade a healthy Hub collection; their counters remain in `tracker`.
Log-source errors are reported independently in `logs` without changing tracker readiness.
HTTP p50/p95, event-loop lag, database/WAL sizes and backup time are also exposed in `/health`.
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
file while WAL writers are running. The unit explicitly places both databases, uploads and map cache
in the state directory. `tools/service_smoke.py --runtime` tests a separate service with two workers
under the same hardening settings; it requires root and a venv built with `/usr/bin/python3`. Retries are unlimited with a five
second delay; configure an external alert for persistent failures.

Schema changes are additive and transactional. On first upgraded startup, duplicate open
sessions are finalized at their last observation without deleting rows, and server/player
aggregates are backfilled. Session triggers maintain aggregates for inserts, updates and
deletes, including external SQL writers. Stop all processes for the first upgrade; mixed-version writers are unsupported. Log index migrations
are additive and preserve the active generation until its replacement is ready. Roll back by stopping
all processes and restoring the previous code and matching pre-upgrade databases together.

Players and session history are retained until a retention policy is chosen. Low free disk
space makes readiness fail (threshold `tracker.min_free_disk_mb`, default 100). Snapshot
retention is 90 days, runs after at least 24 hours of stable process time, and pauses
after a clock jump. `python -m core.maintenance backup` creates verified backups of both databases and raw logs;
`verify` and `restore` validate and restore them. Successful archives have seven-day local retention.
Scheduling, off-host copies and disk alerts remain deployment responsibilities. This service does
not silently delete session history. Full backup and rollback instructions are in [OPERATIONS.md](docs/OPERATIONS.md).

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
- Select **Celadon** as the source in **Раунды** to find rounds by date and ID and import their logs. Celadon must allowlist the Hilleye server's outbound IPv4. Manual uploads and Celadon imports have a 20 MiB per-file cap. `logs.max_upload_mb` or `HILLEYE_LOGS_MAX_UPLOAD_MB` can lower it.
- Uses a dedicated SQLite WAL + FTS5 database (`logs.db`, override with `HILLEYE_LOGS_DB_PATH`) for fast cross-round search, dynamic whitelist management (`/logs/admin`), and access audit logging.

---

## License
MIT License.

### Log indexing and background jobs

`logs.periodic_scan` defaults to true, `logs.scan_interval_seconds` to 60, and
`logs.regex_timeout_seconds` to 5. `logs.auto_scan_on_startup` only controls the immediate
first scan. Missing or partially readable sources preserve the last index; confirmed missing
rounds are removed only after two complete scans at least a minute apart. A changed disk
file is checked against the digest of its indexed prefix. Use reindex for edits that
preserve file metadata.

Regex work runs in a terminable process. Two OS-shared slots bound heavy user operations;
capacity errors return 429 with `Retry-After`, regex deadlines return 408. Ordinary indexed
pages use byte offsets; complex filters preserve their streaming behavior.

The UI submits refresh/reindex in the background. `POST /logs/refresh` and `/logs/reindex`
accept `background=1` and return 202 with `job_id`. The default waits up to 25 seconds
before returning the job ID. Index scans with individual source or round errors report
`partial`. The selected DEBUG indexing policy persists across later refreshes.
The existing background map rebuild response also contains `job_id`. Authenticated users can
read `/api/v1/logs/jobs/{job_id}` for their jobs and `/api/v1/logs/status`; admins can inspect all jobs.
Index and map jobs have independent OS-elected owners. Job state, heartbeat and results survive
web-worker changes. Map assets are published as complete versions, preserving previous versions.

To refresh the generated requirements with uv 0.12.23:

```bash
uv lock
uv export --quiet --locked --no-dev --no-emit-project --format requirements.txt --output-file requirements.txt
uv export --quiet --locked --no-emit-project --format requirements.txt --output-file requirements-dev.txt
```
