"""jobreach — Japanese job-board search engine, built for Hermes Agent.

This package is the **dependency-free core** of the Job Reach Hermes plugin.
It only uses the Python standard library, so it can be imported and executed
by any Python 3.11+ interpreter — including Hermes' own runtime venv.

The only optional dependency is ``playwright`` (plus ``playwright-stealth``),
needed *exclusively* by the JS-rendered scrapers (Wantedly, Mynavi 2027).
Install it into a project-local venv with the ``job_setup`` Hermes tool, or::

    uv pip install -e ".[scrape]"
    playwright install chromium

Layering (inner → outer)::

    domain.py     SourcePlatform, JobPosting      (pure data)
    store.py      JobRepository port + SQLite adapter
    filters.py    relevance profiles (regex / LLM)
    scrapers/     one strategy per job board
    pipeline.py   search / ingest / dedupe / persist
    cli.py        the ``jobreach`` command line
"""

from __future__ import annotations

__version__ = "2.0.0"

__all__ = ["__version__"]
