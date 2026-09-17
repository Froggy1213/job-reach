# Job Reach — a Hermes Agent plugin

Search Japanese job boards, remember what you have already seen, and get told
only about what is new. Runs as a native [Hermes Agent](https://github.com/NousResearch/hermes-agent)
plugin: seven LLM-callable tools, one skill, and scheduled monitoring through
`hermes cron`.

---

## From manual research to agent support

The first version of this project was a **manual research tool**. You opened
Telegram, typed `/jobs wantedly`, and the bot fetched listings and sent them
back. The research stayed yours: you chose what to search, you read the cards,
you remembered what you had already seen. An agent could only reach it through a
shim — `hermes.py` hard-coded an absolute project path — driven by a shell
wrapper that had to strip `VIRTUAL_ENV` first, because Hermes' Python and the
project's Python fought over `pydantic_core`.

This version makes the opposite bet: **the agent is the operator**, and the
repository's job is to be good at being operated. That changes what "good" means
at every layer:

- **The interface is read by a model, not typed by a person.** Each tool schema
  has to carry the knowledge needed to choose correctly: which board answers
  which query, that Mynavi is new-grad design only and ignores location, that
  Indeed is the widest market but the only board that can be bot-blocked, that a
  scrape takes seconds to a minute and must not be retried in a loop. That prose
  is the product, not decoration.
- **Output is a contract.** Every tool returns a stable JSON envelope
  (`summary`, `jobs[]`, `is_new`); `--json` writes nothing but JSON to stdout
  while logs go to stderr; `is_new` makes "what changed?" a field instead of a
  judgement call.
- **Failures are instructions.** A failed board returns
  `{"success": false, "error": …, "hint": …}` where the hint is the exact
  command that fixes it, and one broken board never discards the others.
  Handlers never raise — a stack trace is useless to a model.
- **The tool may not break its host.** The engine imports nothing outside the
  standard library, so it loads cleanly into Hermes' own runtime (Python 3.14)
  and can never take down the agent it serves. Browsers — the one heavy
  dependency — live outside, in an interpreter the plugin only *drives*.
- **Knowledge lives in the repository, not in someone's head.** A skill whose
  `description` is its trigger, plus `references/` for procedures too long for a
  tool description — the Indeed browser recipe, the board-by-board caveats.
- **Scheduling belongs to the host.** `job_cron` creates a real `hermes cron`
  job in monitor mode, so the boards are polled cheaply and the model only wakes
  when the output actually changed. Delivery goes through the gateway you
  already use, to whichever chat platform you already use.

The boards, the scrapers, the hard-won markup workarounds and the
"new since last run" semantics are unchanged — they were the valuable part. What
changed is who they are for. The bot is gone; the research is now something you
ask for rather than something you perform.

---

## What changed

| Removed | Replaced by |
|---------|-------------|
| `aiogram` Telegram bot (`bot/`, `main.py`) | Hermes gateway — the agent answers wherever you already talk to it |
| APScheduler periodic scrape | `hermes cron` in monitor mode (`job_cron`) |
| `services/notifier.py`, subscribers table | `deliver` targets on the cron job |
| `config/settings.py` (required `BOT_TOKEN`) | `jobreach/config.py` — zero required configuration |
| `hermes.py` shim with a hard-coded project path | `tools.py` + `jobreach/runtime.py`, path-agnostic |
| `search_cli.py` + a bash wrapper in `~/.hermes/skills/` | `jobreach/cli.py`, and a skill that ships with the plugin |
| SQLAlchemy + aiosqlite + pydantic + httpx | `sqlite3`, dataclasses, `urllib` |
| Docker / Compose deployment | `hermes plugins install` |

---

## What changed in 2.1

Three upgrades, all driven by capabilities that appeared after 2.0 was written:

1. **Wantedly is read over its JSON API.** The HTML search page turns out to
   *discard* query parameters it no longer recognises and redirect to the
   generic feed — so a keyword search through the page silently returned
   unrelated listings, and because the old scraper declared
   `url_encodes_keyword = True`, the client-side filter did not catch them
   either. `/api/v1/projects` honours `q=` for real, returns typed fields
   (company, address, description, publication date) and costs ~4 s instead of
   ~40 s. This board now needs **no browser at all** (`needs_browser = False`).
2. **Indeed Japan is scraped.** It was ingest-only because Cloudflare refused
   headless clients; a stealth browser (Scrapling's patched Chromium) gets
   through — measured 200 OK, 16 cards, ~7 s, headless, repeatable. The
   `job_ingest` workflow stays as the documented fallback for blocked runs.
3. **Fetching is a backend choice, not a dependency.** Scrapers describe their
   page work as a *step list* (`wait`, `scroll`, `evaluate`, `click`, …) and the
   plugin runs it on whichever backend exists: Scrapling (preferred — usually
   already installed, solves Cloudflare, downloads nothing) or the plugin's own
   Playwright venv (the 150 MB fallback). `job_setup` now adopts Scrapling when
   it finds it.

Also: `posted_at` is carried through the store and the JSON envelope, and
`doctor` reports the backend board by board.

---

## What it does

| Board | Keyword search | Location | How it is fetched |
|-------|----------------|----------|-------------------|
| **Wantedly** | ✅ server-side | ✅ slug (`tokyo`, `osaka`, `any`) | **JSON API over HTTP** — no browser, ~4 s |
| **Indeed Japan** | ✅ server-side | ✅ slug or place name (`大阪`) | **Stealth browser (Scrapling)** — ~30 s |
| **Mynavi 2027** | ⚠️ best-effort | ❌ ignored | Stealth/Playwright browser, by occupation code |
| **LinkedIn** | ✅ server-side | ✅ | `opencli` CLI (your logged-in Chrome) |

Every listing is keyed by its normalised URL in a local SQLite store, so each
run reports genuine changes instead of the same cards again.

---

## Install

```bash
hermes plugins install Froggy1213/job-reach --enable
hermes job-reach setup          # adopts Scrapling if present; else venv + Chromium
hermes job-reach doctor         # verify: backend, boards, interpreter
```

`setup` also copies the bundled skill into `~/.hermes/skills/productivity/job-reach/`
so it can auto-trigger. You can equally just ask the agent to *"run job_setup"*.

Then:

```
find design jobs in Tokyo
```

…or from the shell:

```bash
hermes job-reach search --keyword "frontend engineer" --source wantedly -n 10
```

---

## The tools

| Tool | Purpose |
|------|---------|
| `job_search` | Live scrape of the selected boards; returns a structured envelope. |
| `job_ingest` | Fallback: listings you fetched by hand enter the same pipeline. |
| `job_list` | Read what is already stored — no network. |
| `job_note` | Render a result into an Obsidian Markdown note. |
| `job_status` | Store counts, last run, active backend, board readiness. |
| `job_setup` | One-time runtime install (adopts Scrapling, or builds Playwright). |
| `job_cron` | Schedule recurring monitoring through `hermes cron`. |

Plus a `/jobs <keyword>` slash command and a `hermes job-reach …` CLI that
exposes the same engine to a human.

---

## Architecture

```
plugin.yaml                  Hermes manifest (tools, env, capability hints)
__init__.py                  register(ctx): tools + skill + commands
schemas.py                   tool schemas — the contract the model sees
tools.py                     thin async handlers; spawn the engine, return JSON
skills/job-search/           SKILL.md + references/ (the agent's playbook)
jobreach/                   the engine — standard library only
├── domain.py                SourcePlatform, JobPosting (frozen dataclass)
├── webclient.py             stdlib HTTP for boards with a JSON API
├── fetchers.py              the step vocabulary + backend selection
├── scrapling.py             locate and drive a Scrapling install
├── drivers/                 scripts executed by another interpreter
│   └── scrapling_driver.py  fetches one page with Scrapling (JSON in/out)
├── store.py                 JobRepository port + SQLite adapter
├── filters.py               relevance profiles (regex heuristics / LLM)
├── scrapers/                one strategy per board
├── pipeline.py              search · ingest · dedupe · persist · report
├── notes.py                 Obsidian rendering
├── runtime.py               interpreter resolution + one-time setup
├── install.py               skill/cron installation
└── cli.py                   the `jobreach` command line
```

### Three decisions worth explaining

**1. The engine is standard-library only.** `jobreach/` imports nothing
outside the stdlib — no SQLAlchemy, no aiosqlite, no pydantic, no httpx. Hermes
runs plugins inside its own runtime venv (Python 3.14 today), and an engine that
needs binary wheels there is an engine that can break the agent. SQLite comes
from `sqlite3`, validation is explicit dataclass checks, HTTP comes from
`urllib.request`, and the optional LLM filter uses the same.

**2. A browser is a capability we look for, never a dependency we declare.**
Scrapers describe their page work as a step list (`wait`, `scroll`, `evaluate`,
`click`, `wait_selector`, `capture`) and hand it to a backend: Scrapling, run as
a subprocess in whatever interpreter has it, or Playwright inside the plugin's
own venv. Both execute the same vocabulary, so a board cannot work on one and
silently break on the other. The interpreter is resolved as
`$JOBREACH_PYTHON` → the plugin venv → `sys.executable`, so the read-only tools
work before any setup has run.

**3. Messaging and scheduling belong to Hermes.** `job_cron` creates a real
`hermes cron` job in **monitor mode**: the boards are polled cheaply every tick
and the agent only wakes when the output actually changed. There is no second
scheduler, no bot token, and delivery goes to Telegram/Discord/Slack/… through
the gateway that already exists.

### Where state lives

```
$HERMES_HOME/plugin-data/job-reach/
├── jobs.db          listings, run history
└── venv/            Playwright + Chromium (created by `setup`)
```

Never inside the plugin directory, so `hermes plugins update` cannot wipe it.

---

## Configuration

Everything is optional; see `.env.example`.

| Variable | Purpose |
|----------|---------|
| `JOBREACH_HOME` | Data directory. Default `$HERMES_HOME/plugin-data/job-reach`. |
| `JOBREACH_DB` | Explicit database path. |
| `JOBREACH_PYTHON` | Interpreter used to run the engine. |
| `JOBREACH_BACKEND` | `auto` (default), `scrapling` or `playwright` — pin one backend for debugging. |
| `JOBREACH_SCRAPLING_PYTHON` | Interpreter that has Scrapling, when auto-detection misses it. |
| `OBSIDIAN_VAULT_PATH` | Vault for generated notes (auto-detected otherwise). |
| `JOBREACH_LLM_API_KEY` | Generic key for `validation="llm"`. Requires `JOBREACH_LLM_BASE_URL`. |
| `JOBREACH_LLM_BASE_URL` | OpenAI-compatible endpoint for `validation="llm"`. |
| `JOBREACH_LLM_MODEL` | Model for `validation="llm"`; defaults to the one implied by the key found. |
| `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` | Provider keys that imply their own endpoint and model. |

---

## Command line

```
jobreach search    [-k KW] [-l LOC] [-s SOURCES] [-n N] [--new-only] [--json]
jobreach ingest    [--file F] [--source S] [--json]        # JSON on stdin
jobreach list      [-k TEXT] [-s SOURCE] [-n N] [--json]
jobreach stats     [--json]
jobreach note      [--input F] [--vault V]                 # envelope on stdin
jobreach monitor   [-k KW] [-s SOURCES]                    # stable digest, for cron
jobreach doctor    [--json]
jobreach setup     [--force] [--no-browser]
jobreach install-skill | install-cron
```

Logs always go to stderr, so `--json` output on stdout is safe to parse.

---

## Development

```bash
uv run --python 3.12 --with pytest --with pyyaml --no-project python -m pytest
hermes plugins validate .          # manifest + registration admission checks
hermes plugins doctor . --ci       # real runtime contracts
```

The contract test loads `__init__.py` exactly the way Hermes does (as a package
whose search path is the plugin directory) and pins the three things that fail
silently otherwise: manifest ↔ registration, schema ↔ handler, and the
"handlers always return JSON, never raise" rule.

### Re-syncing a working copy into Hermes

`hermes plugins install` **copies** the repository into
`~/.hermes/plugins/job-reach`, so local edits are not live until you re-copy
them:

```bash
rsync -a --delete --exclude '.git/' --exclude '__pycache__/' \
  --exclude '.pytest_cache/' --exclude '.ruff_cache/' \
  ./ ~/.hermes/plugins/job-reach/
hermes job-reach install-skill     # refresh the auto-discoverable skill copy
```

Restart Hermes afterwards — plugins are imported when a session starts.

---

## What was kept

The pieces that were already right, and that this rewrite deliberately carried
over unchanged in substance:

- the **strategy pattern** for boards — one class per site, one registry entry;
- the **repository port** — the pipeline never sees SQL;
- the **hard-won board knowledge**, now encoded in selectors, URL schemes and
  comments rather than in prose: Wantedly's real search API, Indeed's `jk` keys
  and `[data-testid]` slots, Mynavi's occupation codes and card text;
- the **local/LLM relevance profiles**, and the rule that a failing LLM batch
  degrades to heuristics instead of losing a scrape;
- the **browser-ingest fallback** for Indeed — the one board where the honest
  answer, when automation is blocked, is still "a real browser or nothing".

---

## License

MIT
