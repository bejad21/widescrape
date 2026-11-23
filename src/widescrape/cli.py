"""CLI interface for WideScrape (clean Markdown-first output for humans and AI agents)."""

from __future__ import annotations

from widescrape.logging_setup import configure_quiet_logging

# Configure quiet logging before third-party imports (primp reads RUST_LOG at import).
configure_quiet_logging()

import asyncio
import json
import sys
from typing import Optional

import typer

# Prevent cp1252 UnicodeEncodeError when piping symbols or scraped text on Windows consoles.
for _stream in (sys.stdout, sys.stderr):
    _reconfig = getattr(_stream, "reconfigure", None)
    if callable(_reconfig):
        try:
            _reconfig(encoding="utf-8", errors="replace")
        except Exception:
            pass

from widescrape.adapters.browser_social import capture_auth_session, has_saved_session
from widescrape.adapters.web_dork import map_site
from widescrape.extractor import format_extracted_text
from widescrape.fetcher import format_untrusted_block
from widescrape.filter_rank import prune_text_bm25
from widescrape.installer import collect_diagnostics, install_agent_skills
from widescrape.mcp_server import run_mcp_server
from widescrape.models import ScrapeItem, format_md_link
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
from widescrape.orchestrator import (
    ScrapeRunResult,
    export_saved_run,
    export_task_report,
    run_crawl,
    run_scrape,
)
from widescrape.storage import ScrapeStorage

app = typer.Typer(help="WideScrape: multi-source scraper & context-funnel for AI agents.")
mode_app = typer.Typer(help="Create custom modes and attach custom websites to any mode.")
cache_app = typer.Typer(help="Inspect and prune the local SQLite scrape cache.")
app.add_typer(mode_app, name="mode")
app.add_typer(cache_app, name="cache")


def _format_human_result(rank: int, it: ScrapeItem) -> str:
    """Format a single result cleanly as Markdown for humans and AI UIs."""
    title = " ".join(it.title.split())[:85]
    highlight = it.metadata.get("highlight") if isinstance(it.metadata, dict) else None
    gist = " ".join(str(highlight or it.body or it.title).split())[:160]
    ext_txt = format_extracted_text(it)
    ext_badge = f" · _{ext_txt}_" if ext_txt else ""
    date_part = f" · `{it.published_at[:10]}`" if it.published_at else ""
    return (
        f"{rank}. **{format_md_link(title, it.url)}** · `{it.source}` · score `{it.score:.2f}`{date_part} · ID `{it.item_id}`{ext_badge}\n"
        f"   > {gist}"
    )


def _format_sources_summary(adapter_counts: dict[str, int]) -> str:
    active = [f"{k} ({v})" for k, v in adapter_counts.items() if v > 0]
    return ", ".join(active) if active else "none"


def _print_access_footer(
    run_id: str,
    shown_count: int,
    total_kept: int,
    md_path: object,
    xlsx_path: object,
    html_path: object = None,
) -> None:
    """Clearly tell the user/agent how many items were shown vs. saved and how to access the rest."""
    typer.echo(f"\n---\n**Showing top {shown_count} of {total_kept} ranked results** (Run ID: `{run_id}`)")
    if md_path:
        typer.echo(f"- **Full Markdown report:** `{md_path}`")
    if xlsx_path:
        typer.echo(f"- **Excel workbook (.xlsx):** `{xlsx_path}`")
    if html_path:
        typer.echo(f"- **Interactive table viewer (.html):** `{html_path}`")
    if total_kept > shown_count:
        typer.echo(f"- **Next page:** `widescrape show {run_id} --offset {shown_count}`")
    typer.echo('- **Search saved results:** `widescrape find "<keywords>"` · **Read full item:** `widescrape read <item_id>`')


def _run_json_payload(result: ScrapeRunResult, items: list[ScrapeItem]) -> str:
    """Serialize search or crawl results cleanly for `--json` callers."""
    payload = {
        "run_id": result.run_id,
        "mode": result.plan.mode,
        "days": result.plan.days,
        "extract_mode": result.plan.extract_mode,
        "extract_fields": list(result.plan.extract_fields),
        "shown": len(items),
        "total_kept": len(result.items),
        "md_report": str(result.md_path) if result.md_path else None,
        "xlsx_report": str(result.xlsx_path) if result.xlsx_path else None,
        "html_report": str(result.html_path) if result.html_path else None,
        "stats": result.stats,
        "discovered_sites": result.discovered_sites,
        "learned_sites": result.learned_sites,
        "adapter_counts": result.adapter_counts,
        "adapter_errors": result.adapter_errors,
        "items": [
            {
                "id": it.item_id,
                "score": it.score,
                "source": it.source,
                "engagement": it.engagement,
                "published_at": it.published_at,
                "title": it.title,
                "extracted": it.metadata.get("extracted") if isinstance(it.metadata, dict) else None,
                "fields": it.metadata.get("extracted_fields") if isinstance(it.metadata, dict) else None,
                "highlight": (it.metadata.get("highlight") if isinstance(it.metadata, dict) else None)
                or " ".join((it.body or it.title).split())[:160],
                "url": it.url,
            }
            for it in items
        ],
    }
    return json.dumps(payload, indent=2)


@app.command()
def search(
    query: str = typer.Argument(..., help="What you want to find across the web."),
    mode: str = typer.Option("auto", help="Preset or custom mode (auto, jobs, research, general, or custom name)."),
    sources: Optional[str] = typer.Option(
        None,
        help="Comma-separated sources: reddit,x,linkedin_posts,linkedin_jobs,linkedin,boards,github,hackernews,news,stackexchange,arxiv,web",
    ),
    sites: Optional[str] = typer.Option(
        None,
        help="Optional comma-separated domains to target (e.g. amazon.ae,substack.com,dev.to).",
    ),
    limit: int = typer.Option(100, help="Max results to KEEP AFTER filtering, deduplication, and ranking."),
    days: Optional[int] = typer.Option(None, help="Only include results from the last N days (0 disables mode default)."),
    strict_days: bool = typer.Option(False, "--strict-days", help="Also drop undated pages when --days is active."),
    extract: str = typer.Option("", help="Structured extractor: '', 'auto', 'codes', or 'jobs'."),
    fields: str = typer.Option("", help="Custom schema fields to extract (e.g. 'code,price,bank,deadline')."),
    rank_fields: str = typer.Option("", help="Priority fields to boost in ranking (e.g. 'salary,experience,location')."),
    synonyms: str = typer.Option("", help="Ambitious domain synonyms (JSON dict/list or comma-separated phrases)."),
    exclude: str = typer.Option("", help="Comma-separated negative terms/phrases to filter out early."),
    extra_queries: str = typer.Option("", help="Additional ambitious search angles (comma or pipe separated)."),
    discover: bool = typer.Option(True, "--discover/--no-discover", help="Auto-discover niche sites via open web when needed to hit --limit."),
    learn: bool = typer.Option(True, "--learn/--no-learn", help="Remember high-yield discovered sites & synonyms for future runs."),
    learn_mode: str = typer.Option("", help="Optional custom mode slug to create/enrich with learned sites & synonyms."),
    new_only: bool = typer.Option(False, "--new-only", help="Skip URLs already seen in previous runs."),
    changed: bool = typer.Option(False, "--changed", help="Skip unchanged seen URLs; keep new or updated content."),
    region: str = typer.Option("", help="Locale/region code for Web/News (e.g. 'ae-en', 'us-en', 'GB')."),
    fetch_pages: bool = typer.Option(False, help="Fetch full HTML/PDF/Markdown text for dork results."),
    browser_fetch: bool = typer.Option(False, help="Use headless Playwright fallback for JS-rendered pages."),
    proxy: Optional[str] = typer.Option(None, help="Optional HTTP/SOCKS proxy URL (or set WIDESCRAPE_PROXY)."),
    export: bool = typer.Option(True, "--export/--no-export", help="Write .md/.xlsx/.html files for this run (--no-export keeps cache only for multi-step tasks)."),
    max_show: int = typer.Option(20, help="Ranked results to display (0 = show all; full list is always saved)."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print only summary counts and export file paths."),
    as_json: bool = typer.Option(False, "--json", help="Output compact JSON instead of Markdown."),
) -> None:
    """Scrape multiple sources, remove noise, rank best-first, and display top results cleanly."""
    src_list = [s.strip().lower() for s in sources.split(",") if s.strip()] if sources else None
    site_list = [s.strip().lower() for s in sites.split(",") if s.strip()] if sites else None
    try:
        result = asyncio.run(
            run_scrape(
                query=query,
                mode=mode,
                sources=src_list,
                sites=site_list,
                limit=limit,
                days=days,
                strict_days=strict_days,
                extract=extract,
                fields=fields,
                rank_fields=rank_fields,
                synonyms=synonyms or None,
                exclude=exclude or None,
                extra_queries=extra_queries or None,
                discover_sites=discover,
                learn=learn,
                learn_mode=learn_mode,
                new_only=new_only,
                changed=changed,
                region=region,
                fetch_pages=fetch_pages,
                browser_fetch=browser_fetch,
                proxy=proxy,
                export=export,
            )
        )
    except ValueError as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(code=1)

    total_kept = len(result.items)
    items = result.items[:max_show] if max_show > 0 else result.items

    if as_json:
        typer.echo(_run_json_payload(result, items))
        return

    if quiet:
        typer.echo(f"Found {total_kept} unique results from {result.stats['raw']} scraped (run_id={result.run_id}).")
        if result.md_path:
            typer.echo(f"Markdown: {result.md_path}")
            typer.echo(f"Excel:    {result.xlsx_path}")
            typer.echo(f"Viewer:   {result.html_path}")
        else:
            typer.echo("Cache only (--no-export). Run `widescrape export` once at the end to export consolidated files.")
        if result.adapter_errors:
            errs = ", ".join(f"{k} ({v})" for k, v in result.adapter_errors.items())
            typer.echo(f"Warnings: {errs}")
        return

    typer.echo(f'## WideScrape Results: "{query}"')
    mode_meta = f"mode `{result.plan.mode}`"
    if result.plan.days:
        mode_meta += f" · last `{result.plan.days}`d"
    if result.plan.extract_mode:
        mode_meta += f" · extract `{result.plan.extract_mode}`"
    if result.plan.extract_fields:
        mode_meta += f" · fields `{','.join(result.plan.extract_fields)}`"
    typer.echo(
        f"- **Kept:** {total_kept} unique results (from {result.stats['raw']} scraped · "
        f"{result.stats['compression_pct']}% context saved · {mode_meta})"
    )
    typer.echo(f"- **Active Sources:** {_format_sources_summary(result.adapter_counts)}")
    if result.discovered_sites:
        typer.echo(f"- **Discovered Sites:** {', '.join(result.discovered_sites[:8])}")
    if result.learned_sites:
        typer.echo(f"- **Newly Learned Sites:** {', '.join(result.learned_sites[:8])}")
    if result.stats.get("skipped_seen"):
        typer.echo(f"- **New-Only Diff:** skipped `{result.stats['skipped_seen']}` previously seen URLs")
    if result.stats.get("skipped_unchanged"):
        typer.echo(
            f"- **Watch Diff:** skipped `{result.stats['skipped_unchanged']}` unchanged URLs"
            + (f" (`{result.stats.get('changed_items', 0)}` updated)" if result.stats.get("changed_items") else "")
        )
    if result.adapter_errors:
        errs = ", ".join(f"{k} ({v})" for k, v in result.adapter_errors.items())
        typer.echo(f"- **Adapter Warnings:** {errs}")
    typer.echo("")
    for idx, it in enumerate(items, start=1):
        typer.echo(_format_human_result(idx, it))
    _print_access_footer(result.run_id, len(items), total_kept, result.md_path, result.xlsx_path, result.html_path)


@mode_app.command("list")
def mode_list() -> None:
    """List all built-in and custom modes with their configured websites."""
    typer.echo("## WideScrape Modes")
    for row in list_all_modes():
        extras = []
        if row["sites"]:
            extras.append(f"sites: `{', '.join(row['sites'])}`")
        if row["sources"]:
            extras.append(f"sources: `{', '.join(row['sources'])}`")
        if row["default_days"]:
            extras.append(f"days: `{row['default_days']}`")
        if row["default_extract"]:
            extras.append(f"extract: `{row['default_extract']}`")
        if row.get("default_fields"):
            extras.append(f"fields: `{', '.join(row['default_fields'])}`")
        extra_str = f" · {' · '.join(extras)}" if extras else ""
        typer.echo(f"- **`{row['name']}`** ({row['kind']}) — {row['description']}{extra_str}")


@mode_app.command("show")
def mode_show(name: str = typer.Argument(..., help="Mode name to inspect.")) -> None:
    """Show full configuration for a custom mode."""
    cfg = get_mode(name)
    if cfg is None:
        typer.echo(f"No custom configuration stored for mode `{name}`.")
        raise typer.Exit(code=1)
    typer.echo(f"## Mode `{cfg.name}`")
    typer.echo(f"- **Base preset:** `{cfg.base_mode}`")
    typer.echo(f"- **Description:** {cfg.description or '(none)'}")
    typer.echo(f"- **Custom websites:** {', '.join(cfg.sites) or '(none)'}")
    typer.echo(f"- **Default sources:** {', '.join(cfg.sources) or '(default)'}")
    typer.echo(f"- **Signal keywords:** {', '.join(cfg.signal_keywords) or '(none)'}")
    typer.echo(f"- **Boost keywords:** {', '.join(cfg.boost_keywords) or '(none)'}")
    typer.echo(f"- **Penalty keywords:** {', '.join(cfg.penalty_keywords) or '(none)'}")
    typer.echo(
        f"- **Default days:** {cfg.default_days or 'all'} · **Extractor:** `{cfg.default_extract or 'none'}`"
        + (f" · **Default fields:** `{','.join(cfg.default_fields)}`" if cfg.default_fields else "")
    )


@mode_app.command("add")
def mode_add(
    name: str = typer.Argument(..., help="Custom mode name (e.g. uae-deals, security, ecommerce)."),
    base: Optional[str] = typer.Option(None, help="Base built-in behavior: general, jobs, or research (omit to keep existing)."),
    sites: Optional[str] = typer.Option(None, help="Comma-separated websites to target (e.g. amazon.ae,noon.com)."),
    sources: Optional[str] = typer.Option(None, help="Comma-separated default sources for this mode."),
    days: Optional[int] = typer.Option(None, help="Default recency window in days."),
    extract: str = typer.Option("", help="Default extractor ('', 'auto', 'codes', 'jobs')."),
    fields: str = typer.Option("", help="Default custom schema fields (e.g. 'code,price,deadline')."),
    rank_fields: str = typer.Option("", help="Default priority fields to rank by (e.g. 'salary,experience')."),
    signals: Optional[str] = typer.Option(None, help="Comma-separated ranking signal keywords."),
    boost: Optional[str] = typer.Option(None, help="Comma-separated score boost keywords."),
    penalty: Optional[str] = typer.Option(None, help="Comma-separated penalty keywords."),
    desc: str = typer.Option("", help="Short description of this mode."),
) -> None:
    """Create or update a custom mode with custom websites, sources, and ranking keywords."""
    try:
        cfg = upsert_mode(
            name=name,
            base_mode=base,
            description=desc,
            sources=[s.strip() for s in sources.split(",") if s.strip()] if sources is not None else None,
            sites=[s.strip() for s in sites.split(",") if s.strip()] if sites is not None else None,
            signal_keywords=[s.strip() for s in signals.split(",") if s.strip()] if signals is not None else None,
            boost_keywords=[s.strip() for s in boost.split(",") if s.strip()] if boost is not None else None,
            penalty_keywords=[s.strip() for s in penalty.split(",") if s.strip()] if penalty is not None else None,
            default_days=days,
            default_extract=extract,
            default_fields=[f.strip() for f in fields.split(",") if f.strip()] if fields else None,
            default_rank_fields=[f.strip() for f in rank_fields.split(",") if f.strip()] if rank_fields else None,
        )
    except ValueError as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(code=1)
    typer.echo(f"Saved mode `{cfg.name}` (base=`{cfg.base_mode}`, sites={cfg.sites}, sources={cfg.sources or 'default'}).")


@mode_app.command("add-sites")
def mode_add_sites(
    name: str = typer.Argument(..., help="Target mode name (creates it if new, or extends existing/built-in)."),
    sites: list[str] = typer.Argument(..., help="One or more website domains (e.g. amazon.ae noon.com)."),
) -> None:
    """Add custom websites to an existing or new mode."""
    try:
        cfg = add_sites_to_mode(name, sites)
    except ValueError as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(code=1)
    typer.echo(f"Updated `{cfg.name}` sites: {', '.join(cfg.sites)}")


@mode_app.command("remove-sites")
def mode_remove_sites(
    name: str = typer.Argument(..., help="Mode name."),
    sites: list[str] = typer.Argument(..., help="One or more domains to remove."),
) -> None:
    """Remove custom websites from a mode."""
    try:
        cfg = remove_sites_from_mode(name, sites)
    except (KeyError, ValueError) as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(code=1)
    typer.echo(f"Updated `{cfg.name}` sites: {', '.join(cfg.sites) or '(none)'}")


@mode_app.command("delete")
def mode_delete(name: str = typer.Argument(..., help="Custom mode name to delete.")) -> None:
    """Delete a custom mode (or remove a custom override on a built-in mode)."""
    try:
        removed = delete_mode(name)
    except ValueError as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(code=1)
    if not removed:
        typer.echo(f"Custom mode `{name}` not found.")
        raise typer.Exit(code=1)
    typer.echo(f"Deleted custom mode `{name}`.")


@mode_app.command("learned")
def mode_learned() -> None:
    """List self-learned websites and validated synonyms across topics."""
    topics = list_learned_topics()
    if not topics:
        typer.echo("No self-learned websites/topics recorded yet.")
        return
    typer.echo("## Self-Learned Topic & Website Memory")
    for t in topics:
        sites_txt = ", ".join(t["top_sites"]) or "(none)"
        syn_txt = f" · synonyms: `{', '.join(t['synonyms'][:6])}`" if t["synonyms"] else ""
        typer.echo(f"- **`{t['key']}`** (runs={t['runs']}, updated={t['updated_at']}) · sites: {sites_txt}{syn_txt}")


@mode_app.command("promote")
def mode_promote(
    topic_key: str = typer.Argument(..., help="Learned topic key or matching keyword substring."),
    target_mode: str = typer.Argument(..., help="Custom mode name to create or enrich."),
) -> None:
    """Promote learned websites and synonyms for a topic into a permanent custom mode."""
    try:
        cfg = promote_learned_to_mode(topic_key, target_mode)
    except (KeyError, ValueError) as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(code=1)
    typer.echo(f"Promoted `{topic_key}` into mode `{cfg.name}` (sites={cfg.sites}, synonyms={cfg.synonyms}).")


@mode_app.command("clear-learned")
def mode_clear_learned(topic: str = typer.Option("", help="Optional topic filter (empty clears all).")) -> None:
    """Clear stored self-learned topic memory."""
    removed = clear_learned_topics(topic)
    typer.echo(f"Cleared {removed} learned topic bucket(s).")


@app.command()
def read(
    item_ids: list[str] = typer.Argument(..., help="One or more item IDs from `widescrape search`."),
    focus: str = typer.Option("", help="Optional BM25 query to prune long pages down to relevant sections."),
    max_chars: int = typer.Option(2500, help="Max chars per item when --focus pruning is active."),
) -> None:
    """Read full (or BM25-pruned) stored content for specific item IDs."""
    store = ScrapeStorage()
    items = store.get_items_by_ids(item_ids)
    if not items:
        typer.echo("No matching items found in local cache.")
        raise typer.Exit(code=1)

    for it in items:
        body = prune_text_bm25(it.body or "", focus, max_chars=max_chars) if focus else (it.body or "(no body text)")[:max_chars]
        typer.echo(f"\n### [{it.item_id}] {it.title}")
        typer.echo(f"- **Source:** `{it.source}` | **Author:** {it.author or 'unknown'} | **Engagement:** {it.engagement} | **Date:** {it.published_at or 'unknown'}")
        typer.echo(f"- **URL:** {it.url}")
        typer.echo(format_untrusted_block(it.url, body))


@app.command()
def find(
    keywords: str = typer.Argument(..., help="FTS5 keyword query against already-scraped cache."),
    limit: int = typer.Option(25, help="Max cached results to return."),
) -> None:
    """Search previously scraped items in local SQLite without making network calls."""
    store = ScrapeStorage()
    items = store.search_cache(keywords, limit=limit)
    if not items:
        typer.echo("No cached matches.")
        return
    for idx, it in enumerate(items, start=1):
        typer.echo(_format_human_result(idx, it))


@app.command()
def auth(
    platform: str = typer.Argument(..., help="Platform to sign into (x or linkedin)."),
) -> None:
    """Open a local browser once to save optional burner-account cookies for deep live scraping."""
    try:
        saved_path = asyncio.run(capture_auth_session(platform))
        typer.echo(f"Saved session cookies for '{platform}' to {saved_path}")
    except Exception as exc:
        typer.echo(
            f"Auth setup failed: {exc}\n"
            "If Playwright Chromium is missing, run: uv run playwright install chromium"
        )
        raise typer.Exit(code=1)


@app.command()
def status() -> None:
    """Show active search backends and optional saved cookie sessions."""
    import os

    searx = os.environ.get("WIDESCRAPE_SEARXNG_URL") or "not set (using DuckDuckGo fallback)"
    typer.echo(f"SearXNG URL: {searx}")
    for plat in ("x", "linkedin"):
        state = "saved cookie session" if has_saved_session(plat) else "zero-login mode (dorking/archive)"
        typer.echo(f"  {plat}: {state}")


@app.command()
def show(
    run_id: str = typer.Argument(..., help="Run ID from a previous search/crawl."),
    offset: int = typer.Option(0, help="Start offset (e.g. 20 to view results after the top 20)."),
    limit: int = typer.Option(30, help="Number of results to print."),
    all_items: bool = typer.Option(False, "--all", help="Print all saved results for this run."),
) -> None:
    """View additional or full ranked results from a saved run_id."""
    store = ScrapeStorage()
    total = store.count_run_items(run_id)
    if total == 0:
        typer.echo(f"No saved results found for run_id={run_id}.")
        raise typer.Exit(code=1)

    safe_offset = 0 if all_items else max(0, offset)
    sliced = store.get_run_items(run_id, offset=safe_offset, limit=0 if all_items else max(1, limit))
    if not sliced:
        typer.echo(f"No results in range (offset={safe_offset}, total={total}) for run_id={run_id}.")
        raise typer.Exit(code=1)

    start_rank = safe_offset + 1
    for idx, it in enumerate(sliced, start=start_rank):
        typer.echo(_format_human_result(idx, it))
    typer.echo(f"\nShowing {len(sliced)} results (ranks {start_rank}-{start_rank + len(sliced) - 1} of {total}) for run_id={run_id}.")


@app.command()
def runs(
    limit: int = typer.Option(10, help="Number of recent runs to list."),
) -> None:
    """List recent saved search/crawl runs."""
    store = ScrapeStorage()
    recent = store.list_runs(limit=limit)
    if not recent:
        typer.echo("No saved runs yet.")
        return
    for r in recent:
        typer.echo(f"- `{r['run_id']}` · {r['item_count']} items · {r['created_at']} · `{r['sample_query']}`")


@cache_app.command("stats")
def cache_stats() -> None:
    """Show SQLite cache row counts and disk usage."""
    st = ScrapeStorage().get_cache_stats()
    kb = st["db_bytes"] / 1024.0
    typer.echo(
        f"Cache stats: {st['runs']} runs · {st['items']} unique items · "
        f"{st['run_items']} run links · {kb:.1f} KB"
    )


@cache_app.command("prune")
def cache_prune(
    days: int = typer.Option(30, help="Keep runs from the last N days; prune older runs & orphan items."),
) -> None:
    """Prune old runs from SQLite cache and garbage-collect unreferenced items."""
    try:
        res = ScrapeStorage().prune_cache(keep_days=days)
    except ValueError as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(code=1)
    typer.echo(
        f"Pruned {res['deleted_runs']} runs older than {days}d and removed {res['deleted_items']} orphan items."
    )


@app.command()
def crawl(
    url: str = typer.Argument(..., help="Target webpage, PDF, or docs URL to crawl."),
    query: str = typer.Option("", help="Optional ranking focus query."),
    max_pages: int = typer.Option(15, help="Maximum pages to fetch."),
    limit: Optional[int] = typer.Option(None, help="Max pages to KEEP AFTER filtering & ranking (auto-expands crawl budget up to 100)."),
    depth: int = typer.Option(1, help="Crawl depth (1 = single page, 2 = follow same-domain links)."),
    strategy: str = typer.Option("bfs", help="Traversal strategy: bfs (breadth-first) or dfs (depth-first)."),
    extract: str = typer.Option("", help="Structured extractor: '', 'auto', 'codes', or 'jobs'."),
    fields: str = typer.Option("", help="Custom schema fields to extract (e.g. 'price,code,deadline')."),
    browser_fetch: bool = typer.Option(False, help="Use headless Playwright fallback for JS-rendered pages."),
    proxy: Optional[str] = typer.Option(None, help="Optional HTTP/SOCKS proxy URL."),
    export: bool = typer.Option(False, "--export/--no-export", help="Also write .md/.xlsx/.html files (default saves to cache only)."),
    max_show: int = typer.Option(20, help="Ranked results to display (0 = show all; full list is always saved)."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print only summary counts and export file paths."),
    as_json: bool = typer.Option(False, "--json", help="Output compact JSON instead of Markdown."),
) -> None:
    """Directly crawl a specific website, PDF, or docs URL and return ranked results cleanly."""
    try:
        result = asyncio.run(
            run_crawl(
                url=url,
                query=query,
                max_pages=max_pages,
                depth=depth,
                strategy=strategy,
                extract=extract,
                fields=fields,
                browser_fetch=browser_fetch,
                proxy=proxy,
                export=export,
                limit=limit,
            )
        )
    except ValueError as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(code=1)

    total_kept = len(result.items)
    shown = result.items[:max_show] if max_show > 0 else result.items
    if as_json:
        typer.echo(_run_json_payload(result, shown))
        return
    if quiet:
        typer.echo(f"Crawled {total_kept} pages (run_id={result.run_id}).")
        if result.md_path:
            typer.echo(f"Markdown: {result.md_path}")
            typer.echo(f"Excel:    {result.xlsx_path}")
            typer.echo(f"Viewer:   {result.html_path}")
        else:
            typer.echo("Cache only (use --export or consolidate via `widescrape export --run-ids ...`).")
        return

    typer.echo(f"## WideScrape Crawl: `{url}`")
    typer.echo(f"- **Kept:** {total_kept} pages ({result.stats['compression_pct']}% context saved)\n")
    for idx, it in enumerate(shown, start=1):
        typer.echo(_format_human_result(idx, it))
    _print_access_footer(result.run_id, len(shown), total_kept, result.md_path, result.xlsx_path, result.html_path)


@app.command()
def map(
    url: str = typer.Argument(..., help="Website or docs root URL to discover links from."),
    filter_text: str = typer.Option("", "--filter", help="Optional keyword filter on discovered URLs."),
    limit: int = typer.Option(100, help="Max URLs to return."),
    proxy: Optional[str] = typer.Option(None, help="Optional HTTP/SOCKS proxy URL."),
) -> None:
    """Cheaply discover URLs on a site (sitemap.xml + /llms.txt + links) before crawling."""
    urls = asyncio.run(map_site(url, filter_text=filter_text, limit=limit, proxy=proxy))
    typer.echo(f"## Mapped {len(urls)} URLs from `{url}`")
    for u in urls:
        typer.echo(f"- {u}")


@app.command("export")
def export_cmd(
    run_id: Optional[str] = typer.Argument(None, help="Saved Run ID to re-extract and export offline."),
    run_ids: str = typer.Option("", "--run-ids", help="Comma-separated Run IDs to consolidate into ONE report."),
    item_ids: str = typer.Option("", "--item-ids", help="Comma-separated cached item IDs to include in ONE report."),
    title: str = typer.Option("", "--title", help="Report title when consolidating multiple runs or items."),
    name: str = typer.Option("", "--name", help="Custom output filename stem (without extension)."),
    mode: str = typer.Option("general", "--mode", help="Mode label for consolidated task exports."),
    extract: str = typer.Option("", help="Structured extractor: '', 'auto', 'codes', or 'jobs'."),
    fields: str = typer.Option("", help="Custom schema fields to extract (e.g. 'company,salary,deadline')."),
    rank_fields: str = typer.Option("", help="Priority fields to boost in ranking (e.g. 'salary,experience')."),
    max_show: int = typer.Option(20, help="Ranked results to display (0 = show all)."),
    as_json: bool = typer.Option(False, "--json", help="Output compact JSON instead of Markdown."),
) -> None:
    """Re-export one saved run or consolidate multiple runs/items into ONE .md/.xlsx/.html report."""
    combined_runs = ",".join(part for part in (run_id or "", run_ids) if part.strip())
    is_consolidated = bool(run_ids.strip() or item_ids.strip() or title.strip())
    try:
        if is_consolidated:
            result = export_task_report(
                title=title or name or f"Consolidated Export ({combined_runs or 'curated'})",
                run_ids=combined_runs,
                item_ids=item_ids,
                mode=mode,
                extract=extract,
                fields=fields,
                rank_fields=rank_fields,
                report_name=name,
                storage=ScrapeStorage(),
            )
        elif run_id:
            result = export_saved_run(
                run_id=run_id,
                extract=extract,
                fields=fields,
                rank_fields=rank_fields,
                storage=ScrapeStorage(),
                report_name=name,
            )
        else:
            raise KeyError("Provide a `run_id` argument or `--run-ids` / `--item-ids`.")
    except (KeyError, ValueError) as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(code=1)

    items = result.items[:max_show] if max_show > 0 else result.items
    if as_json:
        typer.echo(_run_json_payload(result, items))
        return

    label = title or run_id or result.run_id
    typer.echo(f"Exported `{label}` ({len(result.items)} items):")
    typer.echo(f"- **Markdown:** `{result.md_path}`")
    typer.echo(f"- **Excel:**    `{result.xlsx_path}`")
    typer.echo(f"- **Viewer:**   `{result.html_path}`")


@app.command()
def install(
    with_mcp: bool = typer.Option(False, help="Also register `widescrape` in Antigravity / Claude MCP JSON configs."),
) -> None:
    """Install WideScrape SKILL.md (and optionally MCP config) into detected AI agents."""
    for line in install_agent_skills(with_mcp=with_mcp):
        typer.echo(line)


@app.command()
def doctor(
    offline: bool = typer.Option(False, help="Skip live search network check."),
) -> None:
    """Run diagnostics on Python, TLS stealth, Excel export, search backends, Playwright, and sessions."""
    for line in collect_diagnostics(check_network=not offline):
        typer.echo(line)


@app.command()
def mcp() -> None:
    """Start the thin MCP stdio server for MCP-compatible AI agents."""
    run_mcp_server()


if __name__ == "__main__":
    app()
