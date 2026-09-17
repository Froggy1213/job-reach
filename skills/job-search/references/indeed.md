# Indeed Japan — the fallback route

Indeed Japan is **scraped automatically** by the plugin's stealth browser:

```
job_search(keyword="web designer", sources=["indeed"], location="tokyo")
```

Use this reference only when that fails — a Cloudflare challenge the stealth
browser lost, an empty page, or a machine with no browser backend at all. The
workflow is: *you* fetch the cards with a real browser, `job_ingest` stores them.

A blocked run means **zero results**. Say that. Never fabricate rows.

---

## 1. Build the URL

```
https://jp.indeed.com/jobs?q=<URL-encoded keyword>&l=<URL-encoded location>&hl=ja
```

| Part | Value | Why |
|------|-------|-----|
| host | `jp.indeed.com` | the Japanese market |
| `q` | `デザイナー`, `engineer`, … | Japanese terms are safest; romaji works |
| `l` | `東京` (`%E6%9D%B1%E4%BA%AC`) | omit for nationwide |
| `hl=ja` | always | without it Indeed may serve `www.indeed.com` |

## 2. Fetch it with a real browser

Prefer the Scrapling MCP tool — it *is* the plugin's stealth engine and it
usually gets through:

```
mcp__scrapling__stealthy_fetch(
  url="https://jp.indeed.com/jobs?q=…&l=…&hl=ja",
  headless=true,
  solve_cloudflare=true,
  css_selector="a[data-jk]",
)
```

Failing that, drive the interactive browser tool and let the cards render
(2–3 s). Then **verify the host before trusting anything**:

```js
() => ({ host: location.host, title: document.title, url: location.href })
```

If `host` is not `jp.indeed.com`, Indeed geo-redirected you to the US site.
Re-open the `jp.indeed.com … &hl=ja` URL; if it keeps bouncing, a Japanese
network is required. **Never extract from `www.indeed.com`.**

## 3. Detect a block before trusting anything

A block looks like any of:

- page text containing `Just a moment`, `Additional Verification Required`,
  `Enable JavaScript and cookies to continue`, or `Ray ID` **and** no cards
- zero elements matching `a[data-jk]`

A block means zero results for that run. Report *"Indeed: blocked this run"*.

## 4. Extract the cards

This is the same selector set the plugin's own scraper uses, verified against
the live site: the title link is `a[data-jk]` (class `jcs-JobTitle`), company is
`[data-testid="company-name"]`, location `[data-testid="text-location"]`, and
every other attribute (salary, employment type) lives in
`[data-testid="attribute_snippet_testid"]`.

```js
() => {
  if (location.host !== 'jp.indeed.com') {
    return { error: 'not on jp.indeed.com — got ' + location.host };
  }
  const out = [], seen = new Set();
  document.querySelectorAll('a[data-jk], a.jcs-JobTitle, a[href*="jk="]').forEach(link => {
    let jk = link.getAttribute('data-jk') || '';
    if (!jk) {
      const m = (link.getAttribute('href') || '').match(/[?&]jk=([0-9a-zA-Z]+)/);
      jk = m ? m[1] : '';
    }
    if (!jk || seen.has(jk)) return;
    const href = link.getAttribute('href') || '';
    if (href.includes('www.indeed.com')) return;   // skip US listings
    seen.add(jk);

    const card = link.closest('div.job_seen_beacon, div.cardOutline, li, div[data-jk]')
              || link.parentElement;
    const text = s => {
      const el = card ? card.querySelector(s) : null;
      return el ? el.textContent.replace(/\s+/g, ' ').trim() : '';
    };
    const attrs = card
      ? [...card.querySelectorAll('[data-testid="attribute_snippet_testid"], .salary-snippet-container')]
          .map(el => (el.textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean)
      : [];

    out.push({
      title: (link.querySelector('span[title]') || link).textContent.trim().slice(0, 200),
      company: text('[data-testid="company-name"]'),
      location: text('[data-testid="text-location"]') || 'Japan',
      salary: attrs.find(t => /[0-9][0-9,]*\s*円|月給|年収|時給|日給/.test(t)) || null,
      url: 'https://jp.indeed.com/viewjob?jk=' + jk,
    });
  });
  return out;
}
```

## 5. Feed the pipeline

```
job_ingest(jobs=<the array from step 4>)
```

- `source_platform` defaults to `indeed`; `title` + `url` are required.
- Records missing either are skipped and counted in
  `summary.errors.ingest_skipped` — mention skipped rows if the count is high.
- The listings then share the store, the `is_new` flag, and the note format with
  every scraped board. Add `save: false` for a throwaway check.

## 6. Deliver

Report Indeed as its own group, alongside the other boards, honouring the
`is_new` flags the ingest returned. If there was nothing, say so.

---

## Why the URL keeps its query string

The dedup key is the normalised URL, and normalization deliberately preserves
the query string, because Indeed's listing id lives in `?jk=`. Dropping it
would collapse every Indeed listing into one row. Boards whose id is in the
path (Wantedly, Mynavi, LinkedIn) strip the query before it reaches the store,
so preserving it costs them nothing.

## Why the plugin can do this on its own now

Cloudflare used to win against headless clients, which is why Indeed was
ingest-only. Scrapling's stealth browser (patched Chromium, turnstile solving)
gets through: measured 200 OK, 16 cards, ~7 s, headless, repeated runs. The
fallback above remains because *sometimes* it still fails — and on those days an
agent with a browser is the difference between "no jobs" and a reported block.
