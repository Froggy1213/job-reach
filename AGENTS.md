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
   an extra binary dependency can break the agent. Playwright is the single
   exception and it lives *outside* — see the next point.
2. **`tools.py` must not import the engine into Hermes' process.** Handlers
   spawn `python -m jobreach` through `jobreach.runtime.run_engine`. This
   keeps Playwright out of Hermes' venv and makes a 90-second scrape killable.
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

## Invariants pinned by tests

`tests/test_plugin_contract.py` fails if any of these drift:

- `plugin.yaml`'s `provides_tools` ≠ the tools `register(ctx)` registers;
- a schema has no handler, or a handler has no schema;
- a tool is missing from `skills/job-search/SKILL.md`;
- a `ctx.register_*` method is used that `PluginContext` does not define;
- the skill's name ≠ its directory, its description > 60 chars, it lacks
  `## When to Use`, or its directory holds a forbidden file.

When you add a tool, also add it to: `schemas.py`, `tools.py`'s `HANDLERS`,
`__init__.py`'s description/emoji maps, `plugin.yaml`'s `provides_tools`, and
the tool table in `SKILL.md`. The tests will tell you if you forget one.

## Adding a job board

1. Add the member to `SourcePlatform` in `jobreach/domain.py`
   (and to `PLATFORM_ALIASES` / `PLATFORM_LABELS`).
2. Add its name to `_VALID_SOURCES` in `jobreach/config.py`.
3. Write the scraper — `BaseScraper` for a JS-rendered board,
   `CliScraper` for one already solved by an external CLI.
4. Register it in `jobreach/scrapers/__init__.py`'s `SCRAPERS`.
5. Document it in the coverage table in `skills/job-search/SKILL.md`.

Set `url_encodes_keyword = True` when the board filters server-side. That flag
is what makes cross-language queries work: an English `engineer` search returns
Japanese `エンジニア` titles, which a literal substring filter would reject.

## Verifying a change

```bash
uv run --python 3.12 --with pytest --with pyyaml --no-project python -m pytest
hermes plugins validate .
hermes plugins doctor . --ci
```

To exercise the real end-to-end path (subprocess bridge, SQLite, JSON):

```bash
export JOBREACH_HOME=$(mktemp -d)
python3 -m jobreach doctor
echo '[{"title":"UI Designer","url":"https://jp.indeed.com/viewjob?jk=x"}]' \
  | python3 -m jobreach ingest --json
```

Scraping additionally needs the venv: `python3 -m jobreach setup`
(installs Playwright + Chromium, ~150 MB). Without it, browser boards fail with
an actionable `MissingDependencyError` rather than an opaque crash — that
behaviour is intentional and worth preserving.

## Style

Match the surrounding code: a module docstring that explains *why* the module
exists, docstrings on public functions, and comments that record the reason for
a non-obvious choice (especially the HTML-selector workarounds in
`jobreach/scrapers/`, which encode hard-won knowledge about live markup).
Prefer explicit code over clever code; this is a small, long-lived tool.
