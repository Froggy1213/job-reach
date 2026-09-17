---
name: job-search
description: Find jobs on Japanese boards and track what is new.
version: 2.0.0
author: Froggy1213
license: MIT
platforms: [macos, linux]
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

## The tools

| Tool | Use it for |
|------|-----------|
| `job_search` | Live scrape of the boards. Returns the result envelope. |
| `job_ingest` | **The only way Indeed Japan listings enter the store.** |
| `job_list` | Read what is already stored — no network, instant. |
| `job_note` | Render a result envelope into an Obsidian Markdown note. |
| `job_status` | Store counts, last run, and which boards can actually run. |
| `job_setup` | One-time runtime install (venv + Chromium + skill). |
| `job_cron` | Schedule recurring monitoring through `hermes cron`. |

## Board coverage — read this before choosing sources

| Board | Selector | Keyword | Location | Notes |
|-------|----------|---------|----------|-------|
| Wantedly | `wantedly` | ✅ server-side | ✅ slug | **The general-purpose board.** Arbitrary queries in any language work. |
| Mynavi 2027 | `mynavi2027` | ⚠️ best-effort | ❌ ignored | New-graduate, design-only, nationwide, scraped by occupation code. |
| LinkedIn | `linkedin` | ✅ server-side | ✅ | Needs Chrome running with the OpenCLI extension. |
| Indeed Japan | `indeed` | — | — | **No scraper.** Cloudflare blocks headless clients; only `job_ingest`. |

**Rule of thumb:** for any general or non-design search
(`"frontend engineer"`, `"marketing"`, `"データサイエンティスト"`) pass
`sources: ["wantedly"]` — or add `linkedin`. Leave the default
(`wantedly, mynavi2027, linkedin`) only for the design-in-Tokyo use case the
project was built around.

`location` is a Wantedly slug: `tokyo` (default), `osaka`, `any` for
nationwide. Mynavi ignores it entirely.

## Default flow

1. **`job_status`** when anything seems off, or on the first run of a session.
   It reports whether the scraping runtime is installed.
2. **`job_search`** with the user's query. A live scrape takes **30–90 seconds
   per board** — wait, do not retry in a loop.
3. **`job_note`**, passing the exact `result` object `job_search` returned.
   This is the default deliverable; skip it only when the user explicitly asks
   for inline output only.
4. **Report** a short digest grouped by board, newest and most relevant first:
   title, company, location, link. Flag `is_new` listings prominently. Say
   which boards failed, if any.

Answer in the language the user writes in.

## Indeed Japan (agent-driven, real browser required)

Indeed Japan has **no scraper** on purpose: Cloudflare blocks headless and
plain-HTTP clients (verified 403), but a **real browser passes** and serves
full listings. So *you* fetch them and hand them to `job_ingest`.

Only do this when the user actually asks for Indeed.

1. Build the URL with the Japanese market locked in:
   `https://jp.indeed.com/jobs?q=<keyword>&l=<location>&hl=ja`
   (URL-encode both; default location 東京 = `l=%E6%9D%B1%E4%BA%AC`).
   Without `hl=ja` Indeed may serve the US site.
2. Open it in a **real browser** (browser tool — not a headless fetch). Wait
   2–3 s for the cards to render, then **verify `location.host` is
   `jp.indeed.com`**. If it bounced to `www.indeed.com`, Indeed geo-redirected
   you to the US site: reopen the `jp.indeed.com … &hl=ja` URL; if it keeps
   bouncing, a Japanese network is required. Never extract from
   `www.indeed.com` — those are US jobs.
3. **Detect a block before trusting anything.** `Just a moment`,
   `Additional Verification Required`, `Ray ID`, or zero job cards means
   **blocked**. Report *"Indeed: blocked this run"* and move on.
   ⛔ **Never invent, guess, or "reconstruct" Indeed listings.** A challenge or
   empty page means **zero** results — say exactly that.
4. Extract the cards with the browser JS in
   `references/indeed-browser.md` (the `jk` query param is the dedup key).
5. Push them through the pipeline:

   ```
   job_ingest(jobs=[{title, company, location, url}, ...])
   ```

   They then share the store, the "new" flag, and the note format with every
   scraped board.

## Monitoring

For "check daily and message me", use `job_cron`. It schedules a **monitor
job**: the boards are polled cheaply on every tick and the agent only wakes
when the output actually changed, so an idle schedule costs nothing.

```
job_cron(schedule="0 9 * * *", keyword="frontend engineer",
         sources="wantedly,linkedin", deliver="telegram")
```

Then tell the user: the job only fires while the **Hermes gateway is running**
(`hermes gateway status`), and `hermes cron list` shows it.

## Output shape

`job_search` / `job_ingest` return:

```json
{
  "mode": "search",
  "query": {"keyword": "engineer", "location": "tokyo", "sources": ["wantedly"]},
  "summary": {
    "total": 16, "new": 16, "saved": 16, "shown": 5,
    "by_platform": {"wantedly": {"total": 16, "new": 16}},
    "errors": {}
  },
  "jobs": [
    {"title": "…", "company": "…", "url": "…", "location": "…",
     "source_platform": "wantedly", "source_label": "Wantedly",
     "salary": null, "is_new": true, "scraped_at": "…"}
  ]
}
```

- `summary.new` = listings not present in the store before this run;
  `summary.saved` = how many rows were written (0 with `save: false`).
- `summary.errors` maps a failed board → reason. **A partial failure still
  returns the boards that succeeded** — report the failure, keep the results.
- `jobs` is ordered new-first, then by board and title.

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
| Only what is new since last check | `job_search(keyword="…", new_only=True)` |
| Throwaway search, do not persist | `job_search(keyword="UX researcher", save=False)` |
| Clean up noisy results | `job_search(keyword="designer", validation="local", profile="designer")` |
| What did we collect this week? | `job_list(new_since="2026-09-10T00:00:00+00:00")` |
| Save the last search to Obsidian | `job_note(result=<the envelope>)` |
| New-grad design, Mynavi only | `job_search(sources=["mynavi2027"])` |

## Pitfalls

1. **Treating a 30–90 s scrape as a hang.** Playwright is launching a real
   browser against live sites. Do not re-issue the search.
2. **Reading `new: 0` as failure.** It means nothing changed since the last
   run. Check `summary.total` for how many currently match.
3. **Routing a general keyword to Mynavi.** It is occupation-code,
   new-graduate, design-only, and ignores location. Use Wantedly.
4. **Using `job_search` for Indeed.** It will reject `indeed` with an
   explanation; that is intentional. Use `job_ingest`.
5. **Fabricating listings.** A blocked or empty board means zero results.
   Report it as blocked/empty. This is the single worst failure mode here.
6. **Skipping `job_note`.** The note is the deliverable that survives the chat.
7. **Empty results for a niche English keyword.** Try the Japanese term
   (`デザイナー`, `エンジニア`) or `location="any"` to widen.
8. **Assuming cron fires without the gateway.** The ticker runs inside the
   Hermes gateway process. If it is stopped, nothing fires.
9. **Expecting LinkedIn to work without Chrome.** `opencli linkedin whoami`
   must succeed first; if it hangs, Chrome is not running.

## Verification checklist

- [ ] Reported counts match the envelope (`total`, `new`, `shown`).
- [ ] Every board in `summary.errors` was acknowledged to the user.
- [ ] A general keyword search used `sources` including `wantedly`.
- [ ] Fresh listings were flagged as new, and the note was written when the
      user wanted a saved artefact.
- [ ] Indeed (if requested): a **real browser** was used, the page was verified
      not to be a Cloudflare challenge, and any blocked run was reported as
      blocked rather than filled in.
- [ ] If a scrape appeared to hang: it was given at least two minutes.
