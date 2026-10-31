# CyberSyn Intel // Test Infrastructure & Quality Assurance Architecture
**Document Version:** 1.0.0  
**Target:** CyberSyn Intel (SS13 Player & Server Playtime Tracker)  
**Author:** Test Writer Track  
**Status:** Active & Ready  

---

## 1. Overview & Test Philosophy

CyberSyn Intel is an autonomous, standalone SS13 playtime tracker and Syndicate Black Ops tactical terminal. Because it operates continuously against external BYOND servers and public hub APIs, reliability, network resilience, protocol conformance, and database consistency are mission-critical.

The test infrastructure is constructed on the following core principles:
1. **Opaque-Box Specification Conformance:** Tests are derived strictly from the design specifications (`ORIGINAL_REQUEST.md`, `DESIGN_DOC.md`, `PROJECT.md`, and `spec_miner_survey_3/report.md`) without coupling to private implementation internals.
2. **Progressive Testability:** The test suite supports progressive execution across implementation milestones. Unimplemented modules are gracefully skipped using `pytest.importorskip`, allowing Milestone workers (M1 through M4) to execute their respective test subsets immediately as components are delivered.
3. **Zero External Network Dependencies:** All socket and HTTP operations are 100% mocked in-memory or through local ephemeral loopback interfaces. Tests run fast, reliably, and deterministically without touching live BYOND infrastructure or public internet endpoints.
4. **Adversarial & Edge-Case Hardening:** Comprehensive verification of boundary conditions, including corrupt packet headers, truncated reads, socket hangs, HTML-polluted server titles, Cyrillic/symbol ckeys, and strict enforcement of the 300-second grace period mathematical invariant.

---

## 2. Test Architecture & Technology Stack

| Component | Tool / Library | Role & Justification |
| :--- | :--- | :--- |
| **Test Runner** | `pytest 9.0.2` | Standard Python test execution framework with rich plugin ecosystem and assertion introspection. |
| **Async Orchestration** | `pytest-asyncio 1.3.0` | Enables native `async def test_*` coroutines with `asyncio_mode = auto` configured via `pytest.ini`. |
| **HTTP Transport Mocking** | `httpx.MockTransport` | In-process HTTP mocking for testing hub scraper JSON/text ingestion and failure cascades. |
| **FastAPI Test Client** | `httpx.AsyncClient` with `ASGITransport` | Tests ASGI application endpoints asynchronously without launching an external Uvicorn process. |
| **Topic Socket Mocking** | `MockTopicServer` (`asyncio.start_server`) | Real ephemeral TCP socket server on `127.0.0.1:0` that replicates BYOND binary Topic communication. |
| **Database Isolation** | `aiosqlite` on `tmp_path` | Each database test provisions a dedicated temporary SQLite database in WAL mode, ensuring complete test isolation. |

---

## 3. Tier Breakdown & Test Matrix

The test suite is structured into five distinct verification tiers:

```
tests/
├── conftest.py            # Global fixtures: MockTopicServer, MockHubTransport, test_db, async_client
├── test_config.py         # AppConfig settings loader, defaults, and aliases (6 tests)
├── test_protocol.py       # Tier 1: BYOND Topic binary protocol & async client (25 tests)
├── test_scraper.py        # Tier 2: BYOND Hub scraper, text fallback, and custom servers (15 tests)
├── test_db_session.py     # Tier 3: SQLite WAL, ckey normalizer, and 300s grace period state machine
├── test_web.py            # Tier 4: FastAPI endpoints, REST JSON API, and HTMX SSR partials
└── test_e2e.py            # Tier 5: Full simulated end-to-end integration scenario
```

### Tier 1: Protocol & Binary Socket Client (`tests/test_protocol.py`)
- **Packet Formulation (`make_topic_packet`):**
  - Verifies `\x00\x83` magic header prefix.
  - Verifies 16-bit big-endian payload length: $L = \text{len}(query) + 6$.
  - Verifies 5 null padding bytes (`\x00\x00\x00\x00\x00`).
  - Verifies null-terminated ASCII query string (`?status\x00`, `?ping\x00`).
  - Validates automatic prepending of `?` if omitted.
- **Packet Deserialization (`parse_topic_response`):**
  - `0x06` String: Decodes ASCII/UTF-8 responses, strips trailing nulls, unpacks URL query parameters (`players=12&version=516`) and comma-separated player lists (`Fox,Wolf`).
  - `0x2a` Float: Unpacks 32-bit IEEE 754 float in x86 little-endian byte order (`<f`), covering positive, negative, zero, and large float values.
  - `0x00` Null: Correctly handles empty null payload responses.
- **Adversarial & Edge Cases:**
  - Truncated headers (< 4 bytes) return `None` or raise controlled `ValueError`.
  - Corrupted magic bytes (e.g. `\x01\x83`, `HTTP`) are rejected.
  - Payload length mismatch (declared length > actual bytes) is dropped safely.
  - Unsupported type bytes (e.g. `0x99`) return `None`.
- **Async Socket Execution (`query_topic`):**
  - End-to-end TCP query against `MockTopicServer` returning string and float payloads.
  - Strict 3.0s (or configurable) timeout: hanging servers return `None` without hanging the worker.
  - Connection refused on closed ports returns `None` without unhandled exceptions.
  - Semaphore bounding: executes under `asyncio.Semaphore(30)` concurrency gating.

### Tier 2: Hub Scraper & Server Registry (`tests/test_scraper.py`)
- **Title Normalization (`clean_server_title`):**
  - Strips HTML tags (`<b>`, `<i>`) and unescapes HTML entities (`&amp;`, `&#8212;`).
  - Cleans BYOND DM bracket escapes (`\[...\]`).
  - Provides fallback title `"Unknown Station"` for empty strings.
- **Format Parsers:**
  - `parse_hub_json`: Handles JSON array format and dictionary-keyed format (`world/1`), extracting identifier, IP, port, title, and player lists.
  - `parse_hub_text`: Regex/DM fallback parser extracting `world/\d+` blocks, `url = "byond://..."`, `status = "..."`, and `players = list(...)`.
- **Custom Server Merging (`merge_custom_servers`):**
  - Merges unannounced private servers from `config.json`.
  - Deduplicates by `identifier` (`byond://ip:port`), allowing custom server titles to override hub titles while preserving live player lists.
- **Network Resilience & Fallbacks (`fetch_hub_servers`):**
  - Fallback from JSON to text format when BYOND hub returns HTML forum redirects.
  - Fallback to text format when JSON endpoint returns HTTP 500.
  - Complete network failure resilience: returns custom servers when all hub endpoints fail.

### Tier 3: Database & Sessionization Engine (`tests/test_db_session.py`)
- **SQLite Initialization:**
  - Enforces `PRAGMA journal_mode = WAL;`.
  - Enforces `PRAGMA synchronous = NORMAL;`.
  - Verifies creation of `servers`, `players`, and `sessions` tables and indexes (`idx_sessions_active`).
- **Ckey Normalization:**
  - Validates `re.sub(r'[^a-z0-9]', '', raw.lower())` against mixed case, punctuation, spaces, unicode, and pure-symbol inputs.
- **5-Minute (300s) Grace Period State Machine:**
  - *Phase 1 (Creation):* Initial player sighting creates an active open session (`ended_at IS NULL`, `duration = 0`).
  - *Phase 2 (Extension):* Continuous observation updates `last_seen_at` and increments `duration_seconds`.
  - *Phase 3 (Transient Disconnect):* Absence $< 300\text{ seconds}$ maintains open session (`ended_at IS NULL`).
  - *Phase 4 (Seamless Reconnect):* Reappearance within $300\text{ seconds}$ resumes session without creating a new session record.
  - *Phase 5 (Grace Expiry):* Absence $\ge 300\text{ seconds}$ finalizes session with:
    $$\text{ended\_at} = \text{last\_seen\_at}$$
    $$\text{duration\_seconds} = \text{last\_seen\_at} - \text{started\_at}$$
    *Absence time is strictly excluded from playtime duration.*
  - *Phase 6 (Re-entry):* Subsequent sighting creates a second session record.
- **Dossier Generation (`get_player_dossier`):**
  - Aggregates online status, total playtime seconds, total hours, per-server breakdown with percentage, and recent 10 sessions.

### Tier 4: Web Application & HTMX Interface (`tests/test_web.py`)
- **Health Check (`GET /health`):** Returns status 200 with `{ "status": "ok", "tracker": { ... } }`.
- **Servers API (`GET /api/v1/servers`):** Returns JSON array of active monitored servers.
- **Player API (`GET /api/v1/player/{ckey}`):**
  - Returns 200 with JSON dossier metrics for known player.
  - Returns 404 with structured error detail for unknown player.
- **HTMX Dynamic Search (`POST /search`):**
  - Form search for known player returns `player_dossier.html` fragment.
  - Form search for unknown player returns `not_found.html` fragment with status 200 (never 500).
  - Empty or invalid ckey searches handle gracefully without crashes.
- **Direct Dossier Page (`GET /player/{ckey}`):** Renders full HTML page with permalink context.
- **Root Dashboard (`GET /`):** Renders tactical terminal shell with search bar and Syndicate Black Ops styling.

### Tier 5: Full Simulated End-to-End Scenario (`tests/test_e2e.py`)
- Full simulation orchestrating:
  1. SQLite initialization in WAL mode.
  2. Server discovery via mock hub transport and custom server merge.
  3. TCP Topic query against `MockTopicServer` returning live player status.
  4. Database sessionization tick (new session creation).
  5. Web API verification of active online status (`GET /health`, `/api/v1/player/honkmaster`, `POST /search`).
  6. Subsequent ticks simulating continuous play, transient disconnect (< 300s), seamless reconnect, and final disconnect (>= 300s).
  7. Verification of session finalization, offline status, and accurate duration calculation.

---

## 4. Mocking Strategy & Isolation

### Socket Mocking (`MockTopicServer`)
Defined in `tests/conftest.py`:
```python
@pytest.fixture
async def mock_topic_server():
    server = MockTopicServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()
```
Capabilities:
- `set_string_response(text: str)`: Formulates valid `\x00\x83` packet with `0x06` string payload.
- `set_float_response(value: float)`: Formulates valid packet with `0x2a` 32-bit float (`<f`).
- `set_delay(seconds: float)`: Delays responses to test client timeout behavior.
- `set_close_immediately()`: Simulates unexpected connection drops.

### HTTP Mocking (`httpx.MockTransport`)
Simulates BYOND Hub responses without external HTTP calls:
```python
@pytest.fixture
def mock_hub_transport():
    ...
```
Routes `format=json` to mock JSON payloads and fallback queries to mock DM text payloads.

### Database Mocking (`test_db`)
Provides an isolated file path in `tmp_path`, automatically initializes WAL tables, and executes clean connection teardown and WAL checkpoint on test completion.

---

## 5. Execution Reference & Commands

### Run Entire Test Suite
```bash
pytest -v
```

### Run by Specific Tier
```bash
# Tier 1: Protocol tests
pytest tests/test_protocol.py -v

# Tier 2: Scraper tests
pytest tests/test_scraper.py -v

# Tier 3: Database & Session engine tests
pytest tests/test_db_session.py -v

# Tier 4: Web API & HTMX tests
pytest tests/test_web.py -v

# Tier 5: End-to-End integration scenario
pytest tests/test_e2e.py -v
```

### Run with Coverage Reporting
```bash
pytest --cov=core --cov=web --cov-report=term-missing
```

---

## 6. Verification Status

- **Syntax & Compilation:** 100% verified (`py_compile` passed for all test modules).
- **Milestone 1 Validation:** 46 passing tests (`test_config.py`, `test_protocol.py`, `test_scraper.py`).
- **Milestone 2-4 Readiness:** All test cases pre-written, type-annotated, and configured for progressive execution.
