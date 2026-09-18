# AGENTS.md — working on the Job Reach plugin

Context for any agent editing this repository. Read this before changing code.

## What this is

A **Hermes Agent plugin**. It is loaded by Hermes itself
(`hermes plugins install`), not run as a standalone service. That shapes
almost every design decision here.

The project deliberately moved from being a *manual research tool* — a Telegram
bot a human drove by hand — to being *agent support*: tools the agent operates
on the user's behalf. The practical consequence for anyone editing this code is
that **the consumer of every interface is a model, not a person.** Tool schemas
are prompt engineering, JSON output is a contract, error strings are
instructions, and a failure only a human could interpret is a bug. The README's
"From manual research to agent support" has the full reasoning.

## Non-negotiable constraints

1. **`jobreach/` must stay standard-library only.** No third-party imports,
   ever. Hermes loads plugins inside its own runtime venv (Python 3.14), where
   an extra binary dependency can break the agent. Browsers are reached by
   *running another interpreter*, never by importing one — see
   `jobreach/scrapling.py` and `jobreach/drivers/`.
2. **`tools.py` must not import the engine into Hermes' process.** Handlers
   spawn `python -m jobreach` through `jobreach.runtime.run_engine`. This keeps
   the browser stack out of Hermes' venv and makes a 90-second scrape killable.
3. **`tools.py` / `__init__.py` / `schemas.py` use relative imports**
   (`from .jobreach.runtime import …`). Hermes' loader makes the plugin
   directory a package; a top-level `import jobreach` would only work by
   accident.
4. **Handlers return a JSON string and never raise.** Hermes shows a raw
   exception to the model as an opaque failure. Return
   `{"success": false, "error": …, "hint": …}` instead.
5. **User state never lives in the repository.**
   `$HERMES_HOME/plugin-data/job-reach/` is the sanctioned location; the
   plugin directory is replaced on every update.
6. **The skill's `description` is its trigger.** Hermes renders the skills
   index from it and truncates at 57 characters. Keep it ≤ 60, end it with a
   period, and never pad it with marketing words.
7. **Every OS difference lives in `jobreach/platforms.py`.** No `os.name`,
   `sys.platform` or `os.sep` check anywhere else — the helpers there take an
   explicit platform argument so both layouts (POSIX `bin/python` and Windows
   `Scripts\python.exe`) are tested from one machine. The plugin ships for
   macOS, Linux *and* Windows; a POSIX-assuming line is a bug, not a nuance.

## Platform support

The plugin is installed by other people's Hermes agents, on whatever OS they
run, so portability is a product feature rather than a nicety. What that costs:

- **`jobreach/platforms.py` is the only place that knows the OS.** It answers:
  where a virtualenv keeps its interpreter, where `uv` lives, how to kill a
  process *tree*, and how to detach a child from console signals.
- **The cron monitor is Python, not bash.** Hermes runs a cron script by
  extension: `.sh`/`.bash` go through bash, anything else through Hermes' own
  interpreter (the shebang is ignored). A stock Windows install has no bash, so
  a bash monitor would simply never run there. The generated script spawns the
  *engine* interpreter, which is resolved at install time.
- **Never `export HERMES_HOME=<temp dir>` in a shell you keep using.** The CLI
  resolves plugins under `$HERMES_HOME/plugins`, so an exported override makes
  `hermes <plugin-name> …` answer *"'job-reach' is not a hermes command"* and
  `hermes plugins list` omit the plugin entirely — while the plugin itself is
  perfectly installed. Use `env HERMES_HOME=… hermes …` per command, or
  `unset HERMES_HOME` afterwards. (Debugging that phantom cost an hour.)
- **Verifying portability from one machine.** `tests/test_platforms.py` pins
  both layouts without a Windows host, drives the "no third-party imports" rule
  by importing the engine in a clean interpreter and comparing module sets, and
  the generated monitor script is executed for real.

## Invariants pinned by tests

`tests/test_plugin_contract.py` fails if any of these drift:

- `plugin.yaml`'s `provides_tools` ≠ the tools `register(ctx)` registers;
- a schema has no handler, or a handler has no schema;
- a tool is missing from `skills/job-search/SKILL.md`;
- a `ctx.register_*` method is used that `PluginContext` does not define;
- the skill's name ≠ its directory, its description > 60 chars, it lacks
  `## When to Use`, or its directory holds a forbidden file;
- **a `config_schema` key in `plugin.yaml` is not read.** Every key declared
  there must reach a handler through `ctx.get_config` and be acted on somewhere.
  A decorated-but-unread setting is a lie to the user: the GUI lists it, nothing
  obeys it. Adding a key means adding it to `tools.SETTINGS_KEYS` (re-exported as
  `__init__.SETTINGS_KEYS`) *and* giving it an effect in the same change.
  Forwarding is not the same as acting: `default_sources` and `note_subfolder`
  are read by the *engine* through `jobreach.settings` (`JOBREACH_SETTING_*`,
  because only the plugin process can call `ctx.get_config`), while
  `default_keyword` and `max_results` are applied in the *handler*, which builds
  the CLI flags. Either is fine; a key that is forwarded but that no code path
  ever consults is not;
- `plugin.yaml`'s `provides_hooks` ≠ the hooks `register(ctx)` registers — a
  declared hook that is never registered (or vice versa) is a silent
  observability hole.

When you add a tool, also add it to: `schemas.py`, `tools.py`'s `HANDLERS`,
`__init__.py`'s description/emoji maps, `plugin.yaml`'s `provides_tools`, and
the tool table in `SKILL.md`. The tests will tell you if you forget one.

## Adding a job board

Two shapes, pick the cheaper one:

**A board with a JSON API** (Wantedly is the example). Subclass
`BaseScraper`, read it with `jobreach.webclient`, set `needs_browser = False`
so the CLI never demands a browser stack for a board that does not use one, and
set `url_encodes_keyword = True` when the API filters server-side.

**A board that renders with JavaScript or sits behind a challenge.** Build a
*step list* (`jobreach.fetchers`: `wait`, `scroll`, `evaluate`, `click`,
`wait_selector`, `capture`) and hand it to `BaseScraper.fetch` /
`evaluate_page`. Never open a browser in the scraper: both backends
(Scrapling in its own interpreter, Playwright in the plugin venv) execute the
same vocabulary, and that is what keeps a board from working on one and
silently breaking on the other. `click(..., optional=True)` is the idiom for
"follow the pager while it exists".

Either way, register it:

1. add the member to `SourcePlatform` in `jobreach/domain.py`
   (and to `PLATFORM_ALIASES` / `PLATFORM_LABELS`);
2. add its name to `_VALID_SOURCES` in `jobreach/config.py`;
3. register the class in `jobreach/scrapers/__init__.py`'s `SCRAPERS`;
4. document it in the coverage table in `skills/job-search/SKILL.md`.

Boards that cannot be scraped at all go into `INGEST_ONLY` (empty today — see
the comment there for why Indeed left it).

## Verifying a change

```bash
uv run --python 3.12 --with pytest --with pyyaml --no-project python -m pytest
hermes plugins validate .
hermes plugins doctor . --ci
```

The suites a change usually has to answer to, runnable on their own while
iterating: `tests/test_plugin_contract.py` (manifest ↔ registration
invariants), `tests/test_tools.py` (handler envelopes, settings plumbing,
`max_results` defaults), `tests/test_settings.py` (the `JOBREACH_SETTING_*`
bridge), and `tests/test_platforms.py` (portability). The full-suite command
above still runs everything.

For the end-to-end path a test fixture tends to fake — registration through a
`PluginContext`, the real subprocess bridge, and a setting observably reaching
the engine — run the acceptance harness. It needs no pytest, no network and no
Hermes import, and it prints one line per assertion with a pass/fail total:

```bash
python3 tools_acceptance.py .        # exits 1 if any check fails
```

Then install the edit where Hermes actually reads it — the installed copy under
`$HERMES_HOME/plugins/job-reach/` is a *snapshot*, not a symlink, so a fix in the
repo is invisible until it is copied over:

```bash
rsync -a --delete --exclude=.git --exclude='*cache*' --exclude=__pycache__ \
      --exclude=.env --filter='protect .env' \
      ~/My_projects/Job_reach/ ~/.hermes/plugins/job-reach/
cp skills/job-search/SKILL.md ~/.hermes/skills/productivity/job-reach/SKILL.md
```

No Hermes restart is needed for engine changes: `tools.py` spawns
`python -m jobreach` in a fresh subprocess per call, so the next tool call runs
the new code. Changes to `tools.py`/`schemas.py`/`__init__.py` *do* need one,
because those load inside Hermes' own process.

To exercise the real end-to-end path (subprocess bridge, SQLite, JSON):

```bash
export JOBREACH_HOME=$(mktemp -d)
python3 -m jobreach doctor
echo '[{"title":"UI Designer","url":"https://jp.indeed.com/viewjob?jk=x"}]' \
  | python3 -m jobreach ingest --json
```

Scraping needs *a* browser backend, not a specific one. Check what is in play
first — `python3 -m jobreach doctor` names it — then either use the Scrapling
install the plugin found, or run `python3 -m jobreach setup` to build the
Playwright fallback (~150 MB Chromium). Without either, browser boards fail
with an actionable `MissingDependencyError` rather than an opaque crash, and
Wantedly still works (it has no browser in its path at all). That behaviour is
intentional and worth preserving.

## Style

Match the surrounding code: a module docstring that explains *why* the module
exists, docstrings on public functions, and comments that record the reason for
a non-obvious choice (especially the HTML-selector workarounds in
`jobreach/scrapers/`, which encode hard-won knowledge about live markup).
Prefer explicit code over clever code; this is a small, long-lived tool.
