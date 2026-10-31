# Project: CyberSyn Intel // SS13 Player & Server Playtime Tracker

## Architecture
CyberSyn Intel is a standalone, high-performance SS13 playtime tracking service and Syndicate Black Ops tactical terminal.

### Component Flow
```
[BYOND Hub API] ----(HTTP format=json / format=text)----> [core/hub_scraper.py]
                                                                  |
[config.json (custom servers)] -----------------------------------+
                                                                  v
[Live SS13 Servers] <---(TCP \x00\x83 Topic Binary Protocol)---> [core/byond_topic.py]
                                                                  |
                                                                  v
                                                         [core/tracker.py (Session Engine)]
                                                          - ckey normalizer
                                                          - 5m (300s) grace period state machine
                                                                  |
                                                                  v
                                                         [core/db.py (SQLite WAL)]
                                                          - servers, players, sessions tables
                                                                  |
                                                                  v
                                                         [web/app.py (FastAPI Lifespan)]
                                                          - REST JSON API (/api/v1/...)
                                                          - Jinja2 + HTMX SSR (/player, /search)
                                                                  |
                                                                  v
                                                    [web/templates/ & web/static/css/style.css]
                                                    (Syndicate Black Ops Tactical Terminal)
```

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Topic Packet Serializer | Constructs `\x00\x83`, 16-bit big-endian payload len (`len(q)+6`), 5 nulls, null-terminated query | M1 | ORIGINAL_REQUEST §R1 |
| 2 | Topic Packet String Deserializer | Unpacks `0x06` ASCII/UTF-8 string payload, strips trailing null, parses URL params or comma lists | M1 | ORIGINAL_REQUEST §R1 |
| 3 | Topic Packet Float Deserializer | Unpacks `0x2a` 32-bit IEEE 754 little-endian float (`<f`) | M1 | ORIGINAL_REQUEST §R1 |
| 4 | Topic Client Concurrency & Timeout | 3.0s socket timeout with `asyncio.Semaphore(30)` concurrency gating | M1 | ORIGINAL_REQUEST §R1 |
| 5 | Hub Scraper JSON Parsing | Scrapes `http://www.byond.com/games/SpaceStation13?format=json` with custom User-Agent | M1 | ORIGINAL_REQUEST §R1 |
| 6 | Hub Scraper Text Fallback | Fallback to `format=text` / `Exadv1/SpaceStation13?format=text` with DM listing regex parsing | M1 | Survey Finding |
| 7 | Custom Servers Integration | Loads and merges custom unannounced `ip:port` servers from `config.json` | M1 | ORIGINAL_REQUEST §R1 |
| 8 | SQLite WAL Initialization | Initializes `tracker.db` with `PRAGMA journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000` | M2 | ORIGINAL_REQUEST §R2 |
| 9 | Database Schema & Indexes | Tables `servers`, `players`, `sessions` with partial index on active sessions | M2 | DESIGN_DOC §4.1 |
| 10 | Ckey Normalizer | Sanitizes player names via `re.sub(r'[^a-z0-9]', '', raw.lower())` | M2 | ORIGINAL_REQUEST §R2 |
| 11 | Active Session Detection | Queries for active sessions (`ended_at IS NULL`) per server and ckey | M2 | ORIGINAL_REQUEST §R2 |
| 12 | Active Session Extension | Updates `last_seen_at = now` and `duration_seconds = now - started_at` for active players | M2 | ORIGINAL_REQUEST §R2 |
| 13 | New Session Creation | Inserts new session with `started_at = now`, `last_seen_at = now`, `ended_at = NULL` | M2 | ORIGINAL_REQUEST §R2 |
| 14 | Grace Period State Machine | Evaluates disconnections; keeps session open if offline < 300s | M2 | ORIGINAL_REQUEST §R2 |
| 15 | Grace Period Finalization | If offline >= 300s, closes session with `ended_at = last_seen_at` and `duration = last_seen - started` | M2 | ORIGINAL_REQUEST §R2 |
| 16 | Background Tracker Polling Loop | Asynchronous worker loop running every 120 seconds with error containment | M2 | ORIGINAL_REQUEST §R2 |
| 17 | FastAPI Lifespan Manager | Gracefully starts background tracker task and cancels/awaits task on SIGINT/SIGTERM | M3 | ORIGINAL_REQUEST §R3 |
| 18 | Clean DB Teardown | Closes aiosqlite connections during shutdown without leaking locks | M3 | Acceptance Criteria |
| 19 | GET /health Endpoint | Returns `{ "status": "ok", "tracker": { ... } }` with uptime and server count | M3 | ORIGINAL_REQUEST §R3 |
| 20 | GET /api/v1/servers Endpoint | Returns JSON array of active monitored servers and current populations | M3 | ORIGINAL_REQUEST §R3 |
| 21 | GET /api/v1/player/{ckey} Endpoint | Returns JSON player dossier with playtime, per-server breakdown, recent sessions | M3 | ORIGINAL_REQUEST §R3 |
| 22 | Structured API 404 Handling | Returns `{ "error": "Player not found", "ckey": "..." }` with HTTP 404 for unknown ckey | M3 | DESIGN_DOC §6 |
| 23 | GET / Dashboard Route | Renders main tactical terminal interface with interactive search input | M4 | ORIGINAL_REQUEST §R4 |
| 24 | GET /player/{ckey} Route | Renders full dossier permalink page or tactical not-found view | M4 | ORIGINAL_REQUEST §R4 |
| 25 | POST /search HTMX Route | Renders HTMX partial (`player_dossier.html` or `not_found.html`) with `HX-Push-Url` | M4 | ORIGINAL_REQUEST §R4 |
| 26 | Syndicate Black Ops Palette & CSS | Monospace styling with obsidian `#0a0b0e`, gunmetal `#12151b`, border `#222733`, carmine `#bb0202`, active crimson `#e53935`, amber `#deb700` | M4 | ORIGINAL_REQUEST §R4 |
| 27 | Target Status Indicator | Emerald indicator `#00e676` for Online targets; Crimson indicator `#e53935` for Offline targets | M4 | DESIGN_DOC §5.1 |
| 28 | Summary Metrics Display | Displays total hours, servers count, first seen, and last seen timestamps | M4 | ORIGINAL_REQUEST §R4 |
| 29 | Horizontal Playtime Distribution Bars | Gradient progress bars with hours, percentage, and last visit timestamp per server | M4 | ORIGINAL_REQUEST §R4 |
| 30 | Recent Session Log Table | Tabular display of recent session history (server, started_at, duration) | M4 | ORIGINAL_REQUEST §R4 |
| 31 | Tactical Not-Found State | Styled "NO RECORD FOUND" partial when ckey is unknown | M4 | ORIGINAL_REQUEST §R4 |
| 32 | Unit Test Suite (Protocol & Scraping) | Packet serialization/deserialization (strings, floats), hub scraping with mocks | M5 | ORIGINAL_REQUEST §R5 |
| 33 | Unit Test Suite (DB & Session Lifecycle) | Database CRUD, ckey normalizer, 5m grace expiry vs seamless reconnect | M5 | ORIGINAL_REQUEST §R5 |
| 34 | Integration Test Suite (Web API & UI) | `httpx.AsyncClient` tests for all endpoints and HTMX partial rendering | M5 | ORIGINAL_REQUEST §R5 |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Core Protocol & Hub Scraper | `core/byond_topic.py`, `core/hub_scraper.py`, `core/config.py`, `config.json` | none | DONE |
| M2 | SQLite WAL & Sessionization Engine | `core/db.py`, `core/tracker.py` | M1 | PLANNED |
| M3 | FastAPI Lifespan & REST API | `web/app.py`, `run.py` | M2 | PLANNED |
| M4 | Syndicate Black Ops Terminal Frontend | `web/templates/`, `web/static/css/style.css`, `web/static/js/htmx.min.js` | M3 | PLANNED |
| M5 | E2E Test Suite Pass & Adversarial Hardening | Phase 1: 100% E2E test pass (Tiers 1-4); Phase 2: Adversarial hardening (Tier 5) | M1, M2, M3, M4 | PLANNED |

## Interface Contracts

### 1. `core/byond_topic.py` ↔ `core/tracker.py`
```python
def make_topic_packet(query: str) -> bytes:
    """Builds BYOND Topic packet: \x00\x83 + uint16_be(len(payload)) + \x00*5 + query.encode('utf-8') + \x00"""

def parse_topic_response(data: bytes) -> Union[str, float, None]:
    """Parses response packet: verifies \x00\x83 header, extracts 0x06 string or 0x2a float."""

async def query_topic(ip: str, port: int, query: str = "?status", timeout: float = 3.0, semaphore: asyncio.Semaphore = None) -> Union[dict, float, None]:
    """Connects to server, sends topic query, decodes and parses response into dict (for 0x06 string) or float (for 0x2a)."""
```

### 2. `core/hub_scraper.py` ↔ `core/tracker.py`
```python
@dataclass
class ServerInfo:
    identifier: str  # "byond://ip:port"
    title: str
    ip: str
    port: int
    players: List[str]  # list of raw player ckeys if available
    player_count: int

async def fetch_hub_servers(client: httpx.AsyncClient, custom_servers: List[dict] = None) -> List[ServerInfo]:
    """Fetches BYOND SS13 hub listings (JSON with text fallback), merges custom servers, returns deduplicated ServerInfo list."""
```

### 3. `core/db.py` ↔ `core/tracker.py` & `web/app.py`
```python
async def init_db(db_path: str = "tracker.db") -> None:
    """Executes PRAGMA journal_mode=WAL, PRAGMA synchronous=NORMAL, PRAGMA busy_timeout=5000, and creates tables/indexes."""

async def close_db() -> None:
    """Safely closes any open pooled connections."""

async def upsert_server(identifier: str, title: str, ip: str, port: int) -> int:
    """Upserts server record and returns server_id."""

async def get_active_servers() -> List[dict]:
    """Returns list of active servers with last_seen and online status."""

async def update_player_sessions(server_id: int, online_ckeys: Set[str], now_ts: float, grace_period_seconds: int = 300) -> None:
    """Performs sessionization step: extends active sessions for seen players, finalizes sessions expired >= grace period."""

async def get_player_dossier(ckey: str) -> Optional[dict]:
    """Returns player dossier dictionary:
    {
        "ckey": str,
        "is_online": bool,
        "current_server": Optional[str],
        "current_session_duration": Optional[str],
        "last_seen": str,
        "first_seen": str,
        "total_playtime_seconds": int,
        "total_hours": float,
        "servers_count": int,
        "server_breakdown": [
            {
                "title": str,
                "seconds": int,
                "hours": float,
                "percentage": float,
                "last_visit": str
            }
        ],
        "recent_sessions": [
            {
                "server_title": str,
                "started_at": str,
                "ended_at": Optional[str],
                "duration_str": str
            }
        ]
    }
    """
```

### 4. `web/app.py` ↔ `web/templates/`
- Context passed to templates:
  - `request`: Request
  - `dossier`: Optional[dict]
  - `query_ckey`: str
  - `servers`: List[dict]
  - `status`: dict

## Code Layout
```
c:\Users\pizzalover\Documents\antigravity\serene-hypatia\
├── config.json                 # Global configuration (intervals, grace period, timeouts, custom servers)
├── requirements.txt            # Python dependencies (fastapi, uvicorn, aiosqlite, httpx, jinja2, pytest)
├── run.py                      # Application entry point (CLI launcher: py run.py)
├── core/
│   ├── __init__.py
│   ├── config.py               # Settings loader & validator
│   ├── byond_topic.py          # BYOND Topic \x00\x83 binary protocol client
│   ├── hub_scraper.py          # BYOND SS13 Hub scraper with JSON & text fallback
│   ├── db.py                   # SQLite WAL schema, connection pool & queries
│   └── tracker.py              # Background polling worker & session state machine
├── web/
│   ├── __init__.py
│   ├── app.py                  # FastAPI app, lifespan context manager & route handlers
│   ├── static/
│   │   ├── css/
│   │   │   └── style.css       # Syndicate Black Ops tactical styles
│   │   └── js/
│   │       └── htmx.min.js     # Bundled HTMX for autonomous offline operation
│   └── templates/
│       ├── base.html           # Tactical terminal base layout
│       ├── index.html          # Main search interface
│       └── partials/
│           ├── player_dossier.html # Player metrics & server breakdown partial
│           └── not_found.html      # Tactical "NO RECORD FOUND" partial
└── tests/
    ├── conftest.py             # Pytest fixtures (async client, test database, mock servers)
    ├── test_protocol.py        # Packet serialization, deserialization & unpacking tests
    ├── test_scraper.py         # Hub scraper parsing and custom servers merging tests
    ├── test_db_session.py      # SQLite WAL, session lifecycle & 5-minute grace period tests
    ├── test_web.py             # REST API & HTML route tests via httpx.AsyncClient
    └── test_e2e.py             # End-to-end integration scenario tests
```
