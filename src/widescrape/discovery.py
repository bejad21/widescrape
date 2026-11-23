"""Domain-agnostic open-web website discovery, directory mining, and adaptive extraction."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging
import re
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from ddgs import DDGS
import httpx

from widescrape.expander import SearchPlan
from widescrape.fetcher import (
    canonicalize_url,
    extract_feed_and_ats_hints,
    extract_html_published_date,
    extract_items_from_json_payload,
    extract_structured_items,
    resolve_proxy,
    stealth_fetch_document,
    validate_public_url,
)
from widescrape.models import ScrapeItem
from widescrape.modes import clean_site_domain

logger = logging.getLogger("widescrape")

_DISCOVERY_BLOCKED_HOSTS = frozenset({
    "google.com", "bing.com", "yahoo.com", "yandex.com", "duckduckgo.com",
    "facebook.com", "instagram.com", "tiktok.com", "pinterest.com",
    "youtube.com", "youtu.be", "twitter.com", "x.com", "t.co",
    "linkedin.com", "lnkd.in", "reddit.com", "old.reddit.com",
    "apple.com", "play.google.com", "apps.apple.com",
    "cloudflare.com", "bit.ly", "tinyurl.com", "ow.ly",
    "wikipedia.org", "en.wikipedia.org", "medium.com",
})
_NOISY_LINK_PATH_RE = re.compile(
    r"/(?:login|signin|signup|register|privacy|terms|cookies?|about|contact|advertise|pricing|faq|help|support|wp-content|tag|category)/?$",
    re.IGNORECASE,
)
_ROUNDUP_TITLE_RE = re.compile(
    r"\b(?:best|top|list|directory|sites|websites|boards|platforms|resources|portals|databases|alternatives|where\s+to)\b",
    re.IGNORECASE,
)
_RESOURCE_SIGNAL_RE = re.compile(
    r"\b(?:job|jobs|career|careers|board|boards|portal|directory|database|archive|docs|documentation|"
    r"community|forum|journal|grants?|fellowships?|coupons?|deals|research|listings?|openings?)\b",
    re.IGNORECASE,
)


@dataclass
class DiscoveryResult:
    """Output of open-web website discovery and newly uncovered structured items."""

    discovered_sites: list[str] = field(default_factory=list)
    discovered_urls: list[str] = field(default_factory=list)
    items: list[ScrapeItem] = field(default_factory=list)
    queries_used: list[str] = field(default_factory=list)


_NOISY_SUBDOMAINS = ("paymentpage.", "checkout.", "billing.", "login.", "signin.", "auth.", "accounts.", "support.")


def _is_candidate_site(domain: str) -> bool:
    clean = clean_site_domain(domain)
    if not clean:
        return False
    host = clean.split("/", 1)[0]
    if any(host.startswith(p) for p in _NOISY_SUBDOMAINS):
        return False
    if host in _DISCOVERY_BLOCKED_HOSTS:
        return False
    return not any(host.endswith(f".{b}") for b in _DISCOVERY_BLOCKED_HOSTS)


def build_discovery_queries(plan: SearchPlan) -> list[str]:
    """Generate clean open-web Google/Bing-style queries to discover top websites for any domain."""
    subject = " ".join(plan.core_terms[:4]) if plan.core_terms else plan.raw_query
    subject = " ".join(subject.split()).strip()
    syns = [s for s in getattr(plan, "synonyms", ()) or () if s.strip()]
    syn_hint = syns[0] if syns else subject

    queries: list[str] = []
    if plan.base_mode == "jobs":
        queries.extend([
            f"best job boards websites for {subject}",
            f"{subject} careers board listings",
            f"top {syn_hint} specialized job portals",
        ])
    elif plan.extract_mode == "codes":
        queries.extend([
            f"best verified promo coupon code websites {subject}",
            f"{subject} deals discount tracker community",
        ])
    elif plan.base_mode == "research":
        queries.extend([
            f"best websites databases resources for {subject}",
            f"top {subject} documentation benchmark papers forum",
            f"{syn_hint} official docs repository guide",
        ])
    else:
        queries.extend([
            f"best websites platforms directories for {subject}",
            f"top {subject} resources portals list",
            f"{syn_hint} community directory listings",
        ])

    for eq in (getattr(plan, "extra_queries", ()) or ())[:2]:
        queries.append(f"best websites for {eq}")

    deduped: list[str] = []
    seen: set[str] = set()
    for q in queries:
        qn = " ".join(q.split()).strip()
        if qn and qn.lower() not in seen:
            seen.add(qn.lower())
            deduped.append(qn)
    return deduped[:4]


def extract_candidate_domains_from_html(
    base_url: str,
    raw_html: str,
    anchor_terms: tuple[str, ...] = (),
) -> list[tuple[str, str, float]]:
    """Mine outbound links from a roundup/directory page to discover specialized websites."""
    if not raw_html or "<a" not in raw_html.lower():
        return []
    base_host = (urlparse(base_url).netloc or "").lower().removeprefix("www.")
    soup = BeautifulSoup(raw_html[:350000], "html.parser")
    terms_low = [t.lower() for t in anchor_terms if len(t.strip()) >= 2]

    scored: dict[str, tuple[str, float]] = {}
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        abs_url = urljoin(base_url, href)
        safe = validate_public_url(abs_url)
        if not safe:
            continue
        parsed = urlparse(safe)
        host = (parsed.netloc or "").lower().removeprefix("www.")
        if not host or host == base_host or not _is_candidate_site(host):
            continue
        if _NOISY_LINK_PATH_RE.search(parsed.path or ""):
            continue

        anchor_txt = " ".join(a.get_text(" ", strip=True).split())
        parent_txt = " ".join((a.parent.get_text(" ", strip=True) if a.parent else "").split())[:240]
        ctx_low = f"{anchor_txt} {parent_txt} {safe}".lower()

        overlap = sum(1 for t in terms_low if t in ctx_low)
        signal_bonus = 0.6 if _RESOURCE_SIGNAL_RE.search(ctx_low) else 0.0
        if overlap == 0 and signal_bonus == 0.0:
            continue

        weight = 1.0 + min(overlap * 0.6, 2.4) + signal_bonus
        prev = scored.get(host)
        if prev is None or weight > prev[1]:
            scored[host] = (safe, round(weight, 3))

    ranked = [(host, u, w) for host, (u, w) in sorted(scored.items(), key=lambda kv: kv[1][1], reverse=True)]
    return ranked[:15]


async def _search_web_raw(
    query: str,
    *,
    searxng_url: str | None = None,
    proxy: str | None = None,
    region: str = "",
    max_results: int = 12,
) -> list[dict[str, str]]:
    """Query open web via SearXNG or DDGS multi-backend (bing, yahoo, google)."""
    if searxng_url:
        try:
            async with httpx.AsyncClient(timeout=12.0, proxy=resolve_proxy(proxy)) as client:
                resp = await client.get(
                    f"{searxng_url.rstrip('/')}/search",
                    params={"q": query, "format": "json", "pageno": 1},
                )
                if resp.status_code == 200:
                    rows = []
                    for r in (resp.json().get("results") or [])[:max_results]:
                        if r.get("url"):
                            rows.append({
                                "url": str(r.get("url") or ""),
                                "title": str(r.get("title") or ""),
                                "body": str(r.get("content") or ""),
                                "published_at": str(r.get("publishedDate") or "")[:10],
                            })
                    if rows:
                        return rows
        except Exception as exc:
            logger.debug("Discovery SearXNG query failed (%r): %s", query, exc)

    def _ddgs_sync() -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        try:
            ddgs = DDGS(proxy=proxy, timeout=8) if proxy else DDGS(timeout=8)
        except TypeError:
            ddgs = DDGS(proxy=proxy) if proxy else DDGS()
        for backend in ("bing", "yahoo"):
            try:
                kwargs: dict[str, Any] = {"max_results": max_results, "backend": backend, "page": 1}
                if region:
                    kwargs["region"] = region
                res = ddgs.text(query, **kwargs)
                if res:
                    for r in res:
                        u = r.get("href") or r.get("url") or ""
                        if u:
                            out.append({
                                "url": str(u),
                                "title": str(r.get("title") or ""),
                                "body": str(r.get("body") or r.get("snippet") or ""),
                                "published_at": str(r.get("date") or r.get("published") or "")[:10],
                            })
                    if len(out) >= 6:
                        break
            except Exception:
                continue
        return out

    try:
        return await asyncio.wait_for(asyncio.to_thread(_ddgs_sync), timeout=12.0)
    except asyncio.TimeoutError:
        return []


async def _parse_feed_or_ats_url(
    feed_url: str, client: httpx.AsyncClient, proxy: str | None, query_used: str
) -> list[ScrapeItem]:
    """Fetch detected RSS/Atom/JSON/ATS endpoints and convert into generic ScrapeItems."""
    doc = await stealth_fetch_document(feed_url, http_client=client, proxy=proxy)
    if doc is None:
        return []
    _, body_text, raw_text = doc
    source_raw = raw_text or body_text
    if not source_raw:
        return []

    low = source_raw[:600].lower()
    if low.lstrip().startswith("{") or low.lstrip().startswith("["):
        try:
            payload = json.loads(source_raw)
            return extract_items_from_json_payload(feed_url, payload, source="discovered", query_used=query_used, max_items=25)
        except ValueError:
            return []

    if "<rss" in low or "<feed" in low:
        from widescrape.adapters.news import parse_google_news_rss
        from widescrape.adapters.reddit import parse_reddit_rss

        items = parse_reddit_rss(source_raw, query=query_used, limit=25)
        if not items:
            items = parse_google_news_rss(source_raw, query=query_used, limit=25)
        for it in items:
            it.source = "discovered"
        return items
    return []


async def _harvest_roundups(
    roundup_urls: list[str],
    do_fetch: Callable[[str, httpx.AsyncClient], Any],
    client: httpx.AsyncClient,
    anchor_terms: tuple[str, ...],
    known_exclude: set[str],
    domain_scores: dict[str, float],
    seen_urls: set[str],
    query_used: str,
) -> tuple[list[str], list[ScrapeItem], list[str]]:
    """Fetch top roundup pages to mine outbound portal domains, structured items, and feed hints."""
    fetched_docs = await asyncio.gather(*(do_fetch(u, client) for u in roundup_urls), return_exceptions=True)
    discovered_urls: list[str] = []
    extracted_items: list[ScrapeItem] = []
    feed_hints: list[str] = []

    for target_url, doc in zip(roundup_urls, fetched_docs):
        if isinstance(doc, BaseException) or doc is None:
            continue
        _, _, raw_html = doc
        if not raw_html:
            continue
        discovered_urls.append(target_url)
        for host, link_url, w in extract_candidate_domains_from_html(target_url, raw_html, anchor_terms):
            if host not in known_exclude:
                domain_scores[host] = domain_scores.get(host, 0.0) + w
            discovered_urls.append(link_url)
        for s_item in extract_structured_items(
            target_url, raw_html, source="discovered", query_used=query_used, max_items=20
        ):
            c = canonicalize_url(s_item.url)
            if c not in seen_urls:
                seen_urls.add(c)
                extracted_items.append(s_item)
        for hint in extract_feed_and_ats_hints(target_url, raw_html):
            if hint not in feed_hints:
                feed_hints.append(hint)
    return discovered_urls, extracted_items, feed_hints


async def _search_discovered_sites(
    top_sites: list[str],
    plan: SearchPlan,
    run_search: Callable[[str], Any],
    seen_urls: set[str],
) -> list[ScrapeItem]:
    """Run targeted second-wave site: queries across newly discovered domains."""
    site_anchor = " ".join(plan.core_terms[:3]) or plan.raw_query
    if getattr(plan, "synonyms", ()):
        top_syns = " OR ".join(f'"{s}"' if " " in s else s for s in plan.synonyms[:3])
        site_q_suffix = f"({site_anchor} OR {top_syns})"
    else:
        site_q_suffix = site_anchor

    wave2_queries = [f"site:{dom} {site_q_suffix}" for dom in top_sites[:4]]
    wave2_batches = await asyncio.gather(*(run_search(wq) for wq in wave2_queries), return_exceptions=True)
    out: list[ScrapeItem] = []
    for wq, batch in zip(wave2_queries, wave2_batches):
        if isinstance(batch, BaseException):
            continue
        for row in batch:
            u = validate_public_url(str(row.get("url") or ""))
            if not u:
                continue
            canon = canonicalize_url(u)
            if canon in seen_urls:
                continue
            seen_urls.add(canon)
            out.append(
                ScrapeItem(
                    source="discovered",
                    url=u,
                    title=" ".join(str(row.get("title") or u).split()),
                    body=" ".join(str(row.get("body") or "").split())[:2500],
                    published_at=str(row.get("published_at") or "")[:10],
                    query_used=wq,
                    metadata={"discovered_via": "open_web_site_discovery"},
                )
            )
    return out


async def _inspect_portal_feeds(
    top_sites: list[str],
    feed_hints: list[str],
    do_fetch: Callable[[str, httpx.AsyncClient], Any],
    client: httpx.AsyncClient,
    active_proxy: str | None,
    seen_urls: set[str],
    query_used: str,
) -> list[ScrapeItem]:
    """Extract structured listings from discovered portal homepages and embedded RSS/ATS endpoints."""
    portal_urls = [f"https://{dom}" for dom in top_sites[:3]]
    portal_docs = await asyncio.gather(*(do_fetch(pu, client) for pu in portal_urls), return_exceptions=True)
    out: list[ScrapeItem] = []
    local_hints = list(feed_hints)

    for pu, doc in zip(portal_urls, portal_docs):
        if isinstance(doc, BaseException) or doc is None:
            continue
        _, _, raw_html = doc
        if not raw_html:
            continue
        for s_item in extract_structured_items(pu, raw_html, source="discovered", query_used=query_used, max_items=20):
            c = canonicalize_url(s_item.url)
            if c not in seen_urls:
                seen_urls.add(c)
                out.append(s_item)
        for hint in extract_feed_and_ats_hints(pu, raw_html):
            if hint not in local_hints:
                local_hints.append(hint)

    if local_hints:
        feed_batches = await asyncio.gather(
            *(_parse_feed_or_ats_url(fu, client, active_proxy, query_used) for fu in local_hints[:4]),
            return_exceptions=True,
        )
        for fb in feed_batches:
            if isinstance(fb, BaseException):
                continue
            for f_item in fb:
                c = canonicalize_url(f_item.url)
                if c not in seen_urls:
                    seen_urls.add(c)
                    out.append(f_item)
    return out


async def discover_sites_and_items(
    plan: SearchPlan,
    *,
    searxng_url: str | None = None,
    proxy: str | None = None,
    max_sites: int = 6,
    max_items: int = 50,
    search_fn: Callable[[str], Any] | None = None,
    fetch_fn: Callable[..., Any] | None = None,
) -> DiscoveryResult:
    """Discover new niche websites via open web search, mine directories, and scrape/extract generically."""
    queries = build_discovery_queries(plan)
    active_proxy = resolve_proxy(proxy)
    anchor_terms = tuple(
        dict.fromkeys([*(plan.core_terms or ()), *(getattr(plan, "synonyms", ()) or ())[:8]])
    )
    anchor_low = tuple(t.lower() for t in anchor_terms if len(t.strip()) >= 2)

    async def _run_search(q: str) -> list[dict[str, str]]:
        if search_fn is not None:
            res = search_fn(q)
            return await res if asyncio.iscoroutine(res) else list(res)
        return await _search_web_raw(
            q, searxng_url=searxng_url, proxy=active_proxy, region=plan.region, max_results=12
        )

    search_batches = await asyncio.gather(*(_run_search(q) for q in queries), return_exceptions=True)

    domain_scores: dict[str, float] = {}
    roundup_candidates: list[tuple[float, str, str]] = []
    direct_items: list[ScrapeItem] = []
    seen_urls: set[str] = set()
    known_exclude = set(plan.explicit_sites) | set(plan.learned_sites)

    for q, batch in zip(queries, search_batches):
        if isinstance(batch, BaseException):
            continue
        for row in batch:
            url = (row.get("url") or "").strip()
            safe = validate_public_url(url)
            if not safe:
                continue
            canon = canonicalize_url(safe)
            title = " ".join(str(row.get("title") or "").split())
            body = " ".join(str(row.get("body") or "").split())
            host = (urlparse(safe).netloc or "").lower().removeprefix("www.")
            if not host:
                continue

            combined_low = f"{title} {body} {safe}".lower()
            overlap = sum(1 for t in anchor_low if t in combined_low)
            if _is_candidate_site(host) and overlap > 0:
                score_w = 1.0 + overlap * 0.5
                if _ROUNDUP_TITLE_RE.search(title):
                    roundup_candidates.append((score_w + 1.0, safe, title))
                elif host not in known_exclude:
                    domain_scores[host] = domain_scores.get(host, 0.0) + score_w

            if canon not in seen_urls:
                seen_urls.add(canon)
                direct_items.append(
                    ScrapeItem(
                        source="web",
                        url=safe,
                        title=title or safe,
                        body=body[:2500] or title,
                        published_at=str(row.get("published_at") or "")[:10],
                        query_used=q,
                    )
                )

    roundup_candidates.sort(key=lambda x: x[0], reverse=True)

    async def _do_fetch(target_url: str, client: httpx.AsyncClient):
        try:
            if fetch_fn is not None:
                res = fetch_fn(target_url)
                return await asyncio.wait_for(res, timeout=10.0) if asyncio.iscoroutine(res) else res
            return await asyncio.wait_for(
                stealth_fetch_document(target_url, http_client=client, timeout=8, proxy=active_proxy),
                timeout=10.0,
            )
        except Exception:
            return None

    async with httpx.AsyncClient(
        timeout=12.0,
        follow_redirects=False,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        proxy=active_proxy,
    ) as client:
        discovered_urls, extracted_items, feed_hints = await _harvest_roundups(
            [u for _, u, _ in roundup_candidates[:3]],
            _do_fetch,
            client,
            anchor_terms,
            known_exclude,
            domain_scores,
            seen_urls,
            plan.raw_query,
        )
        top_sites = [
            dom for dom, _ in sorted(domain_scores.items(), key=lambda kv: kv[1], reverse=True)
            if _is_candidate_site(dom)
        ][:max_sites]

        extracted_items.extend(await _search_discovered_sites(top_sites, plan, _run_search, seen_urls))
        extracted_items.extend(
            await _inspect_portal_feeds(top_sites, feed_hints, _do_fetch, client, active_proxy, seen_urls, plan.raw_query)
        )

    merged_items = [*extracted_items, *direct_items][:max_items]
    return DiscoveryResult(
        discovered_sites=top_sites,
        discovered_urls=list(dict.fromkeys(discovered_urls))[:30],
        items=merged_items,
        queries_used=queries + ([f"site:{d}" for d in top_sites[:4]] if top_sites else []),
    )
