# WideScrape — Independent Senior Code & Architecture Audit

- **Target Repository:** `<repo-root>/widescrape` (`https://github.com/bejad21/widescrape`)
- **Runtime State Audited:** `%USERPROFILE%\.widescrape\` (`widescrape_cache.db`, `modes.json`, `exports/`, `logs/widescrape.log`)
- **Audit Scope:** Full read-through of all 26 Python files (core package, 8 source adapters, tests, benchmarks), packaging/infra (`pyproject.toml`, `docker-compose.yml`, `searxng/settings.yml`, `.gitignore`), documentation (`README.md`, `docs/advanced.md`), and Agent Skill files (`SKILL.md`, `references/recipes.md`, `references/output-guide.md`).
- **Verification Executed:**
  - `uv run --no-sync --with pytest pytest -v` -> **23/23 passed in 10.62s** (Windows, Python 3.13.14).
  - Read-only inspection of live `%USERPROFILE%\.widescrape\widescrape_cache.db` (1,066 items), `modes.json`, `logs/widescrape.log` (0 bytes), and recent `.md` exports (`3ce2440ee3`, `3ed0ffc185`, `dd417e3adb`).
  - Targeted in-memory Python reproductions for security, SQLite, HTML sanitization, extractors, ranking, mode persistence, and MCP error handling.

---

## 1. Executive Summary & Overall Engineering Assessment

WideScrape has a genuinely strong core product concept: zero-API-key multi-source discovery, progressive disclosure (`search`/`cache_find` -> `read --focus`), reusable `modes.json` recipes, `/llms.txt` fast-path crawling, and multi-format exports (`.md`, `.xlsx`, `.html`). The codebase is clean, small enough to reason about quickly, and pleasant to navigate.

However, **beneath the passing 23-test suite (`pytest` covers happy-path unit behavior with mocked adapters), the production pipeline has severe reliability, data-integrity, and security flaws**:

1. **Silent Data Loss & Windows Resource Leaks:** `ScrapeStorage.save_run()` uses `INSERT OR REPLACE INTO items` keyed solely on `item_id` (derived from URL), overwriting `run_id` whenever a URL appears again. In the live database on this machine, export `3ce2440ee3` originally saved **22 items**, yet `SELECT COUNT(*) FROM items WHERE run_id='3ce2440ee3'` now returns only **4 items**—18 rows were silently reassigned to later runs. Additionally, `with sqlite3.connect(...)` never closes the SQLite connection on exit, triggering `PermissionError [WinError 32]` on Windows.
2. **Broken `--days` Freshness Contract & Fabricated Deadlines:** `ScrapeItem.__post_init__` defaults missing `published_at` values to **today's UTC date**. Because `DDGS` (`web_dork`), `BrowserSocialAdapter`, and `crawl_site` omit publication dates (and `DDGS` explicitly retries *without* `timelimit` when `site:` dorks return 0 hits), stale 2020–2024 articles are stamped with today's date, pass local `--days` filtering, and even appear in the `deadline` custom column (`2026-11-21`).
3. **High-Error-Rate Regex Extractors & Field Misrouting:** Real exports (`3ce2440ee3`, `3ed0ffc185`, `dd417e3adb`) contain fabricated matches such as `bank = DIB` extracted from the word `"Incredible"`, `discount = $81.34` from `"$81.34bn offering"`, and `code = "November 2026 - RetailMeNot..."` from a page title. Custom field matching routes `"company"` to salary (`"comp"`) and `"office"` to discounts (`"off"`) before checking `Key: Value`.
4. **Security Gaps Across Boundaries:** There is zero SSRF protection against loopback/private/cloud-metadata IPs across `fetcher.py`, `crawl_site`, `map_site`, and Playwright MCP tools; `exporter.py` writes unescaped Excel formulas (`=HYPERLINK(...)` -> `data_type='f'`) and crashes with `IllegalCharacterError` on PDF control chars; `installer.py` silently overwrites corrupt/JSONC `~/.claude.json` or `mcp_config.json` files with `{}`; and `<untrusted_web_data>` delimiters can be escaped by scraped text.
5. **Hidden Request Storms & Blind Error Swallowing:** A default `jobs` search with `limit=100` launches **252–504 unthrottled `DDGS` requests**, and 7 of 8 adapters swallow HTTP 403/429/5xx failures returning `[]`, which explains why `%USERPROFILE%\.widescrape\logs\widescrape.log` is **0 bytes** after dozens of live runs.

**Bottom line:** Solid architectural skeleton and great agent ergonomics, **not yet production-safe** for untrusted web inputs, repeatable `--days` accuracy, historical run auditing, or automated MCP deployment until the Critical items below are resolved.

---

## 2. Findings Summary Table

| ID | Severity | Component | Location | Summary |
| :--- | :--- | :--- | :--- | :--- |
| **C-01** | **Critical** | Fetcher / Crawler / MCP | `fetcher.py:59-69, 219-286`; `web_dork.py:63-135, 162-206, 405-445` | SSRF via unvalidated URLs, `follow_redirects=True`, and Playwright `page.goto` |
| **C-02** | **Critical** | Exporter (`.xlsx`) | `exporter.py:153-174, 209-231` | Excel formula injection (`=`, `+`, `-`, `@`) + `IllegalCharacterError` crash on control chars |
| **C-03** | **Critical** | Storage (SQLite) | `storage.py:35-39, 45-68, 70-193` | `INSERT OR REPLACE` steals rows from historical `run_id`s; unclosed `sqlite3` connections leak handles on Windows |
| **C-04** | **Critical** | Models / Ranker / Adapters | `models.py:31-32`; `filter_rank.py:216-220`; `web_dork.py:288-370`; `browser_social.py:57-309` | Undated items default `published_at = today`, silently bypassing `--days` and fabricating dates |
| **C-05** | **Critical** | Installer / Modes | `installer.py:59-66`; `modes.py:73-76, 102-108` | Unparseable external MCP configs (`~/.claude.json`, etc.) or `modes.json` are silently wiped and overwritten |
| **C-06** | **Critical** | Extractors (`--extract`/`--fields`) | `extractor.py:11-43, 64-121, 150-198` | Systematic false positives (`"incredible"`->`DIB`, `$81.34bn`->discount) & substring misrouting (`company`->salary, `office`->discount) |
| **C-07** | **Critical** | WebDork / Adapters | `web_dork.py:254-388`; `adapters/*.py` | 252–504 unthrottled `DDGS` requests per `jobs` search; 7/8 adapters swallow 403/429/5xx errors silently (0-byte log) |
| **C-08** | **Critical** | Packaging / Installer | `pyproject.toml:7-28`; `installer.py:16-24` | `primp` missing from dependencies; `widescrape install` fails in non-editable / wheel / `uv tool` installs |
| **C-09** | **Critical** | Sanitizer / Prompt Defense | `fetcher.py:97-155`; `cli.py:328`; `mcp_server.py:134` | `<untrusted_web_data>` delimiter breakout; `opacity:0.85` deleted; `<template>` leaked; table cells fused; multi-`<article>` dropped |
| **C-10** | **Critical** | Crawler (`crawl_site`) | `web_dork.py:116-235` | `/llms.txt` consumes `max_pages=1` skipping target URL; spawns up to 6 concurrent Chromium browsers without `try/finally` close |
| **N-01** | **Normal** | GitHub Adapter | `adapters/github.py:13-25, 38, 46-49, 86-93` | Sends invalid `pushed:>=` qualifier to `/search/issues`; exceeds `limit` across dual endpoints; no `GITHUB_TOKEN` support |
| **N-02** | **Normal** | arXiv Adapter | `adapters/arxiv.py:17-62, 70-85` | Broken `all:term1 term2` query syntax; ingests `<title>Error</title>` XML entries; missing `follow_redirects=True` |
| **N-03** | **Normal** | Expander | `expander.py:254` | `core_terms` strips all 2-char tech tokens (`AI`, `Go`, `ML`, `C#`, `JS`, `TS`, `UI`, `UX`) |
| **N-04** | **Normal** | Expander (`mode="auto"`) | `expander.py:88-95` | Bare substring classification routes `"internet outage"` and `"TV remote control"` into `jobs` mode |
| **N-05** | **Normal** | Proxy Plumbing | `orchestrator.py:55-86`; `adapters/*.py`; `web_dork.py:126, 424`; `browser_social.py:157`; `installer.py:99-100` | Proxy ignored by 6/8 adapters; `user:pass` proxy breaks Playwright; `widescrape doctor` prints proxy credentials in plaintext |
| **N-06** | **Normal** | Auth / Cookies | `browser_social.py:17-34, 113-179`; `orchestrator.py:80-86`; `cli.py:347-372` | `auth reddit` is dead code; cookies saved without `0600` perms or platform allowlist; blocking `input()` inside `async def` |
| **N-07** | **Normal** | Browser Social Adapter | `browser_social.py:36-110, 181-327` | Untracked `create_task` response handlers leak past `context.close()`; ignores `plan.days`; no login-redirect detection; drops `created_at`/author/eng |
| **N-08** | **Normal** | Tokenizer / FTS5 | `filter_rank.py:35-39`; `storage.py:15-24` | ASCII-only regex drops Arabic/CJK/accented keywords to `[]` in BM25, highlights, and FTS5 |
| **N-09** | **Normal** | HTML Viewer | `exporter.py:271-272, 301, 335, 380-396` | Hides `discounts`/`bank`/`extracted` when `codes` exist; unawaited clipboard copy on `file://`; missing `\u2028`/`\u2029`/`<` JS escaping; dead sort cursor on `<th>` |
| **N-10** | **Normal** | Filter, Rank & Dedup | `filter_rank.py:21-32, 46-60, 209-244`; `fetcher.py:35-38, 72-80` | Order-dependent dedup drops rich GitHub items behind thin snippets; `www.`/param-order/WordPress `?s=` canonicalization flaws; BM25 collapses to `0` for `N<=2`; docs killed by login-wall regex |
| **N-11** | **Normal** | HTTP Fetcher | `fetcher.py:167, 219-286` | Redundant `httpx` retry after 404/challenge/oversize `primp` failure; buffers entire payload before size guard; creates `primp.Client` per URL; silent 15-page PDF cap |
| **N-12** | **Normal** | Mode & `--sites` Semantics | `orchestrator.py:109`; `expander.py:107-143`; `modes.py:33-37, 117-182`; `mcp_server.py:80` | `--sites` overrides sources to `["web"]` in `run_scrape` (contradicting docs); `--days 0` cannot override `default_days`; `upsert_mode` silently resets `base_mode`; `clean_site_domain` leaves queries/spaces |
| **N-13** | **Normal** | MCP Server | `mcp_server.py:36-290`; `orchestrator.py:148-176` | Unhandled `ValueError`/`KeyError` crashes in `widescrape_modes`/`widescrape_search`; `widescrape_read` ignores `max_chars` without `focus`; `widescrape_crawl` unbounded; sync DB/XLSX blocks event loop |
| **N-14** | **Normal** | Reddit Adapter | `adapters/reddit.py:19-23, 48-54, 117-124, 165, 249` | Hardcodes `"dubai"`/`"UAE"` globally; fails to map custom modes to `base_mode`; `< 5` cutoff counts `[removed]` posts; fails on ISO `created_utc` |
| **N-15** | **Normal** | HN / SE / News Adapters | `hackernews.py:19, 39, 62`; `stackexchange.py:28-68`; `news.py:52-60, 87` | HN uses `search_by_date` with unstripped HTML & no `limit`; SE omits question bodies & ignores `backoff`; News hardcodes `gl=US` & keeps duplicate title/opaque URLs |
| **N-16** | **Normal** | Mapper & Crawler Scope | `web_dork.py:31, 39-60, 95-101, 224-233`; `orchestrator.py:205` | `map_site` skips nested `<sitemapindex>` child sitemaps; `crawl_site` drifts outside doc path prefix; `run_crawl` ranks against raw URL when `query=""` |
| **N-17** | **Normal** | SearXNG Docker Config | `docker-compose.yml:6`; `searxng/settings.yml:4-5` | Binds unthrottled JSON search proxy to `0.0.0.0:8080` with static `"change-me"` secret |
| **N-18** | **Normal** | Test Suite Coverage | `tests/test_pipeline.py:54-56, 504-508` | Time-bomb date `20 Nov 2026` breaks on 2026-11-28; 0 tests for live adapter HTTP paths, `crawl_site`, `map_site`, 5/6 MCP tools, or security invariants |
| **L-01** | **Low** | Compression Metric | `filter_rank.py:186, 276-280`; `orchestrator.py:147-160`; `eval_compression.py` | Reports `0%` saved on real runs vs `80%` synthetic benchmark; stale after `--new-only` & extractors |
| **L-02** | **Low** | Highlight Selection | `filter_rank.py:149-181` | Early-space long URL yields `'A...'`; `max(20, ...)` exceeds small `max_chars`; missing `USD`/`GBP` & case-insensitive check |
| **L-03** | **Low** | Markdown Escaping | `models.py:42, 62`; `exporter.py:96-114`; `cli.py:43-50` | Unescaped `[`/`]` in titles & trailing `\xa0`/`)` in URLs break Markdown links and tables |
| **L-04** | **Low** | CLI UX & Windows Stdout | `cli.py:124-126, 166-171, 216-221, 383-394, 411-450` | `show` ignores SQLite pagination and prints invalid ranges when `offset >= total`; `--quiet` hides `adapter_errors`; `cp1252` pipe risk |
| **L-05** | **Low** | Side-Effect Logging | `logging_setup.py:24-51`; `cli.py:8`; `mcp_server.py:8` | Import-time handler stripping interferes with pytest/library callers; unbounded log file growth |
| **L-06** | **Low** | Dependencies & Types | `cli.py:14, 38`; `stackexchange.py:5`; `pyproject.toml:13, 16`; `expander.py:33-48` | Unused `rich.Console` and `asyncio` imports; mandatory `playwright`; mutable list defaults on frozen `SearchPlan` |
| **L-07** | **Low** | Docs & Versioning | `docs/advanced.md:42`; `pyproject.toml:3`; `__init__.py:3`; `SKILL.md:15` | Hardcoded local `C:/Users/bigoa/...` path in `advanced.md`; version mismatch (`0.1.0` vs `0.2.0`) |
| **L-08** | **Low** | Mode Introspection Gaps | `modes.py:95, 241-266`; `cli.py:222-230`; `mcp_server.py:221-280` | `default_fields` hidden from `mode list`/`show`; keywords unreachable via MCP; `bool` passes `isinstance(..., int)` |
| **L-09** | **Low** | Minor Scoring / ID Quirks | `filter_rank.py:236`; `models.py:28-30` | `core_terms` double-counted in BM25 query vector; `item_id` hashes raw un-canonicalized URL (32-bit prefix) |
| **L-10** | **Low** | Empty Run Artifacts | `orchestrator.py:162-176`; `storage.py:180-193` | 0-item runs export blank `.md`/`.xlsx`/`.html` files with a `run_id` absent from `widescrape runs` / `show` |

---

## 3. Critical Findings (Detailed Technical Breakdown)

### C-01: SSRF & Unrestricted Redirects Across Fetcher, Crawler, and MCP Tools
- **Location:**
  - `src/widescrape/fetcher.py:59-69` (`normalize_http_url`), `219-240` (`_sync_primp_get`), `243-286` (`stealth_fetch_document`)
  - `src/widescrape/adapters/web_dork.py:63-87` (`map_site`), `116-135` (`_fetch_html_playwright`), `162-206` (`crawl_site`), `393-445` (`_enrich_bodies`, `_enrich_via_playwright`)
  - `src/widescrape/mcp_server.py:161-214` (`widescrape_crawl`, `widescrape_map`)
- **What Is Wrong & Why It Matters:**
  `normalize_http_url()` only checks that `scheme in ("http", "https")` and `parsed.netloc` is non-empty. Nothing prevents targeting `127.0.0.1`, `localhost`, RFC1918 subnets (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), link-local/cloud metadata (`169.254.169.254`), or IPv6 loopback/ULA (`::1`, `fc00::/7`). Even worse, `primp.Client(..., follow_redirects=True)`, `httpx.AsyncClient(..., follow_redirects=True)`, and Playwright `page.goto()` automatically follow `301/302/307` redirects to internal services. Because `widescrape_crawl`, `widescrape_map`, and `widescrape_search(..., fetch_pages=True)` are exposed via MCP to an AI agent processing untrusted search snippets or pages containing links, a malicious webpage or prompt-injection link can force WideScrape to probe local ports (e.g., SearXNG, Docker API, cloud metadata, dev servers) and exfiltrate their JSON/HTML responses back into the agent's context.
- **How to Trigger / Verify:**
  Run `widescrape crawl "http://127.0.0.1:8080/search?q=test&format=json"` or `widescrape map "http://169.254.169.254/latest/meta-data/"` or serve an external `302 Location: http://127.0.0.1:8080/` response and run `fetch_pages=True`. Both `primp` and `httpx` fetch the internal payload verbatim.
- **Concrete Fix:**
  1. Add a centralized `validate_public_url(url: str) -> str` helper in `fetcher.py` that resolves hostnames (`socket.getaddrinfo`) and rejects any resolved IP where `ip.is_private`, `ip.is_loopback`, `ip.is_link_local`, `ip.is_multicast`, `ip.is_reserved`, or `ip.is_unspecified` (unless an explicit opt-in flag/env such as `WIDESCRAPE_ALLOW_PRIVATE_URLS=1` is set for local testing).
  2. Disable blind automatic redirects (`follow_redirects=False`) in both `primp` and `httpx`, manually stepping up to `max_redirects=5` while calling `validate_public_url()` on every `Location` target (and ideally pinning the validated IP or re-checking via an `httpx` event hook to eliminate DNS rebinding).
  3. In Playwright helpers (`_fetch_html_playwright`, `_enrich_via_playwright`), install a `page.route("**/*", ...)` handler that validates every navigation/subresource URL with `validate_public_url()` before calling `route.continue_()`.

---

### C-02: Excel Formula Injection & Hard Crash on Control Characters (`IllegalCharacterError`)
- **Location:**
  - `src/widescrape/exporter.py:153-174` (`_write_xlsx_workbook` data rows)
  - `src/widescrape/exporter.py:209-231` (`Run Info` metadata sheet)
- **What Is Wrong & Why It Matters:**
  1. **Formula Injection (CSV/Excel Injection):** Untrusted web strings (`it.title`, `gist`, `it.author`, extracted `dyn_vals`, `format_extracted_text(it)`, and even `query`/`adapter_errors` on `Run Info`) are passed straight into `ws.append(...)`. OpenPyXL automatically sets `data_type = 'f'` for any string starting with `=`, enabling malicious web pages or titles starting with `=`, `+`, `-`, `@`, `\t`, or `\r` to execute Excel formulas (`=HYPERLINK(...)`, `=WEBSERVICE(...)`, DDE commands) when opened by the user.
  2. **Hard Crash on Control Characters:** PDFs extracted by `pypdf` (`\x0c` form-feed, `\x00` null bytes) and malformed HTML routinely contain ASCII control characters (`\x00-\x08`, `\x0b`, `\x0c`, `\x0e-\x1f`). OpenPyXL rejects these characters on `ws.append()` / `cell.value = ...` with `openpyxl.utils.exceptions.IllegalCharacterError`, crashing the entire `run_scrape` / `run_crawl` run *after* all scraping has finished and *after* SQLite persistence.
- **Verified Reproduction:**
  Executed directly in Python against `export_run_files`:
  - Input title `'=HYPERLINK("http://evil.test","Click")'` -> `wb["Results"]["H2"].data_type == 'f'`, `value == '=HYPERLINK("http://evil.test","Click")'`.
  - Input body `'Page 1\x0cPage 2\x00End'` -> crashed with `IllegalCharacterError: Page 1 Page 2 End cannot be used in worksheets.`
- **Concrete Fix:**
  Add `_sanitize_excel_cell(value: object) -> object` in `exporter.py`:
  ```python
  from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

  _FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

  def _sanitize_excel_text(val: str) -> str:
      cleaned = ILLEGAL_CHARACTERS_RE.sub("", val or "")
      if cleaned and cleaned[0] in _FORMULA_PREFIXES:
          return f"'{cleaned}"
      return cleaned
  ```
  Apply `_sanitize_excel_text` to every string written to both `"Results"` and `"Run Info"` (also keeping `url_cell.hyperlink` only when `it.url.startswith(("http://", "https://"))` and stripped of whitespace/control chars).

---

### C-03: SQLite Historical Run Data Loss (`INSERT OR REPLACE`) & Unclosed Connection Leak on Windows
- **Location:**
  - `src/widescrape/storage.py:35-39` (`_connect`)
  - `src/widescrape/storage.py:45-68` (`_init_db`)
  - `src/widescrape/storage.py:70-102` (`save_run`)
  - `src/widescrape/storage.py:104-193` (`get_seen_urls`, `get_items_by_ids`, `search_cache`, `get_run_items`, `list_runs`)
- **What Is Wrong & Why It Matters:**
  1. **Silent Historical Run Corruption:** The `items` table defines `item_id TEXT PRIMARY KEY` alongside `run_id TEXT NOT NULL`, and `save_run` executes `INSERT OR REPLACE INTO items (...)`. Whenever a second search encounters any previously seen URL (`item_id` is `sha1(url)[:8]`), SQLite **deletes the previous row and overwrites `run_id` with the new run's ID**. Historical `widescrape show <old_run_id>` outputs silently shrink or disappear completely, and `widescrape runs` reports false item counts.
  2. **Windows File Handle Leak:** In Python's `sqlite3`, `with sqlite3.connect(...) as conn:` commits or rolls back the transaction on exit—**it does NOT close `conn`**. Every `ScrapeStorage` method leaks an open SQLite handle until garbage collection. On Windows, this keeps locks on `.db`/`.db-wal`/`.db-shm` and prevents deletion/cleanup (`PermissionError [WinError 32]`), and in long-lived MCP processes it accumulates file descriptors.
- **Verified Reproduction & Live Production Evidence:**
  - **Live DB check (`%USERPROFILE%\.widescrape\widescrape_cache.db`):** The exported file `3ce2440ee3_amazon_ae_white_friday_promo_codes.md` recorded **22 kept results**, while `SELECT COUNT(*) FROM items WHERE run_id='3ce2440ee3'` now returns only **4 rows** (18 items stolen by subsequent runs). Similarly, `dd417e3adb` exported **15 rows** to `.md` and now retains only **11 rows** in SQLite.
  - **In-memory test:** Saving `item_a` under `"run_1"`, then saving `item_a` under `"run_2"` reduced `len(store.get_run_items("run_1"))` from `1` to `0`, and `TemporaryDirectory` cleanup on Windows crashed with `PermissionError: [WinError 32]`.
- **Concrete Fix:**
  1. Normalize the schema into three tables (with safe schema migration for existing DBs):
     - `runs(run_id TEXT PRIMARY KEY, query TEXT, mode TEXT, stats_json TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)`
     - `items(item_id TEXT PRIMARY KEY, canonical_url TEXT UNIQUE, source TEXT, url TEXT, title TEXT, body TEXT, author TEXT, published_at TEXT, engagement INTEGER, metadata_json TEXT, updated_at TIMESTAMP)`
     - `run_items(run_id TEXT, item_id TEXT, rank INTEGER, score REAL, query_used TEXT, metadata_json TEXT, PRIMARY KEY (run_id, item_id))`
     Keep FTS5 (`items_fts`) synced via `AFTER INSERT` / `AFTER UPDATE` / `AFTER DELETE` SQLite triggers (using external-content FTS5 `content='items'`).
  2. Wrap `_connect()` usage with `contextlib.closing(self._connect()) as conn:` so connections close deterministically on every query.

---

### C-04: Silent Fabrication of Publication Dates Defeats `--days` Local Filtering
- **Location:**
  - `src/widescrape/models.py:31-32` (`ScrapeItem.__post_init__`)
  - `src/widescrape/filter_rank.py:216-220` (`filter_and_rank` date check)
  - `src/widescrape/adapters/web_dork.py:288-295` (SearXNG), `347-370` (DDGS retry without `timelimit`)
  - `src/widescrape/adapters/browser_social.py:57-66, 95-102, 231-239, 301-309`
- **What Is Wrong & Why It Matters:**
  `ScrapeItem.__post_init__` executes:
  ```python
  if not self.published_at:
      self.published_at = datetime.now(timezone.utc).strftime("%Y-%m-%d")
  ```
  Meanwhile:
  - `WebDorkAdapter` (`DDGS` and SearXNG) never extracts a date (`published_at=""`). Even worse, line 353 retries `ddgs.text(...)` **without `timelimit`** whenever a `site:` query with `timelimit` returns 0 hits!
  - `BrowserSocialAdapter` never extracts `created_at` from X GraphQL (`legacy.created_at`) or DOM `<time>`, nor timestamps from LinkedIn Voyager/DOM.
  - `crawl_site` never parses HTML `<meta property="article:published_time">` or JSON-LD `datePublished`.
  Consequently, **every undated item from 2018 or 2022 is silently stamped with today's date**. When `filter_and_rank` runs `if item_date < cutoff_date`, today's fabricated date always passes, completely defeating `--days` while presenting ancient pages as `Date: 2026-11-21` (and even leaking today's date into `deadline` columns!).
- **Verified Evidence:**
  - **Live export (`3ce2440ee3_amazon_ae_white_friday_promo_codes.md`):** Row 6 (`we_784b914f`) displays `Date: 2026-11-21` even though its snippet begins with `"2 days ago ·"` (Nov 19); Row 7 (`we_4c0c2f75`) displays `Date: 2026-11-21` even though its snippet says `"5 days ago ·"` (Nov 16) and sets `deadline: 2026-11-21`!
  - **In-memory test:** An undated item titled `"Black Friday 2020 deals"` passed `expand_query(..., days=7)` with `dropped_old=0, kept=1, published_at='2026-11-21'`.
- **Concrete Fix:**
  1. **Stop fabricating `published_at` in `ScrapeItem.__post_init__`.** Leave `published_at = ""` when unknown, and display `"unknown"` or `"-"` in reports/CLI when empty.
  2. Parse real dates where available:
     - SearXNG `r.get("publishedDate")` / `r.get("pubdate")`.
     - DDGS snippet leading relative/absolute date prefixes (`"2 days ago ·"`, `"Nov 19, 2026 ·"`, `"2026-11-19 ·"`).
     - X `legacy.created_at` (`"%a %b %d %H:%M:%S %z %Y"`) and DOM `<time datetime="...">`.
     - HTML `<meta property="article:published_time">`, `<meta name="date">`, `<time datetime>`, and JSON-LD `datePublished`.
  3. Under `--days N`, distinguish verified-fresh items from undated items: either drop undated items from `DDGS` fallback retries (or when `--strict-days` is active), or keep them with an explicit warning and freshness penalty instead of mislabeling them as published today.

---

### C-05: Destructive Overwrite of `~/.claude.json`, `mcp_config.json`, `mcp.json`, and `modes.json` on Parse Errors
- **Location:**
  - `src/widescrape/installer.py:59-66`
  - `src/widescrape/modes.py:73-76, 102-108`
- **What Is Wrong & Why It Matters:**
  1. In `installer.py:59-66`, if `~/.claude.json`, `~/.cursor/mcp.json`, or `~/.gemini/antigravity/mcp_config.json` contains trailing commas, JSONC comments, BOM/encoding glitches, or a transient invalid edit, `json.loads(...)` raises `JSONDecodeError` (a `ValueError` subclass). The `except ValueError: data = {}` block **silently replaces the user's entire multi-tool agent config with `{"mcpServers": {"widescrape": ...}}`** and overwrites the file—destroying all other MCP servers, Claude OAuth/project settings, and user preferences! (Even if valid JSON has `"mcpServers": null`, `data.setdefault(...)` returns `None` and crashes on `servers["widescrape"] = ...`).
  2. In `modes.py:73-76`, `load_custom_modes()` catches `(OSError, ValueError)` and returns `{}`. Any subsequent `upsert_mode` / `add_sites_to_mode` / `save_custom_modes` call writes non-atomically via `cfg_path.write_text(...)`, erasing all previously saved modes.
- **How to Trigger / Verify:**
  Place `// user comment\n{"mcpServers": {"other": {}}}` (valid JSONC supported by editors like Cursor) in `~/.cursor/mcp.json` and run `widescrape install --with-mcp`. The existing `"other"` server is permanently deleted.
- **Concrete Fix:**
  1. In `installer.py`: if `cfg_path.is_file()` fails JSON parsing or root is not a `dict`, **abort modifying that file** and emit an explicit warning message instead of resetting `data = {}`. Before writing any external config file, write a timestamped/single backup (`cfg_path.with_suffix(cfg_path.suffix + ".bak")`).
  2. In `modes.py`: raise or back up corrupt `modes.json` instead of silently returning `{}` on write paths, and implement atomic writes via temporary file in `cfg_path.parent` + `os.replace(tmp_path, cfg_path)`.

---

### C-06: Systematic False Positives & Field Misrouting in Built-in and Custom Extractors
- **Location:**
  - `src/widescrape/extractor.py:11-43` (regex definitions)
  - `src/widescrape/extractor.py:64-121` (`_extract_codes_fields`, `_extract_jobs_fields`)
  - `src/widescrape/extractor.py:150-198` (`_extract_single_custom_field`)
- **What Is Wrong & Why It Matters:**
  Six compounding design flaws cause heavy false positives in real runs:
  1. **`_CODE_CONTEXT_RE` (`line 11-14`) uses `re.IGNORECASE` globally:** Even the capture group `([A-Z0-9][A-Z0-9_-]{3,15})` matches **lowercase** English words! `"use promo code available today"` captures `"AVAILABLE"`, `"discount code from store"` captures `"FROM"`. Moreover, `code:` without a proper delimiter captured `"November 2026 - RetailMeNotAmazon UAE Promo Codes"` in live run `3ce2440ee3` (via `Key: Value` or earlier runs) and `"15% off"` noise.
  2. **`_DISCOUNT_RE` (`line 24-27`) makes `off|discount` optional:** `(?:AED|USD|\$|...) \d+(?:off|discount)?` matches *any* currency figure in news headlines. Live run `3ce2440ee3` extracted `discount = "$81.34"` from `"Anthropic opens 34% above IPO price after $81.34bn offering"`.
  3. **`_BANK_CARDS` (`line 86`) and `_SENIORITY_TERMS` (`line 112`) and `_WORK_TYPE_PATTERNS` (`line 108`) do raw `in text_lower` substring checks without word boundaries or negation:**
     - `"incredible"` -> matches `"DIB"` (observed live in `3ce2440ee3` row 31 & `dd417e3adb` row 25!).
     - `"fabulous"` -> matches `"FAB"`.
     - `"cities"` / `"citizen"` -> matches `"Citi"`.
     - `"international"` -> matches `"intern"`.
     - `"leading"` -> matches `"lead"`.
     - `"Not remote, onsite only"` -> matches `"remote"` first in `_WORK_TYPE_PATTERNS`!
  4. **`_extract_single_custom_field` (`lines 155-198`) runs loose substring heuristics BEFORE explicit `Key: Value` matching (`line 173`):**
     - `fname = "company"` contains `"comp"` -> triggers salary extractor (`$150k-$180k`) instead of `"Acme Corp"` from `"Company: Acme Corp"`!
     - `fname = "office"` contains `"off"` -> triggers discount extractor!
     - `fname = "zip_code"` or `"status_code"` contains `"code"` -> triggers promo code extractor!
     - `fname = "tier"` matches `("bank", "card", "tier")`; `"card"` matches `"discard"`; `"pay"` matches `"payment_method"`.
  5. **`deadline`/`expir`/`valid` (`line 182`) falls back to `item.published_at[:10]`:** Every article without a deadline gets a fabricated deadline equal to its publish date (or today's date via C-04!).
  6. **`_SALARY_RE` (`line 34-37`) bugs:** Uses `\d{2,3}(?:,\d{3}|\.\d+)?k?`, which fails to match `$150000` (6 digits without comma), uses `[-–to]+` (character class matching individual `t` and `o` chars too), and accepts unscaled `$50` as a salary.
- **Verified Reproduction:**
  - `_extract_codes_fields("Use promo code available today or discount code from store. Incredible fabulous cities! Costs $81.34bn.")` -> `{'codes': ['AVAILABLE', 'FROM'], 'discounts': ['$81.34'], 'bank_card': ['FAB', 'Citi', 'DIB']}`
  - `_extract_jobs_fields("Not remote, onsite only. Our international leading company offers $50 stipend.")` -> `{'salary': ['$50'], 'work_type': 'remote', 'seniority': ['lead', 'intern']}`
  - `apply_custom_fields([ScrapeItem("web", "https://example.com/1", "Company: Acme Corp", "Salary $150k-$180k, office in London...")], "company,office,zip_code,deadline")` -> `{'company': '$150k-$180k', 'office': '$150, $180', 'deadline': '2026-11-21'}`.
- **Concrete Fix:**
  1. Check explicit `Key: Value` (`kv_match`) **first** before heuristic type routing, and route field names via exact alias sets or `_`-split tokens (`"comp"` in `tokens`, never `"comp" in fname`).
  2. Enforce case-sensitive capture for promo codes using inline flag `(?-i:[A-Z0-9][A-Z0-9_-]{3,15})` (and require at least one uppercase letter + digit, or uppercase + separator, or explicit `use code` context with strict stopwords).
  3. Require `(?:off|discount|save|cashback)` around currency discounts in `_DISCOUNT_RE`.
  4. Compile `\b...\b` regexes for all bank names, seniority levels, and work types; reject negated remote strings (`not remote`, `no remote`, `onsite only`).
  5. Remove `or item.published_at[:10]` fallback from `deadline`/`expir`/`valid`.
  6. Fix `_SALARY_RE` to `(?:[-–]|to)` instead of `[-–to]+`, handle `\d{2,3}(?:,\d{3})+` or `\d{5,6}` or `\d{2,3}k`, and require `k`/`,000`/period or value >= 1000 so `$50` isn't treated as annual salary. Make `_BANK_CARDS` configurable rather than hardcoding UAE banks globally.

---

### C-07: `WebDorkAdapter` Request Amplification Storm (252–504 Requests) & Silent Error Swallowing Across Adapters
- **Location:**
  - `src/widescrape/adapters/web_dork.py:254-263, 297-298, 337-388`
  - `src/widescrape/adapters/reddit.py:142-151, 172-181, 220-236`
  - `src/widescrape/adapters/github.py:50-51, 81-82`
  - `src/widescrape/adapters/hackernews.py:34-35, 53-54`
  - `src/widescrape/adapters/news.py:89-99`
  - `src/widescrape/adapters/arxiv.py:83-84`
  - `src/widescrape/adapters/browser_social.py:177-178, 248-249, 325-326`
- **What Is Wrong & Why It Matters:**
  1. **Request Amplification Storm:** In default `jobs` mode (`limit=100`), `expand_query` generates **21 `dork_queries`**. `_fetch_via_ddgs` multiplies this by `4` backends (`bing, yahoo, google, yandex`) × `3` pages = **252 `DDGS()` queries** in a single search! If `--days` is passed (`timelimit` set), almost all strict `site:` queries return empty on the first pass and immediately trigger the fallback retry (`line 353`), surging up to **504 requests**, each instantiating a brand-new `DDGS()` session! This triggers IP rate-limiting/CAPTCHA bans from search engines, stalls latency, and returns far more than `limit` because `_fetch_via_ddgs` never caps its output (`items[:limit]`). Moreover, `_enrich_bodies(items[:40])` (`line 262`) fetches full HTML/PDFs for the first 40 *unranked, un-deduped* snippets before `filter_and_rank` runs.
  2. **Silent Error Swallowing:** `orchestrator.py:137-143` was updated to surface `adapter_errors`, **but 7 of 8 adapters catch all HTTP non-200 (403, 429, 503) and network/JSON exceptions internally and return `[]`**! Only `StackExchangeAdapter` raises `RuntimeError` on HTTP error, and `ArxivAdapter` only lets `HTTPError` bubble (while still swallowing HTTP 429/500). In production, `logs/widescrape.log` remained **0 bytes** despite repeated scraping runs because adapters fail invisibly as `0 items` rather than warning the user/agent.
- **Verified Evidence:**
  - Python verification: `len(expand_query("remote AI engineer", mode="jobs").dork_queries) == 21` -> `21 * 4 * 3 = 252` requests (up to `504` with `days`).
  - Disk inspection: `~/.widescrape/logs/widescrape.log` is `0 bytes` even though `widescrape_cache.db` holds 1,066 items across ~20+ runs.
- **Concrete Fix:**
  1. Cap dork fan-out with a query/request budget (e.g., max 2 backends by default, page 1 first, fetch page 2–3 only if unique hits < target `limit`, stop early once `limit` reached, reuse `DDGS` client safely, add jitter/backoff on 429).
  2. Move `fetch_pages` / Playwright enrichment **after** deduplication, noise filtering, and initial snippet BM25 ranking so WideScrape only fetches full pages for the top N *unique, relevant* hits (not duplicates or spam).
  3. Standardize adapter diagnostics: return/attach structured warnings when partial backends fail (HTTP 403/429/5xx, expired cookies, network timeouts) and raise when *all* endpoints in an adapter fail, so `adapter_errors` and `widescrape.log` accurately report upstream blocks.

---

### C-08: Missing `primp` Dependency in `pyproject.toml` & Broken Non-Editable `widescrape install` Packaging
- **Location:**
  - `pyproject.toml:7-18, 27-28`
  - `src/widescrape/installer.py:16-24, 46-49`
- **What Is Wrong & Why It Matters:**
  1. `primp` is imported directly in `fetcher.py:221`, `adapters/reddit.py:215`, and `installer.py:81` (and `RUST_LOG=off` in `logging_setup.py:31` explicitly targets it), yet `primp` is **absent from `pyproject.toml` `dependencies`**. It currently only exists in `.venv` as a transitive dependency of `ddgs`. Any `ddgs` update that drops or swaps `primp` immediately breaks TLS-stealth fetching and `widescrape doctor`.
  2. `installer.py` locates `SKILL.md` and `references/` via `Path(__file__).resolve().parents[2]`. That only works in editable/source-tree checkouts (`uv run --project ...`). If someone installs WideScrape via `pip install .`, `pipx install`, or `uv tool install`, `__file__` sits inside `site-packages/widescrape/installer.py`, `parents[2]` resolves to `site-packages/`'s parent (`Lib/`), and `widescrape install` crashes with `FileNotFoundError: Missing SKILL.md`. Similarly, `mcp_entry` (`lines 46-49`) hardcodes `uv run --no-sync --project <PROJECT_ROOT>`, which fails if `uv` or the source checkout isn't used.
- **Concrete Fix:**
  1. Add `"primp>=0.14.0"` (matching the installed/compatible version in `uv.lock`) directly to `[project].dependencies` in `pyproject.toml`.
  2. Move or include `SKILL.md` and `references/` inside package data (e.g. `src/widescrape/skill_assets/` via Hatch `force-include` / `artifacts` and read via `importlib.resources.files("widescrape")`), with fallback to `parents[2]` in dev mode.
  3. In `install_agent_skills(with_mcp=True)`, detect whether running from a source repo (keep `uv run --no-sync --project ...`) vs an installed console entrypoint (`shutil.which("widescrape")` / `sys.executable -m widescrape mcp`).

---

### C-09: Prompt-Injection `<untrusted_web_data>` Delimiter Escape & HTML Sanitizer Regressions
- **Location:**
  - `src/widescrape/fetcher.py:97-155` (`_strip_hidden_and_comments`, `_node_to_md`, `html_to_markdown`)
  - `src/widescrape/cli.py:328`
  - `src/widescrape/mcp_server.py:134`
- **What Is Wrong & Why It Matters:**
  1. **Delimiter Breakout:** Both `cli.py:328` and `mcp_server.py:134` interpolate untrusted `body` and `it.url` straight into `<untrusted_web_data url="{it.url}">\n{body}\n</untrusted_web_data>`. A scraped page containing `</untrusted_web_data>` closes the untrusted boundary prematurely and injects arbitrary text outside the sandbox tag, while a `"` in `url` breaks out of the attribute.
  2. **Legitimate Content Deleted (`opacity:0` Substring Bug):** Line 108 checks `"opacity:0" in style` after stripping spaces. Any visible fade-in or semi-transparent element with `style="opacity: 0.85"` or `style="opacity:0.9"` becomes `"opacity:0.85"`, matches `"opacity:0"`, and is **decomposed**!
  3. **Sanitizer Bypasses:** `_strip_hidden_and_comments` misses `<template>` (HTML5 inert DOM containers whose text `_node_to_md` traverses!), `<iframe>`, `font-size:0`, `color:transparent`, `visibility:collapse`, `text-indent:-9999px`, `clip:rect(0,0,0,0)`, and `.sr-only` / `.visually-hidden`.
  4. **Content Extraction Regressions:**
     - `soup.find("article")` (`line 151`) returns only the *first* `<article>` when `<main>` is missing, silently discarding every subsequent post/item on forum threads, search pages, or blogs.
     - `<header>` is dropped globally (`line 118`), which strips `<article><header><h1>Title</h1></header>...</article>` headings on semantic HTML5 pages.
     - `<td>` and `<th>` fall through to `line 143`, concatenating adjacent table columns without spaces (`<td>ADCB20</td><td>15% off</td>` -> `'ADCB2015% off'`).
- **Verified Reproduction:**
  `html_to_markdown('<html><body><main><p style="opacity: 0.85">Important visible text</p><table><tr><td>ADCB20</td><td>15% off</td></tr></table><template>Hidden template injection</template></main></body></html>')`
  returned `'ADCB2015% off\n\nHidden template injection'`.
- **Concrete Fix:**
  1. Generate a random per-response boundary or escape `</untrusted_web_data` inside `body` (e.g. `<\/untrusted_web_data` or nonce tag `<untrusted_web_data id="ws_8f3a...">...</untrusted_web_data id="ws_8f3a...">`) and `html.escape(it.url, quote=True)`.
  2. Replace `"opacity:0" in style` with regex `re.search(r"(?:^|;)\s*opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)", style)`.
  3. Decompose `<template>`, `<iframe>`, `font-size:0`, `color:transparent`, and common screen-reader-only spoofing classes/clips.
  4. Handle `<table>` / `<tr>` / `<th>` / `<td>` cleanly with `" | "` Markdown cells and newlines; keep `<header>` inside `<article>`/`<main>` (only strip top-level site `<header>` outside `<main>`/`<article>`); and when `<main>` is absent and multiple `<article>` tags exist, extract all `<article>` blocks.

---

### C-10: `crawl_site` Preempts `start_url` When `max_pages=1` + `/llms.txt` & Spawns Unbounded Concurrent Chromium Processes
- **Location:**
  - `src/widescrape/adapters/web_dork.py:116-135` (`_fetch_html_playwright`)
  - `src/widescrape/adapters/web_dork.py:165-208` (`crawl_site`)
  - `src/widescrape/adapters/web_dork.py:421-445` (`_enrich_via_playwright`)
- **What Is Wrong & Why It Matters:**
  1. **`max_pages=1` Skips Requested Page:** At lines 165–179, if `use_llms_txt=True` and the domain has `/llms.txt`, `items` gets 1 item (`llms.txt`). Next, line 181 checks `while queue and len(items) < max_pages:`—which evaluates to `1 < 1` (`False`)! So `widescrape crawl https://docs.example.com/specific-guide --max-pages 1` **never fetches `/specific-guide`** and returns only `/llms.txt`.
  2. **Chromium Process Storm & Resource Leaks:** Inside `crawl_site`, `batch` collects up to 6 URLs and runs `asyncio.gather(*(_grab(u, d) for u, d in batch))`. If `browser_fetch=True` or pages look like SPA shells, `_grab` calls `_fetch_html_playwright` **per URL**—launching up to **6 simultaneous Chromium browser instances** in parallel! Furthermore, neither `_fetch_html_playwright` (lines 127–132) nor `_enrich_via_playwright` (lines 425–443) wraps `browser.close()` in `try/finally`, so any `CancelledError` or exception before line 132/443 leaks orphaned `chrome`/`chromium` processes on Windows.
- **How to Trigger / Verify:**
  Call `crawl_site("https://modelcontextprotocol.io/specification", max_pages=1, depth=1)` -> only `llms.txt` is returned, while `https://modelcontextprotocol.io/specification` is never fetched. Call `crawl_site("https://spa.example.com", max_pages=12, depth=2, browser_fetch=True)` -> spawns 6 simultaneous `async_playwright()` + Chromium processes per batch.
- **Concrete Fix:**
  1. Either keep `/llms.txt` outside the `max_pages` cap for `start_url` (always fetch `start_url` when `depth == 1` / `max_pages == 1`), or check `start_url` first and use `/llms.txt` as an enrichment/discovery boost when crawling docs (`depth >= 2` or root URL).
  2. Lazily launch **at most one shared Playwright browser** per `crawl_site` run inside `try ... finally: await browser.close()`, reusing browser contexts/pages protected by a small semaphore (`asyncio.Semaphore(2)`). Wrap `_enrich_via_playwright`'s `browser.close()` in `try/finally` as well.

---

## 4. Normal Findings

### N-01: `GitHubAdapter` Sends Invalid `pushed:>=` Qualifier to Issues Search & Exceeds `limit`
- **Location:** `src/widescrape/adapters/github.py:13-25, 38, 46-49, 86-93`
- **What Is Wrong & Why It Matters:** `build_github_query` appends `pushed:>=YYYY-MM-DD` for `--days`, then sends that exact query to *both* `/search/repositories` and `/search/issues`. GitHub's Issue Search API does **not** recognize `pushed:` (issues use `updated:>=` or `created:>=`), causing GitHub to treat `pushed:>=2026-11-07` as plain literal text or return zero/irrelevant issue matches. Additionally, `per_page = min(max(limit, 10), 100)` requests up to `limit` from *both* endpoints and returns up to `2 * limit` items without slicing `items[:limit]`, and there is no support for an optional `GITHUB_TOKEN` (60 req/hr unauthenticated limit).
- **How to Trigger:** Run `widescrape search "mcp server" --sources github --days 7` and inspect issue search hits (`kind="issue"`).
- **Concrete Fix:** Build separate repo (`pushed:>=`) and issue (`updated:>=`) queries, split `per_page` or cap `items[:limit]`, and include `Authorization: Bearer <token>` if `os.environ.get("GITHUB_TOKEN")` is set.

### N-02: `ArxivAdapter` Uses Broken Space-Separated `all:` Query & Ingests API Error Feeds
- **Location:** `src/widescrape/adapters/arxiv.py:17-62, 70-85`
- **What Is Wrong & Why It Matters:**
  1. `params = {"search_query": f"all:{q}"}` with `q = "agentic rag memory"` searches `all:agentic` AND untyped `rag` / `memory` (or triggers Lucene syntax surprises on colons/hyphens/quotes) instead of `all:agentic AND all:rag AND all:memory`.
  2. When arXiv returns an error Atom feed (`<entry><id>http://arxiv.org/api/errors#...</id><title>Error</title>...`), `parse_arxiv_atom` treats it as a real paper!
  3. `httpx.AsyncClient` omits `follow_redirects=True` (arXiv frequently redirects `http://`/`https://` or trailing slashes), and `days` uses `YYYY-MM-DD` midnight-UTC comparison (`> days * 86400`), rejecting valid papers by up to 23 hours.
- **Concrete Fix:** Format multi-word queries as `" AND ".join(f"all:{tok}" for tok in safe_tokens)`, skip entries whose `<id>` contains `arxiv.org/api/errors` or `<title>Error</title>`, enable `follow_redirects=True`, and compare full ISO timestamps.

### N-03: `core_terms` Drops All 2-Character Technical Terms (`AI`, `Go`, `ML`, `C#`, `JS`, `TS`, `UI`, `UX`)
- **Location:** `src/widescrape/expander.py:254`
- **What Is Wrong & Why It Matters:** `core_terms = [t for t in lower.replace('"', "").split() if len(t) > 2]` discards `ai`, `go`, `ml`, `c#`, `js`, `ts`, `db`, `ui`, `ux`, `qa`, `os`. Verified in Python:
  - `"remote AI engineer"` -> `core_terms=['remote', 'engineer']` (loses `"ai"`!)
  - `"AI agents in Go"` -> `core_terms=['agents']` (loses `"ai"` and `"go"`!)
  - `"C# ML UI UX"` -> `core_terms=[]`!
  Because `GitHubAdapter`, `StackExchangeAdapter`, `ArxivAdapter`, `RedditArchiveAdapter` (Arctic Shift / RSS), and `filter_rank.py` term coverage rely on `core_terms`, dropping 2-char domain nouns severely degrades precision and recall.
- **Concrete Fix:** Replace `len(t) > 2` with a curated English stopword set (`{"a","an","and","are","as","at","be","by","for","from","how","in","is","it","of","on","or","the","to","vs","with"}`), preserving 2-char tokens and single-letter languages (`c`, `r`, `c#`, `f#`).

### N-04: `mode="auto"` Substring Matching Misroutes Normal Queries Into `jobs` or `research`
- **Location:** `src/widescrape/expander.py:88-95`
- **What Is Wrong & Why It Matters:** `any(w in lower for w in ("job", "hiring", "role", "career", "intern", "freelance", "remote"))` checks raw substrings without word boundaries (`\b`). Verified in Python:
  - `"internet outage dubai"` -> **`jobs`** (contains `"intern"`)
  - `"TV remote control"` -> **`jobs`** (contains `"remote"`)
  - `"tv show recommendations"` -> **`research`** (contains `"how"` inside `"show"`)
  - `"petrol prices"` / `"controller"` (`"role"` vs `"roll"`—note `"tv show"` hits `"how"` and `"cholesterol"`? Let's check exact observed: `"tv show recommendations"` -> `research`).
  This silently activates 21 LinkedIn/X/ATS job dorks for `"internet outage dubai"`.
- **Concrete Fix:** Use compiled word-boundary regexes (`\b(?:jobs?|hiring|career|internships?|freelance)\b` and phrases like `\bremote\s+(?:job|role|work|engineer|developer)\b`) rather than bare substring matching.

### N-05: Proxy Ignored by 6 of 8 Adapters, Broken in Playwright With Credentials, and Exposed by `doctor`
- **Location:**
  - `src/widescrape/orchestrator.py:55-86`
  - `src/widescrape/adapters/{github,hackernews,reddit,news,stackexchange,arxiv}.py`
  - `src/widescrape/adapters/web_dork.py:126, 424` and `src/widescrape/adapters/browser_social.py:157`
  - `src/widescrape/installer.py:99-100`
- **What Is Wrong & Why It Matters:**
  1. `run_scrape(..., proxy=...)` only passes `proxy` to `WebDorkAdapter` and `BrowserSocialAdapter`. All 6 HTTP API adapters instantiate `httpx.AsyncClient()` without `proxy=resolve_proxy(...)`, directly contradicting `SKILL.md` / `references/recipes.md` (`"WIDESCRAPE_PROXY: passed across primp, httpx, ddgs, and Playwright"`).
  2. Chromium/Playwright rejects credentials embedded inside `proxy={"server": "http://user:pass@host:port"}`—Playwright requires separate `username` and `password` fields in the proxy dict.
  3. `widescrape doctor` (`installer.py:100`) prints `WIDESCRAPE_PROXY` verbatim to stdout, leaking proxy credentials into terminal logs and AI agent transcripts.
- **Concrete Fix:** Add `proxy: str | None = None` to `BaseAdapter`/all adapters (resolving `WIDESCRAPE_PROXY`), add a shared `playwright_proxy_config(proxy)` parser splitting `username`/`password` via `urlparse`, and mask `user:pass` as `***:***` in `collect_diagnostics()`.

### N-06: `widescrape auth reddit` Is Dead Code; Cookie Files Lack Validation, Restrictive Permissions, and Async `input()`
- **Location:**
  - `src/widescrape/adapters/browser_social.py:17-34, 113-130, 138-179`
  - `src/widescrape/orchestrator.py:80-86`
  - `src/widescrape/cli.py:347-372`, `src/widescrape/installer.py:129-130`
- **What Is Wrong & Why It Matters:**
  1. `cli.py auth`, `status`, and `doctor` advertise `reddit` session capture, and `capture_auth_session("reddit")` opens a browser and saves `~/.widescrape/cookies/reddit.json`. However, `orchestrator.py:80-86` only builds `BrowserSocialAdapter` for `"x"` and `"linkedin_posts"`, and `BrowserSocialAdapter._run_platform()` has no handler for `"reddit"`. The saved Reddit session is completely unused.
  2. `cookie_path_for(platform)` and `has_saved_session(platform)` do not validate `platform in LOGIN_URLS`, allowing path traversal (`../../foo`) if exposed programmatically.
  3. Saved session cookie files (`storage_state`) grant full account takeover and are written with default umask (`0644` on Unix) instead of `0600`.
  4. `capture_auth_session` is `async def` yet calls blocking `input(...)` directly (`line 127`), freezing the asyncio event loop while waiting for the user to log in.
- **Concrete Fix:** Either implement authenticated Reddit scraping or drop `"reddit"` from `LOGIN_URLS` / `status` / `doctor` until implemented; enforce `platform in LOGIN_URLS` inside `cookie_path_for`/`has_saved_session`; `os.chmod(target, 0o600)` on POSIX; and use `await asyncio.to_thread(input, ...)`.

### N-07: `BrowserSocialAdapter` Leaks Untracked `create_task` Listeners, Ignores `plan.days`, and Misses Expiry / Metadata
- **Location:** `src/widescrape/adapters/browser_social.py:36-110, 181-327`
- **What Is Wrong & Why It Matters:**
  1. `page.on("response", lambda resp: asyncio.create_task(_on_response(resp)))` (`lines 199, 270`) fires background tasks without tracking or awaiting them. When `max_scrolls` finishes or returns early, `finally: await context.close()` kills still-running `_on_response` tasks or triggers `"Task exception was never retrieved"` warnings.
  2. `plan.days` is completely ignored (`_scrape_x` could append `since:YYYY-MM-DD`, LinkedIn supports `datePosted` URL filters).
  3. Expired cookies silently redirect Playwright to `/login` or `/i/flow/login`, returning `0` items with zero warning in `adapter_errors`.
  4. `_extract_tweets_from_graphql` ignores `legacy.created_at` and long-form `note_tweet` text (`note_tweet_results.result.text`); `_extract_linkedin_from_voyager` ignores author and engagement counts (`socialDetail`).
- **Concrete Fix:** Track listener tasks in a `pending_tasks: set[asyncio.Task]` and `await asyncio.gather(*pending_tasks, return_exceptions=True)` before closing context; check `page.url` after navigation for login walls and warn; extract `created_at` and `note_tweet`; apply `since:` filter for X when `plan.days` is set.

### N-08: Non-ASCII / Unicode Queries Collapse to Empty Tokens in BM25, Highlights, and SQLite FTS5
- **Location:**
  - `src/widescrape/filter_rank.py:35-39` (`_TOKEN_RE`)
  - `src/widescrape/storage.py:15-24` (`_SAFE_TOKEN_RE`, `sanitize_fts5_query`)
- **What Is Wrong & Why It Matters:** Both `_TOKEN_RE = re.compile(r"[a-z0-9_+#.-]+", re.IGNORECASE)` and `_SAFE_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+")` match **only ASCII** letters and digits. For Arabic, CJK, Cyrillic, or accented European words (particularly relevant given the UAE/Dubai use cases in `modes.py` and `reddit.py`!), `_tokenize("عروض أمازون")` returns `[]` and `sanitize_fts5_query("عروض أمازون")` returns `""`, forcing FTS5 to skip indexing queries and degrading BM25 scoring to `0.0` (falling back to slow `LIKE %...%`).
- **Verified Reproduction:** `_tokenize("عروض أمازون") == []`, `sanitize_fts5_query("عروض أمازون") == ''`.
- **Concrete Fix:** Switch to Unicode-aware character classes: `_TOKEN_RE = re.compile(r"[^\W_]+(?:[+#.-][^\W_]+)*", re.UNICODE)` and `_SAFE_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)`, plus `unicode61` tokenizer on FTS5 (already SQLite default).

### N-09: Interactive HTML Viewer Hides Discounts/Banks When Codes Exist & Has Broken Sort/Clipboard UX
- **Location:** `src/widescrape/exporter.py:271-272, 301, 335, 380-396`
- **What Is Wrong & Why It Matters:**
  1. **Hidden Data Bug (`line 387`):** `if (r.extracted && (!r.codes || !r.codes.length)) extTd.appendChild(el('span', '', r.extracted));` — Whenever an item has at least one promo code, all discounts (`20% off`), bank/card names (`ADCB`), and other extracted tags are **hidden from the HTML table**!
  2. **Broken Clipboard Feedback on `file://` (`line 384`):** `navigator.clipboard` is undefined or rejects on `file://` origins in several browsers, yet the click handler unconditionally marks the pill `"(copied)"` without awaiting the Promise or providing a fallback.
  3. **Incomplete Script-Context JSON Escaping (`lines 271-272`):** Only `.replace("</", "<\\/")` is applied; `<!--` inside strings can also misbehave in HTML parsers, and unescaped `\u2028` / `\u2029` break older JS engines.
  4. **Misleading `<th>` Cursor (`line 301`):** Table headers have `cursor:pointer; user-select:none` implying clickable column sorting, yet clicking headers does nothing (only the dropdown sorts).
- **Concrete Fix:** Render both the clickable code pills AND a secondary line showing non-code `extracted` details; escape `<` -> `\u003c`, `>` -> `\u003e`, `&` -> `\u0026`, `\u2028`, `\u2029` in `safe_json`; handle `clipboard.writeText(...).then(...)` with `document.execCommand('copy')` textarea fallback; and wire click-to-sort on `<th>` headers or remove `cursor:pointer`.

### N-10: `filter_and_rank` + `canonicalize_url` Drop Rich Results, Misclassify Docs, and Collapse on Small Corpora
- **Location:**
  - `src/widescrape/filter_rank.py:21-32, 46-60, 209-229, 234-244`
  - `src/widescrape/fetcher.py:35-38, 72-80`
- **What Is Wrong & Why It Matters:**
  1. **Order-Dependent Dedup Drops Rich Items (`lines 209-229`):** Because `WebDorkAdapter` runs concurrently with `GitHubAdapter`/`RedditArchiveAdapter` and `filter_and_rank` keeps whichever item appears first in `items`, a bare 44-char search snippet from `web` (`eng=0`) often deduplicates and discards the rich 700-char, 1,500-star item from `github`! Verified in Python (`kept source='web', eng=0, body_len=44, deduped=1`).
  2. **Canonicalization Gaps (`fetcher.py:35-38, 72-80`):**
     - `www.reddit.com/...` vs `reddit.com/...` do not deduplicate (`www.` is not stripped).
     - Query parameter order (`?b=2&a=1` vs `?a=1&b=2`) is not sorted.
     - `"s"` in `TRACKING_PARAMS` strips legitimate search parameters on WordPress/forums (`https://blog.example.com/?s=python` -> `https://blog.example.com`).
     - Google News opaque `/rss/articles/...` links and direct publisher URLs never deduplicate against each other (causing duplicate Gulf News / Arabian Business entries observed in live run `3ce2440ee3`).
  3. **False-Positive Noise Filter (`filter_rank.py:21-24, 56-59`):**
     - `len(combined) < 25` drops short, legitimate GitHub repos (e.g., `"org/mcp" / "MCP client"`, 18 chars).
     - `"sign in to view"` inside `SPAM_PATTERNS` drops a 2,000-char documentation page that happens to include `"First, sign in to view your dashboard"`! Verified in Python.
  4. **BM25 Collapse on `N <= 2` (`line 234-244`):** `BM25Okapi`'s IDF formula $\log\frac{N - n_q + 0.5}{n_q + 0.5}$ turns negative/zero whenever a term appears in $\ge N/2$ documents (e.g., `N=1` or `N=2`), collapsing `bm25_norm` to `0.0` for all matching items.
- **Concrete Fix:** Merge-deduplicate by keeping the candidate with richest body + highest engagement (preserving metadata/sources); normalize leading `www.` carefully, sort query params, remove `"s"` from global `TRACKING_PARAMS` (only strip `"s"` on `x.com`/`twitter.com`), restrict login-wall regex checks to short bodies (`len(body) < 350`), exempt structured API sources from strict 25-char minimums (or lower to 12), and implement positive BM25+ IDF (`log((N + 1) / (df + 0.5))`).

### N-11: `stealth_fetch_document` Double-Fetches Terminal Errors, Buffers Unbounded RAM, and Silently Truncates PDFs
- **Location:** `src/widescrape/fetcher.py:167, 219-286`
- **What Is Wrong & Why It Matters:**
  1. `_sync_primp_get` returns `None` if `len(content) > MAX_FETCH_BYTES` (`line 237`), and `stealth_fetch_document` discards any non-200 (`404`, `410`), unsupported MIME (`image/png`, `video/mp4`), or Cloudflare challenge from `primp` (`lines 267-270`)—then **immediately fetches the exact same URL a second time via `httpx`**! That doubles latency and bandwidth on every 404/challenge/binary URL.
  2. Neither `primp` nor `httpx` streams responses; both download a 500 MB file completely into memory before checking `len(content) <= MAX_FETCH_BYTES`.
  3. `primp.Client(...)` is re-created from scratch on every URL (`line 228`), discarding TLS/HTTP2 connection pooling.
  4. `extract_pdf_text` (`line 167`) silently stops at `reader.pages[:15]` with no indicator in the title, body, or metadata that pages 16+ were omitted.
- **Concrete Fix:** Return a small dataclass/tuple with status & terminal reason from `_sync_primp_get` so `404`/`410`/oversize/unsupported-MIME/challenge skip the `httpx` retry (only falling back on transport/TLS/5xx/403 issues); stream `httpx` (`client.stream`) checking `Content-Length` and chunk byte sum against `MAX_FETCH_BYTES`; reuse `primp.Client` safely per worker/thread; and record `pdf_pages_total` vs `pdf_pages_extracted` in metadata (appending a truncation notice when `total > 15`).

### N-12: Mode & `--sites` Semantics Bugs Across `orchestrator`, `expander`, `modes`, and `mcp_server`
- **Location:**
  - `src/widescrape/orchestrator.py:109`
  - `src/widescrape/expander.py:107-143`
  - `src/widescrape/modes.py:33-37, 117-182`
  - `src/widescrape/mcp_server.py:80`
- **What Is Wrong & Why It Matters:**
  1. **`--sites` Contradiction:** `orchestrator.py:109` executes `effective_sources = sources or (["web"] if sites else None)`, overriding *both* `--mode jobs` / custom mode `sources` to `["web"]` whenever `--sites` is supplied! This contradicts `README.md:37` (`search "remote AI engineer" --mode jobs --sites jobs.ashbyhq.com,ycombinator.com`) and differs from calling `expand_query()` directly (which keeps all `jobs` sources and sets `has_explicit_sites=True`).
  2. **Cannot Override `default_days` With `days=0`:** Both `expander.py:126` (`days if (days is not None and days > 0) else custom_cfg.default_days`) and `mcp_server.py:80` (`days if days > 0 else None`) forbid overriding a custom mode's `default_days=14` back to "all dates" (`--days 0`).
  3. **`upsert_mode` Silently Resets `base_mode`:** `modes.py:119, 133` defaults `base_mode: str = "general"`. Updating `default_days` or `sites` on an existing `research` custom mode without re-specifying `base_mode="research"` silently overwrites `base_mode` back to `"general"` (verified in Python!). Nor can `upsert_mode` clear `description`, `default_extract`, or `default_days` back to empty/`None`.
  4. **`clean_site_domain` Preserves Invalid Query/Spaces (`modes.py:33-37`):** Passing `https://amazon.ae/deals?ref=1#top` leaves `?ref=1#top` intact; passing `"amazon.ae OR evil.com"` preserves spaces, corrupting `site:` dorks.
- **Concrete Fix:** Let `sites` augment/scope web dorks without erasing `mode`/`custom_cfg.sources` unless `--sources web` is passed; differentiate `days=None` (inherit mode default) from `days=0` (explicitly disable date filter) using `-1` or `None` sentinel in MCP; default `base_mode: str | None = None` in `upsert_mode` so existing `base_mode` is preserved when omitted; and parse `clean_site_domain` using `urlparse`, rejecting whitespace and stripping query/fragment.

### N-13: MCP Server Crashes on Bad Inputs, Ignores `max_chars` in `read`, and Blocks the Async Loop
- **Location:**
  - `src/widescrape/mcp_server.py:36-110, 117-136, 161-199, 221-290`
  - `src/widescrape/orchestrator.py:148-176`
- **What Is Wrong & Why It Matters:**
  1. **Unhandled Exceptions in MCP Tools:** Calling `widescrape_modes(action="add", name="auto")`, `widescrape_modes(action="remove_sites", name="nonexistent", sites="a.com")`, `widescrape_modes(action="add_sites", name="x", sites="")`, `widescrape_modes(action="show", name="auto")`, or `widescrape_search(..., mode="unknown")` raises unhandled `ValueError` or `KeyError` (all 4 verified in Python!). Meanwhile `cli.py` catches `(KeyError, ValueError)`.
  2. **`widescrape_read` Context Flood:** Line 129 only applies `max_chars` when `focus` is non-empty (`prune_text_bm25(..., max_chars=max_chars) if focus else it.body`). Calling `widescrape_read` on 10 crawled `/llms.txt` or PDF pages (up to 6,000+ chars each) without `focus` dumps 60,000+ characters into the LLM context window!
  3. **`widescrape_crawl` Missing `max_show` & Input Bounds:** No `max_show` cap (unlike `widescrape_search`), and no bounds checking on `limit_per_source`, `max_pages` (e.g. `10000`), or `depth` (`100`).
  4. **Event-Loop Blocking:** `run_scrape` and `run_crawl` are `async def` yet execute synchronous SQLite writes (`store.save_run`), `openpyxl` styling/saving, and `HTML` writing directly on the event loop thread.
- **Concrete Fix:** Catch `(ValueError, KeyError)` in `widescrape_search`, `widescrape_crawl`, and `widescrape_modes` returning clear tool-friendly guidance strings; apply `max_chars` truncation cleanly in `widescrape_read` even when `focus == ""` (or default `max_chars=4000` with a truncation marker + hint to use `focus`); add `max_show: int = 20` to `widescrape_crawl`; clamp `limit_per_source` (1–200), `max_pages` (1–100), and `depth` (1–4); and wrap `store.save_run` + `export_run_files` in `await asyncio.to_thread(...)`.

### N-14: `RedditArchiveAdapter` Regional Bias, Custom Mode Mismatch, and Early Fallback Cutoff
- **Location:** `src/widescrape/adapters/reddit.py:19-23, 48-54, 117-124, 165, 208-211, 245-251`
- **What Is Wrong & Why It Matters:**
  1. `DEFAULT_SUBREDDITS["general"]` hardcodes `["LocalLLaMA", "MachineLearning", "technology", "dubai", "UAE"]`—injecting Dubai/UAE subreddits into global generic queries for all users worldwide.
  2. Line 165 uses `DEFAULT_SUBREDDITS.get(plan.mode, DEFAULT_SUBREDDITS["general"])`. When a custom mode built on `base_mode="jobs"` or `"research"` is active (`plan.mode == "ai-jobs"`), it silently falls back to `"general"` instead of `"jobs"`/`"research"`.
  3. Lines 118 and 121 check `if len(items) < 5:` on *raw* PullPush/Arctic Shift items before `_is_low_quality` removes `[removed]`/`[deleted]` posts—so 5 deleted posts stop Arctic Shift and Reddit RSS from ever running!
  4. `_post_to_item` (`line 249`) does `float(created)`, failing silently if an archive mirror returns an ISO timestamp string. And `parse_reddit_rss` (`line 50`) parses only `YYYY-MM-DD` (midnight UTC), discarding `HH:MM:SS` precision on `--days 1` filters.
- **Concrete Fix:** Remove `"dubai"`/`"UAE"` from global default subreddits (or expose configurable subreddits on custom modes); store `base_mode` on `SearchPlan` and use `plan.base_mode`; filter `[removed]`/`[deleted]` stubs when counting usable hits before skipping fallbacks; support ISO strings in `_post_to_item`; parse full Atom `updated` timestamps in `parse_reddit_rss`.

### N-15: Quality & Fidelity Gaps in HackerNews, StackExchange, and Google News Adapters
- **Location:**
  - `src/widescrape/adapters/hackernews.py:19, 39, 62`
  - `src/widescrape/adapters/stackexchange.py:28-48, 58, 68`
  - `src/widescrape/adapters/news.py:52-60, 87`
- **What Is Wrong & Why It Matters:**
  - **HackerNews (`hackernews.py`):** Queries `search_by_date` unconditionally (so `"MCP server"` without `--days` returns obscure zero-vote posts from minutes ago instead of canonical high-point threads via `/api/v1/search`), stores raw `<p>`/`<a>`/HTML-escaped entities in `body`, and ignores `limit` when merging two queries (`<= 2 * limit`).
  - **StackExchange (`stackexchange.py`):** Queries without `filter=withbody`, so `body` only contains `"[Answered · 3 answers] Tags: python, asyncio"` with zero question text! Hardcodes `"site": "stackoverflow"` (missing `serverfault`, `superuser`, `ai`, `security`), ignores StackExchange's mandatory `backoff` field (causing IP throttling), and sets `query_used=q_primary` even when `q_short` was the query that returned results (`line 68`).
  - **Google News (`news.py`):** Hardcodes `hl=en-US&gl=US&ceid=US:en` (reducing regional UAE/EU/APAC news relevance), embeds Google's opaque `/rss/articles/...` redirect URLs (which also evade dedup against `web_dork`), and keeps the repeated `<title> - <Publisher>` string inside `body`, biasing BM25 term frequencies.
- **Concrete Fix:** Use Algolia `/api/v1/search` unless `plan.days` is small (`<= 30`), clean HN HTML bodies with BeautifulSoup/`html.unescape`, and slice `items[:limit]`; add `filter=withbody` (stripped to clean text) + honor `payload.get("backoff")` + record actual `query_used` in StackExchange; make Google News locale configurable (`WIDESCRAPE_NEWS_LOCALE` or mode option) and strip duplicate title/publisher prefixes from `body`.

### N-16: `map_site` Skips `<sitemapindex>`, `crawl_site` Drifts Outside Docs Prefix, and `run_crawl` Ranks Against URLs
- **Location:**
  - `src/widescrape/adapters/web_dork.py:31, 39-60, 95-101, 224-233`
  - `src/widescrape/orchestrator.py:205`
- **What Is Wrong & Why It Matters:**
  1. `map_site` detects `<sitemapindex>` (`line 95`), then runs `_LOC_RE.findall(...)` which returns the **child `.xml` sitemap URLs** (`.../sitemap-0.xml`, `.../post-sitemap.xml`) instead of fetching them to discover actual content URLs! Also `_LOC_RE` misses `<![CDATA[...]]>` wrapped URLs and fails to unescape `&amp;` in query strings.
  2. `crawl_site(..., depth=2)` calls `extract_same_host_links` (`line 226`) across the entire domain, so crawling `https://docs.example.com/v2/auth` spends half its `max_pages` budget on `/pricing`, `/careers`, `/terms`, and `/v1/`.
  3. `run_crawl` (`orchestrator.py:205`) calls `expand_query(query=query or url, ...)`. When the user leaves `query=""`, `filter_and_rank` computes BM25 relevance against the literal URL string (`https://example.com/docs/intro`) and applies spam/login-wall filters to intentionally crawled pages!
- **Concrete Fix:** In `map_site`, detect `<sitemapindex>` and concurrently fetch up to e.g. 5 child sitemaps, parsing `<loc>` with `ET.fromstring` + `html.unescape`; in `crawl_site`, add a `scope_prefix: bool = True` option (restricting child links to the starting path prefix when starting from a subpath); in `run_crawl`, preserve crawl traversal order and skip query-based BM25 re-ranking when `query == ""`.

### N-17: SearXNG Docker Compose Exposes Unthrottled JSON Search Engine to the Local Network
- **Location:** `docker-compose.yml:6`; `searxng/settings.yml:4-5`
- **What Is Wrong & Why It Matters:** `ports: - "8080:8080"` binds on `0.0.0.0:8080`, while `settings.yml` sets `limiter: false` and a hardcoded `secret_key: "change-me-widescrape-local-key"`. Anyone on the same Wi-Fi/LAN can send unauthenticated, rate-limit-free search queries through the host's IP.
- **Concrete Fix:** Bind `"127.0.0.1:8080:8080"` in `docker-compose.yml`, enable `limiter: true` (or loopback-only), and document setting `SEARXNG_SECRET` via environment/compose override.

### N-18: Test Suite Contains a 2026-11-28 Time-Bomb & Leaves Most Live Code Paths Untested
- **Location:** `tests/test_pipeline.py:54-56, 504-508` (and overall file `1-685`)
- **What Is Wrong & Why It Matters:**
  1. **Guaranteed Failure on Nov 28, 2026:** Line 504 hardcodes `<pubDate>Fri, 20 Nov 2026 10:00:00 GMT</pubDate>` and line 508 asserts `len(parse_google_news_rss(news_xml, "amazon.ae", days=7)) == 1`. Seven days after Nov 20, 2026 (using `datetime.now(timezone.utc)` inside `parse_google_news_rss`), this test **will fail permanently**!
  2. **Home Directory Pollution Risk:** `_isolated_modes` isolates `WIDESCRAPE_MODES_PATH`, yet `configure_quiet_logging` (`line 394`) and `collect_diagnostics` (`line 249`) still read/write real `%USERPROFILE%\.widescrape\`.
  3. **Major Uncovered Areas:** Zero tests execute:
     - Any adapter's `fetch()` HTTP/XML/JSON parsing (`GitHubAdapter`, `HackerNewsAdapter`, `RedditArchiveAdapter`, `GoogleNewsAdapter`, `StackExchangeAdapter`, `ArxivAdapter`, `WebDorkAdapter`) even with `httpx.MockTransport`.
     - `crawl_site`, `map_site`, `stealth_fetch_document`, `run_crawl`, `_fetch_html_playwright`.
     - 5 of 6 MCP tools (`widescrape_read`, `widescrape_cache_find`, `widescrape_crawl`, `widescrape_map`, `widescrape_modes`).
     - Most CLI commands (`read`, `find`, `show`, `runs`, `crawl`, `map`, `status`, `doctor`, `mode delete/remove-sites/show`).
     - Security/edge invariants (SSRF, Excel formula injection/control chars, Unicode tokenization, concurrency, Windows DB handle cleanup).
- **Concrete Fix:** Compute test RSS dates dynamically relative to `datetime.now(timezone.utc)`; isolate `LOG_DIR` and `DEFAULT_DB_PATH` in an `autouse` fixture; add deterministic `httpx.MockTransport` tests for every adapter, `map_site`, `crawl_site`, and `stealth_fetch_document`; and add regression tests for all Critical/Normal bugs.

---

## 5. Low Findings

- **L-01: Misleading `compression_pct` Metric (`filter_rank.py:186, 276-280`; `orchestrator.py:147-160`; `benchmarks/eval_compression.py`):**
  On synthetic 1,400-char bodies, `eval_compression.py` reports `80% context saved`. On real `search` runs without `fetch_pages`, short 120–160 char snippets expand into longer ~200 char `compact_summary()` lines, causing `max(0, ...)` to report `0%` in all three real exports (`3ce2440ee3`, `3ed0ffc185`, `dd417e3adb`). Worse, `stats` is calculated *before* `--new-only` filtering and *before* `apply_extractors`/`apply_custom_fields` mutate `compact_summary()`, making `compression_pct` stale when `--new-only` or `--fields` runs.
  - *Fix:* Recalculate `summary_chars` / `compression_pct` after `--new-only` and extraction, and track `kept_index_pct` vs `full_payload_saved_pct` (comparing against full cached bodies + dropped items).
- **L-02: `select_best_highlight` Edge-Case Truncation & Missing Currencies (`filter_rank.py:149-181`):**
  1. `clipped = chosen[: max(20, budget - 3)].rsplit(" ", 1)[0]` ignores `max_chars < 24` (`max_chars=15` returned 22 chars in verification!).
  2. When the first word is `"A "` followed by a 200-char URL (`"A https://example.com/..."`), `rsplit(" ", 1)[0]` splits back to `'A'`, returning `'A...'`! Only accept `rsplit` if the split position is past ~40% of the budget.
  3. Line 169 uses case-sensitive `AED|EUR` (missing lowercase `aed`, plus `USD`, `GBP`, `SAR`).
- **L-03: Unescaped Markdown Brackets & Trailing Whitespace/Non-Breaking Spaces in Links (`models.py:42, 62`; `exporter.py:96-114`; `cli.py:43-50`):**
  Titles containing `[` or `]` break `[title](url)` Markdown rendering across CLI, `compact_summary`, and `.md` reports. Furthermore, live export `3ce2440ee3` line 12 has a trailing `\xa0` in `https://www.retailmenot.com/view/amazon.com\xa0`, and `exporter.py`/`cli.py` do not verify `http://`/`https://` scheme before formatting `[title](url)` in Markdown.
  - *Fix:* Escape `[`/`]` in Markdown link text, strip whitespace/trailing control chars in `canonicalize_url` / `ScrapeItem.__post_init__`, and validate `http(s)://` before emitting clickable Markdown links.
- **L-04: CLI UX Rough Spots & Windows Pipe Encoding Risk (`cli.py:124-126, 166-171, 216-221, 383-394, 411-450`):**
  - `show` fetches *all* rows via `store.get_run_items(run_id)` instead of passing `offset`/`limit` to SQLite, and when `offset >= total` it prints `"Showing 0 results (ranks 21-20 of 5)"`.
  - `--quiet` (`lines 166-171`) suppresses `adapter_errors`, hiding upstream failures completely.
  - `crawl` lacks `--max-show` and `--json` flags (unlike `search`), and doesn't catch `ValueError` cleanly.
  - `mode show auto` lets `normalize_mode_name("auto")` raise an unhandled traceback instead of catching `ValueError`.
  - Piped CLI output on Windows (`cp1252` stdout) can raise `UnicodeEncodeError` on `·` (`\u00b7`), `—` (`\u2014`), or scraped Unicode emojis unless `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` is set.
- **L-05: Import-Time Global Logging Side Effects & Unbounded Log Growth (`logging_setup.py:24-51`; `cli.py:8`; `mcp_server.py:8`):**
  Importing `cli` or `mcp_server` immediately mutates the global `logging` root logger (stripping `StreamHandler`s, breaking `pytest --log-cli` and embedding callers) and creates `~/.widescrape/logs/widescrape.log` on disk even during `--help` or tests. `FileHandler` also grows without rotation.
  - *Fix:* Invoke `configure_quiet_logging()` inside CLI callback / `run_mcp_server()` (setting `RUST_LOG` lazily via `os.environ.setdefault` at top of entrypoint scripts), and use `RotatingFileHandler(maxBytes=2_000_000, backupCount=3)`.
- **L-06: Dead Imports, Mandatory `playwright`, and Mutable Defaults on Frozen `SearchPlan` (`cli.py:14, 38`; `stackexchange.py:5`; `pyproject.toml:13, 16`; `expander.py:33-48`):**
  - `cli.py` imports `rich.console.Console` and instantiates `console = Console()` without ever using it (adding CLI import latency alongside `rich` in `pyproject.toml`).
  - `adapters/stackexchange.py:5` has an unused `import asyncio`.
  - `pyproject.toml:13` lists `playwright>=1.49.0` as a hard dependency even though `README.md` / `SKILL.md` describe Playwright as optional (`[project.optional-dependencies] browser = [...]`).
  - `SearchPlan` (`expander.py:33-48`) is `frozen=True` yet holds mutable `list` fields (`core_terms`, `dork_queries`, etc.) and uses `boost_keywords: list[str] = ()` with `# type: ignore[assignment]`. Use `tuple` consistently.
- **L-07: Hardcoded Personal Path in Docs & Version Drift (`docs/advanced.md:42`; `pyproject.toml:3`; `__init__.py:3`; `SKILL.md:15`):**
  - `docs/advanced.md:42` hardcodes `"C:/Users/bigoa/.gemini/antigravity/scratch/widescrape"` in the public configuration example instead of `"/path/to/widescrape"`.
  - `pyproject.toml` and `widescrape/__init__.py` declare `"0.1.0"`, whereas `SKILL.md` declares `"0.2.0"`.
- **L-08: Mode Introspection & Boolean Type Gaps (`modes.py:95, 241-266`; `cli.py:222-230`; `mcp_server.py:221-280`):**
  - `default_fields` is omitted from `list_all_modes()`, `widescrape mode list`, `widescrape mode show`, and `widescrape_modes(action="show")`.
  - `signal_keywords`, `boost_keywords`, and `penalty_keywords` are editable in CLI `mode add` (`cli.py:242-244`) and stored in `modes.json`, yet are completely missing from MCP `widescrape_modes` (`mcp_server.py:221-280`).
  - `modes.py:95` checks `isinstance(days_val, int) and days_val > 0`, which evaluates `True` for Python `bool` (`True == 1`), and `modes.py:244` labels a built-in override as `"built-in"` even if it overrides `sources`, `default_days`, or `default_extract` without `sites`.
- **L-09: BM25 Double-Counts `core_terms` & `item_id` Collisions (`filter_rank.py:236`; `models.py:28-30`):**
  - `query_tokens = _tokenize(f"{plan.raw_query} {' '.join(plan.core_terms)}")` doubles term weight for every word >2 chars relative to 2-char words (`AI`, `Go`).
  - `item_id` hashes raw `self.url` rather than `canonicalize_url(self.url)` truncated to 8 hex chars (32 bits, ~50% birthday collision probability around 77,000 items—switch to 12 hex chars / 48 bits on canonicalized URL).
- **L-10: Empty-Result Runs Create Orphan Files Without SQLite Run Metadata (`orchestrator.py:162-176`; `storage.py:180-193`):**
  When `ranked_items` is empty (`0` kept, e.g., `--new-only` skipping everything), `run_scrape` still generates empty `.md`, `.xlsx`, and `.html` files under a new `run_id`, while `store.save_run(run_id, [])` inserts 0 rows. Because `list_runs` aggregates `FROM items`, the generated `run_id` does not exist in `widescrape runs` or `widescrape show`. (Resolved cleanly alongside the `runs` table in C-03.)

---

## 6. Test Suite & Benchmark Audit

### What `tests/test_pipeline.py` Actually Verifies vs. Misses
| Area | Current Status | Gap / Risk |
| :--- | :--- | :--- |
| `expander.py`, `modes.py`, `models.py` | Unit-tested for happy paths | Untested: `mode="auto"` edge queries, `days=0` override, `upsert_mode` preserving `base_mode`, `clean_site_domain` validation |
| `fetcher.py` | HTML Chrome strip, challenge string, 1 minimal PDF, `canonicalize_url` | **Untested:** `stealth_fetch_document()` HTTP/primp path, size cap, redirects, SSRF, `opacity:0.x`, `<template>`, `<table>` |
| `filter_rank.py` | Basic spam/dedup/BM25/highlight tests | **Untested:** Unicode queries, order-dependent dedup, login-wall false positives on docs, small-corpus BM25 (`N<=2`), undated `--days` items |
| `extractor.py` | Happy-path regex fixtures (`WHITE20`, `$160k-$210k`, custom fields) | **Untested:** False-positive precision tests on ordinary English prose, word boundaries, field alias misrouting |
| `storage.py` | Save, read, FTS5 special chars, pagination | **Untested:** Multiple runs sharing a URL (masks C-03 data loss!), Windows DB handle closure, Unicode FTS5 |
| `exporter.py` | File existence, workbook sheet names, `<script>` escaping in HTML | **Untested:** Excel formula injection, control char crashes (`\x0c`, `\x00`), HTML pills + extracted co-display |
| `adapters/*.py` | Only static XML/JSON parser helpers (`parse_*`, `_extract_*`, `build_github_query`) | **0% coverage** of `fetch()` methods across all 8 adapters (`MockTransport`), `map_site`, `crawl_site`, `WebDorkAdapter` |
| `cli.py` & `mcp_server.py` | `mode add/list`, `search --quiet`, `widescrape_search` (mocked) | **0% coverage** of `read`, `find`, `show`, `runs`, `crawl`, `map`, `status`, `doctor`, and 5/6 MCP tools |

### Specific Test & Benchmark Bugs
1. **Time-Bomb Test (`tests/test_pipeline.py:504-508`):** Hardcodes `Fri, 20 Nov 2026` with `days=7`. Starting **2026-11-28**, `parse_google_news_rss` compares against `datetime.now(timezone.utc)` and filters the item out, breaking CI deterministically.
2. **Benchmark vs. Reality Gap (`benchmarks/eval_compression.py`):** The benchmark achieves `80%` compression only because synthetic items have **1,400-char bodies**, 30 duplicate URLs, and 40 spam/stub items. In real multi-source searches (without `fetch_pages=True`), search engines return ~130-char snippets, and formatting them into `compact_summary()` yields **0% reported compression** in `3ce2440ee3`, `3ed0ffc185`, and `dd417e3adb`.

---

## 7. Better Engineering Approaches (Architecture Recommendations)

### 1. Normalized 3-Table SQLite Schema + External-Content FTS5 Triggers
- **Current Choice:** Single `items` table with `item_id PRIMARY KEY` and `INSERT OR REPLACE`, manually deleting/inserting `items_fts` rows per item.
- **Better Approach:** Split into `runs`, `items` (deduplicated canonical document cache keyed on canonical URL / 48-bit ID), and `run_items` (`(run_id, item_id)` join storing per-run `rank`, `score`, `query_used`, and run-specific extractions), with SQLite triggers keeping `items_fts` synchronized and explicit connection closing.
- **Pros:** Eliminates historical run data loss; records empty/zero-result runs (`adapter_errors`, `stats`) cleanly; speeds up repeated searches; makes per-run diffs and re-exports trivial.
- **Cons:** Requires a one-time idempotent schema migration when opening existing `~/.widescrape/widescrape_cache.db` files.

### 2. Two-Stage Pipeline: Merge-Dedup & Rank First, Enrich Pages Second
- **Current Choice:** `WebDorkAdapter` enriches `items[:40]` (raw, un-deduped, unranked search results) inside `fetch()` before `filter_and_rank()` ever sees them.
- **Better Approach:** Fetch lightweight metadata/snippets from all adapters concurrently -> **merge-deduplicate** across sources (keeping richest body/engagement + combining `all_sources` tags) -> score candidates -> optionally fetch full HTML/PDF/Playwright content **only for the top K (`10–15`) unique relevant URLs** -> re-score, highlight, and extract on the enriched bodies.
- **Pros:** Cuts HTTP/Playwright traffic and latency by 3–5×, stops wasting page fetches on duplicate/spam/irrelevant hits, and prevents thin search snippets from shadowing rich GitHub/Reddit items.
- **Cons:** Slightly more structured orchestrator flow between adapters and ranker (adapters focus purely on fast discovery; orchestrator drives optional enrichment).

### 3. Shared `SafeFetcher` Layer (SSRF Guard + Streaming Caps + Pooled Clients + Table-Aware Markdown)
- **Current Choice:** Ad-hoc `httpx.AsyncClient` and per-call `primp.Client` scattered across 7 files with `follow_redirects=True` and raw BeautifulSoup `html_to_markdown`.
- **Better Approach:** Centralize HTTP transport into a single `SafeFetcher` module providing: DNS-validated SSRF blocking on every redirect hop; streaming size limits; shared proxy credential parsing; per-domain concurrency + jittered backoff; and optional `trafilatura` (or improved BeautifulSoup table/article/JSON-LD extraction) for higher-accuracy article boilerplate removal.
- **Pros:** Single security boundary for SSRF, redirects, proxy auth, and memory limits; dramatic speedup via connection reuse and elimination of redundant `primp`->`httpx` double-fetches.
- **Cons:** Adds slight upfront refactor across adapters; `trafilatura` (if added) introduces another dependency, though improving Pure-BS4 as proposed in C-09 requires zero new dependencies.

### 4. Three-Tier Structured Extraction (Metadata -> Strict Typed Regex -> Optional LLM Pass)
- **Current Choice:** Substring-heavy regexes trying to extract arbitrary `--fields` (`company`, `deadline`, `min_spend`) directly from unstructured HTML/snippets.
- **Better Approach:**
  1. **Tier 1 (Zero-cost structured web metadata):** Parse `<script type="application/ld+json">` (`JobPosting`, `Offer`, `Product`, `Article`) and OpenGraph tags (`og:price`, `article:published_time`) when full pages are fetched.
  2. **Tier 2 (Strict word-boundary + case-aware regexes):** Only emit promo codes, salaries, and dates that pass strict structural checks (`(?-i:...)`, explicit `Key: Value` first).
  3. **Tier 3 (Agent-friendly schema mode):** When an arbitrary field like `pros_cons` or `eligibility` isn't deterministically matched, return the best matching passage snippet cleanly via `compact_summary` / `widescrape_read` rather than guessing false values like `company="$150k-$180k"`.
- **Pros:** Eliminates embarrassing false extractions (`DIB` from `"incredible"`, `$81.34` IPO price as discount) while capturing genuine salaries/prices/deadlines from JSON-LD on Ashby/Greenhouse/ecommerce pages.
- **Cons:** Tier 1 requires `fetch_pages=True` (or enrichment on top hits) to read JSON-LD.

### 5. Honest Freshness Modeling Instead of Today-Date Imputation
- **Current Choice:** Imputes `today` for undated items and treats `--days` identically across timestamped APIs and timeless web snippets.
- **Better Approach:** Keep `published_at = ""` when genuinely unknown; parse relative/absolute snippet dates and page metadata; apply a **recency bonus/decay** for timestamped fresh hits; and let `--days N` either require verified dates (`--strict-days`) or clearly separate `verified fresh` vs `undated` results with a caveat.
- **Pros:** Restores trust in `Date` and `deadline` columns and makes `--days` monitoring accurate.
- **Cons:** Timeless search results without extractable snippet dates may look less uniform until `fetch_pages` or JSON-LD extraction inspects them.

### 6. Adaptive Dork Budgeting & Backoff
- **Current Choice:** Static Cartesian product (`len(dork_queries) × 4 backends × 3 pages`) launching 250–500 search requests per run.
- **Better Approach:** Prioritize high-yield dorks/backends first (Page 1 on top 2 backends), track unique URL yield per dork, stop early once target `limit` is satisfied, and apply exponential backoff + jitter on 429/timeout.
- **Pros:** 5–10× faster default searches, dramatically fewer IP blocks from Bing/Yahoo/Google, and cleaner logs.
- **Cons:** Very rare/niche dorks might retrieve slightly fewer total raw items unless `limit` is raised.

---

## 8. High-Value Missing Features (Prioritized by Real-World Impact)

1. **Content-Hash Change Detection (`--changed` / Watch Mode):**
   Currently `--new-only` only compares URLs. For tracking promo pages, pricing pages, changelogs, or documentation where the *URL stays identical* while the content updates, storing `sha256(normalized_body)` per canonical URL in SQLite lets `--changed` surface "New or Updated" pages immediately.
2. **Real Per-Adapter Telemetry & Health Summary:**
   Track `{status, raw_count, kept_count, elapsed_ms, warning}` per adapter and print a compact status summary (including `"x: skipped (no cookies)"`, `"github: rate-limited HTTP 403"`, `"ddgs: bing blocked, yahoo ok"`). Eliminates blind debugging when results drop to zero.
3. **Configurable News Locale (`--locale` / `--region`) & Google News Publisher Dedup:**
   Right now `news.py` hardcodes `hl=en-US&gl=US&ceid=US:en` and `reddit.py` hardcodes `dubai`/`UAE`. Letting users/modes pass `region="AE"` / `lang="en"` improves local relevance dramatically, and normalizing `"Title - Publisher"` improves cross-source deduplication against `web_dork`.
4. **Optional Zero-Config Token Uplift (`GITHUB_TOKEN`, `STACKEXCHANGE_KEY`, `REDDIT_CLIENT_*`):**
   Keep WideScrape 100% zero-API-key out of the box, while automatically sending `Authorization: Bearer $GITHUB_TOKEN` (lifting GitHub from 60 req/hr to 5,000 req/hr and unlocking code/richer search) or `key=$STACKEXCHANGE_KEY` when present in env vars.
5. **Cache Management & Offline Re-Export (`widescrape cache stats|prune`, `widescrape export <run_id> --fields ...`):**
   `widescrape_cache.db` already reached 3.5 MB after a handful of local runs and currently grows forever. Adding cache pruning (`--older-than 30d`) and offline re-export/re-extraction against an existing `run_id` lets users tweak `--fields` or export formats without re-scraping the internet.
6. **Robots/Subtree-Aware Crawling (`--include`, `--exclude`, `--same-prefix`) + Sitemap Index Recursion:**
   Essential for documentation and site crawling: scoping `crawl_site` to `/docs/v2/*`, recursing `<sitemapindex>` child sitemaps in `map_site`, and offering a `--respect-robots` toggle prevents wasting crawl budgets on boilerplate marketing links.

---

## 9. Recommended Remediation Roadmap (Ordered by ROI)

1. **Phase 1 — Data Integrity, Security & Crashes (Fix First):**
   - Fix **C-03** (3-table SQLite schema + closing DB connections on Windows).
   - Fix **C-02** (Excel formula sanitization + `ILLEGAL_CHARACTERS_RE` stripping).
   - Fix **C-01** (`validate_public_url()` SSRF + redirect + Playwright route guard).
   - Fix **C-05** (non-destructive config error handling + `.bak` backups + atomic `modes.json` writes).
   - Fix **C-09** (`<untrusted_web_data>` nonce/escaping + `opacity:0` regex + `<template>` + table cells).
2. **Phase 2 — Core Accuracy & Reliability:**
   - Fix **C-04** (stop inventing `today` dates; parse snippet/X/JSON-LD dates; handle `--days` honestly).
   - Fix **C-06** (`(?-i:...)` promo codes, boundary-safe banks/seniority, `Key: Value` priority before heuristic field routing).
   - Fix **C-07** & **C-10** (adaptive dork budget + surface HTTP 403/429/5xx warnings + fix `crawl_site` `/llms.txt` & shared Chromium lifecycle).
   - Fix **N-01, N-02, N-03, N-04, N-08, N-10, N-12, N-13** (GitHub issues qualifier, arXiv `AND`, 2-char `core_terms`, `auto` word boundaries, Unicode tokens, merge-dedup, `--sites`/`--days 0`/`upsert_mode`, MCP validation & `max_chars`).
3. **Phase 3 — Packaging, Polish & Regression Test Suite:**
   - Fix **C-08** (declare `primp` in `pyproject.toml`; package `SKILL.md` + `references/` with `importlib.resources`).
   - Fix **N-18** immediately before Nov 28, 2026 (replace hardcoded `20 Nov 2026` test date; add `MockTransport` adapter tests, security tests, and Windows DB cleanup tests).
   - Address **N-05..N-17** and **L-01..L-10** polish items.
