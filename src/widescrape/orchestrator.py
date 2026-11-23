"""Main WideScrape pipeline: expand -> fan-out -> filter/rank -> extract -> diff -> store -> export."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import hashlib
import logging
import os
from pathlib import Path
import re
from typing import Any
import uuid

from widescrape.adapters.arxiv import ArxivAdapter
from widescrape.adapters.base import BaseAdapter
from widescrape.adapters.browser_social import BrowserSocialAdapter
from widescrape.adapters.github import GitHubAdapter
from widescrape.adapters.hackernews import HackerNewsAdapter
from widescrape.adapters.linkedin_jobs import LinkedInJobsGuestAdapter
from widescrape.adapters.news import GoogleNewsAdapter
from widescrape.adapters.reddit import RedditArchiveAdapter
from widescrape.adapters.remote_boards import RemoteJobBoardsAdapter
from widescrape.adapters.stackexchange import StackExchangeAdapter
from widescrape.adapters.web_dork import WebDorkAdapter, crawl_site, enrich_items
from widescrape.discovery import discover_sites_and_items
from widescrape.expander import SearchPlan, expand_query, normalize_sources
from widescrape.exporter import DEFAULT_EXPORT_DIR, export_run_files
from widescrape.extractor import (
    apply_custom_fields,
    apply_extractors,
    normalize_fields,
    resolve_extract_mode,
)
from widescrape.fetcher import canonicalize_url
from widescrape.filter_rank import (
    apply_field_ranking_boost,
    filter_and_rank,
    recompute_stats,
    select_best_highlight,
)
from widescrape.models import ScrapeItem
from widescrape.modes import record_learned_run
from widescrape.storage import ScrapeStorage, content_digest

logger = logging.getLogger("widescrape")


@dataclass
class ScrapeRunResult:
    """Result bundle returned by a WideScrape run."""

    run_id: str
    plan: SearchPlan
    items: list[ScrapeItem]
    stats: dict[str, int]
    adapter_counts: dict[str, int]
    adapter_errors: dict[str, str] = field(default_factory=dict)
    md_path: Path | None = None
    xlsx_path: Path | None = None
    html_path: Path | None = None
    discovered_sites: list[str] = field(default_factory=list)
    learned_sites: list[str] = field(default_factory=list)


def build_adapters(
    sources: list[str] | None = None,
    mode: str = "general",
    fetch_pages: bool = False,
    browser_fetch: bool = False,
    proxy: str | None = None,
    auto_enrich: bool = True,
) -> list[BaseAdapter]:
    """Instantiate adapters matching the requested or mode-resolved sources."""
    active = normalize_sources(sources, mode=mode)
    adapters: list[BaseAdapter] = []

    if "github" in active:
        adapters.append(GitHubAdapter(proxy=proxy))
    if "hackernews" in active:
        adapters.append(HackerNewsAdapter(proxy=proxy))
    if "reddit" in active:
        adapters.append(RedditArchiveAdapter(proxy=proxy))
    if "news" in active:
        adapters.append(GoogleNewsAdapter(proxy=proxy))
    if "stackexchange" in active:
        adapters.append(StackExchangeAdapter(proxy=proxy))
    if "arxiv" in active:
        adapters.append(ArxivAdapter(proxy=proxy))
    if "linkedin_jobs" in active:
        adapters.append(LinkedInJobsGuestAdapter(fetch_details=True, proxy=proxy))
    if "boards" in active:
        adapters.append(RemoteJobBoardsAdapter(proxy=proxy))
    if active & {"linkedin_posts", "linkedin_jobs", "x", "reddit", "web"}:
        searxng_url = os.environ.get("WIDESCRAPE_SEARXNG_URL")
        adapters.append(
            WebDorkAdapter(
                searxng_url=searxng_url,
                fetch_pages=fetch_pages,
                browser_fetch=browser_fetch,
                proxy=proxy,
                auto_enrich=auto_enrich,
            )
        )
    social_targets: list[str] = []
    if "x" in active:
        social_targets.append("x")
    if "linkedin_posts" in active:
        social_targets.append("linkedin_posts")
    if "linkedin_jobs" in active:
        social_targets.append("linkedin_jobs")
    if social_targets:
        adapters.append(BrowserSocialAdapter(platforms=social_targets, proxy=proxy))

    return adapters


def _compute_overfetch_limit(
    target_limit: int,
    plan: SearchPlan,
    *,
    new_only: bool = False,
    changed: bool = False,
) -> int:
    """Compute raw per-adapter over-fetch budget so post-filter results can reach `target_limit`."""
    multiplier = 2
    if plan.days or plan.strict_days or plan.base_mode == "jobs" or getattr(plan, "exclude_terms", ()):
        multiplier += 1
    if new_only or changed:
        multiplier += 1
    return min(max(target_limit * multiplier, target_limit + 25), 500)


def _apply_new_only_filter(
    items: list[ScrapeItem], seen_urls: set[str], stats: dict[str, int]
) -> list[ScrapeItem]:
    """Filter out items whose canonical URL was already saved in earlier runs."""
    fresh_items = [it for it in items if canonicalize_url(it.url) not in seen_urls]
    stats["skipped_seen"] = len(items) - len(fresh_items)
    return fresh_items


def _apply_changed_filter(
    items: list[ScrapeItem],
    seen_hashes: dict[str, str],
    stats: dict[str, int],
    *,
    mutate: bool = True,
) -> list[ScrapeItem]:
    """Skip unchanged seen URLs while retaining new or updated content."""
    diff_items: list[ScrapeItem] = []
    skipped_unchanged = 0
    changed_count = 0
    for it in items:
        canon = canonicalize_url(it.url)
        digest = content_digest(f"{it.title}\n{it.body}")
        if canon in seen_hashes:
            prev_hash = seen_hashes[canon]
            if prev_hash == digest:
                skipped_unchanged += 1
                continue
            if prev_hash:
                if mutate:
                    it.metadata["changed"] = True
                changed_count += 1
        diff_items.append(it)
    if mutate:
        stats["skipped_unchanged"] = skipped_unchanged
        stats["changed_items"] = changed_count
    return diff_items


def _apply_post_filter_limit(
    items: list[ScrapeItem], target_limit: int, stats: dict[str, int]
) -> list[ScrapeItem]:
    """Enforce `target_limit` AFTER filtering, diffing, extraction, and ranking."""
    stats["filtered_total"] = len(items)
    if target_limit > 0 and len(items) > target_limit:
        return items[:target_limit]
    return items


async def _collect_with_backfill(
    *,
    adapters: list[BaseAdapter],
    plan: SearchPlan,
    target_limit: int,
    per_adapter_limit: int | None,
    query: str,
    new_only: bool,
    changed: bool,
    defer_strict: bool,
    seen_urls: set[str] | None,
    seen_hashes: dict[str, str] | None,
) -> tuple[list[ScrapeItem], dict[str, int], dict[str, str]]:
    """Fetch raw items from adapters, automatically backfilling truncated sources if filtering undershoots."""
    round1_limit = (
        max(1, per_adapter_limit)
        if per_adapter_limit is not None
        else _compute_overfetch_limit(target_limit, plan, new_only=new_only, changed=changed)
    )
    raw_batches = await asyncio.gather(
        *(adapter.fetch(plan, limit=round1_limit) for adapter in adapters),
        return_exceptions=True,
    )

    batches_by_idx: list[list[ScrapeItem]] = []
    adapter_counts: dict[str, int] = {}
    adapter_errors: dict[str, str] = {}
    succeeded_indices: list[int] = []
    for idx, (adapter, batch) in enumerate(zip(adapters, raw_batches)):
        if isinstance(batch, BaseException):
            adapter_counts[adapter.name] = 0
            err_msg = f"{type(batch).__name__}: {batch}"
            adapter_errors[adapter.name] = err_msg[:120]
            logger.warning("Adapter '%s' failed for query %r: %s", adapter.name, query, err_msg)
            batches_by_idx.append([])
            continue
        adapter_counts[adapter.name] = len(batch)
        batches_by_idx.append(list(batch))
        succeeded_indices.append(idx)

    # Backfill only when caller did not pin a raw `per_adapter_limit` and post-filter survival < `target_limit`.
    if per_adapter_limit is None and target_limit > 0 and round1_limit < 500 and succeeded_indices:
        all_round1 = [it for batch in batches_by_idx for it in batch]
        prelim, _ = filter_and_rank(
            all_round1, plan, apply_ranking=False, enforce_strict_days=not defer_strict
        )
        if new_only and seen_urls is not None and prelim:
            prelim = [it for it in prelim if canonicalize_url(it.url) not in seen_urls]
        elif changed and seen_hashes is not None and prelim:
            prelim = _apply_changed_filter(prelim, seen_hashes, {}, mutate=False)

        if len(prelim) < target_limit:
            min_saturated = max(5, int(round1_limit * 0.6))
            expandable = [idx for idx in succeeded_indices if len(batches_by_idx[idx]) >= min_saturated]
            backfill_limit = min(max(round1_limit * 2, target_limit * 4), 500)
            if expandable and backfill_limit > round1_limit:
                retry_batches = await asyncio.gather(
                    *(adapters[idx].fetch(plan, limit=backfill_limit) for idx in expandable),
                    return_exceptions=True,
                )
                for idx, batch in zip(expandable, retry_batches):
                    if isinstance(batch, BaseException):
                        continue
                    batches_by_idx[idx] = list(batch)
                    adapter_counts[adapters[idx].name] = len(batch)

    all_raw = [it for batch in batches_by_idx for it in batch]
    return all_raw, adapter_counts, adapter_errors


async def run_scrape(
    query: str,
    mode: str = "auto",
    sources: list[str] | None = None,
    sites: list[str] | None = None,
    limit: int = 100,
    per_adapter_limit: int | None = None,
    days: int | None = None,
    strict_days: bool = False,
    extract: str = "",
    fields: list[str] | str | None = None,
    rank_fields: list[str] | str | None = None,
    new_only: bool = False,
    changed: bool = False,
    fetch_pages: bool = False,
    browser_fetch: bool = False,
    proxy: str | None = None,
    region: str = "",
    synonyms: dict[str, list[str] | str] | list[str] | str | None = None,
    exclude: list[str] | str | None = None,
    extra_queries: list[str] | str | None = None,
    discover_sites: bool = True,
    learn: bool = True,
    learn_mode: str = "",
    storage: ScrapeStorage | None = None,
    modes_path: Path | str | None = None,
    learned_path: Path | str | None = None,
    export_dir: Path | str | None = None,
    export: bool = True,
) -> ScrapeRunResult:
    """Execute the full WideScrape pipeline (expand -> scrape -> discover -> filter/rank -> learn -> export)."""
    target_limit = max(1, limit)
    plan = expand_query(
        query=query,
        mode=mode,
        sources=sources,
        sites=sites,
        days=days,
        strict_days=strict_days,
        extract=extract,
        fields=fields,
        rank_fields=rank_fields,
        region=region,
        synonyms=synonyms,
        exclude=exclude,
        extra_queries=extra_queries,
        discover_sites=discover_sites,
        learn=learn,
        learn_mode=learn_mode,
        modes_path=modes_path,
        learned_path=learned_path,
    )
    resolved_src_list = list(plan.effective_sources) if plan.effective_sources else sources
    adapters = build_adapters(
        sources=resolved_src_list,
        mode=plan.base_mode,
        fetch_pages=fetch_pages,
        browser_fetch=browser_fetch,
        proxy=proxy,
        auto_enrich=False,
    )

    store = storage or ScrapeStorage()
    defer_strict = plan.strict_days and (fetch_pages or browser_fetch)
    seen_urls = await asyncio.to_thread(store.get_seen_urls) if new_only else None
    seen_hashes = await asyncio.to_thread(store.get_seen_hashes) if (changed and not new_only) else None

    all_raw, adapter_counts, adapter_errors = await _collect_with_backfill(
        adapters=adapters,
        plan=plan,
        target_limit=target_limit,
        per_adapter_limit=per_adapter_limit,
        query=query,
        new_only=new_only,
        changed=changed,
        defer_strict=defer_strict,
        seen_urls=seen_urls,
        seen_hashes=seen_hashes,
    )

    ranked_items, stats = filter_and_rank(all_raw, plan, enforce_strict_days=not defer_strict)

    if new_only and seen_urls is not None and ranked_items:
        ranked_items = _apply_new_only_filter(ranked_items, seen_urls, stats)

    # Open-web domain-agnostic discovery wave when Wave 1 falls short of `target_limit`.
    discovered_sites_list: list[str] = []
    has_web_adapter = any(getattr(a, "name", "") == "web_dork" for a in adapters)
    if (
        plan.discover_sites
        and has_web_adapter
        and "web" in plan.effective_sources
        and len(ranked_items) < target_limit
    ):
        try:
            disc = await discover_sites_and_items(
                plan,
                searxng_url=os.environ.get("WIDESCRAPE_SEARXNG_URL"),
                proxy=proxy,
                max_sites=6,
                max_items=max(40, min(target_limit * 2, 120)),
            )
            discovered_sites_list = list(disc.discovered_sites)
            if disc.items:
                adapter_counts["discovery"] = len(disc.items)
                all_raw.extend(disc.items)
                ranked_items, stats = filter_and_rank(all_raw, plan, enforce_strict_days=not defer_strict)
                if new_only and seen_urls is not None and ranked_items:
                    ranked_items = _apply_new_only_filter(ranked_items, seen_urls, stats)
        except Exception as exc:
            logger.debug("Open-web site discovery failed gently: %s", exc)

    if ranked_items and (fetch_pages or browser_fetch):
        enrich_cap = min(max(target_limit * 2 if defer_strict else target_limit, 15), 60 if defer_strict else 40)
        targets = [
            it
            for it in ranked_items
            if it.source not in ("github", "arxiv", "stackexchange")
            and not it.metadata.get("linkedin_job_id")
            and not (it.source in ("himalayas", "remotive", "jobicy", "remoteok", "workingnomads", "weworkremotely") and len(it.body or "") > 400)
        ][:enrich_cap]
        if targets:
            await enrich_items(targets, browser_fetch=browser_fetch, proxy=proxy)
            for it in targets:
                hl = select_best_highlight(it.body or it.title, plan.raw_query, max_chars=165)
                if hl:
                    it.metadata["highlight"] = hl
            stats["raw_chars"] = max(
                stats.get("raw_chars", 0),
                sum(len(it.title) + len(it.body) for it in ranked_items),
            )

    if defer_strict and ranked_items:
        dated_items = [
            it
            for it in ranked_items
            if it.published_at and re.match(r"^\d{4}-\d{2}-\d{2}$", it.published_at[:10])
        ]
        stats["dropped_old"] = stats.get("dropped_old", 0) + (len(ranked_items) - len(dated_items))
        ranked_items = dated_items

    if changed and not new_only and seen_hashes is not None and ranked_items:
        ranked_items = _apply_changed_filter(ranked_items, seen_hashes, stats, mutate=True)

    if plan.extract_mode and ranked_items:
        apply_extractors(ranked_items, plan.extract_mode)
    if plan.extract_fields and ranked_items:
        apply_custom_fields(ranked_items, list(plan.extract_fields))
    if ranked_items and (plan.extract_fields or plan.extract_mode):
        apply_field_ranking_boost(ranked_items, plan)

    ranked_items = _apply_post_filter_limit(ranked_items, target_limit, stats)
    recompute_stats(ranked_items, stats)

    newly_learned: list[str] = []
    if plan.learn and ranked_items:
        try:
            newly_learned = await asyncio.to_thread(
                record_learned_run,
                tokens=plan.core_terms,
                base_mode=plan.base_mode,
                mode_name=plan.mode,
                kept_items=ranked_items,
                discovered_sites=discovered_sites_list,
                used_synonyms=plan.synonyms,
                learn_mode=plan.learn_mode,
                modes_path=modes_path,
                learned_path=learned_path,
            )
        except Exception as exc:
            logger.debug("Self-learning persistence failed: %s", exc)

    stats["discovered_sites"] = len(discovered_sites_list)
    stats["learned_sites"] = len(newly_learned)

    run_id = uuid.uuid4().hex[:10]
    await asyncio.to_thread(store.save_run, run_id, ranked_items, query=query, mode=plan.mode, stats=stats)
    md_path: Path | None = None
    xlsx_path: Path | None = None
    html_path: Path | None = None
    if export:
        md_path, xlsx_path, html_path = await asyncio.to_thread(
            export_run_files,
            run_id=run_id,
            query=query,
            mode=plan.mode,
            items=ranked_items,
            stats=stats,
            export_dir=export_dir or DEFAULT_EXPORT_DIR,
            adapter_counts=adapter_counts,
            adapter_errors=adapter_errors,
            days=plan.days,
            extract_mode=plan.extract_mode,
            extract_fields=plan.extract_fields,
        )

    return ScrapeRunResult(
        run_id=run_id,
        plan=plan,
        items=ranked_items,
        stats=stats,
        adapter_counts=adapter_counts,
        adapter_errors=adapter_errors,
        md_path=md_path,
        xlsx_path=xlsx_path,
        html_path=html_path,
        discovered_sites=discovered_sites_list,
        learned_sites=newly_learned,
    )


async def run_crawl(
    url: str,
    query: str = "",
    max_pages: int = 15,
    depth: int = 1,
    strategy: str = "bfs",
    extract: str = "",
    fields: list[str] | str | None = None,
    browser_fetch: bool = False,
    proxy: str | None = None,
    storage: ScrapeStorage | None = None,
    export_dir: Path | str | None = None,
    export: bool = False,
    limit: int | None = None,
) -> ScrapeRunResult:
    """Crawl a specific site/page directly, filter/rank, enforce optional post-filter `limit`, and save."""
    has_query = bool(query.strip())
    effective_limit = max(1, limit) if limit is not None else None
    crawl_budget = (
        max_pages
        if effective_limit is None
        else max(max_pages, min(max(effective_limit * 2, effective_limit + 10), 100))
    )
    plan = replace(
        expand_query(query=query or url, mode="general", sources=["web"], extract=extract, fields=fields),
        mode="crawl",
    )
    raw_items = await crawl_site(
        start_url=url,
        max_pages=crawl_budget,
        depth=depth,
        strategy=strategy,
        browser_fetch=browser_fetch,
        proxy=proxy,
    )
    ranked_items, stats = filter_and_rank(raw_items, plan, apply_ranking=has_query)
    if plan.extract_mode and ranked_items:
        apply_extractors(ranked_items, plan.extract_mode)
    if plan.extract_fields and ranked_items:
        apply_custom_fields(ranked_items, list(plan.extract_fields))
    if ranked_items and (plan.extract_fields or plan.extract_mode):
        apply_field_ranking_boost(ranked_items, plan)
    ranked_items = _apply_post_filter_limit(ranked_items, effective_limit or 0, stats)
    recompute_stats(ranked_items, stats)

    run_id = uuid.uuid4().hex[:10]
    store = storage or ScrapeStorage()
    await asyncio.to_thread(store.save_run, run_id, ranked_items, query=query or url, mode="crawl", stats=stats)
    md_path: Path | None = None
    xlsx_path: Path | None = None
    html_path: Path | None = None
    if export:
        md_path, xlsx_path, html_path = await asyncio.to_thread(
            export_run_files,
            run_id=run_id,
            query=query or url,
            mode="crawl",
            items=ranked_items,
            stats=stats,
            export_dir=export_dir or DEFAULT_EXPORT_DIR,
            adapter_counts={"web_crawl": len(raw_items)},
            extract_mode=plan.extract_mode,
            extract_fields=plan.extract_fields,
        )

    return ScrapeRunResult(
        run_id=run_id,
        plan=plan,
        items=ranked_items,
        stats=stats,
        adapter_counts={"web_crawl": len(raw_items)},
        md_path=md_path,
        xlsx_path=xlsx_path,
        html_path=html_path,
    )


_CURATED_STANDARD_KEYS = frozenset(
    {
        "item_id",
        "source",
        "title",
        "url",
        "body",
        "published_at",
        "score",
        "query_used",
        "highlight",
        "snippet",
        "metadata",
        "fields",
    }
)


def _normalize_id_list(values: list[str] | tuple[str, ...] | str | None) -> list[str]:
    """Normalize comma-separated or list-based ID inputs into stripped non-empty strings."""
    if not values:
        return []
    raw_parts = values.split(",") if isinstance(values, str) else list(values)
    seen: set[str] = set()
    ordered: list[str] = []
    for part in raw_parts:
        clean = str(part).strip()
        if clean and clean not in seen:
            seen.add(clean)
            ordered.append(clean)
    return ordered


def _coerce_curated_item(entry: dict[str, Any], index: int, total: int, fallback_query: str) -> ScrapeItem:
    """Convert a caller-curated finding dict into a `ScrapeItem` with structured fields."""
    url = str(entry.get("url") or "").strip()
    title = str(entry.get("title") or url or f"Finding {index + 1}").strip()
    source = str(entry.get("source") or "curated").strip()
    published_at = str(entry.get("published_at") or "").strip()
    highlight = str(entry.get("highlight") or entry.get("snippet") or "").strip()

    raw_meta = entry.get("metadata")
    metadata: dict[str, Any] = dict(raw_meta) if isinstance(raw_meta, dict) else {}
    existing_fields = metadata.get("extracted_fields")
    curated_fields: dict[str, str] = (
        {str(k): str(v) for k, v in existing_fields.items() if str(v).strip()}
        if isinstance(existing_fields, dict)
        else {}
    )
    entry_fields = entry.get("fields")
    if isinstance(entry_fields, dict):
        for k, v in entry_fields.items():
            val = str(v).strip()
            if val:
                curated_fields[str(k).strip()] = val
    for k, v in entry.items():
        if k in _CURATED_STANDARD_KEYS:
            continue
        val = str(v).strip() if v is not None else ""
        if val:
            curated_fields[str(k).strip()] = val
    if curated_fields:
        metadata["extracted_fields"] = curated_fields
    if highlight:
        metadata["highlight"] = highlight

    body = str(entry.get("body") or "").strip()
    if not body:
        parts = [f"{k}: {v}" for k, v in curated_fields.items()]
        if highlight:
            parts.append(highlight)
        body = " | ".join(parts)

    item_id = str(entry.get("item_id") or "").strip()
    if not item_id:
        seed = canonicalize_url(url) or url or f"{title}:{index}"
        item_id = f"cur_{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:10]}"

    raw_score = entry.get("score")
    score = float(raw_score) if raw_score is not None and raw_score != "" else float(max(total - index, 1))
    return ScrapeItem(
        item_id=item_id,
        source=source,
        title=title,
        url=url,
        body=body,
        published_at=published_at,
        score=score,
        query_used=str(entry.get("query_used") or fallback_query),
        metadata=metadata,
    )


def _finalize_and_export_items(
    *,
    run_id: str,
    title: str,
    mode: str,
    items: list[ScrapeItem],
    stats: dict[str, int],
    extract: str = "",
    fields: list[str] | str | None = None,
    rank_fields: list[str] | str | None = None,
    export_dir: Path | str | None = None,
    adapter_counts: dict[str, int] | None = None,
    filename_stem: str = "",
    overwrite_extracted: bool = False,
    apply_ranking: bool = True,
) -> ScrapeRunResult:
    """Apply extractors/ranking to `items` and export one `.md`/`.xlsx`/`.html` triplet."""
    base_mode = mode if mode in ("jobs", "research", "general") else "general"
    resolved_extract = resolve_extract_mode(extract, query=title, base_mode=base_mode) if extract else ""
    norm_fields = normalize_fields(fields)
    norm_rank = normalize_fields(rank_fields)

    if overwrite_extracted:
        for it in items:
            if resolved_extract:
                it.metadata.pop("extracted", None)
            if norm_fields:
                it.metadata.pop("extracted_fields", None)

    if resolved_extract and items:
        if overwrite_extracted:
            apply_extractors(items, resolved_extract)
        else:
            missing_ext = [it for it in items if not it.metadata.get("extracted")]
            if missing_ext:
                apply_extractors(missing_ext, resolved_extract)

    if norm_fields and items:
        if overwrite_extracted:
            apply_custom_fields(items, norm_fields)
        else:
            for it in items:
                preset = dict(it.metadata.get("extracted_fields") or {})
                missing_keys = [f for f in norm_fields if not preset.get(f)]
                if missing_keys:
                    apply_custom_fields([it], missing_keys)
                    merged = {f: preset.get(f) or (it.metadata.get("extracted_fields") or {}).get(f, "") for f in norm_fields}
                    for k, v in preset.items():
                        merged.setdefault(k, v)
                    it.metadata["extracted_fields"] = {k: v for k, v in merged.items() if v}

    plan = SearchPlan(
        raw_query=title,
        mode=mode,
        base_mode=base_mode,
        core_terms=(),
        dork_queries=(),
        api_queries=(),
        signal_keywords=(),
        extract_mode=resolved_extract,
        extract_fields=tuple(norm_fields),
        rank_fields=tuple(norm_rank),
    )
    if apply_ranking and items and (plan.rank_fields or plan.extract_fields or plan.extract_mode):
        apply_field_ranking_boost(items, plan)

    stats.setdefault("raw", len(items))
    stats.setdefault("raw_chars", sum(len(it.title) + len(it.body) for it in items))
    recompute_stats(items, stats)

    md_path, xlsx_path, html_path = export_run_files(
        run_id=run_id,
        query=title,
        mode=mode,
        items=items,
        stats=stats,
        export_dir=export_dir or DEFAULT_EXPORT_DIR,
        adapter_counts=adapter_counts,
        extract_mode=resolved_extract,
        extract_fields=tuple(norm_fields),
        filename_stem=filename_stem,
    )
    return ScrapeRunResult(
        run_id=run_id,
        plan=plan,
        items=items,
        stats=stats,
        adapter_counts=adapter_counts or {},
        md_path=md_path,
        xlsx_path=xlsx_path,
        html_path=html_path,
    )


def export_saved_run(
    run_id: str,
    extract: str = "",
    fields: list[str] | str | None = None,
    rank_fields: list[str] | str | None = None,
    storage: ScrapeStorage | None = None,
    export_dir: Path | str | None = None,
    report_name: str = "",
) -> ScrapeRunResult:
    """Re-run structured extractors and rebuild Markdown, Excel, and HTML offline for `run_id`."""
    store = storage or ScrapeStorage()
    info = store.get_run_info(run_id)
    items = store.get_run_items(run_id)
    if info is None and not items:
        raise KeyError(f"No saved run or items found for run_id={run_id}")

    query = (info["query"] if info else "") or (items[0].query_used if items else run_id)
    mode = (info["mode"] if info else "general") or "general"
    stats = dict(info["stats"]) if info and info.get("stats") else {
        "raw": len(items),
        "raw_chars": sum(len(it.title) + len(it.body) for it in items),
    }
    return _finalize_and_export_items(
        run_id=run_id,
        title=query,
        mode=mode,
        items=items,
        stats=stats,
        extract=extract,
        fields=fields,
        rank_fields=rank_fields,
        export_dir=export_dir,
        filename_stem=report_name,
        overwrite_extracted=True,
        apply_ranking=True,
    )


def export_task_report(
    title: str,
    run_ids: list[str] | str | None = None,
    item_ids: list[str] | str | None = None,
    entries: list[dict[str, Any]] | None = None,
    mode: str = "general",
    extract: str = "",
    fields: list[str] | str | None = None,
    rank_fields: list[str] | str | None = None,
    report_name: str = "",
    storage: ScrapeStorage | None = None,
    export_dir: Path | str | None = None,
) -> ScrapeRunResult:
    """Consolidate a task's findings into ONE Markdown, ONE Excel, and ONE HTML deliverable.

    Combines curated `entries`, cached `item_ids`, and saved `run_ids` (in that deduplication
    priority order) so multi-step agent tasks leave one unified report without intermediate clutter.
    """
    clean_title = title.strip() or report_name.strip() or "WideScrape Task Report"
    store = storage or ScrapeStorage()
    norm_run_ids = _normalize_id_list(run_ids)
    norm_item_ids = _normalize_id_list(item_ids)
    curated_entries = list(entries or [])

    collected: list[ScrapeItem] = []
    seen_keys: set[str] = set()
    inferred_fields: list[str] = []
    adapter_counts: dict[str, int] = {}

    def _add_unique(it: ScrapeItem) -> None:
        key = canonicalize_url(it.url) or it.url.strip() or it.item_id
        if key in seen_keys:
            return
        seen_keys.add(key)
        collected.append(it)
        adapter_counts[it.source] = adapter_counts.get(it.source, 0) + 1

    total_curated = len(curated_entries)
    for idx, entry in enumerate(curated_entries):
        item = _coerce_curated_item(entry, idx, total_curated, clean_title)
        _add_unique(item)
        for k in (item.metadata.get("extracted_fields") or {}).keys():
            if k not in inferred_fields:
                inferred_fields.append(k)

    if norm_item_ids:
        cached_by_id = {it.item_id: it for it in store.get_items_by_ids(norm_item_ids)}
        for iid in norm_item_ids:
            if iid in cached_by_id:
                _add_unique(cached_by_id[iid])

    raw_total = 0
    raw_chars = 0
    for rid in norm_run_ids:
        info = store.get_run_info(rid)
        if info and info.get("stats"):
            raw_total += int(info["stats"].get("raw", 0))
            raw_chars += int(info["stats"].get("raw_chars", 0))
        for it in store.get_run_items(rid):
            _add_unique(it)

    if not collected and not (curated_entries or norm_item_ids or norm_run_ids):
        raise ValueError("Provide at least one of run_ids, item_ids, or entries to export_task_report.")

    effective_fields: list[str] | str | None = fields
    if not normalize_fields(fields) and inferred_fields:
        effective_fields = inferred_fields

    task_run_id = uuid.uuid4().hex[:10]
    stats = {
        "raw": max(raw_total, len(collected)),
        "raw_chars": max(raw_chars, sum(len(it.title) + len(it.body) for it in collected)),
    }
    stem = report_name.strip() or clean_title
    should_rank = bool(normalize_fields(rank_fields)) or (not curated_entries)
    result = _finalize_and_export_items(
        run_id=task_run_id,
        title=clean_title,
        mode=mode.strip() or "general",
        items=collected,
        stats=stats,
        extract=extract,
        fields=effective_fields,
        rank_fields=rank_fields,
        export_dir=export_dir,
        adapter_counts=adapter_counts or None,
        filename_stem=stem,
        overwrite_extracted=False,
        apply_ranking=should_rank,
    )
    store.save_run(task_run_id, result.items, query=clean_title, mode=result.plan.mode, stats=result.stats)
    return result
