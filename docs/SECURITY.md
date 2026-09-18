# Source-access policy and security posture

RowanJobs reads pages that any member of the public can read, at a pace that does
not burden the source, and treats everything it retrieves as untrusted input.
This document states the policy and points at the code that enforces it.

---

## 1. Public, unauthenticated collection only

RowanJobs requests exactly two families of public URL on `jobs.rowan.edu`:

- `/en-us/listing/` and its `?page=N&page-items=20` variants;
- `/en-us/job/<id>[/<slug>]` detail pages;

plus job-specific documents linked from inside an advertisement and hosted on
Rowan's own domain (`rowan.edu` or any `*.rowan.edu` host — the employer spreads
these across `engineering.rowan.edu`, `sites.rowan.edu` and similar), and
`robots.txt`.

No Rowan page is requested for any other reason: reaching a `rowan.edu` URL
requires an advertisement to have linked to a document, and
`classify_link` to have classified it `job_document`. A `job_document` on a
third-party host is recorded with its exclusion reason and **not** retrieved.

It categorically does **not**:

| Not done | Enforced by |
|---|---|
| Create or use an applicant account | No credential is ever sent to the source. The one credential RowanJobs holds is the outbound SMTP App Password used to email run reports (§7); it is never presented to `jobs.rowan.edu` or any other collection host |
| Submit an application, or follow an apply workflow | `classify_link` marks `apply-link`, `employee-referral-link`, `/apply/` and any `pageuppeople.com` host as `apply_workflow` with `collection_decision='exclude'` and the reason *"application submission workflow is never followed"* |
| Use Rowan internal credentials or internal systems | The host allowlist is `jobs.rowan.edu`, `careers-static.pageuppeople.com` and `rowan.edu` (with `allow_subdomains = true`, so `*.rowan.edu` matches); nothing else can be contacted |
| Send any authentication header | No `Authorization`, cookie jar seeding or API key is ever constructed. The only cookies the client ever holds are the WAF tokens described in §4 |
| Collect applicant or personal data | Only published advertisement content is stored |

There is no login, no session, no form POST. Every request is a `GET`
(`fetches.method` is always `GET`).

## 2. robots.txt is respected

The audit of 2026-09-16 recorded `robots.txt` in full
(`docs/SOURCE_ADAPTER_AUDIT.md`). Its single `User-agent: *` group disallows only
administrative and pre-production path prefixes — `/admin`, `/awake`, `/uat`,
`/cwuat`, `/ciuat`, `/ci`, `/uatinternal`, `/testint`, `/staging`, in upper and
lower case and in locale-prefixed form.

- `/en-us/listing/` and `/en-us/job/...` are **permitted**.
- **No disallowed path is ever requested.** The collector constructs URLs only
  from the configured listing path, the source's own `more-link` href, and
  `/{locale}/job/{id}` — none of which can reach a disallowed prefix, and the
  host allowlist stops anything else regardless.
- There is no `Crawl-delay`, so the pacing below is self-imposed courtesy.
- There is no `Sitemap`, so full pagination is the only enumeration method.

If the source later disallows the listing or job paths, collection must stop.
That is a policy decision for a human, not something to work around.

## 3. Request budget and pacing

Every live request the application makes — listing pages, detail pages,
resources, probes — passes through **one** `RequestBudget`
(`src/rowanjobs/net/budget.py`). That single chokepoint is what makes the pacing
guarantee real rather than per-module wishful thinking, and it is why no code may
add an HTTP call that bypasses `SourceClient`.

| Control | Default | Effect |
|---|---|---|
| `concurrency` | 1 | One request at a time; the budget serialises with a lock |
| `min_interval_seconds` | 2.5 s | Minimum gap between requests |
| `jitter_seconds` | 0.75 s | Uniform random addition, so requests are not metronomic |
| `max_requests_per_run` | 1200 | Hard per-run ceiling; exceeding it fails the fetch as `budget_exhausted` |
| `Retry-After` | honoured | `honour_retry_after` pushes the next permitted time out |
| challenge slowdown | ×1.6 per challenge, capped at 30 s | The run gets progressively gentler, never faster |
| `max_consecutive_challenges` | 4 | `ChallengeWall` — the run **stops requesting** |
| `max_retries` | 3 | Retries only for transient failures; a 404 is never retried |

The defaults were chosen against a measurement, not a guess: the audit tripped
the source's WAF after roughly seven requests in about ninety seconds.

The collector also identifies itself honestly. The default user agent is
`RowanJobsArchiver/1.0 (+https://github.com/sedlock/RowanJobs)`: it names the
software and links to the public repository, which is the stable way for a site
operator to find out who we are and how to reach us. It deliberately carries **no
personal email address** — it is sent to a third party on every request and
written into every archived `fetches` row.

## 4. Access-control challenges: respect and back off

`detect_access_control` (`src/rowanjobs/net/client.py`) classifies a response as
an access-control signal when it carries `x-amzn-waf-action` /
`x-amz-waf-action`, or is HTTP 429, or is a Cloudflare-style 503 challenge, or a
403 CAPTCHA page. The Rowan site answers a challenge with **HTTP 202,
`x-amzn-waf-action: challenge`, and an empty body**.

The response to being challenged is to **slow down and, if it persists, stop**:
widen the interval (×1.6 per challenge, capped at 30 s), wait
`challenge_backoff_seconds × 2^(n−1)` — 60 s → 120 s → 240 s at the default — and
raise `ChallengeWall` after four in a row. The observation is recorded as
`access_control_challenge` — *collection uncertainty, never evidence of absence*
— and the affected listing scan fails qualification, so nothing false is
concluded from it.

### The browser step is not evasion

`src/rowanjobs/net/browser.py` optionally opens the **public page** in a real,
unmodified headless Chromium, lets the ordinary AWS WAF challenge script run,
and takes the `aws-waf*` cookie it issues — exactly what happens when a member of
the public visits the site in a browser. There is no CAPTCHA and no human
interaction involved in that flow.

Explicitly, it does **not**:

- spoof or randomise browser fingerprints — it uses the **same honest user agent
  as the HTTP client** (`Collector._make_client` passes `net.user_agent` to the
  solver), so the browser step is a transport detail, not a different claim about
  who we are;
- rotate proxies or IP addresses — there is no proxy configuration at all;
- install stealth patches or anti-detection plugins — plain Playwright Chromium;
- solve CAPTCHAs, by service or otherwise;
- bypass the pacing or the destination policy — the browser page load goes
  through `UrlPolicy.check` and `budget.acquire()` like any other live request
  (`SourceClient._solve`). A page load is live traffic that reaches the source
  and every subresource the page references, so charging it to the run's budget
  is what keeps the one-budget guarantee true of the whole application rather
  than only of the HTTP path.

It is used twice: once **before** the first request (`SourceClient.prime`), so
that the run does not spend source requests being refused, and again if a
challenge nevertheless occurs mid-run — with `force=True`, because the token
being held has just been rejected, and only **after** the client has already
recorded the challenge and backed off. Priming up front is the gentler of the two
behaviours: measured against the live source, a token-less client is challenged
from about the seventh request, while a client holding a browser-issued token
served 12 requests at 2.5-second intervals without being challenged at all.

It is bounded: `max_solves_per_run` (default 6), and the token is reused for
`token_ttl_seconds` (default 30 minutes) unless it has just been rejected.

**Collection remains correct with it disabled.** With `browser.enabled = false`,
or with Playwright simply not installed, priming is a no-op that records
`access_priming_unavailable` in the run's errors, and a challenge is recorded as
`access_control_challenge`: an explicit coverage exception that never becomes
evidence of a missing advertisement. A priming attempt where the page loaded
normally and the source issued no token is *not* recorded as an error — the
result is `{"primed": false, "required": false}`, because no token exists until
the source decides to challenge. The browser step improves coverage; it is
not load-bearing for correctness.

Every challenged attempt is archived as its own `fetches` row even when a later
attempt succeeded (`FetchResult.superseded_attempts`), so the fact that the
source pushed back can never be invisible.

## 5. The SSRF guard

Fetched pages are attacker-influenced input. A link or a redirect inside one must
not be able to make the collector reach into the local network. **Every** URL —
initial request and every redirect target — goes through
`UrlPolicy.check` (`src/rowanjobs/net/guard.py`) *before* a socket is opened.
Redirects are followed manually for precisely this reason.

It refuses:

| Refused | Reason recorded |
|---|---|
| Any scheme other than `http`/`https` | `scheme '…' not permitted` — blocks `file:`, `gopher:`, `ftp:`, `data:` |
| A missing host | `no host` |
| `169.254.169.254`, `metadata.google.internal`, `metadata.goog`, `100.100.100.200`, `fd00:ec2::254` | `metadata service address` — cloud instance metadata, blocked by name as well as by address class |
| Any host not in `network.allowed_hosts` | `host is not in the collection allowlist`. Exact match, plus subdomains of an allowlisted host when `network.allow_subdomains` is true — the default, and what makes a job document on `engineering.rowan.edu` reachable while everything outside the listed domains stays refused (`UrlPolicy.host_allowed`) |
| `user:pass@host` forms | `embedded credentials are not permitted` |
| A literal IP that is private, loopback, link-local, multicast, reserved or unspecified | `literal address is not a public address` |
| A hostname that **resolves** to any such address | `resolves to non-public address <addr>` — DNS is resolved and every returned address is checked, so DNS rebinding into RFC1918 is caught |
| A host that fails to resolve | `DNS resolution failed (…)` |

A refusal is recorded as `failure_kind='blocked_destination'` on the `fetches`
row and, for a resource, as outcome `blocked_destination`. It is evidence, not a
silent skip.

## 6. Fetched pages and downloaded files are untrusted data

- **Archive first, parse second.** Bytes are stored and hashed before any parser
  sees them; the client never decodes or interprets them
  (`src/rowanjobs/net/client.py`).
- **Response size is capped.** `max_response_bytes` (25 MiB) and
  `max_resource_bytes` (50 MiB). Exceeding the cap stores an explicit `partial`
  prefix with a recorded coverage exception — never a silent truncation called
  complete.
- **Downloaded documents are never executed, opened or rendered.** A resource is
  stored as bytes; text extraction is attempted only for textual media types, and
  its failure is recorded rather than raised (`details.py::_resource_text`).
- **Only `job_document` links on Rowan's own domain are fetched** (`rowan.edu` or
  any `*.rowan.edu` host), and only when the extraction that read the
  advertisement classified them that way. Everything else — third-party
  documents, external sites, internal navigation, mailto, in-page anchors, apply
  workflows — is classified and excluded with a stored reason, so the archive can
  later answer what was *not* followed. Those decisions are stored per
  extraction (`resource_links`, unique on `(extraction_id, url_raw, position)`),
  so the stored decision and what was actually retrieved cannot drift apart when
  the scope is widened.
- **Parsing is defensive.** Both adapters catch parse failure and return a
  `failed` extraction with the detail recorded, rather than propagating.
- **Decoding failures are reported, not hidden**: `decode_strategy='replace'`
  plus a replacement-character count on the extraction row.
- **Redirect loops are bounded** by `max_redirects` (5), with every hop archived.

## 7. Credential-bearing headers are redacted

`SENSITIVE_HEADERS` in `src/rowanjobs/net/client.py`:

```
authorization, proxy-authorization, cookie, set-cookie,
x-api-key, x-amz-security-token
```

`sanitize_headers` replaces the value of any of these with `<redacted>` and keeps
every other header verbatim. It is applied to **both** the request headers and
the response headers before they reach `fetches.request_headers_json` /
`response_headers_json` — so the WAF token the browser step obtains, and any
`Set-Cookie` the site sends, never land in the archive.

Because the collector emits its output as JSON on stdout and the service units
send stdout to journald, the same redaction covers the logs: there is no separate
log path that sees unredacted headers.

RowanJobs holds one credential and only one: the SMTP App Password used to send
run reports. `[backup] offhost_*` runs **operator-supplied argv without a
shell**; any credentials those tools need belong to their own configuration, not
to RowanJobs, which will not borrow another application's credentials.

### The SMTP credential

Stored in `[notify] credentials_path` (default
`~/.config/rowanjobs/credentials.env`), never in `config.toml`, never in Git.
`src/rowanjobs/ops/credentials.py` enforces, **before reading any secret
material**:

* the file exists and is a regular file;
* it is mode `0600` — any other mode is refused by name and mode;
* its directory is not group- or world-accessible.

The value is never printed, logged, or included in an exception message.
`SmtpCredentials.__repr__` and `__str__` both render the password as
`<redacted>`, so it cannot reach a traceback or a log line through ordinary
formatting. The one place SMTP would otherwise echo it back —
`smtplib.SMTPAuthenticationError`, whose text can quote the attempted
credential — is caught and reduced to the response code alone
(`src/rowanjobs/ops/mail.py`). A test asserts the secret appears in neither the
error nor the repr.

Delivery is STARTTLS on the submission port with certificate verification
(`ssl.create_default_context()`); no path disables verification. The credential
is only ever held in memory inside the sending process, and reporting commands
open the archive read-only and never load it at all.

`rowanjobs doctor` **fails** on a credential file that is missing or insecurely
stored once reporting is configured; an unconfigured destination is only a note,
because choosing not to send is not a fault.

## 8. File permissions

`Layout.ensure()` (`src/rowanjobs/paths.py`) and the write paths apply:

| Path | Mode | Set by |
|---|---|---|
| `<data_root>` | `0700` | `Layout.ensure` |
| `<data_root>/{backups,logs,runtime,source-audit,exports}` | `0700` | `Layout.ensure` |
| `<data_root>/rowanjobs.db` | `0600` | `open_db` on creation |
| Backup snapshots | `0600` | `BackupManager.create`, `restore_to` |
| `health.json`, manifests, `restore-verification.json`, `deployment.json` | `0600` | `ops/atomic.py::write_bytes` (default mode) |
| Export files, and the diagnostic bundle | `0600` | `export.py::export_to_path` (they carry full advertisement text, so the directory mode alone is not relied on); the bundle goes through `ops/atomic.py` |
| `runtime/collector.lock` | `0600` | `CollectorLock.acquire` |
| `~/.config/rowanjobs/credentials.env` | `0600` **required** | The operator. `load_credentials` refuses any other mode rather than reading it |

`rowanjobs doctor` checks the data root is `0o700` and fails if it is not.

The systemd units add process-level hardening: `NoNewPrivileges`, `PrivateTmp`,
`ProtectSystem=strict`, `ProtectHome=read-only` with `ReadWritePaths` limited to
the data root, `ProtectKernelTunables`, `ProtectKernelModules`,
`ProtectControlGroups`, `RestrictNamespaces`, `RestrictRealtime`,
`RestrictSUIDSGID`, `LockPersonality`, `SystemCallArchitectures=native`, and
`RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`. RowanJobs opens no listening
port and needs no elevated capability.

## 9. CSV export: formula injection is neutralised

A job description is untrusted text from a web page, and `=cmd|'/c calc'!A1` in a
spreadsheet cell is a real attack on whoever opens the export.

`src/rowanjobs/export.py::spreadsheet_safe` prefixes an apostrophe to any string
value beginning with `=`, `+`, `-`, `@`, a tab or a carriage return, and it is
applied to **every** cell of a CSV export. The export's provenance header states
that this was done.

The **JSON export keeps the untouched source value**, because JSON is not
evaluated by its consumer and the apostrophe would be a corruption of the
archived text. If you build a new export format, decide deliberately which of the
two it is.

## 10. What is out of scope

Stated so nobody assumes otherwise:

- RowanJobs performs no authentication, authorisation or rate limiting of its
  own — it has no users and no network listener.
- It does not attempt to detect or resist tampering with the archive by someone
  who already has write access to the data root. `verify` detects *corruption*
  (integrity, foreign keys, payload hashes), not a deliberate, consistent
  rewrite.
- It does not encrypt the archive at rest. The data is public advertisements; the
  protection is filesystem permissions.
- It makes no claim about the source's own security.
