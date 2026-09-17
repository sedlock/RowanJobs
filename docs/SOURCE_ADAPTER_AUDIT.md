# Source adapter audit — jobs.rowan.edu

A live audit of the source was performed on **2026-09-16** before the adapters
were written. Everything below was observed directly; the responses were saved
and are the basis for the adapter code in
`src/rowanjobs/extract/pageup_listing.py` and
`src/rowanjobs/extract/pageup_detail.py`, and for the qualification checks in
`src/rowanjobs/collect/qualify.py`.

**Where the captures live:** `~/.local/share/rowanjobs/source-audit/2026-09-16/`
(that is, `<data_root>/source-audit/<date>/`, `Layout.audit_dir` in
`src/rowanjobs/paths.py`). Each capture is stored as three files: `*.html` /
`*.body` (the payload), `*.headers` (the response headers) and `*.meta`
(retrieval time, HTTP code, effective URL, size, timing).

These captures are **audit material, not production history**. They are not part
of the archive and are not backed up as evidence; they exist so the adapter's
assumptions can be re-checked. Trimmed, public versions of the same pages are
committed under `tests/fixtures/pageup/` for the test suite.

Summary of findings is also recorded in `EXECUTION_STATE.md`.

---

## Capture log

| Capture | URL | Retrieved (UTC) | HTTP |
|---|---|---|---|
| `robots` | `/robots.txt` | 2026-09-16T21:41:00Z | 200 |
| `listing-p1` | `/en-us/listing/` | 2026-09-16T21:41:08Z | 200 |
| `detail-501826` | `/en-us/job/501826/temporary-part-time-hourly-public-safety-telecommunicator-police-department` | 2026-09-16T21:41:53Z | 200 |
| `listing-p2` | `/en-us/listing/?page=2&page-items=20` | 2026-09-16T21:41:54Z | 200 |
| `listing-p7` | `/en-us/listing/?page=7&page-items=20` | 2026-09-16T21:42:35Z | 200 |
| `listing-p8` | `/en-us/listing/?page=8&page-items=20` | 2026-09-16T21:42:38Z | 200 |
| `listing-p99` | `/en-us/listing/?page=99&page-items=20` | 2026-09-16T21:42:40Z | 200 |
| `detail-missing-400000` | `/en-us/job/400000/nonexistent-placeholder` | 2026-09-16T21:43:01Z | **202 challenge** |
| `detail-wrongslug-501826` | `/en-us/job/501826/wrong-slug-here` | 2026-09-16T21:43:03Z | **202 challenge** |
| `detail-noslug-501826` | `/en-us/job/501826` | 2026-09-16T21:43:04Z | **202 challenge** |
| `recheck-detail-501826` | detail URL, repeat | 2026-09-16T21:43:28Z | **202 challenge** |
| `recheck-listing-p1` | `/en-us/listing/` | 2026-09-16T21:43:32Z | **202 challenge** |
| `waf-t1-bot` | `/en-us/` URL, archiver user agent | 2026-09-16T21:45:34Z | **202 challenge** |
| `waf-t2-browserua` | same URL, ordinary browser user agent | 2026-09-16T21:45:40Z | **202 challenge** |
| `waf-t3-robots-bot` | `/robots.txt`, archiver user agent | 2026-09-16T21:45:45Z | 200 (654 bytes) |
| recovery probe (`h.tmp`/`b.tmp`) | `/en-us/listing/` | 2026-09-16T21:54:06Z | 200 (103,064 bytes) |

Because the audit was interrupted by an access-control challenge part-way
through, several intended captures (`detail-missing-400000`,
`detail-wrongslug-501826`, `detail-noslug-501826`) recorded a challenge rather
than the behaviour they were meant to probe. **Those questions are therefore
still open** — see "What this audit did not establish" below.

---

## robots.txt

`GET /robots.txt` → HTTP 200, `text/plain`, 654 bytes, `Last-Modified:
Thu, 09 Jul 2026 06:53:47 GMT`.

It contains a single `User-agent: *` group whose `Disallow` rules are all
administrative or pre-production path prefixes, in both lower- and upper-case
forms and in both root and locale-prefixed forms:

```
/admin  /awake  /uat  /cwuat  /ciuat  /ci  /uatinternal  /testint  /staging
/Admin  /Awake  /UAT  /CWUAT  /CIUAT  /CI  /UATINTERNAL  /TESTINT  /STAGING
/*/uat/  /*/cwuat/  /*/ciuat/  /*/ci/  /*/uatinternal/  /*/testint/  /*/staging/
/*/UAT/  /*/CWUAT/  /*/CIUAT/  /*/CI/  /*/UATINTERNAL/  /*/TESTINT/  /*/STAGING/
```

Consequences for RowanJobs:

- `/en-us/listing/` and `/en-us/job/...` are **permitted**. These are the only
  path families the collector requests.
- There is **no `Crawl-delay`** directive, so the pacing in
  `src/rowanjobs/net/budget.py` is a self-imposed courtesy limit rather than a
  published requirement.
- There is **no `Sitemap`** directive, so full pagination is the only way to
  enumerate the inventory.
- No disallowed path is ever requested. The allowlist in
  `network.allowed_hosts` restricts traffic to `jobs.rowan.edu` and
  `careers-static.pageuppeople.com`.

## Platform

The site is PageUp. The listing page carries an inline configuration object:

```js
PU.Jobs.source = {"instId":860,"sourcePointer":"cw","language":"en-us",
                  "baseDomain":"https://jobs.rowan.edu",
                  "dynamicTemplate":true,"action":"Listing"}
```

`instId` 860 also appears in the apply-link host (`secure.dc4.pageuppeople.com/apply/860/...`).
This is why the adapter family is named `pageup_v1` and the source namespace is
`rowan.pageup`.

## Pagination

Page URLs are `?page=N&page-items=20`
(`src/rowanjobs/collect/scanner.py::page_url`; `collection.page_items` defaults
to 20). Page 1 is the bare `/en-us/listing/`.

The source publishes its own next-page link:

```html
<a href="/en-us/listing/?page=2&page-items=20" class="more-link button"
   style="display:block" title="More Jobs" data-page="2" data-page-items="20">
  More Jobs <span class="count">113</span>
</a>
```

The collector follows the `more-link` href rather than incrementing a counter,
so the source's own pagination contract drives the traversal.

### `more-link` count semantics

**The number in `span.count` is the number of advertisements REMAINING AFTER
this page, not the total.** Verified across two pages:

| Page | Rows in `search-results` | `span.count` |
|---|---|---|
| 1 | 20 | 113 |
| 2 | 20 | 93 |

113 − 20 = 93. Therefore total = 113 + 20 = **133** advertisements at the time
of the audit, across **7** pages (6 × 20 + 13).

`src/rowanjobs/collect/scanner.py` records this as
`source_reported_total = more_link_remaining + len(unique ids on page 1)`, and
`src/rowanjobs/collect/qualify.py` reconciles that against the unique
identifiers actually collected. Reading the count as a total would understate
the inventory by exactly one page.

Category and location facet counts on the page are **never** summed: they
overlap, and adding them would over-count.

### Final-page signal

Page 7 carried 13 advertisements and **no `more-link` element at all**
(`grep -c more-link` = 0). The absence of the more-link is the source's
end-of-results signal; the scanner terminates with `no_more_link`
(`SCAN_TERMINATION`).

The traversal deliberately does **not** stop because a page contributed no new
identifiers — that would confuse the duplicated section with the end of results.

### Validated empty result past the end

Pages 8 and 99 both returned **HTTP 200** with the complete page structure
intact and **zero** result rows:

- `<h2>Search results</h2>` present
- `<th>Position</th> <th>Location</th> <th>Closes</th>` present
- both `<tbody id="search-results-content">` and
  `<tbody id="recent-jobs-content">` present and empty
- no `more-link`
- page 8: 51,960 bytes; page 99: 51,961 bytes

This is a *validated empty result*, which is materially different from an error
page that happens to yield no rows. The adapter only sets
`empty_result_validated` when the heading, the three column headers and the
authoritative tbody are all present with zero rows
(`src/rowanjobs/extract/pageup_listing.py::parse_listing`). A zero-result scan
that cannot demonstrate that intact template does **not** qualify
(`qualify.py`, check `empty_result_validated`).

### Pagination loop protection

Each page's `page_signature` is a SHA-256 over its ordered identifiers plus its
more-link URL. A repeated signature terminates the traversal as `loop_detected`
and costs the scan its qualification.

## The duplicated `recent-jobs` section

Every listing page repeats **every advertisement on that page** in a second
table under a `Current Opportunities` heading:

- `<tbody id="search-results-content">` — the authoritative result rows
- `<tbody id="recent-jobs-content">` — the same advertisements again

Verified by counting job links per page:

| Page | `class="job-link"` occurrences | Advertisements |
|---|---|---|
| 1 | 40 | 20 |
| 2 | 40 | 20 |
| 7 | 26 | 13 |

**Counting `a.job-link` across the page doubles the inventory.** The adapter
records entries per section (`listing_entries.section` is `search-results` or
`recent-jobs`) and derives the inventory only from
`ListingExtraction.authoritative_entries`, i.e. the `search-results` section.
`listing_scans.duplicate_occurrences` therefore being non-zero is expected, not
an anomaly.

In the `recent-jobs` copy the summary rows are **HTML-commented out**
(`<!--<tr class="summary">…`), so they are not rendered and the verbatim text
contract correctly drops them.

## Where stable job identifiers appear

The source job number appears in three places:

1. **The listing href**: `/en-us/job/501826/temporary-part-time-hourly-public-safety-telecommunicator-police-department`.
   Parsed by `JOB_HREF = ^/(?P<locale>[a-z]{2}-[a-z]{2})/job/(?P<job_id>\d+)(?:/(?P<slug>.*))?$`
   in `pageup_listing.py`. The slug is decorative; the numeric segment is the
   identifier.
2. **The detail page body**: `<span class="job-externalJobNo">501826</span>`,
   inside `<div id="job-content">`.
3. **The `Job no:` label** in the detail metadata block.

The adapter prefers (2), falls back to (3), and records a `job_no_label_vs_span`
conflict — **unresolved, both values preserved** — if they disagree
(`src/rowanjobs/collect/details.py::_field_conflicts`). A detail page whose
displayed job number disagrees with the one requested is recorded as
`identity_mismatch` or `redirected_to_other_job`; the destination's description
is never assigned to the original posting.

## Listing summary vs. detail description

These are different things and the schema keeps them apart.

**Listing summary** (`listing_entries.summary_text` / `summary_html`): a
`<tr class="summary"><td colspan="3">…</td></tr>` row following each data row in
the `search-results` section. It is short, plain, and contains no markup beyond
the cell — in the captured page it began `Summary:` followed by a single
paragraph of the advertisement's opening prose. It is what the *listing* chose
to show.

**Detail description** (`posting_versions.description_html` /
`description_text`): the contents of `<div id="job-details">` on the detail page
— the full advertisement body, with its own headings, lists and links.

The listing summary is never used as the description, and never contributes to
the content fingerprints. Only the detail body does.

## Detail page structure

Inside `<div id="job"><div id="job-content">`:

```html
<h2>Temporary Part Time Hourly Public Safety Telecommunicator (Police Department)</h2>
<p>
  <span style="float:right"><a class="apply-link button" href="https://secure.dc4.pageuppeople.com/apply/860/gateway/default.aspx?...">Apply now</a></span>
  <b>Job no:</b> <span class="job-externalJobNo">501826</span><br>
  <b>Work type:</b> <span class="work-type temporary-part-time">Temporary Part-Time</span><br>
  <b>Location:</b> <span class="location">Glassboro, New Jersey</span><br>
  <b>Categories:</b> <span class="categories">Public Safety/Security</span><br>
</p>
<div id="job-details"> ... advertisement body ... </div>
<p>
  <b>Advertised:</b> <span class="open-date"><time datetime="2026-09-15T12:00:00Z">Sep 15 2026 </time></span> Eastern Daylight Time<br>
  <b>Applications close:</b> <span class="close-date"><time datetime="2026-09-30T03:55:00Z">Sep 29 2026 11:55 PM </time></span> Eastern Daylight Time
</p>
```

Design consequences:

- Labels are read **generically** from `<b>`/`<strong>` elements that end in a
  colon, so a label the adapter has never seen is still captured, flagged
  `known_label = 0` rather than dropped.
- `#job-details` is **excluded** from label scanning. Rowan's advertisement
  bodies use bold run-in headings ("Summary:", "Major Duties:") that are prose,
  not source metadata. Treating them as fields would invent empty metadata and
  make an edit to the prose look like a metadata change.
- The apply link (`class="apply-link"`, host `secure.dc4.pageuppeople.com`) is
  classified `apply_workflow` and is **never** followed.
- Multivalued fields keep the source's own separator. **Locations are not split
  on commas**: "Glassboro, New Jersey" is one place; PageUp separates genuinely
  distinct values with `;` or repeated spans.

## No JSON-LD or structured data

`grep -c 'application/ld+json'` returned **0** on both the listing page and the
detail page. There is no schema.org `JobPosting` block, no microdata and no
embedded JSON feed of the postings. The HTML structure documented above is the
only interface, which is why `structure_recognized` is a first-class
qualification check rather than an afterthought.

## Date formats and timezone wording

| Field | Displayed | `datetime` attribute | Timezone wording |
|---|---|---|---|
| `Advertised` | `Sep 15 2026` (bare date) | `2026-09-15T12:00:00Z` | `Eastern Daylight Time`, printed after the `<time>` element |
| `Applications close` | `Sep 29 2026 11:55 PM` (minute precision) | `2026-09-30T03:55:00Z` | `Eastern Daylight Time`, printed after the `<time>` element |
| Listing row `Closes` | `Sep 29 2026` (date only) | `2026-09-30T03:55:00Z` | — |

Two things matter here:

1. **The `Advertised` machine value carries a 12:00Z placeholder.** The display
   commits only to a date. `src/rowanjobs/extract/dates.py` records
   `source_precision='date'` and attaches the note that the machine value is the
   source's own placeholder and must not be reported as a published time.
   Without that, the archive would later claim a posting was advertised at
   08:00 Eastern — a time the source never published.
2. **The timezone wording is literal text after the `<time>` element**, not an
   attribute. `_time_parts` in `pageup_detail.py` splits the cell into machine
   value, displayed date and the trailing timezone text, and all three are
   stored (`source_machine_value`, `value_text`, `source_tz_text`).

The same `datetime` value (`2026-09-30T03:55:00Z`) appears in the listing row
with a date-only display, which is a further reason not to treat the machine
value as the published precision.

## AWS WAF access-control challenge

This is the most operationally significant finding.

**What happened.** After roughly seven application requests within about ninety
seconds — the successful captures at 21:41:08, 21:41:53, 21:41:54, 21:42:35,
21:42:38 and 21:42:40 — the request at **21:43:01Z** was answered with:

```
HTTP/2 202
server: CloudFront
content-length: 0
x-amzn-waf-action: challenge
cache-control: no-store, max-age=0
content-type: text/html; charset=UTF-8
access-control-expose-headers: x-amzn-waf-action
x-cache: Error from cloudfront
```

That is: **HTTP 202, an empty body, and `x-amzn-waf-action: challenge`.**

**Scope.** Every `/en-us/` URL was challenged from then on, regardless of user
agent — the archiver user agent at 21:45:34 and an ordinary browser user agent
at 21:45:40 both received 202 challenge. Changing the user agent made no
difference, which is exactly why the collector does not attempt to disguise
itself.

**`robots.txt` kept serving normally throughout**: 200 with the full 654 bytes
at 21:45:45, while application URLs were still being challenged.

**Recovery.** The first successful application response after the challenge was
at **21:54:06Z** — approximately **eleven minutes** of quiet after the first
challenge at 21:43:01Z.

**How RowanJobs handles it** (`src/rowanjobs/net/client.py::detect_access_control`,
`src/rowanjobs/net/budget.py`):

- Any response carrying `x-amzn-waf-action` (or `x-amz-waf-action`) is
  classified as an access-control signal, as are HTTP 429, a Cloudflare-style
  503 challenge and a 403 CAPTCHA page.
- The fetch is recorded with `access_control_signal` set and
  `failure_detail` stating explicitly that this is *collection uncertainty, not
  evidence of absence*.
- A detail observation becomes `availability_state='access_control_challenge'`,
  which is in `UNCERTAIN_AVAILABILITY` and can never support an absence claim.
- A listing page challenged this way fails the `no_access_control_response`
  check, so the scan does not qualify and the run's absence analysis is
  suppressed.
- The budget widens the inter-request interval by 1.6× per challenge (capped at
  30 s), waits `challenge_backoff × 2^(n-1)` (60 s, 120 s, 240 s, …), and raises
  `ChallengeWall` after `max_consecutive_challenges` (default 4) — the run stops
  requesting rather than hammering the source.

### The token changes the picture

A follow-up measurement against the live source established the difference an
access token makes:

| Client | Result |
|---|---|
| Token-less HTTP client | Challenged from about the **seventh** request |
| Client holding a browser-issued `aws-waf-token` | **12 requests at 2.5 s intervals with no challenge at all** |

This is why the collector now primes once up front (workflow step 0,
`SourceClient.prime`): obtaining the token the way an ordinary visitor's browser
does on its first page load is both **gentler on the source** — no requests spent
being refused — and a more faithful reproduction of the public access path than
repeatedly triggering the challenge. Collection still works without it, by
backing off.

The audit's observed threshold (about seven requests in about ninety seconds) is
why `network.min_interval_seconds` defaults to 2.5 s with up to 0.75 s of
jitter, and why `network.concurrency` is 1.

## What browser rendering is and is not needed for

**Not needed for content.** The listing rows, the pagination links, the detail
body, the labelled fields and the dates are all present in the server-rendered
HTML. Both adapters work on the raw bytes; no page in the audit required
JavaScript execution to yield its advertisements. `PU.Jobs.source` sets
`dynamicTemplate: true`, but the rendered rows were nonetheless in the initial
response.

**Potentially needed only for the WAF challenge.** An ordinary visitor's browser
runs the AWS WAF challenge script, receives an `aws-waf-token` cookie and
continues. `src/rowanjobs/net/browser.py` reproduces exactly that path with an
unmodified headless Chromium, using the **same** user agent as the HTTP client,
and hands the resulting `aws-waf*` cookie back. It does not spoof fingerprints,
rotate proxies, install stealth patches or solve CAPTCHAs.

It is used in two places: once up front to prime the client
(`SourceClient.prime`), and again — with `force=True`, because the token we hold
has just been rejected — if a challenge occurs mid-run, after the collector has
already slowed down and backed off.

Collection stays correct with the browser step disabled or absent: a challenge
is then simply recorded as `access_control_challenge`, an explicit coverage
exception. See `docs/SECURITY.md`.

## What this audit did not establish

Stated explicitly so nobody later assumes otherwise:

- **The behaviour of a genuinely non-existent job id.** The
  `/en-us/job/400000/...` probe was answered by the WAF challenge, not by the
  site. Whether the source returns 404, a closure template, or a redirect to the
  listing is **unknown**. The adapter handles all three
  (`not_found`, `explicit_closure`, `redirected_to_listing`) but none of them has
  been confirmed against this source.
- **Slug canonicalisation behaviour.** The wrong-slug and no-slug probes were
  also challenged, so it is not known whether the site redirects to the
  canonical slug, serves the page regardless, or errors. `_redirect_class`
  distinguishes `same_job_canonicalised` from `to_other_job` in anticipation.
- **The closure template's wording.** `CLOSURE_PHRASES` in `pageup_detail.py`
  is a defensive list, not a list of phrases observed on this source.
- **Whether `#message-list` is ever populated.** It was present but empty in the
  captured detail page.
- **Long-term stability of the WAF threshold.** One observation of one rate
  limit on one afternoon is not a published policy.

The first production harvest and the first observed withdrawal will resolve
several of these; record the answers here when they do.
