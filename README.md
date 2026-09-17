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

## What changed in 2.3

**The plugin is portable to any Hermes, including on Windows** — and the README
now says exactly what a host needs (see "Requirements & platform support"
above). The work was not documentation, it was removing the assumptions:

- `jobreach/platforms.py` collects every OS difference: virtualenv layout
  (`bin/python` vs `Scripts\python.exe`), `uv` discovery, process-tree teardown
  (`taskkill /F /T` on Windows), and how a child is isolated from console
  signals. Each takes an explicit platform argument, so both layouts are
  unit-tested from one machine.
- The cron monitor is generated as **Python** instead of bash. Hermes runs
  `.sh` scripts through bash, which a stock Windows install does not have; a
  `.py` script runs on all three platforms — verified here by generating it,
  running it, and checking the digest it printed.
- `plugin.yaml` declares `platforms: [macos, linux, windows]` and
  `python_dependencies: []` explicitly, and a new test imports the whole engine
  in a clean interpreter and asserts that it adds **zero** non-stdlib modules.
- Printed commands are quoted for the shell the user is actually in.

---

## What changed in 2.2

Three more boards, chosen so the *cheap* half of the market is covered without a
browser at all:

- **Green** (`green`) — IT/Web industry postings. The site is a Next.js app, but
  it serialises its results into the page's `__NEXT_DATA__` payload, so the
  whole search comes back as typed JSON: title, company, area, **salary**,
  description, publication date. ~1 s per run.
- **Daijob** (`daijob`) — bilingual and foreign-capital employers. Plain
  server-rendered HTML, read with the standard library
  (`jobreach/htmlextract.py`): card split by `article.job-card`, fields from the
  card's `<dt>/<dd>` pairs (勤務地, 年収, 仕事内容). ~2 s per run.
- **Japan Dev** (`japandev`) — English-speaking tech jobs. Server-rendered too,
  but its search box filters **client-side** (verified: three different queries
  returned the identical set of 60 listings), so the plugin downloads the page
  and applies its own title filter. ~1 s per run.

With Wantedly that makes **four of seven boards browser-free**, which matters on
a machine where the browser stack is missing: they keep working. `doctor` now
reports each board's backend (`http`, `scrapling`, `cli`), and `-s all` fans out
across all seven.

Also: `htmlextract.py` (a small, tested stdlib HTML reader) and
source aliases (`green-japan`, `japan-dev`, `mynavi`) on the command line.

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
| **Green** | ✅ server-side | ✅ slug or place name, by prefecture id | **JSON embedded in the page** — no browser, ~1 s |
| **Daijob** | ✅ server-side | ✅ slug (`tokyo`, `osaka`), by prefecture code | **Server-rendered HTML** — no browser, ~2 s |
| **Japan Dev** | ⚠️ titles only (the site's search is client-side) | ❌ | **Server-rendered HTML** — no browser, ~1 s |
| **Indeed Japan** | ✅ server-side | ✅ slug or place name (`大阪`) | **Stealth browser (Scrapling)** — ~30 s |
| **Mynavi 2027** | ⚠️ best-effort | ❌ ignored | Stealth/Playwright browser, by occupation code |
| **LinkedIn** | ✅ server-side | ✅ | `opencli` CLI (your logged-in Chrome) |

Every listing is keyed by its normalised URL in a local SQLite store, so each
run reports genuine changes instead of the same cards again.

---

## Requirements & platform support

**Short version: nothing to install.** The plugin is a Hermes plugin, not a
service: no MCP server, no daemon, no Docker, no Node, no Python packages. Four
of the seven boards are read over plain HTTP, so a fresh install can search
Japan immediately.

### What a host must have

| Requirement | Why | Notes |
|---|---|---|
| Hermes ≥ 0.21 | the plugin API this was built against | `requires_hermes` in `plugin.yaml` |
| Python 3.11+ | the engine uses `StrEnum`, `datetime.UTC`, `slots=True` | Hermes' own runtime (3.14) already qualifies; `hermes job-reach` uses it automatically |
| Network access to the boards | obvious | HTTPS only |

Deliberately **not** required: an MCP server, a database server, a browser
install, an API key, a config file. `plugin.yaml` declares
`python_dependencies: []`, and the engine's standard-library-only rule is
enforced by a test — importing `jobreach` in a clean interpreter adds exactly
zero non-stdlib modules.

> **MCP note:** the plugin does **not** need any MCP server. Scrapling is a
> Python library here, driven directly by a small subprocess driver. If you
> happen to run Scrapling's own MCP server (for ad-hoc page fetching), that is
> unrelated — the two share the installed library, nothing more.

### What each board needs

| Board | Extra software | If it is missing |
|---|---|---|
| `wantedly`, `green`, `daijob`, `japandev` | **nothing** | always work |
| `indeed`, `mynavi2027` | a browser backend: Scrapling (preferred) or Playwright | those boards report an actionable error; the other five still run |
| `linkedin` | `opencli` on `PATH` + Chrome running with its extension | LinkedIn reports `BROWSER_CONNECT`; nothing else is affected |

So the honest answer to "does this pull in dependencies?" is: **only if you want
Indeed, Mynavi 2027 or LinkedIn**, and only for those boards.

### Installing the optional pieces

```bash
# Browser backend (Indeed, Mynavi 2027) — pick one:

# a) Scrapling, if it is already on the machine (an existing MCP setup, a venv…)
#    the plugin auto-detects it; point at it explicitly if detection misses:
export JOBREACH_SCRAPLING_PYTHON=/path/to/venv/bin/python      # Windows: ...\Scripts\python.exe

# b) or let the plugin build its own Playwright venv (~150 MB, cross-platform)
hermes job-reach setup

# LinkedIn (optional)
#   install opencli and keep Chrome running with its extension; verify with
#   `opencli linkedin whoami`
```

`hermes job-reach setup` prints what it found and what it skipped; it never
downloads anything when Scrapling is already available.

### Platform support

| Platform | Status | Notes |
|---|---|---|
| macOS | ✅ full | the author's platform; everything here is verified live |
| Linux | ✅ full | same code paths; no platform-specific assumptions left |
| Windows | ✅ supported | see below — the platform differences are handled, not documented away |

Windows specifics, because "works on Windows" deserves evidence rather than a
shrug:

- **Virtualenv layout** — the plugin looks for `venv\Scripts\python.exe`, not
  `venv/bin/python` (`jobreach/platforms.py`, unit-tested for both layouts).
- **Process-tree teardown** — a timed-out scrape is killed with
  `taskkill /F /T /PID` instead of POSIX signals, so the browser stack cannot be
  left running.
- **Cron monitoring** — the generated monitor script is **Python**, not bash.
  Hermes runs `.sh` scripts through bash (absent on a stock Windows install) and
  everything else through its own interpreter, so a `.py` monitor behaves the
  same on all three platforms.
- **`uv` discovery** — includes `%LOCALAPPDATA%\uv\uv.exe` and friends.
- **Printed commands** — quoted with Windows rules when the shell is Windows.
- **LinkedIn** is the one third-party caveat: it depends on `opencli` and a
  Chrome extension, so it is only as portable as that tool is.

The browser boards depend on Scrapling/Playwright, both of which support
Windows; the plugin's own code has no platform-specific branch left outside
`jobreach/platforms.py`.

### Verifying an install (any platform)

```bash
hermes job-reach doctor        # backend, engine interpreter, board-by-board readiness
```

A healthy output names a browser backend (`scrapling — Scrapling 0.4.x …`) and
marks all seven boards `ok`. On a host with no browser stack, the four
HTTP boards are still `ok` and Indeed/Mynavi say what to install.

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
├── htmlextract.py           stdlib HTML reading (cards, fields, embedded JSON)
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
