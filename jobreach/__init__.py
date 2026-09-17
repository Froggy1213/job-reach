"""jobreach — Japanese job-board search engine, built for Hermes Agent.

This package is the **dependency-free core** of the Job Reach Hermes plugin.
It only uses the Python standard library, so it can be imported and executed
by any Python 3.11+ interpreter — including Hermes' own runtime venv.

Browsers are optional and looked up at run time, in this order:

1. **Scrapling** (``$JOBREACH_SCRAPLING_PYTHON``, a ``scrapling`` binary on
   PATH, the plugin venv, or a known local install) — the preferred backend:
   it solves Cloudflare challenges and usually needs no download.
2. **Playwright**, if the plugin's own venv was built by ``hermes job-reach
   setup`` (``uv pip install -e ".[scrape]"`` + ``playwright install chromium``).

Wantedly is read over its JSON API and LinkedIn through ``opencli``, so neither
needs a browser at all.

Layering (inner → outer)::

    domain.py     SourcePlatform, JobPosting      (pure data)
    webclient.py  stdlib HTTP for JSON APIs
    fetchers.py   the step vocabulary + backend selection
    scrapling.py  locate and drive a Scrapling install
    scrapers/     one strategy per job board
    store.py      JobRepository port + SQLite adapter
    filters.py    relevance profiles (regex / LLM)
    pipeline.py   search / ingest / dedupe / persist
    cli.py        the ``jobreach`` command line
"""

from __future__ import annotations

__version__ = "2.3.0"

__all__ = ["__version__"]
