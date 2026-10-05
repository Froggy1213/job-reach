# Job Reach — a Hermes Agent plugin

Search Japanese job boards, remember what you have already seen, and get told
only about what is new. Runs as a native [Hermes Agent](https://github.com/NousResearch/hermes-agent)
plugin: seven LLM-callable tools, one skill, and scheduled monitoring through
`hermes cron`.

---

## Design constraints

The consumer of every interface here is a **model**, not a person. That one fact
is what makes these rules non-negotiable, and it is why the code looks the way it
does:

- **The interface is read by a model.** Each tool schema has to carry the
  knowledge needed to choose correctly: which board answers which query, that
  Mynavi is new-grad design only and ignores location, that Indeed is the widest
  market but the only board that can be bot-blocked, that a scrape takes seconds
  to a minute and must not be retried in a loop. That prose is the product, not
  decoration.
- **Output is a contract.** Every tool returns a stable JSON envelope
  (`summary`, `jobs[]`, `is_new`); `--json` writes nothing but JSON to stdout
  while logs go to stderr; `is_new` makes "what changed?" a field instead of a
  judgement call.
- **Failures are instructions.** A failed board returns
  `{"success": false, "error": ..., "hint": ...}` where the hint is the exact
  command that fixes it, and one broken board never discards the others. Handlers
  never raise — a stack trace is useless to a model.
- **Knowledge lives in the repository.** A skill whose `description` is its
  trigger, plus `references/` for procedures too long for a tool description —
  the Indeed browser recipe, the board-by-board caveats.

See [Architecture](#architecture) for how those shape the engine itself.

---

## What it does

| Board | Keyword search | Location | How it is fetched |
|-------|----------------|----------|-------------------|
| **Wantedly** | ✅ server-side | ✅ slug (`tokyo`, `osaka`, `any`) | **JSON API over HTTP** — no browser, ~4 s |
| **Green** | ✅ server-side | ✅ slug or place name, by prefecture id | **JSON embedded in the page** — no browser, ~1 s |
| **Daijob** | ✅ server-side | ✅ slug (`tokyo`, `osaka`), by prefecture code | **Server-rendered HTML** — no browser, ~2 s |
| **Japan Dev** | ⚠️ titles only (the site's search is client-side) | ❌ | **Server-rendered HTML** — no browser, ~1 s |
| **Indeed Japan** | ✅ server-side | ✅ slug or place name (`大阪`) | **Stealth browser (Scrapling)** — ~30 s from a residential IP, and it never resolves at all from a Cloudflare-blocked one |
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
#   install opencli and keep Chrome running with its extension; check with
#   `hermes job-reach doctor` or run a test search directly (do not rely on
#   `opencli linkedin whoami`, which can return `Navigation rejected` even on
#   a working setup)
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
  same on all three platforms. It merges the `$JOBREACH_HOME/settings.json`
  snapshot at run time so scheduled runs pick up `config.yaml` edits on any
  platform.
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
so it can auto-trigger. (If the skill does not auto-trigger after setup, check that
`skills.disabled` in Hermes' `config.yaml` is not blocking it.) You can equally
just ask the agent to *"run job_setup"*.

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

### Result ordering

`job_search` results are returned **new-first** (`is_new: true` rows outrank previously seen rows). Within each group (new vs. seen), listings are ordered **newest-first**: an explicit publication date (`posted_at`) outranks a synthesized or missing date, and higher timestamps sort earlier. The Obsidian note renderer (`job_note`) groups listings by board itself, so markdown note layout and board grouping are unaffected.

### Tool argument handling and envelopes

Tool handlers validate arguments strictly and return structured JSON envelopes rather than failing silently or raising exceptions:

- **Boolean arguments:** handlers accept real JSON booleans as well as case-insensitive strings (`true`, `false`, `1`, `0`, `yes`, `no`, `y`, `n`, `on`, `off`). Any other value raises a typed error naming the argument, eliminating bugs where strings like `"false"` enabled flags (`new_only`, `detail`) or triggered a venv wipe on `force: "false"`.
- **Bounds and empty inputs:** `limit` must be an integer ≥ 1 if passed (0 is rejected with a hint; omit `limit` for search to return all matches or the configured `max_results`); `recent_runs` in `job_status` must be ≥ 0; `sources: []` is an explicit error with a hint rather than a silent fallback to default boards.
- **Diagnostic warnings in `job_status`:** always returns a `warnings` mapping (`{"store": {...}}` / `{"runtime": {...}}`). A half-failed diagnostics call (e.g. database error while doctor succeeds, or vice versa) is visible instead of disappearing behind a bare `null`, and a total failure preserves both error messages.
- **Honest setup reporting in `job_setup`:** setup never reports success if runtime preparation failed (venv creation, Playwright, or Chromium download), even if installing the skill succeeded — the failure envelope retains the error text, `skill` path, and any `skill_warning`. When runtime setup succeeds but skill installation fails, the success envelope carries a top-level `skill_error`.

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
`click`, `goto`, `wait_load`, `wait_selector`, `capture`) and hand it to a
backend: Scrapling, run as a subprocess in whatever interpreter has it, or
Playwright inside the plugin's own venv. Both execute the same vocabulary, so a
board cannot work on one and silently break on the other. The interpreter is
resolved as `$JOBREACH_PYTHON` → the plugin venv → `sys.executable`, so the
read-only tools work before any setup has run.

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

### Settings (`config.yaml`)

Six user-visible knobs live in Hermes' `config.yaml` (`$HERMES_HOME/config.yaml`,
by default `~/.hermes/config.yaml`) under the plugin's own namespace, which
Hermes validates against `config_schema` in `plugin.yaml`. They are read
through `ctx.get_config` on every tool call, so an edit takes effect on the next
tool call AND on the next scheduled monitor run — no Hermes restart.

| Setting | `config.yaml` path | Default | Effect |
|---------|--------------------|---------|--------|
| `default_keyword` | `plugins.entries.job-reach.settings.default_keyword` | `""` | Keyword `job_search` uses when the caller passes none. Empty means the built-in design feed. |
| `default_sources` | `plugins.entries.job-reach.settings.default_sources` | `[]` | Boards searched when the caller passes no `sources`. Empty means the built-in board set (`wantedly`, `mynavi2027`, `linkedin`). Warning: adding `indeed` here makes every bare search and the daily monitor pay its worst-case time on slow networks (up to ~4 min unblocked, capped at <1 s with a 30-minute cooldown on hard blocks). |
| `note_subfolder` | `plugins.entries.job-reach.settings.note_subfolder` | `"job-searches"` | Subfolder inside the Obsidian vault that `job_note` writes into. |
| `max_results` | `plugins.entries.job-reach.settings.max_results` | unset | How many listings `job_list` returns when no `limit` is passed (default: 25 when unset or invalid), and the limit `job_search` falls back to. The engine's manual `jobreach list` mirrors this same default (25 when unset or invalid) so the manual CLI agrees with the tool path. For searches, unset means all matches, so set it if you want searches capped. An explicit `limit` always wins. |
| `default_validation` | `plugins.entries.job-reach.settings.default_validation` | `"off"` | Relevance filter used when a call passes no `validation` (a bare `jobreach search` passes no `--validate` either): `off` returns every match, `local` drops the obvious noise with free regex heuristics, `llm` classifies against the profile below and needs an API key. Set it to `local` to make a bare search return the relevant listings instead of everything. An explicit argument always wins. |
| `default_profile` | `plugins.entries.job-reach.settings.default_profile` | `"designer"` | Filter profile used when a call passes no `profile`: `designer`, `frontend`, `engineering`, `product` or `any`. It only has an effect while relevance filtering is on (`local` or `llm`). An explicit argument always wins. |

```yaml
plugins:
  entries:
    job-reach:
      settings:
        default_keyword: "frontend engineer"
        default_sources: [wantedly, green, daijob]
        note_subfolder: job-searches
        max_results: 50
        default_validation: local
        default_profile: frontend
```

**An explicit tool argument always beats the setting.** A call that passes
`keyword`, `sources`, `limit`, `subfolder`, `validation` or `profile` is obeyed
verbatim; the setting only fills the gap when the caller omits the argument. A
key left out of `config.yaml` means "use the default", never "override with
nothing" — and a value outside the allowed list is a warning on stderr plus the
default, never a failure.

**The manual CLI path does not read configured settings.** `hermes job-reach …`
(and `python -m jobreach …`) forwards no settings — only ambient
`JOBREACH_SETTING_*` variables exported in your shell cross — so a manual run
resolves boards, profiles and limits from the engine's own built-in defaults.
`config.yaml` settings will not alter a manual command; use the agent tools
when configured defaults matter.

### Environment variables

`JOBREACH_SETTING_*` (`JOBREACH_SETTING_DEFAULT_KEYWORD`,
`JOBREACH_SETTING_DEFAULT_SOURCES`, `JOBREACH_SETTING_NOTE_SUBFOLDER`,
`JOBREACH_SETTING_MAX_RESULTS`, `JOBREACH_SETTING_DEFAULT_VALIDATION`,
`JOBREACH_SETTING_DEFAULT_PROFILE`) is the **internal bridge**, not a user-facing
knob: the plugin process mirrors the settings it read into those variables so
the engine subprocess (which cannot reach `ctx.get_config`) can see them.

To bridge settings to external processes like the cron monitor (which Hermes'
scheduler runs independently without importing the plugin), the plugin writes a
JSON snapshot to `$JOBREACH_HOME/settings.json` (default
`$HERMES_HOME/plugin-data/job-reach/settings.json`) every time a tool call reads
the configuration (`tools._settings` → `jobreach.runtime.publish_settings` →
`jobreach.settings.publish_snapshot`). The snapshot maps environment-variable
names to strings (e.g. `{"JOBREACH_SETTING_DEFAULT_SOURCES": "wantedly,green"}`).
Nothing sensitive is stored (only the six config keys), and the file is
rewritten even when nothing is configured so it always reflects the last
configuration the plugin saw. The generated monitor script bakes that path and
merges the snapshot into the engine child's environment at run time, so a later
`config.yaml` change is picked up on the next scheduled run instead of being
frozen at install time. Plugin settings win over ambient `JOBREACH_SETTING_*`
variables; a missing or unreadable snapshot is ignored and the monitor runs
with its built-in defaults.

Set the `config.yaml` keys above instead of setting `JOBREACH_SETTING_*` by
hand. The variables listed here are genuine
user knobs — `JOBREACH_HOME`, `JOBREACH_DB` and the rest are read directly and
remain supported.

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

Key CLI defaults and validation:
- **`jobreach list` limit default:** `-n` defaults to the mirrored `max_results` setting (25 when unset or invalid), so manual listing agrees with `job_list`. `jobreach search` continues to default to all matches when `-n` is omitted.
- **`--profile` validation:** `jobreach search --profile` and `jobreach monitor --profile` validate arguments against the supported profiles (`designer`, `frontend`, `engineering`, `product`, `any`) and reject invalid profiles with an argparse usage error (exit code 2). If an unknown profile reaches the filter layer directly (e.g. via direct Python API call), it falls back to keeping the listing, records the unknown profile in the decision reason, and logs a warning once.
- **Manual CLI settings:** as noted under Configuration, manual `jobreach` and `hermes job-reach` commands do not read `config.yaml` settings.

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

What takes effect when:
- **Engine changes** (`jobreach/**`): live on the next tool call immediately,
  because `tools.py` spawns `python -m jobreach` in a fresh child process per call.
- **Plugin changes** (`tools.py`, `schemas.py`, `__init__.py`): require a
  **Hermes restart**, because those modules load directly inside Hermes' own
  process at startup.
- **Configuration edits** (`config.yaml`): live on the next tool call and on the
  next scheduled monitor run without a restart.

---

## License

MIT
