# WideScrape Domain Recipes & Operations

Load this reference when building custom workflows, debugging zero results, or configuring SearXNG/proxies/sessions.

## 1. Custom Modes & Custom Websites

Persist reusable workflows in `~/.widescrape/modes.json`:

```bash
# Create a reusable UAE shopping/coupons mode with auto-extraction and custom table columns
widescrape mode add uae-deals --base general --sites amazon.ae,noon.com --sources reddit,news,web --days 14 --extract codes --fields "code,discount,bank,deadline" --desc "UAE deals & coupons"

# Attach additional websites later (also works on built-in jobs/research/general)
widescrape mode add-sites uae-deals pricena.ae yallacompare.com

# Inspect or remove
widescrape mode list
widescrape mode show uae-deals
widescrape mode learned                     # View auto-discovered sites & synonyms learned across runs
widescrape mode promote nurse telehealth    # Promote learned topic into a reusable custom mode
widescrape search "white friday headphones" --mode uae-deals
```

## 2. Proven Query Recipes

- **Domain-agnostic ambitious search (any non-tech or niche field):**
  Pass generous `synonyms` + `extra_queries` + `exclude` so WideScrape searches broadly across terms, discovers specialized websites on the open web (`discover_sites=True`), and extracts `JSON-LD` / `__NEXT_DATA__` / feeds generically:
  `widescrape search "remote telehealth nurse" --mode jobs --synonyms '{"nurse":["registered nurse","RN","clinical reviewer","triage nurse"]}' --extra-queries "utilization review nurse remote,virtual care coordinator" --exclude "US only,5+ years" --learn-mode healthcare-remote`
- **Promo codes / live deals:**
  `mode="general"`, `days=14`, `extract="codes"`, `fields="code,discount,bank,deadline"`, `sources="reddit,news,web"`.
  Keep queries 3–5 focused keywords (e.g. `"amazon.ae white friday promo codes"`).
- **Hidden hiring, remote & no-experience / preferred-experience roles:**
  `mode="jobs"`, `days=30`, `fetch_pages=True`, `extract="jobs"`, `fields="company,salary,experience,location"`, `rank_fields="experience,salary"` (queries zero-login LinkedIn Jobs `/jobs-guest/` with automatic `f_E` experience & geo targeting plus up to 80 full descriptions, direct public remote boards via `boards` [Himalayas, Remotive, Jobicy, RemoteOK, WorkingNomads, WWR], LinkedIn Posts, X, Reddit, HN Who Is Hiring, Ashby, `boards`/`job-boards` Greenhouse, Lever, Workable, YC, Wellfound, plus open-web company career pages; captures preferred vs. required years and filters out `#opentowork`/`[FOR HIRE]` candidate noise).
- **Deep technical / engineering research:**
  `mode="research"`, `days=60` (targets GitHub, HN, StackExchange, arXiv, Reddit, X, LinkedIn, News).
- **Recurring monitoring / watch runs:**
  Run `search(..., new_only=True)` for strictly unseen URLs, or `search(..., changed=True)` to skip unchanged pages while surfacing updated ones. Add `strict_days=True` with `days` to drop undated pages, and `region="ae-en"` for regional News/Web localization.
- **Cache maintenance & offline re-export (CLI):** `widescrape cache stats` / `widescrape cache prune --days 30` / `widescrape export <run_id> --fields "company,salary,experience,deadline" --rank-fields "experience,salary"`.

## 3. Crawling Docs & PDFs

```bash
# Discover site URLs cheaply first (reads sitemap.xml, /llms.txt, page links)
widescrape map "https://example.com/docs" --filter auth --limit 50

# Crawl using DFS for deep doc hierarchies, with auto Playwright SPA fallback
widescrape crawl "https://example.com/docs/auth" --depth 2 --strategy dfs --max-pages 15
```

## 4. Environment Variables & Optional Backends

- `WIDESCRAPE_PROXY`: HTTP or SOCKS proxy passed across `primp`, `httpx`, `ddgs`, and Playwright.
- `WIDESCRAPE_SEARXNG_URL`: Local SearXNG URL (e.g. `http://localhost:8080`) for high-volume unthrottled dorking.
- `WIDESCRAPE_MODES_PATH`: Optional path override for `modes.json`.
- `GITHUB_TOKEN` / `STACKEXCHANGE_KEY`: Optional tokens to raise GitHub/StackExchange rate limits.
- **Zero-login LinkedIn Jobs vs. optional Playwright sessions:** LinkedIn Job Board (`linkedin_jobs`) works without login; use `widescrape auth x` / `widescrape auth linkedin` only for deep live X & LinkedIn Posts GraphQL feeds.
- **Health check:** `widescrape doctor` (logs warnings to `~/.widescrape/logs/widescrape.log`).
