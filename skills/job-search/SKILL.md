---
name: job-search
description: Find jobs on Japanese boards and track what is new.
version: 2.3.0
author: Froggy1213
license: MIT
platforms: [macos, linux, windows]
metadata:
  hermes:
    tags: [jobs, japan, career, wantedly, mynavi, linkedin, indeed, obsidian, monitoring]
    category: productivity
    requires_toolsets: [job_reach]
---

# Japan Job Search

## Overview

Search Japanese job boards for real openings, remember what has already been
seen, and report only what changed. Backed by the **job-reach** Hermes plugin,
which exposes seven tools (`job_search`, `job_ingest`, `job_list`, `job_note`,
`job_status`, `job_setup`, `job_cron`) over a local SQLite store.

The whole value of this skill is the **"new since last run"** flag: every
listing is keyed by its normalised URL, so re-running a search reports genuine
changes instead of the same 40 cards.

## When to Use

- The user asks to find, search, or look for jobs, roles, or vacancies —
  especially in Japan or Tokyo.
- The user wants to **monitor** boards and be told about new openings
  ("check every morning", "tell me when something new appears").
- The user wants a saved, readable artefact of a job search (Obsidian note).

**Don't use for:** reading one already-known job URL (fetch it directly), or
writing your own scraper for a board not listed here (that is ordinary
development work). For non-Japanese boards use the relevant dedicated tool.

## Requirements

Nothing to install for the tools themselves (macOS, Linux and Windows are all
supported). Four boards need no extra software at all; `indeed` and `mynavi2027`
need a browser backend (Scrapling, which the plugin auto-detects, or the venv
`job_setup` builds); `linkedin` needs `opencli` with Chrome running. No MCP
server is involved. `job_status` reports exactly what this machine has.

## The tools

| Tool | Use it for |
|------|-----------|
| `job_search` | Live scrape of the boards. Returns the result envelope. |
| `job_ingest` | Fallback: listings you fetched by hand enter the same pipeline. |
| `job_list` | Read what is already stored — no network, instant. |
| `job_note` | Render a result envelope into an Obsidian Markdown note. |
| `job_status` | Store counts, last run, browser backend, board readiness. |
| `job_setup` | One-time runtime install (adopts Scrapling, or builds Playwright). |
| `job_cron` | Schedule recurring monitoring through `hermes cron`. |

## Board coverage — read this before choosing sources

| Board | Selector | Keyword | Location | Cost | Notes |
|-------|----------|---------|----------|------|-------|
| Wantedly | `wantedly` | ✅ server-side | ✅ slug | ~4 s | **The general-purpose board.** JSON API, any language, no browser. |
| Green | `green` | ✅ server-side | ✅ slug/地名 | ~1 s | IT/Web industry. Salary is usually on the card. Payload embedded in the page. |
| Daijob | `daijob` | ✅ server-side | ✅ slug (東京/大阪) | ~2 s | Bilingual and foreign-capital employers. Server-rendered HTML. |
| Japan Dev | `japandev` | ⚠️ titles only | ❌ | ~1 s | English-speaking tech jobs. **The site ignores `?query=`** — the plugin filters titles itself, so Japanese keywords match nothing here. |
| Indeed Japan | `indeed` | ✅ server-side | ✅ slug or 地名 | ~30 s | The widest market. Stealth browser; the only board that can be bot-blocked. |
| Mynavi 2027 | `mynavi2027` | ⚠️ best-effort | ❌ ignored | ~1 min | New-graduate, design-only, nationwide, by occupation code. |
| LinkedIn | `linkedin` | ✅ server-side | ✅ | ~20 s | Needs Chrome running with the OpenCLI extension. |

**Rule of thumb:** for any general or non-design search
(`"frontend engineer"`, `"marketing"`, `"データサイエンティスト"`) pass
`sources: ["wantedly"]` plus `"green"` and/or `"daijob"` — all three are fast and
browser-free. Add `"indeed"` for the widest net (it costs ~30 s), and
`"japandev"` when the user wants English-speaking workplaces. The default
(`wantedly, mynavi2027, linkedin`) is the design-in-Tokyo feed the project was
built around; it is *not* the best choice for an arbitrary query.

`location` accepts a slug (`tokyo`, `osaka`), a Japanese place name (`大阪`), or
`any` for nationwide. Each board maps it to its own codes (Green uses prefecture
ids, Daijob prefecture codes); a location a board does not know is ignored
rather than wrong — and Mynavi ignores it entirely.

## Default flow

1. **`job_status`** when anything seems off, or on the first run of a session.
   It reports the browser backend and whether each board can run.
2. **`job_search`** with the user's query. Expect 4–90 seconds depending on the
   boards — wait, do not retry in a loop.
3. **`job_note`**, passing the exact `result` object `job_search` returned.
   This is the default deliverable; skip it only when the user explicitly asks
   for inline output only.
4. **Report** a short digest grouped by board, newest and most relevant first:
   title, company, location, link. Flag `is_new` listings prominently. Say
   which boards failed, if any.

Answer in the language the user writes in.

## How fetching works (and how to fix it when it doesn't)

Scrapers never open a browser themselves — they hand a list of page steps to a
**backend**, and the plugin picks the best one available:

| Backend | Chosen when | Notes |
|---------|-------------|-------|
| `scrapling` | Installed (the default) | Stealth browser, solves Cloudflare. Auto-detected; needs no download. |
| `playwright` | Scrapling absent but the plugin venv exists | Built by `job_setup` (~150 MB Chromium). |

Four of the seven boards need no browser at all (Wantedly, Green, Daijob, Japan
Dev), so they keep working on a machine where the browser stack is missing — if
a search fails only on Indeed/Mynavi, that is why.

- `job_status` shows which backend is active and what each board needs.
- `JOBREACH_BACKEND=scrapling|playwright` pins one (useful when a board breaks
  on one backend only); `JOBREACH_SCRAPLING_PYTHON` points at an interpreter
  that has Scrapling, if auto-detection missed it.

## Indeed Japan

Indeed is scraped by the plugin (stealth browser). Three things make its output
trustworthy, and all three are enforced in code:

- the URL always carries `&hl=ja`, and a response from `www.indeed.com` is a
  hard failure — those would be American listings;
- `jk` (the listing id) is the dedup key, so re-runs do not re-report;
- a Cloudflare challenge is reported as **blocked**, never as "no jobs".

**When Indeed comes back blocked or empty,** fall back to fetching it yourself
and pushing the cards through `job_ingest` — the full recipe, including the
extraction script, is in `references/indeed.md`. Never invent, guess, or
"reconstruct" listings: a blocked board means **zero** results, say exactly
that, and move on.

## Reading one listing in detail

To summarise or evaluate a single listing, fetch its page with the Scrapling
MCP tools (`stealthy_fetch` — the plugin's own stealth browser) and read the
markdown; pass only the jobs the user cares about. Do not run a full search to
answer a question about one URL the user already has.

## Monitoring

For "check daily and message me", use `job_cron`. It schedules a **monitor
job**: the boards are polled cheaply on every tick and the agent only wakes
when the output actually changed, so an idle schedule costs nothing.

```
job_cron(schedule="0 9 * * *", keyword="frontend engineer",
         sources="wantedly,indeed", deliver="telegram")
```

Then tell the user: the job only fires while the **Hermes gateway is running**
(`hermes gateway status`), and `hermes cron list` shows it.

## Output shape

`job_search` / `job_ingest` return:

```json
{
  "mode": "search",
  "query": {"keyword": "engineer", "location": "tokyo", "sources": ["wantedly", "indeed"]},
  "summary": {
    "total": 16, "new": 16, "saved": 16, "shown": 5,
    "by_platform": {"wantedly": {"total": 16, "new": 16}},
    "errors": {}
  },
  "jobs": [
    {"title": "…", "company": "…", "url": "…", "location": "…",
     "source_platform": "wantedly", "source_label": "Wantedly",
     "salary": null, "is_new": true, "scraped_at": "…", "posted_at": "…"}
  ]
}
```

- `summary.new` = listings not present in the store before this run;
  `summary.saved` = how many rows were written (0 with `save: false`).
- `summary.errors` maps a failed board → reason. **A partial failure still
  returns the boards that succeeded** — report the failure, keep the results.
- `jobs` is ordered new-first, then by board and title.
- `posted_at` is the board's publication date when it exposes one (Wantedly
  does; Indeed and Mynavi do not — it stays `null`).

## How "new" tracking works

Listings persist to SQLite keyed by normalised URL. Each run compares what it
found against the store: unseen → `is_new: true` and saved; already-known →
`is_new: false`. So the **first run of a query marks everything new** — that is
correct, not a bug. Use `new_only: true` to surface only fresh listings, or
`save: false` for a throwaway search that leaves the store untouched.

## Recipes

| Goal | Call |
|------|------|
| Design jobs in Tokyo (the default feed) | `job_search()` |
| Engineer roles, Wantedly only, top 10 | `job_search(keyword="engineer", sources=["wantedly"], limit=10)` |
| Full market sweep, still fast | `job_search(keyword="データサイエンティスト", sources=["wantedly", "green", "daijob"])` |
| English-speaking employers | `job_search(keyword="engineer", sources=["japandev", "daijob"])` |
| Widest net for one query | `job_search(keyword="designer", sources=["wantedly", "indeed", "green"])` |
| Only what is new since last check | `job_search(keyword="…", new_only=True)` |
| Throwaway search, do not persist | `job_search(keyword="UX researcher", save=False)` |
| Clean up noisy results | `job_search(keyword="designer", validation="local", profile="designer")` |
| What did we collect this week? | `job_list(new_since="2026-09-10T00:00:00+00:00")` |
| Save the last search to Obsidian | `job_note(result=<the envelope>)` |
| New-grad design, Mynavi only | `job_search(sources=["mynavi2027"])` |

## One note from several searches

`job_note` renders a single envelope, so a multi-board, multi-keyword sweep scatters
across several notes. For **one** consolidated note:

1. run every search with `save: true` — the SQLite store becomes the source of truth;
2. rebuild a merged envelope from the store (`~/.hermes/plugin-data/job-reach/jobs.db`,
   table `jobs`: url/title/company/location/source_platform/salary/posted_at/first_seen_at/
   last_seen_at — **no run id**, so filter `last_seen_at >= <session start>`), dropping
   boards with no relevant rows, de-duplicating per board by (title, company) and
   recomputing `summary.total`/`by_platform`; flag `is_new` only for rows first seen
   after the session start, not for the whole day (known rows get their `last_seen_at`
   bumped on re-scrape, so a date filter over-claims "new");
3. render with the plugin's own writer so the format matches earlier notes:

```bash
cd ~/.hermes/plugins/job-reach
~/.hermes/plugin-data/job-reach/venv/bin/python -m jobreach note \
    --input /tmp/merged.json --vault ~/Obsidian/Adi --json
```

Use the plugin venv (or any interpreter ≥3.10): macOS' `/usr/bin/python3` is 3.9 and dies
on `@dataclass(slots=True)` in `config.py`. Section headers come from
`domain.PLATFORM_LABELS`, so every board reads properly (Green, Daijob, Japan Dev) — a
lowercase board id in a header means the installed plugin copy is older than the repo;
sync `jobreach/notes.py` from `~/My_projects/Job_reach`.
Finish with a hand-written "read this first" block above the tables (direct hits for the
user's exact ask, similar roles, per-board caveats): the tables alone are raw data.

## Pitfalls

1. **Treating a 30–90 s scrape as a hang.** Two boards drive a real browser
   against live sites. Do not re-issue the search.
2. **Reading `new: 0` as failure.** It means nothing changed since the last
   run. Check `summary.total` for how many currently match.
3. **Routing a general keyword to Mynavi.** It is occupation-code,
   new-graduate, design-only, and ignores location. Use Wantedly or Indeed.
4. **Assuming Wantedly is broken when the browser stack is.** It is HTTP-only;
   a Wantedly failure is a network/API problem, not a missing browser.
5. **Fabricating listings.** A blocked or empty board means zero results.
   Report it as blocked/empty. This is the single worst failure mode here.
6. **Skipping `job_note`.** The note is the deliverable that survives the chat.
7. **Empty results for a niche English keyword.** Try the Japanese term
   (`デザイナー`, `エンジニア`) or `location="any"` to widen.
8. **Assuming cron fires without the gateway.** The ticker runs inside the
   Hermes gateway process. If it is stopped, nothing fires.
9. **Expecting LinkedIn to work without Chrome.** `opencli linkedin whoami`
   must succeed first; if it hangs, Chrome is not running.
10. **Pinning a backend and leaving it pinned.** `JOBREACH_BACKEND` is a
    debugging tool; `auto` is the right setting in normal use.
11. **Sending a Japanese keyword to Japan Dev.** Its titles are English and its
    search box is client-side, so `デザイナー` returns nothing there. Search it
    with English terms (`designer`, `engineer`) or leave it out.
12. **Expecting a board to honour every location.** Green and Daijob only know
    their own codes: an unmapped location means "no filter" (nationwide), and
    Mynavi ignores location entirely. Say so instead of implying a city filter.

13. **Grepping the store by date to find "today's" rows.** `jobs` has no run id and a
    re-scrape bumps `last_seen_at` of known rows, so filter by timestamp, not by date,
    and check `runs` to find when the session actually started.
14. **Expecting Daijob to honour a design keyword.** In practice its keyword search
    returns recruiter/office noise for `グラフィックデザイナー` — verify a sample of
    titles before believing a board matched, and drop the board from the note if it
    did not. Likewise Green's keyword search is fuzzy: it returns sales/HR roles for
    `DTP` and `グラフィックデザイナー`, so re-filter titles before reporting.

## Verification checklist

- [ ] Reported counts match the envelope (`total`, `new`, `shown`).
- [ ] Every board in `summary.errors` was acknowledged to the user.
- [ ] A general keyword search used `sources` beyond the Mynavi default.
- [ ] Fresh listings were flagged as new, and the note was written when the
      user wanted a saved artefact.
- [ ] Indeed (if requested): a blocked run was reported as blocked rather than
      filled in, and no US listings (`www.indeed.com`) were presented as Tokyo
      jobs.
- [ ] If a scrape appeared to hang: it was given at least two minutes.
