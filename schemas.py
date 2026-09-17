"""Tool schemas — exactly what the model sees.

Kept in a separate module from the handlers on purpose: the schema is the
*contract* (name, description, argument shape) and the handler is the
*implementation*. Reviewing a tool's surface should not require reading its
code.

Descriptions are written for an LLM that has never seen this project, and they
carry the operational knowledge the model needs to choose correctly:

* which board answers which kind of query (Wantedly = anything, Mynavi =
  new-grad design only, Indeed = ingest-only),
* that a scrape takes 30–90 seconds and must not be retried in a loop,
* that ``job_ingest`` is the *only* way Indeed Japan listings enter the store.
"""

from __future__ import annotations

from typing import Any

TOOLSET = "job_reach"

_SEARCH = {
    "name": "job_search",
    "description": (
        "Search Japanese job boards live and return structured listings. "
        "Boards: 'wantedly' (general-purpose, honours keyword + location — use "
        "this for any free-text or non-design search; read over Wantedly's own "
        "JSON API, so it is fast and needs no browser), 'indeed' (all of Japan's "
        "market, honours keyword + location; drives a stealth browser and takes "
        "~30 seconds), 'mynavi2027' (new-graduate design roles only; ignores "
        "location), 'linkedin' (via the OpenCLI Chrome bridge; needs Chrome "
        "running). With no keyword it runs the built-in design-in-Tokyo feed. "
        "Listings are deduplicated against the local database and flagged "
        "'is_new' when this run is the first to see them. A live scrape takes "
        "5-90 seconds depending on the boards — do not retry in a loop."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": (
                    "Free-text query, e.g. 'frontend engineer' or 'デザイナー'. "
                    "Omit for the default design feed. Recommended with "
                    "sources=['wantedly']."
                ),
            },
            "location": {
                "type": "string",
                "description": (
                    "Location: a Wantedly/Indeed slug ('tokyo', 'osaka'), a "
                    "Japanese place name ('大阪'), or 'any' for nationwide. "
                    "Mynavi ignores it. Default: 'tokyo'."
                ),
            },
            "sources": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Boards to search: 'wantedly', 'indeed', 'mynavi2027', "
                    "'linkedin'. Default: ['wantedly','mynavi2027','linkedin']. "
                    "Add 'indeed' explicitly for the widest coverage."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Maximum listings returned (newest and new-first). Omit for all.",
            },
            "new_only": {
                "type": "boolean",
                "description": "Return only listings not seen in any previous run.",
            },
            "save": {
                "type": "boolean",
                "description": (
                    "Persist results and mark them seen (default true). Set false "
                    "for a throwaway search that should not affect 'new' tracking."
                ),
            },
            "validation": {
                "type": "string",
                "enum": ["off", "local", "llm"],
                "description": (
                    "Relevance filter. 'local' = free regex heuristics. 'llm' = "
                    "classify through an OpenAI-compatible API; it needs a "
                    "configured provider (DEEPSEEK_API_KEY or OPENAI_API_KEY), or "
                    "JOBREACH_LLM_API_KEY plus JOBREACH_LLM_BASE_URL. A batch that "
                    "fails degrades to the local filter rather than losing the "
                    "search. Default 'off'."
                ),
            },
            "profile": {
                "type": "string",
                "enum": ["designer", "frontend", "engineering", "product", "any"],
                "description": "Target role profile used when validation is enabled.",
            },
            "headless": {
                "type": "boolean",
                "description": "Run the browser headless (default true).",
            },
        },
        "required": [],
    },
}

_INGEST = {
    "name": "job_ingest",
    "description": (
        "Feed listings you fetched yourself into the same dedupe/persist/'new' "
        "pipeline the scrapers use. This is the **fallback path**: use it when a "
        "board's scraper could not run — a Cloudflare challenge that the stealth "
        "browser lost, a board the plugin does not know, or listings a browser "
        "tool (or the Scrapling MCP server) already returned. Every listing with "
        "a URL already stored comes back with is_new=false, so re-running is safe."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "jobs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "url": {"type": "string"},
                        "company": {"type": "string"},
                        "location": {"type": "string"},
                        "salary": {"type": "string"},
                        "source_platform": {
                            "type": "string",
                            "description": "Defaults to 'indeed'.",
                        },
                    },
                    "required": ["title", "url"],
                },
                "description": "Listings to ingest. Each needs at least title and url.",
            },
            "source": {
                "type": "string",
                "description": "Board name applied to records that omit source_platform. Default 'indeed'.",
            },
            "limit": {"type": "integer", "description": "Cap how many listings are returned."},
            "new_only": {"type": "boolean", "description": "Return only newly seen listings."},
            "save": {
                "type": "boolean",
                "description": "Persist and mark seen (default true).",
            },
        },
        "required": ["jobs"],
    },
}

_LIST = {
    "name": "job_list",
    "description": (
        "Read listings already stored in the local database, newest first. Use "
        "this for questions about what has been collected so far ('what did we "
        "find this week?', 'show me the LinkedIn ones') without hitting the "
        "network. Filter with `text` (substring of title/company/location) and "
        "`source`."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Substring match on title, company or location."},
            "source": {
                "type": "string",
                "description": "Restrict to one board: wantedly, mynavi_2027, linkedin, indeed.",
            },
            "limit": {"type": "integer", "description": "How many listings to return (default 25)."},
            "offset": {"type": "integer", "description": "Skip this many listings (paging)."},
            "new_since": {
                "type": "string",
                "description": "ISO-8601 timestamp; only listings first seen at or after it.",
            },
        },
        "required": [],
    },
}

_NOTE = {
    "name": "job_note",
    "description": (
        "Write a search result into the Obsidian vault as a Markdown note "
        "(grouped by board, with a NEW badge per fresh listing). The vault comes "
        "from OBSIDIAN_VAULT_PATH or auto-detection. Pass the exact object that "
        "job_search or job_ingest returned in `result`. This is the default "
        "deliverable for a job search — call it before summarising to the user."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "result": {
                "type": "object",
                "description": "The search/ingest result envelope to render.",
            },
            "vault": {
                "type": "string",
                "description": "Obsidian vault path (overrides auto-detection).",
            },
            "subfolder": {
                "type": "string",
                "description": "Subfolder inside the vault. Default 'job-searches'.",
            },
        },
        "required": ["result"],
    },
}

_STATUS = {
    "name": "job_status",
    "description": (
        "Report the state of Job Reach: how many listings are stored (by "
        "board), when the last run happened and what it found, plus runtime "
        "readiness — which browser backend is in use, which interpreter runs "
        "the engine, which boards can actually run, and whether the Obsidian "
        "vault was found. Call this first when a search fails or before "
        "setting up monitoring."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "recent_runs": {
                "type": "integer",
                "description": "How many past runs to include (default 5).",
            }
        },
        "required": [],
    },
}

_SETUP = {
    "name": "job_setup",
    "description": (
        "One-time preparation of the scraping runtime, and it prefers to do "
        "nothing: if Scrapling is already installed (very likely — it is the "
        "plugin author's backend and other MCP servers use it too) that install "
        "is adopted as-is and no download happens. Only when Scrapling is absent "
        "does this create the plugin venv, install Playwright plus its Chromium "
        "build (~150 MB), and install the skill into Hermes' skills directory. "
        "Takes minutes in the fallback case, seconds otherwise."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "with_browser": {
                "type": "boolean",
                "description": (
                    "Download Chromium in the fallback path (default true; "
                    "ignored when Scrapling is used)."
                ),
            },
            "force": {
                "type": "boolean",
                "description": "Recreate the plugin venv from scratch (default false).",
            },
            "install_skill": {
                "type": "boolean",
                "description": "Copy the bundled SKILL.md into ~/.hermes/skills (default true).",
            },
        },
        "required": [],
    },
}

_CRON = {
    "name": "job_cron",
    "description": (
        "Schedule a recurring job-monitoring run through Hermes' own cron "
        "system, delivering the digest to a chat platform. Uses monitor mode: "
        "the boards are polled cheaply and the agent only wakes when something "
        "actually changed, so an idle schedule costs nothing. Use for requests "
        "like 'check for new design jobs every morning and message me on "
        "Telegram'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "schedule": {
                "type": "string",
                "description": "Cron expression or interval, e.g. '0 9 * * *' or '6h'. Default '0 9 * * *'.",
            },
            "keyword": {"type": "string", "description": "Query to watch. Omit for the default design feed."},
            "location": {"type": "string", "description": "Location slug to watch. Default 'tokyo'."},
            "sources": {
                "type": "string",
                "description": "Comma-separated boards to watch. Default 'wantedly,linkedin'.",
            },
            "deliver": {
                "type": "string",
                "description": "Delivery target: 'telegram', 'discord', 'origin', 'local', or 'platform:chat_id'.",
            },
            "name": {"type": "string", "description": "Job name. Default 'job-reach-monitor'."},
        },
        "required": [],
    },
}

#: Ordered so the model sees the primary actions first.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    _SEARCH,
    _INGEST,
    _LIST,
    _NOTE,
    _STATUS,
    _SETUP,
    _CRON,
]

#: Names of the registered tools — mirrored into ``plugin.yaml``'s
#: ``provides_tools`` and asserted by the contract test.
TOOL_NAMES: tuple[str, ...] = tuple(schema["name"] for schema in TOOL_SCHEMAS)
