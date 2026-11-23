# Advanced Setup, Modes & Architecture

Deep-dive setup for custom modes, AI agent MCP integration, local SearXNG, proxies, and development.

---

## 1. Custom Modes & Custom Websites

Save reusable search presets in `~/.widescrape/modes.json`:

```bash
# Create a custom mode with target websites, recency window, extractor, columns, and rank priority
uv run widescrape mode add uae-deals --base general --sites amazon.ae,noon.com --days 14 --extract codes --fields "code,discount,bank,deadline" --rank-fields "code,discount"

# Add or remove websites later (also works on built-in jobs/research/general)
uv run widescrape mode add-sites uae-deals pricena.ae
uv run widescrape mode remove-sites uae-deals noon.com
uv run widescrape mode list
uv run widescrape mode learned                 # View auto-discovered sites & synonyms learned across runs
uv run widescrape mode promote "nurse" healthcare-remote
```

### Dynamic Synonyms, Open-Web Discovery & Self-Learning

- **`--synonyms` / `--extra-queries` / `--exclude`:** Pass generous domain/role synonyms (`'{"term": ["syn1", "syn2"]}'` or comma list), additional adjacent search angles, and hard negative exclusions on any domain (tech, healthcare, legal, finance, deals, research, etc.).
- **`--discover` (default on):** When initial sources fall short of `--limit`, `WideScrape` searches the open web for specialized websites/directories, mines recommended portals, and generically extracts `JSON-LD`, `__NEXT_DATA__`/hydration state, RSS/ATS feeds, and Playwright XHR JSON payloads.
- **`--learn` / `--learn-mode`:** Automatically remembers productive discovered websites and validated synonyms in `~/.widescrape/learned_sites.json` (topic-scoped so unrelated topics stay isolated) or saves directly into a named custom mode in `~/.widescrape/modes.json`.

---

## 2. AI Agent & MCP Setup

One command installs `SKILL.md` + `references/` and registers the 7-tool MCP server (`widescrape_search`, `widescrape_read`, `widescrape_cache_find`, `widescrape_crawl`, `widescrape_map`, `widescrape_modes`, `widescrape_export`) for Antigravity, Claude Code, Codex, and Cursor. Intermediate MCP `search` and `crawl` calls write only to SQLite cache (`export=False` by default), and `widescrape_export` writes **one** consolidated `.md`/`.xlsx`/`.html` deliverable at the end of a task:

```bash
uv run widescrape install --with-mcp
```

Manual `mcp_config.json` / `.mcp.json` entry:

```json
{
  "mcpServers": {
    "widescrape": {
      "command": "uv",
      "args": [
        "run",
        "--no-sync",
        "--project",
        "/path/to/widescrape",
        "widescrape",
        "mcp"
      ]
    }
  }
}
```

---

## 3. Optional Backends & Auth

- **Local SearXNG** *(recommended for heavy `site:` dorking without public rate limits)*:
  ```bash
  docker compose up -d
  export WIDESCRAPE_SEARXNG_URL=http://localhost:8080   # Windows PowerShell: $env:WIDESCRAPE_SEARXNG_URL="http://localhost:8080"
  ```
- **HTTP/SOCKS Proxy & Optional API Tokens:** Pass `--proxy http://user:pass@host:port` or set `WIDESCRAPE_PROXY`. Optional `GITHUB_TOKEN` / `STACKEXCHANGE_KEY` raise upstream API limits.
- **Zero-Login LinkedIn Jobs:** LinkedIn Job Board listings and full descriptions run out of the box via LinkedIn's public `jobs-guest` endpoints (no login required).
- **Live X / LinkedIn Posts Feeds (Playwright):**
  ```bash
  uv run playwright install chromium
  uv run widescrape auth x
  uv run widescrape auth linkedin
  ```
- **Cache Maintenance & Offline Re-Export:** `uv run widescrape cache stats`, `cache prune --days 30`, and `export <run_id> --fields ... --rank-fields ...` keep `~/.widescrape/widescrape_cache.db` compact and rebuild exports offline.
- **Region, Strict Dates & Watch Diffing:** Pass `--region ae-en` for regional News/Web, `--strict-days` to drop undated pages under `--days`, or `--changed` to re-surface seen URLs only when content changes.

---

## 4. Architecture & Security

```mermaid
flowchart LR
    Q["Query + Synonyms + Mode"] --> E["Expander + Learned Recall"]
    E --> S["Async Adapters (LinkedIn, Boards, GitHub, HN, Reddit, News, arXiv, SE, Web)"]
    S --> D["Open-Web Site Discovery + Generic Extractors (JSON-LD, Hydration, XHR JSON, Feeds)"]
    D --> F["Noise & Exclude Filter + BM25 Ranker + --fetch-pages"]
    F --> X["Extractors (codes/jobs + --fields + --rank-fields) + Self-Learning Save"]
    X --> O["SQLite FTS5 + Top-20 Markdown + Excel (.xlsx) + Interactive HTML (.html)"]
```

- Strips HTML comments (`<!-- ... -->`) and CSS-hidden elements before Markdown conversion.
- Wraps full bodies inside `<untrusted_web_data>` delimiters in `widescrape read`.
- Routes internal library warnings to `~/.widescrape/logs/widescrape.log` (`RUST_LOG=off`).

---

## 5. Tests & Benchmarks

```bash
uv run --no-project --with pytest pytest -v
uv run --no-sync python benchmarks/eval_compression.py
```
