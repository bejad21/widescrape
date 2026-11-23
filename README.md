# WideScrape

> Free, zero-API-key web scraper & context-funnel for humans and AI agents (CLI + MCP).

Scrapes **Reddit, X, LinkedIn, GitHub, Hacker News, Google News, StackExchange, arXiv, PDFs, and custom sites**, filters spam/noise, highlights the exact query-matched sentences, exports **Markdown (`.md`) + Excel (`.xlsx`) + interactive HTML table viewer (`.html`)**, and keeps AI context small.

---

## Requirements

- **Python 3.11+**
- **[`uv`](https://docs.astral.sh/uv/)** *(recommended package manager — or standard `pip`)*
- *Optional:* Chromium (`playwright install chromium`) for JS-heavy pages; Docker for local SearXNG

---

## Setup (1 Minute)

```bash
git clone https://github.com/bejad21/widescrape.git && cd widescrape
uv sync
uv run widescrape install --with-mcp   # Register SKILL.md + MCP server (Antigravity, Claude, Codex, Cursor)
uv run widescrape doctor               # Verify dependencies and live search
```

*(Prefer `pip`? Run `pip install -e .` and replace `uv run widescrape` with `widescrape`.)*

---

## Quick Start

```bash
# 1. Search across platforms and extract structured fields/columns automatically
uv run widescrape search "amazon.ae white friday promo codes" --days 14 --fields "code,discount,bank,deadline"

# 2. Live remote job hunt (zero-login LinkedIn Jobs + remote job boards + ATS boards + full-page JSON-LD + custom rank priority)
uv run widescrape search "remote python developer no experience" --mode jobs --fetch-pages --fields "company,salary,experience,location" --rank-fields "experience,salary"

# 3. Crawl a docs site or PDF directly (checks /llms.txt first)
uv run widescrape crawl "https://modelcontextprotocol.io/introduction" --depth 2 --max-pages 15
```

### Example Output

```markdown
## WideScrape Results: "amazon.ae white friday promo codes"
- **Kept:** 33 unique results (from 47 scraped · mode `uae-deals` · last `14`d · extract `codes`)

1. **[Amazon UAE Promo Codes | 25% Off](https://couponfollow.com/site/amazon.ae)** · `web` · score `2.07` · `2026-11-20`
   > Save up to 25% with current Amazon UAE coupons for November 2026...

---
**Showing top 5 of 33 ranked results** (Run ID: `c97cb190ef`)
- **Full Markdown report:** `~/.widescrape/exports/c97cb190ef_amazon_ae_white_friday_promo_codes.md`
- **Excel workbook (.xlsx):** `~/.widescrape/exports/c97cb190ef_amazon_ae_white_friday_promo_codes.xlsx`
- **Interactive table viewer (.html):** `~/.widescrape/exports/c97cb190ef_amazon_ae_white_friday_promo_codes.html`
```

---

## Commands & Key Flags

| Command / Flag | What it does |
| :--- | :--- |
| `search "<query>"` | Multi-source scrape -> shows top 20, saves cache (`--no-export` skips per-run files) |
| `show <run_id> --offset 20` | View remaining results from any saved run |
| `export <run_id>` / `--run-ids ...` | Rebuild 1 run or consolidate multiple runs/items into 1 `.md`, `.xlsx`, `.html` |
| `read <id...> --focus "<topic>"` | Read full body from SQLite (BM25-pruned to topic) |
| `find "<keywords>"` | Instant offline FTS5 search over previously scraped items |
| `map <url>` / `crawl <url>` | Discover URLs (`sitemap.xml`, `/llms.txt`) or crawl pages/PDFs |
| `mode add` / `add-sites` / `learned` / `promote` | Manage custom modes and promote self-learned websites |
| `--synonyms` / `--exclude` / `--extra-queries` | Agent-supplied domain synonyms, exclusions & ambitious search angles |
| `--discover` / `--learn` / `--learn-mode SLUG` | Auto-discover new sites via open web and persist high-yield sources |
| `--days N` / `--strict-days` | Keep last `N` days (`0` disables default); `--strict-days` drops undated pages |
| `--extract auto \| codes \| jobs` | Pull promo codes/cards or salaries/remote tags |
| `--fields "code,price,deadline"` | Extract any custom columns straight into `.md`, `.xlsx`, and `.html` |
| `--rank-fields "salary,experience"` | Choose which extracted fields weigh highest in ranking |
| `--fetch-pages` | Open top 15 web pages directly for full text, `JSON-LD`, and real dates |
| `--new-only` / `--changed` | Skip seen URLs (`--new-only`), or keep only changed content (`--changed`) |
| `--region CODE` | Localize Web/News search (e.g. `ae-en`, `us-en`, `GB`) |
| `cache stats` / `cache prune` | Check SQLite size or prune runs older than N days |
| `--sources` / `--sites` | Filter sources (`reddit,x,linkedin,boards,github,hackernews,news,stackexchange,arxiv,web`) or lock strictly to given domains |
| `--limit N` | Max results kept **after** filtering, dedup & ranking (auto over-fetches & backfills) |
| `-q, --quiet` / `--json` | Minimal summary + export paths, or machine-readable JSON |

---

## Where Files Are Saved

- **Reports:** `~/.widescrape/exports/` (interactive **`.html`** table viewer, Excel **`.xlsx`**, and **`.md`**)
- **Local Cache:** `~/.widescrape/widescrape_cache.db` (SQLite + FTS5)
- **Custom Modes & Logs:** `~/.widescrape/modes.json` and `~/.widescrape/logs/widescrape.log`

---

## More Docs

- **Agent & MCP Setup (Antigravity, Claude Code, Codex, Cursor, VS Code, Windsurf):** [docs/agent-setup.md](./docs/agent-setup.md)
- **Custom Modes, MCP Config, SearXNG, Proxies & Tests:** [docs/advanced.md](./docs/advanced.md)
- **AI Agent Instructions:** [SKILL.md](./SKILL.md)
