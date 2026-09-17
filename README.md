# Job Hunter Bot

Async Telegram bot that aggregates job listings (design jobs in Tokyo) from Japanese job boards.

## Features

- **Asynchronous Scraping**: Efficiently scrapes multiple job boards concurrently.
- **Modular Architecture**: Built with clean architecture principles using Strategy and Repository patterns.
- **Easy Extensibility**: Simple process to add new job board sources.

## Architecture

```
main.py                          ← Composition root (DI wiring)
├── config/settings.py           ← Pydantic Settings from .env
├── core/                        ← Infrastructure (exceptions, logging, DI container)
├── models/                      ← Domain models (JobPosting, SourcePlatform)
├── database/                    ← Repository pattern over SQLAlchemy async
│   ├── repository.py            ← JobRepository ABC
│   └── sqlalchemy_repository.py ← Concrete implementation
├── scrapers/                    ← Strategy pattern
│   ├── base.py                  ← BaseScraper ABC
│   ├── orchestrator.py          ← Runs scrapers in parallel, deduplicates
│   └── implementations/         ← One file per job board
└── bot/                         ← aiogram v3 handlers & middleware
```

### Key design decisions

- **Strategy pattern**: Each job board is one scraper class. The orchestrator treats them polymorphically.
- **Repository pattern**: Application code works with `JobPosting` domain models; SQLAlchemy is an implementation detail.
- **Manual DI**: No framework — all wiring is explicit in `main.py`.
- **Immutable domain model**: `JobPosting` is frozen after construction.
- **Single `SourcePlatform` enum**: Shared by models, ORM, scrapers, and bot — add a member here when adding a new board.

## Setup

```bash
# Install dependencies
uv sync --dev

# Install Playwright browser (needed for JS-rendered scrapers)
uv run playwright install chromium

# Copy and edit the environment file
cp .env.example .env
# Edit .env → paste your Telegram bot token from @BotFather

# Run
uv run python main.py
```

## Standalone search (no bot)

`search_cli.py` runs the same scrapers **without** the Telegram bot, token,
or scheduler. It scrapes the selected boards, dedupes against the SQLite
database, marks which listings are new since the last run, persists the new
ones, and prints text or JSON. This is also what the `japan-job-search`
Hermes skill (`~/.hermes/skills/productivity/japan-job-search/`) drives.

```bash
# Default: design roles in Tokyo, both boards, readable output
uv run python search_cli.py

# Custom query + location, JSON, top 10 (new listings first)
uv run python search_cli.py --keyword "frontend engineer" --location tokyo --json -n 10

# Only Wantedly, only what's new since the last run
uv run python search_cli.py --source wantedly --new-only

# One-off search that must NOT persist to the DB
uv run python search_cli.py --keyword "UX researcher" --no-save
```

Key flags: `--keyword/-k`, `--location/-l` (Wantedly slug; `any` disables),
`--source/-s` (`wantedly`, `mynavi2027`, or `all`), `--limit/-n`,
`--new-only`, `--no-save`, `--json`, `--headful`, `--db`, `--verbose`.
Run `--help` for the full list.

**Source note:** Wantedly is the general-purpose board (keyword + location
go into its search URL). Mynavi 2027 is a new-grad, design-focused,
nationwide board scraped by occupation code — it ignores `--location` and
only best-effort-filters arbitrary keywords, so use `--source wantedly` for
general searches.

### Ingest mode (Indeed & other browser-fetched listings)

Indeed Japan is behind Cloudflare and can't be scraped headless (the old bot
got banned; direct requests return 403). But a **real browser passes the
Cloudflare check** and serves full listings. So Indeed has **no scraper** —
instead an agent (the Hermes `japan-job-search` skill) drives a real browser,
extracts the cards, and pipes them into the **same** dedup / DB / new-flagging
pipeline via `--ingest`:

```bash
echo '[{"title":"Backend Engineer","company":"Acme","url":"https://jp.indeed.com/viewjob?jk=abc123","location":"Tokyo"}]' \
  | uv run python search_cli.py --ingest --json
```

Records are JSON (a list, or `{"jobs": [...]}`); each needs `title` + `url`
(`company`, `location`, `salary`, `source_platform` optional — source
defaults to `indeed`). Because Indeed's id lives in the `?jk=` query,
`normalize_url` preserves query strings so listings don't collapse.

## Adding a new job board

1. Add the platform to `models/enums.py` → `SourcePlatform`
2. Create `scrapers/implementations/<board>_scraper.py` subclassing `BaseScraper`
3. Register the instance in the `scrapers` list in `main.py`

No other files need to change.

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and command list |
| `/jobs` | List all scraped jobs (paginated) |
| `/jobs <source>` | Filter by platform (e.g. `/jobs wantedly`) |
| `/stats` | Job counts by platform |
| `/subscribe` | Get notified about new jobs |
| `/unsubscribe` | Stop notifications |
| `/scrape` | Manually trigger scrape (admin only) |

## Testing

```bash
uv run pytest -v
```
