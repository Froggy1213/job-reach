# Indeed Japan — browser extraction reference

Indeed Japan (`jp.indeed.com`) is the one board with **no scraper**. Cloudflare
blocks headless and plain-HTTP clients (verified: 403 on a direct request, a
challenge page via reader services), but a **real browser passes** and serves
full listings with company, salary, and `jk` links.

So the agent fetches, and `job_ingest` stores. Never fabricate rows.

---

## 1. Build the URL

```
https://jp.indeed.com/jobs?q=<URL-encoded keyword>&l=<URL-encoded location>&hl=ja
```

| Part | Value | Why |
|------|-------|-----|
| host | `jp.indeed.com` | the Japanese market |
| `q` | `デザイナー`, `engineer`, … | Japanese terms are safest; romaji works |
| `l` | `東京` (`%E6%9D%B1%E4%BA%AC`) | default location |
| `hl=ja` | always | without it Indeed may serve `www.indeed.com` |

## 2. Navigate with a real browser

Use the browser tool — not a headless HTTP fetch. Wait ~2–3 seconds for the
cards to render. Then **verify the host before extracting**:

```js
() => ({ host: location.host, title: document.title, url: location.href })
```

If `host` is not `jp.indeed.com`, Indeed geo-redirected you to the US site
(non-Japanese IP). Re-open the `jp.indeed.com … &hl=ja` URL; if it keeps
bouncing, a Japanese network/VPN is required. **Never extract from
`www.indeed.com`.**

## 3. Detect a block before trusting anything

A block looks like any of:

- page text containing `Just a moment`, `Additional Verification Required`,
  or `Ray ID`
- zero elements matching `a[data-jk], a[href*="jk="]`

A block means **zero results this run**. Report *"Indeed: blocked this run"*
and move on. It is a perfectly acceptable outcome; inventing listings is not.

## 4. Extract the cards

Run this in the page. `jk` is the dedup key; company/location/salary selectors
drift over time, so empty values are tolerated (they are best-effort).

```js
() => {
  if (location.host !== 'jp.indeed.com') {
    return { error: 'not on jp.indeed.com — got ' + location.host };
  }
  const out = [], seen = new Set();
  document.querySelectorAll('a[data-jk], a[href*="jk="]').forEach(a => {
    if ((a.href || '').includes('www.indeed.com')) return;   // skip US listings
    let jk = a.getAttribute('data-jk');
    if (!jk) {
      const m = (a.getAttribute('href') || '').match(/[?&]jk=([0-9a-f]+)/);
      if (m) jk = m[1];
    }
    if (!jk || seen.has(jk)) return;
    seen.add(jk);

    const card = a.closest('.job_seen_beacon, .cardOutline, li') || a.parentElement;
    const text = s => {
      const el = card && card.querySelector(s);
      return el ? el.textContent.trim() : '';
    };
    out.push({
      title: (a.textContent || '').trim().slice(0, 200),
      company: text('[data-testid="company-name"]') || text('.companyName'),
      location: text('[data-testid="text-location"]') || text('.companyLocation') || 'Japan',
      salary: text('[data-testid="attribute_snippet_testid"]')
              || text('.salary-snippet-container') || null,
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
- The listings now share the store, the `is_new` flag, and the note format with
  every scraped board. Add `save: false` for a throwaway check.

## 6. Deliver

Report Indeed as its own group, alongside Wantedly/Mynavi/LinkedIn, honouring
the `is_new` flags the ingest returned. If there was nothing, say so.

---

## Why the URL keeps its query string

The dedup key is the normalised URL, and normalization deliberately preserves
the query string, because Indeed's listing id lives in `?jk=`. Dropping it
would collapse every Indeed listing into one row. Boards whose id is in the
path (Wantedly, Mynavi, LinkedIn) strip the query before it reaches the store,
so preserving it costs them nothing.
