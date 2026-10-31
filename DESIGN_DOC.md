# ДИЗАЙН-ДОКУМЕНТ (DESIGN DOCUMENT)
## Проект: CyberSyn Intel // SS13 Player & Server Playtime Tracker
**Версия документа:** 1.0  
**Статус:** Утверждён к реализации  
**Автор:** Antigravity & User  

---

## 1. Введение и концепция проекта

### 1.1. Обзор
**CyberSyn Intel** — это высокоскоростной, автономный веб-сервис и трекер онлайна игроков Space Station 13. Система работает как единый монолитный процесс, непрерывно собирает данные с официального хаба BYOND и напрямую опрашивает серверы по бинарному протоколу BYOND Topic, ведя точный учёт сыгранного времени каждого игрока по серверам.

Интерфейс выполнен в минималистичном стиле терминала разведки Синдиката (Black Ops / Syndicate Uplink): глубокие обсидиановые тона, стальные панели, приглушённые алые акценты и мгновенный поиск досье любого `ckey`.

### 1.2. Ключевые цели
* **Абсолютная автономность**: Работает «из коробки» без внешних сервисов (без Node.js, без Docker, без внешнего PostgreSQL) на базе Python + SQLite.
* **Минимальная нагрузка**: Опрос серверов раз в 120 секунд с параллельным пулом сокетов и 5-минутным grace-периодом для исключения разрывов сессий при крашах или перезаходах.
* **Быстрый фокусный UX**: Только то, что нужно пользователю: поиск по `ckey`, текущий статус (Online/Offline, сервер), общее наигранное время, горизонтальные шкалы распределения по серверам и история сессий.

---

## 2. Архитектура системы

### 2.1. Диаграмма компонентов
```mermaid
flowchart TD
    subgraph Engine [Единый процесс Python (FastAPI + Asyncio)]
        subgraph Collector [Фоновый сервис трекинга]
            HubScraper["Hub Scraper<br/>(BYOND Hub API JSON)"] --> ServerDiscovery["Реестр серверов<br/>(Авто-хаб + кастомные)"]
            ServerDiscovery --> TopicPool["Асинхронный сокет-пул<br/>(BYOND Topic Protocol \x00\x83)"]
            TopicPool --> SessionManager["Sessionization Engine<br/>(Нормализация ckey, 5m Grace)"]
        end

        subgraph Storage [Локальное хранилище]
            SessionManager --> DB[(SQLite tracker.db<br/>aiosqlite + WAL Mode)]
        end

        subgraph WebLayer [Веб-сервер]
            DB --> QueryEngine["API & Data Service"]
            QueryEngine --> FastAPIRoutes["FastAPI Endpoints"]
            FastAPIRoutes --> JinjaRenderer["Jinja2 + HTMX Fragments"]
        end
    end

    UserBrowser["Браузер пользователя<br/>(Синдикат Black Ops UI)"] <--> FastAPIRoutes
```

### 2.2. Технологический стек
| Компонент | Технология | Обоснование |
| :--- | :--- | :--- |
| **Язык & Рантайм** | Python 3.10+ | Высокая скорость разработки, встроенный `asyncio` для асинхронных сокетов. |
| **Веб-фреймворк** | FastAPI + Uvicorn | Высокая производительность (ASGI), встроенное управление фоновыми задачами (`lifespan`). |
| **База данных** | SQLite 3 (`aiosqlite`) | Нулевая настройка, хранение в одном файле `tracker.db`, режим WAL гарантирует параллельное чтение без блокировок. |
| **Сетевые сокеты** | `asyncio.open_connection` | Прямая работа с сокетами TCP для отправки бинарных пакетов BYOND `\x00\x83`. |
| **Фронтенд** | Jinja2 + HTMX | Динамическое обновление без full-page reload и без сборщиков (Webpack/Vite). |
| **Стилизация** | Tailwind CSS (CDN / Bundled) | Быстрая верстка тёмного тактического UI. |

---

## 3. Сбор данных и протокол BYOND Topic

### 3.1. Дискавери серверов (Hub Scraping)
* **Эндпоинт**: `http://www.byond.com/games/SpaceStation13?format=json` (с fallback на `format=text`).
* **Периодичность**: каждые 120 секунд.
* **Кастомные серверы**: возможность прописать в `config.json` неанонсированные/приватные серверы (`ip:port`), которые опрашиваются наравне с публичными.

### 3.2. Спецификация BYOND Topic Protocol
Для опроса серверов формируется специальный бинарный пакет:

```
[0x00][0x83][Len High][Len Low][0x00 * 5][Query String \0]
```
1. Байты 0–1: Сигнатура протокола `0x00 0x83`.
2. Байты 2–3: 16-битный Big-Endian размер полезной нагрузки (`len(query) + 6`).
3. Байты 4–8: 5 нулевых байтов (`\x00\x00\x00\x00\x00`).
4. Байт 9+: Строка запроса `?status` или `?players`, оканчивающаяся нулевым байтом `\x00`.

**Обработка ответа сервера:**
* Сервер отвечает заголовком: `0x00 0x83`, длина, 1 байт типа данных:
  - `0x06` — ASCII строка (URL-encoded параметры или список через запятую).
  - `0x2a` — 32-битный IEEE float (при ping/числовых ответах).
* Из ответа парсятся параметры: `players` (число), `playerlist` / `list` (список ckey), `map_name`, `round_duration` и т.д.

### 3.3. Параллелизм и защита от зависаний
* **Семафор**: одновременно опрашивается не более 30 сокетов (`asyncio.Semaphore(30)`).
* **Таймаут**: 3.0 секунды на сокет. Серверы, превысившие таймаут, помечаются как `offline` в текущем тике.

---

## 4. Модель данных и движок сессий (Sessionization)

### 4.1. Схема базы данных SQLite
```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

-- Серверы (из хаба и кастомные)
CREATE TABLE IF NOT EXISTS servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identifier TEXT UNIQUE NOT NULL,      -- e.g. "byond://178.62.241.130:1337"
    title TEXT NOT NULL,                  -- e.g. "Tau Ceti Classic"
    ip TEXT NOT NULL,
    port INTEGER NOT NULL,
    last_seen TIMESTAMP NOT NULL,
    is_active BOOLEAN DEFAULT 1
);

-- Нормализованные игроки
CREATE TABLE IF NOT EXISTS players (
    ckey TEXT PRIMARY KEY,                -- e.g. "honkmaster" (lowercase, [a-z0-9])
    first_seen TIMESTAMP NOT NULL,
    last_seen TIMESTAMP NOT NULL
);

-- Сессии онлайна
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ckey TEXT NOT NULL REFERENCES players(ckey),
    server_id INTEGER NOT NULL REFERENCES servers(id),
    started_at TIMESTAMP NOT NULL,
    last_seen_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP,                  -- NULL пока сессия активна
    duration_seconds INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sessions_ckey ON sessions(ckey);
CREATE INDEX IF NOT EXISTS idx_sessions_active ON sessions(ended_at) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_server ON sessions(server_id);
```

### 4.2. Алгоритм трекинга сессий
1. **Нормализация ckey**: `ckey = re.sub(r'[^a-z0-9]', '', raw_name.lower())`.
2. **Вход игрока / продолжение игры**:
   * Для каждого обнаруженного на сервере `ckey` ищется сессия, где `ended_at IS NULL` и `server_id = current_server.id`.
   * Если сессия найдена: обновляется `last_seen_at = now()`, `duration_seconds = now() - started_at`.
   * Если сессия не найдена: создаётся новая открытая сессия (`started_at = now()`, `last_seen_at = now()`).
3. **Выход игрока и 5-минутный Grace-период**:
   * Во время каждого цикла сканируются все открытые сессии (`ended_at IS NULL`).
   * Если игрок не был замечен на сервере и `now() - last_seen_at >= 300` секунд (5 минут):
     Сессия закрывается: `ended_at = last_seen_at`, `duration_seconds = last_seen_at - started_at`.
   * Если игрок переподключился в пределах 5 минут — сессия не прерывается, а продолжается бесшовно.

---

## 5. Спецификация UI/UX: Синдикат / Black Ops Terminal

### 5.1. Цветовая палитра и стили
* **Background Primary**: `#0a0b0e` (глубокий обсидиан)
* **Surface / Panels**: `#12151b` (оружейная сталь)
* **Borders & Dividers**: `#222733` / `#2e3545` (технологичный контур)
* **Syndicate Red (Primary Accent)**: `#bb0202` (приглушённый кармин) и `#e53935` (активный алый)
* **Indicator Glow**: `box-shadow: 0 0 10px rgba(229, 57, 53, 0.25)`
* **Status Colors**:
  - 🟢 **Online**: `#00e676` (изумрудный индикатор активной цели)
  - 🔴 **Offline**: `#e53935` / `#64748b` (цель потеряна из виду)
  - 🟡 **Info & Timestamps**: `#deb700` (янтарный моноширинный акцент)
* **Типографика**: Моноширинный стек (`JetBrains Mono`, `Fira Code`, `Consolas`, `monospace`). Высокая контрастность, отсутствие утомляющих сканлайнов.

### 5.2. Состояния экрана и компоненты

#### А. Главный экран (Начальное состояние)
* Стилизованный заголовок Синдиката:  
  `[ CYBERSYN INTEL NETWORK // TARGET DOSSIER TRACKER ]`
* Центральное тактическое поле поиска:
  ```
  ┌─────────────────────────────────────────────────────────────┬──────────────┐
  │ [ ENTER CKEY: honkmaster__________________________________ ] │ [ TRACK ]    │
  └─────────────────────────────────────────────────────────────┴──────────────┘
  ```
* Запуск поиска по нажатию **Enter** или клику на кнопку.
* Прямой URL: `/player/{ckey}` (позволяет делиться ссылкой на статистику или открывать закладку).

#### Б. Карточка досье игрока (Результат поиска)
1. **Статус-блок цели**:
   * Если Online:  
     🟢 `[TARGET ACTIVE: SECTOR // TAU CETI CLASSIC] — ON STATION: 1h 24m`
   * Если Offline:  
     🔴 `[TARGET INACTIVE / LAST SEEN: 4 HOURS AGO ON WHITE DREAM 1]`
2. **Сводные метрики**:
   * Всего наиграно: `184.2 ч`
   * Зафиксировано серверов: `4`
   * Первый контакт: `14.03.2024`
   * Крайний визит: `28.10.2026`
3. **Распределение часов по серверам (Горизонтальные шкалы)**:
   ```
   [TAU CETI CLASSIC]  ████████████████████░░░░░░░░  112.4 ч  (61%)  [ПОСЛЕДНИЙ ВИЗИТ: 2 ЧАСА НАЗАД]
   [WHITE DREAM 1]     ████████░░░░░░░░░░░░░░░░░░░░   48.2 ч  (26%)  [ПОСЛЕДНИЙ ВИЗИТ: 3 ДНЯ НАЗАД]
   [TG MANUEL]         ███░░░░░░░░░░░░░░░░░░░░░░░░░   23.6 ч  (13%)  [ПОСЛЕДНИЙ ВИЗИТ: 2 НЕДЕЛИ НАЗАД]
   ```
   Шкалы заполняются алым тактическим градиентом с мягким свечением.
4. **История последних сессий**:
   Компактный журнал последних 10 высадок: Сервер, Время начала, Продолжительность сессии.

#### В. Состояние «Цель не найдена»
Если `ckey` отсутствует в архиве:
```
[!] NO RECORD FOUND FOR SPECIFIED CKEY
TARGET HAS NOT BEEN DETECTED ON ANY MONITORED SECTOR FREQUENCIES.
```

---

## 6. Спецификация API и маршрутов

| Метод | URL | Описание | Формат ответа |
| :--- | :--- | :--- | :--- |
| `GET` | `/` | Главная страница с поисковой строкой | HTML (Jinja2) |
| `GET` | `/player/{ckey}` | Страница досье конкретного игрока | HTML (Jinja2) |
| `POST` | `/search` | HTMX-эндпоинт поиска (отдаёт фрагмент досье) | HTML Fragment |
| `GET` | `/api/v1/player/{ckey}` | Публичный JSON API статистики игрока | JSON |
| `GET` | `/api/v1/servers` | Список активных отслеживаемых серверов | JSON |
| `GET` | `/health` | Статус приложения и воркера трекера | JSON |

---

## 7. Файловая структура проекта

```
serene-hypatia/
├── DESIGN_DOC.md               # Данный дизайн-документ
├── config.json                 # Конфигурация (интервал, grace-период, custom servers)
├── requirements.txt            # Python-зависимости (fastapi, uvicorn, aiosqlite, httpx, jinja2)
├── run.py                      # Точка входа для запуска (python run.py)
├── core/
│   ├── __init__.py
│   ├── config.py               # Загрузка и валидация настроек
│   ├── byond_topic.py          # Бинарный протокол BYOND Topic (\x00\x83)
│   ├── hub_scraper.py          # Парсер официального BYOND SS13 Hub JSON
│   ├── db.py                   # Инициализация SQLite WAL и CRUD-операции
│   └── tracker.py              # Фоновый движок сессий и опросник
└── web/
    ├── __init__.py
    ├── app.py                  # Приложение FastAPI и роуты
    ├── static/
    │   └── css/
    │       └── style.css       # Кастомные стили Синдикат/Black Ops и анимации
    └── templates/
        ├── base.html           # Базовый каркас (шрифты, метатеги, Tailwind)
        ├── index.html          # Главная страница поиска
        └── partials/
            ├── player_dossier.html # Фрагмент досье игрока
            └── not_found.html      # Фрагмент отсутствия данных
```

---

## 8. План реализации (Roadmap)

1. **Фаза 1: Сетевое ядро и протокол BYOND**
   - Реализация `byond_topic.py` (формирование пакета `0x00 0x83`, отправка, парсинг ответов).
   - Реализация `hub_scraper.py` (получение списка серверов с хаба).
   - Тестирование на живых серверах BYOND.
2. **Фаза 2: База данных и движок сессий**
   - Настройка `db.py` (таблицы `servers`, `players`, `sessions`, режим WAL).
   - Разработка `tracker.py` (машина состояний сессий, grace-период 5 мин, фоновый цикл 120с).
3. **Фаза 3: Веб-сервер и маршруты**
   - Настройка `web/app.py` и связывание жизненного цикла воркера с FastAPI.
   - Эндпоинты `/`, `/player/{ckey}`, `/search`, `/api/v1/player/{ckey}`.
4. **Фаза 4: Верстка интерфейса Синдиката**
   - Разработка `base.html`, `index.html` и компонентов HTMX.
   - Стилизация под Black Ops (обсидиан, алый, моноширинный шрифт, горизонтальные шкалы часов).
5. **Фаза 5: Комплексное тестирование и запуск**
   - Сквозное тестирование: запуск воркера, поиск игроков, верификация расчёта часов.
