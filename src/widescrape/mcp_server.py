"""Thin 7-tool MCP server exposing WideScrape with minimal context overhead."""

from __future__ import annotations

import json
from typing import Any

from widescrape.logging_setup import configure_quiet_logging

# Configure quiet logging before third-party imports so stdio transport stays clean.
configure_quiet_logging()

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # pragma: no cover - mcp < 2 fallback
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef]
from mcp.types import ToolAnnotations

from widescrape.adapters.web_dork import map_site
from widescrape.fetcher import format_untrusted_block
from widescrape.filter_rank import prune_text_bm25
from widescrape.modes import (
    add_sites_to_mode,
    clear_learned_topics,
    delete_mode,
    get_mode,
    list_all_modes,
    list_learned_topics,
    promote_learned_to_mode,
    remove_sites_from_mode,
    upsert_mode,
)
from widescrape.orchestrator import ScrapeRunResult, export_task_report, run_crawl, run_scrape
from widescrape.storage import ScrapeStorage

mcp = MCPServer("widescrape")


def _format_run_storage_line(result: ScrapeRunResult, exported: bool) -> str:
    """Format either exported file paths or a cache-only hint for MCP responses."""
    if exported and result.md_path is not None:
        return (
            f"- Run ID: `{result.run_id}` | Markdown: `{result.md_path}` | "
            f"Excel: `{result.xlsx_path}` | Interactive HTML: `{result.html_path}`"
        )
    return (
        f"- Run ID: `{result.run_id}` | Saved to cache only (no intermediate files written). "
        "Call `widescrape_export` once at the end of the task to build 1 `.md`, 1 `.xlsx`, and 1 `.html`."
    )


@mcp.tool(
    title="Search Multiple Web & Community Sources",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
)
async def widescrape_search(
    query: str,
    mode: str = "auto",
    sources: str = "",
    sites: str = "",
    limit: int = 100,
    limit_per_source: int | None = None,
    max_show: int = 20,
    days: int | None = None,
    strict_days: bool = False,
    extract: str = "",
    fields: str = "",
    rank_fields: str = "",
    synonyms: str = "",
    exclude: str = "",
    extra_queries: str = "",
    discover_sites: bool = True,
    learn: bool = True,
    learn_mode: str = "",
    new_only: bool = False,
    changed: bool = False,
    region: str = "",
    fetch_pages: bool = False,
    browser_fetch: bool = False,
    proxy: str = "",
    export: bool = False,
) -> str:
    """Scrape Reddit, X, LinkedIn, GitHub, HN, News, StackExchange, arXiv, and open-web targets.

    Use this for multi-source discovery across any domain (jobs, research, products/deals, health, legal,
    science, or general web). Be ambitious when supplying `synonyms` and `extra_queries`: include rich
    alternate titles, terminology, adjacent subfields, and unstated helpful angles that still obey the
    user's constraints, plus `exclude` for hard negative criteria.
    Prefer `widescrape_cache_find` first for already-scraped topics, and `widescrape_map`
    followed by `widescrape_crawl` when targeting one known site or documentation URL.

    Args:
        query: Focused search topic (3-5 keywords work best, e.g., "amazon.ae white friday codes").
        mode: Preset or custom mode ("auto", "jobs", "research", "general", or custom mode name).
        sources: Optional comma filter: "reddit,x,linkedin_posts,linkedin_jobs,linkedin,boards,github,hackernews,news,stackexchange,arxiv,web".
        sites: Optional comma-separated domains (e.g., "amazon.ae,noon.com,substack.com"); locks web search strictly to those sites.
        limit: Max results to KEEP AFTER filtering, deduplication, and ranking (clamped 1-500; default 100). Adapters automatically over-fetch and backfill so filters do not cut results short.
        limit_per_source: Legacy alias for `limit` (also applied post-filter if provided).
        max_show: Ranked summaries returned in tool output (default 20, max 200; 0 = all capped at 200). Full results save to SQLite, Markdown, Excel (.xlsx), and HTML viewer.
        days: Optional recency filter in days (None = inherit mode default, 0 = all dates / disable default, >0 = last N days).
        strict_days: If True when `days` > 0, drop undated items whose publication date could not be verified.
        extract: Structured extractor: "", "auto", "codes" (promo/bank/discounts), or "jobs" (salary/remote/level).
        fields: Optional comma-separated custom fields to extract into columns (e.g. "code,price,bank,deadline").
        rank_fields: Optional comma-separated priority fields for ranking (e.g. "salary,experience,location").
        synonyms: Ambitious domain/role synonyms as JSON dict (e.g. `'{"nurse": ["RN", "telehealth", "clinical reviewer"]}'`) or comma-separated phrases.
        exclude: Comma-separated negative terms/phrases to filter out early (e.g., "US only, unpaid, 5+ years").
        extra_queries: Comma/pipe-separated additional ambitious search angles related to the user's goal.
        discover_sites: If True (default when `sites` is empty), automatically search the open web for niche sites/boards & directories when needed to hit `limit`.
        learn: If True (default), remember high-yield discovered sites & validated synonyms in `~/.widescrape/learned_sites.json` for future runs.
        learn_mode: Optional custom mode slug to automatically create or enrich with learned websites and synonyms from this run.
        new_only: If True, skip canonical URLs already stored from previous runs.
        changed: If True, skip previously seen URLs whose content hash has not changed (watch mode).
        region: Optional locale/region code (e.g., "ae-en", "us-en", "GB").
        fetch_pages: Whether to fetch full public HTML/PDF/Markdown text for web/dork hits.
        browser_fetch: Whether to use headless Playwright (with passive JSON XHR capture) for JS-heavy pages.
        proxy: Optional HTTP or SOCKS proxy URL.
    """
    src_list = [s.strip().lower() for s in sources.split(",") if s.strip()] or None
    site_list = [s.strip().lower() for s in sites.split(",") if s.strip()] or None
    requested_limit = limit if limit_per_source is None else limit_per_source
    clamped_limit = max(1, min(requested_limit, 500))
    clamped_show = 200 if max_show == 0 else max(1, min(max_show, 200))
    try:
        result = await run_scrape(
            query=query,
            mode=mode,
            sources=src_list,
            sites=site_list,
            limit=clamped_limit,
            days=days,
            strict_days=strict_days,
            extract=extract,
            fields=fields,
            rank_fields=rank_fields,
            synonyms=synonyms or None,
            exclude=exclude or None,
            extra_queries=extra_queries or None,
            discover_sites=discover_sites,
            learn=learn,
            learn_mode=learn_mode,
            new_only=new_only,
            changed=changed,
            region=region,
            fetch_pages=fetch_pages,
            browser_fetch=browser_fetch,
            proxy=proxy or None,
            export=export,
        )
    except (ValueError, KeyError, RuntimeError) as exc:
        return f"Search configuration error: {exc}"

    total_kept = len(result.items)
    filtered_total = result.stats.get("filtered_total", total_kept)
    shown = result.items[:clamped_show]
    kept_summary = (
        f"Kept: {total_kept} post-filter ({filtered_total} matched of {result.stats['raw']} raw)"
        if filtered_total > total_kept
        else f"Kept: {total_kept} matching filter (from {result.stats['raw']} raw scraped)"
    )
    lines = [
        f'## WideScrape Results: "{query}"',
        f"- Mode: `{result.plan.mode}`"
        + (f" · Last {result.plan.days}d" if result.plan.days else "")
        + (f" · Extractor `{result.plan.extract_mode}`" if result.plan.extract_mode else "")
        + (f" · Fields `{','.join(result.plan.extract_fields)}`" if result.plan.extract_fields else "")
        + f" · {kept_summary} (showing top {len(shown)} · {result.stats['compression_pct']}% context saved)",
        _format_run_storage_line(result, export),
    ]
    if result.discovered_sites:
        lines.append(f"- Discovered sites: {', '.join(result.discovered_sites[:8])}")
    if result.learned_sites:
        lines.append(f"- Newly learned sites saved: {', '.join(result.learned_sites[:8])}")
    if result.stats.get("dropped_excluded"):
        lines.append(f"- Exclude filter: dropped {result.stats['dropped_excluded']} excluded items")
    if result.stats.get("skipped_seen"):
        lines.append(f"- New-only diff: skipped {result.stats['skipped_seen']} previously seen URLs")
    if result.stats.get("skipped_unchanged"):
        lines.append(
            f"- Watch diff: skipped {result.stats['skipped_unchanged']} unchanged URLs"
            + (f" ({result.stats.get('changed_items', 0)} updated)" if result.stats.get("changed_items") else "")
        )
    if result.adapter_errors:
        errs = ", ".join(f"{k} ({v})" for k, v in result.adapter_errors.items())
        lines.append(f"- Adapter warnings: {errs}")
    if total_kept == 0:
        lines.append("- Recovery hint: 0 kept results. Try relaxing `days`, removing restrictive `sites`, or broadening `query` terms.")
    else:
        lines.append("- Next: use `widescrape_read(item_ids=[...])` to read full text or `widescrape_cache_find` to query all saved items.")
    lines.append("")
    lines.extend(it.compact_summary() for it in shown)
    return "\n".join(lines)


@mcp.tool(
    title="Read Full Stored Scrape Items",
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
)
def widescrape_read(item_ids: list[str], focus: str = "", max_chars: int = 2500) -> str:
    """Retrieve stored full content for item IDs from prior search/crawl results.

    Use this after `widescrape_search`, `widescrape_crawl`, or `widescrape_cache_find`
    when a snippet is truncated (`...`) and you need exact text or numbers.
    """
    clamped_chars = max(100, min(max_chars, 50000))
    store = ScrapeStorage()
    items = store.get_items_by_ids(item_ids)
    if not items:
        return "No matching items found in local cache."
    blocks = []
    for it in items:
        raw_body = it.body or ""
        if focus:
            body = prune_text_bm25(raw_body, focus, max_chars=clamped_chars)
        elif len(raw_body) > clamped_chars:
            body = raw_body[:clamped_chars] + f"\n\n[Truncated at {clamped_chars} of {len(raw_body)} chars. Pass `focus` to BM25-prune relevant sections.]"
        else:
            body = raw_body
        blocks.append(
            f"### [{it.item_id}] {it.title}\n"
            f"- Source: `{it.source}` | Author: {it.author or 'unknown'} | Engagement: {it.engagement} | Date: {it.published_at or 'unknown'}\n"
            f"- URL: {it.url}\n"
            f"{format_untrusted_block(it.url, body)}"
        )
    return "\n\n".join(blocks)


@mcp.tool(
    title="Search Local SQLite Cache Offline",
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
)
def widescrape_cache_find(keywords: str, limit: int = 25) -> str:
    """Query previously scraped items in local SQLite FTS5 without making network requests.

    Call this before `widescrape_search` when revisiting a topic already scraped earlier.
    """
    clamped_limit = max(1, min(limit, 200))
    store = ScrapeStorage()
    items = store.search_cache(keywords, limit=clamped_limit)
    if not items:
        return f'No cached matches for "{keywords}". Run `widescrape_search` for live scraping.'
    lines = [f'## Cached Matches ({len(items)}) for "{keywords}"', ""]
    lines.extend(it.compact_summary() for it in items)
    return "\n".join(lines)


@mcp.tool(
    title="Crawl Specific Website, Docs, or PDF",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
)
async def widescrape_crawl(
    url: str,
    query: str = "",
    max_pages: int = 15,
    max_show: int = 20,
    depth: int = 1,
    strategy: str = "bfs",
    extract: str = "",
    fields: str = "",
    browser_fetch: bool = False,
    proxy: str = "",
    export: bool = False,
    limit: int | None = None,
) -> str:
    """Crawl a specific page, documentation subtree, or PDF (with `/llms.txt` fast-path).

    Prefer `widescrape_map` first on large sites to identify the right path before crawling.
    Use `widescrape_search` instead when researching across unknown domains and social platforms.
    Pass `limit` to cap pages AFTER filtering & ranking (crawl budget expands automatically if needed).
    Saves pages into SQLite cache by default; pass `export=True` only for standalone single-crawl export.
    """
    clamped_pages = max(1, min(max_pages, 100))
    clamped_depth = max(1, min(depth, 4))
    clamped_show = 200 if max_show == 0 else max(1, min(max_show, 200))
    clamped_limit = max(1, min(limit, 200)) if limit is not None else None
    try:
        result = await run_crawl(
            url=url,
            query=query,
            max_pages=clamped_pages,
            depth=clamped_depth,
            strategy=strategy,
            extract=extract,
            fields=fields,
            browser_fetch=browser_fetch,
            proxy=proxy or None,
            export=export,
            limit=clamped_limit,
        )
    except (ValueError, KeyError, RuntimeError) as exc:
        return f"Crawl configuration error: {exc}"

    shown = result.items[:clamped_show]
    lines = [
        f"## WideScrape Crawl: `{url}`",
        f"- Kept: {result.stats['kept']} pages (showing top {len(shown)} · {result.stats['compression_pct']}% context saved)",
        _format_run_storage_line(result, export),
    ]
    if not result.items:
        lines.append("- Recovery hint: 0 pages extracted. Try `browser_fetch=True` for JS-rendered SPAs or verify URL accessibility.")
    else:
        lines.append("- Next: use `widescrape_read(item_ids=[...])` to inspect full page text.")
    lines.append("")
    lines.extend(it.compact_summary() for it in shown)
    return "\n".join(lines)


@mcp.tool(
    title="Map Site URLs Before Crawling",
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True),
)
async def widescrape_map(url: str, filter_text: str = "", limit: int = 100, proxy: str = "") -> str:
    """Discover same-host URLs from sitemap.xml, `/llms.txt`, and links without scraping full bodies.

    Call this before `widescrape_crawl` when exploring unfamiliar docs or large sites.
    """
    clamped_limit = max(1, min(limit, 500))
    try:
        urls = await map_site(url, filter_text=filter_text, limit=clamped_limit, proxy=proxy or None)
    except (ValueError, RuntimeError) as exc:
        return f"Map error: {exc}"
    if not urls:
        return f"No URLs discovered at {url}"
    return f"## Mapped {len(urls)} URLs from `{url}`\n" + "\n".join(f"- {u}" for u in urls)


@mcp.tool(
    title="Manage Custom Search Modes & Websites",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
)
def widescrape_modes(
    action: str = "list",
    name: str = "",
    base_mode: str = "",
    sites: str = "",
    sources: str = "",
    synonyms: str = "",
    exclude_keywords: str = "",
    target_mode: str = "",
    days: int | None = None,
    extract: str = "",
    fields: str = "",
    rank_fields: str = "",
    signal_keywords: str = "",
    boost_keywords: str = "",
    penalty_keywords: str = "",
    description: str = "",
) -> str:
    """Create, list, update, delete, or inspect self-learned WideScrape modes and websites.

    Args:
        action: One of "list", "show", "add", "add_sites", "remove_sites", "delete", "learned", "clear_learned", "promote".
        name: Mode slug (e.g., "uae-deals", "security-research", built-in "jobs"), or topic filter/key for learned/promote actions.
        base_mode: Base preset for new modes ("general", "jobs", or "research"; empty preserves existing).
        sites: Comma-separated websites (e.g., "amazon.ae,noon.com,pricena.ae").
        sources: Optional comma-separated default sources for this mode.
        synonyms: Optional comma-separated ambitious domain synonyms stored on this mode.
        exclude_keywords: Optional comma-separated negative terms excluded automatically in this mode.
        target_mode: Destination custom mode name when action="promote".
        days: Optional default recency window in days (0 disables default, None preserves existing).
        extract: Optional default extractor ("", "auto", "codes", "jobs").
        fields: Optional comma-separated custom fields to extract by default.
        rank_fields: Optional comma-separated priority fields to rank by default.
        signal_keywords: Optional comma-separated signal keywords to boost matching hits.
        boost_keywords: Optional comma-separated high-priority intent keywords.
        penalty_keywords: Optional comma-separated spam/noise keywords to downrank.
        description: Short description for the mode.
    """
    act = (action or "list").strip().lower()
    site_list = [s.strip() for s in sites.split(",") if s.strip()]
    src_list = [s.strip() for s in sources.split(",") if s.strip()] if sources else None
    syn_list = [s.strip() for s in synonyms.split(",") if s.strip()] if synonyms else None
    exc_list = [s.strip() for s in exclude_keywords.split(",") if s.strip()] if exclude_keywords else None
    sig_list = [s.strip() for s in signal_keywords.split(",") if s.strip()] if signal_keywords else None
    bst_list = [s.strip() for s in boost_keywords.split(",") if s.strip()] if boost_keywords else None
    pen_list = [s.strip() for s in penalty_keywords.split(",") if s.strip()] if penalty_keywords else None

    try:
        if act == "list":
            rows = list_all_modes()
            lines = ["## WideScrape Modes"]
            for r in rows:
                sites_str = f" · sites: {', '.join(r['sites'])}" if r["sites"] else ""
                fields_str = f" · fields: {', '.join(r['default_fields'])}" if r.get("default_fields") else ""
                lines.append(f"- **`{r['name']}`** ({r['kind']}) — {r['description']}{sites_str}{fields_str}")
            return "\n".join(lines)
        if act == "learned":
            topics = list_learned_topics()
            if not topics:
                return "No self-learned websites/topics recorded yet."
            lines = ["## Self-Learned Topic & Website Memory"]
            for t in topics:
                sites_txt = ", ".join(t["top_sites"]) or "none"
                syn_txt = f" · synonyms: {', '.join(t['synonyms'][:6])}" if t["synonyms"] else ""
                lines.append(f"- **`{t['key']}`** (runs={t['runs']}, updated={t['updated_at']}) · sites: {sites_txt}{syn_txt}")
            return "\n".join(lines)
        if act == "clear_learned":
            removed = clear_learned_topics(name)
            return f"Cleared {removed} learned topic bucket(s)."
        if not name:
            return f"`name` is required for action='{act}'."
        if act == "promote":
            dest = target_mode.strip() or name
            cfg = promote_learned_to_mode(name, dest)
            return f"Promoted learned topic `{name}` into mode `{cfg.name}` (sites={cfg.sites}, synonyms={cfg.synonyms})."
        if act == "show":
            cfg = get_mode(name)
            if cfg is None:
                return f"No custom config stored for `{name}`."
            return (
                f"## Mode `{cfg.name}`\n"
                f"- Base: `{cfg.base_mode}`\n"
                f"- Sites: {', '.join(cfg.sites) or 'none'}\n"
                f"- Sources: {', '.join(cfg.sources) or 'default'}\n"
                f"- Synonyms: {', '.join(cfg.synonyms) or 'none'}\n"
                f"- Exclude keywords: {', '.join(cfg.exclude_keywords) or 'none'}\n"
                f"- Default days: {cfg.default_days or 'all'} | Extractor: `{cfg.default_extract or 'none'}`"
                + (f" | Fields: `{','.join(cfg.default_fields)}`" if cfg.default_fields else "")
                + (f" | Rank fields: `{','.join(cfg.default_rank_fields)}`" if cfg.default_rank_fields else "")
            )
        if act == "add":
            cfg = upsert_mode(
                name=name,
                base_mode=base_mode.strip() or None,
                description=description,
                sources=src_list,
                sites=site_list if sites else None,
                synonyms=syn_list,
                exclude_keywords=exc_list,
                signal_keywords=sig_list,
                boost_keywords=bst_list,
                penalty_keywords=pen_list,
                default_days=days,
                default_extract=extract,
                default_fields=[f.strip() for f in fields.split(",") if f.strip()] if fields else None,
                default_rank_fields=[f.strip() for f in rank_fields.split(",") if f.strip()] if rank_fields else None,
            )
            return f"Saved mode `{cfg.name}` (base=`{cfg.base_mode}`, sites={cfg.sites}, sources={cfg.sources or 'default'})."
        if act == "add_sites":
            cfg = add_sites_to_mode(name, site_list)
            return f"Updated `{cfg.name}` sites: {', '.join(cfg.sites)}"
        if act == "remove_sites":
            cfg = remove_sites_from_mode(name, site_list)
            return f"Updated `{cfg.name}` sites: {', '.join(cfg.sites) or 'none'}"
        if act == "delete":
            removed = delete_mode(name)
            return f"Deleted custom mode `{name}`." if removed else f"Custom mode `{name}` was not found."
        return f"Unknown action '{action}'. Use list, show, add, add_sites, remove_sites, delete, learned, clear_learned, or promote."
    except (ValueError, KeyError, RuntimeError) as exc:
        return f"Mode error: {exc}"


@mcp.tool(
    title="Export One Consolidated Task Report",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
)
def widescrape_export(
    title: str,
    run_ids: str = "",
    item_ids: str = "",
    entries: list[dict[str, Any]] | None = None,
    entries_json: str = "",
    mode: str = "general",
    extract: str = "",
    fields: str = "",
    rank_fields: str = "",
    report_name: str = "",
) -> str:
    """Export ONE consolidated Markdown, ONE Excel (.xlsx), and ONE HTML file for a completed task.

    Call this once at the end of a multi-step research/search task instead of exporting every
    intermediate search or crawl. Accepts curated `entries` (dicts with title, url, source,
    published_at, highlight, and any custom columns or `fields` dict), cached `item_ids`
    (comma-separated), saved `run_ids` (comma-separated), or any combination of the three.
    """
    parsed_entries: list[dict[str, Any]] = list(entries or [])
    if entries_json.strip():
        try:
            loaded = json.loads(entries_json)
        except json.JSONDecodeError as exc:
            return f"Export error: invalid entries_json ({exc})."
        if not isinstance(loaded, list):
            return "Export error: entries_json must decode to a JSON array of objects."
        parsed_entries.extend(item for item in loaded if isinstance(item, dict))

    try:
        result = export_task_report(
            title=title,
            run_ids=run_ids,
            item_ids=item_ids,
            entries=parsed_entries,
            mode=mode,
            extract=extract,
            fields=fields,
            rank_fields=rank_fields,
            report_name=report_name,
        )
    except (ValueError, KeyError, RuntimeError) as exc:
        return f"Export error: {exc}"

    return "\n".join(
        [
            f'## WideScrape Consolidated Task Export: "{title}"',
            f"- Consolidated Items: {len(result.items)} | Mode: `{result.plan.mode}` | Task Run ID: `{result.run_id}`",
            f"- Markdown: `{result.md_path}`",
            f"- Excel: `{result.xlsx_path}`",
            f"- Interactive HTML: `{result.html_path}`",
        ]
    )


def run_mcp_server() -> None:
    """Launch stdio MCP server."""
    mcp.run()
