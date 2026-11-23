"""Optional Playwright session adapter with network JSON interception for live X & LinkedIn."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote_plus

from widescrape.adapters.base import BaseAdapter
from widescrape.expander import SearchPlan
from widescrape.fetcher import playwright_proxy_config, resolve_proxy
from widescrape.models import ScrapeItem

COOKIE_DIR = Path.home() / ".widescrape" / "cookies"

LOGIN_URLS = {
    "x": "https://x.com/i/flow/login",
    "linkedin": "https://www.linkedin.com/login",
}
_PLATFORM_ALIASES = {
    "x": "x",
    "linkedin": "linkedin",
    "linkedin_posts": "linkedin",
    "linkedin_jobs": "linkedin",
}
_JOB_ID_RE = re.compile(r"(?:jobPosting(?:Card)?\s*[:(]+\s*|/jobs/view/)(\d{5,15})", re.IGNORECASE)


def _validate_platform(platform: str) -> str:
    clean = (platform or "").strip().lower()
    if clean not in LOGIN_URLS:
        raise ValueError(f"Unsupported auth platform '{platform}'. Choose from: {', '.join(LOGIN_URLS)}")
    return clean


def cookie_path_for(platform: str) -> Path:
    """Return local Playwright storage_state path for a validated platform."""
    clean = _validate_platform(platform)
    COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    return COOKIE_DIR / f"{clean}.json"


def has_saved_session(platform: str) -> bool:
    """Check whether a saved browser storage_state exists for a validated platform."""
    clean = _validate_platform(platform)
    path = COOKIE_DIR / f"{clean}.json"
    return path.is_file() and path.stat().st_size > 20


def _parse_x_created_at(raw_date: str) -> str:
    """Parse X GraphQL timestamp (`Thu Nov 20 12:34:56 +0000 2026`) into `YYYY-MM-DD`."""
    if not raw_date:
        return ""
    try:
        dt = datetime.strptime(raw_date.strip(), "%a %b %d %H:%M:%S %z %Y")
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _extract_tweets_from_graphql(payload: Any, query: str) -> list[ScrapeItem]:
    """Recursively extract structured tweets, timestamps, long notes, and engagement from X JSON."""
    items: list[ScrapeItem] = []

    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            legacy = obj.get("legacy")
            rest_id = obj.get("rest_id")
            if isinstance(legacy, dict) and rest_id and ("full_text" in legacy or "note_tweet" in obj):
                note_text = (
                    ((obj.get("note_tweet") or {}).get("note_tweet_results") or {})
                    .get("result", {})
                    .get("text")
                    or ""
                )
                text = (note_text or legacy.get("full_text") or "").strip()
                user_res = ((obj.get("core") or {}).get("user_results") or {}).get("result") or {}
                user_legacy = user_res.get("legacy") or {}
                screen_name = user_legacy.get("screen_name") or "i"
                author = user_legacy.get("name") or screen_name
                pub_date = _parse_x_created_at(str(legacy.get("created_at") or ""))
                eng = (
                    int(legacy.get("favorite_count") or 0)
                    + int(legacy.get("retweet_count") or 0)
                    + int(legacy.get("reply_count") or 0)
                )
                if text:
                    items.append(
                        ScrapeItem(
                            source="x",
                            url=f"https://x.com/{screen_name}/status/{rest_id}",
                            title=f"{author}: {text[:70]}",
                            body=text[:2500],
                            author=screen_name,
                            published_at=pub_date,
                            engagement=eng,
                            query_used=query,
                        )
                    )
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for el in obj:
                _walk(el)

    _walk(payload)
    return items


def _extract_linkedin_from_voyager(payload: Any, query: str) -> list[ScrapeItem]:
    """Recursively extract LinkedIn posts, actors, and engagement from Voyager JSON."""
    items: list[ScrapeItem] = []

    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            urn = obj.get("entityUrn") or obj.get("urn") or ""
            commentary = obj.get("commentary")
            text = ""
            if isinstance(commentary, dict):
                text = ((commentary.get("text") or {}).get("text") or "").strip()
            elif isinstance(obj.get("title"), dict) and "text" in obj["title"]:
                summary = obj.get("summary")
                if isinstance(summary, dict) and summary.get("text"):
                    text = str(summary["text"]).strip()
            if text and ("activity:" in str(urn) or "ugcPost:" in str(urn)):
                actor = ((obj.get("actor") or {}).get("name") or {}).get("text") or ""
                url = f"https://www.linkedin.com/feed/update/{urn}"
                items.append(
                    ScrapeItem(
                        source="linkedin_posts",
                        url=url,
                        title=f"{actor or 'LinkedIn Post'}: {text[:70]}",
                        body=text[:2500],
                        author=actor,
                        query_used=query,
                    )
                )
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for el in obj:
                _walk(el)

    _walk(payload)
    return items


def _linkedin_text_field(val: Any) -> str:
    if isinstance(val, dict):
        return str(val.get("text") or "").strip()
    if isinstance(val, str):
        return val.strip()
    return ""


def _extract_linkedin_jobs_from_voyager(payload: Any, query: str) -> list[ScrapeItem]:
    """Recursively extract LinkedIn Job Board listings from Voyager jobs API JSON."""
    items: list[ScrapeItem] = []
    seen_ids: set[str] = set()

    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            raw_urn = str(obj.get("entityUrn") or obj.get("trackingUrn") or obj.get("jobPostingUrn") or "")
            m = _JOB_ID_RE.search(raw_urn)
            title = _linkedin_text_field(obj.get("jobPostingTitle")) or _linkedin_text_field(obj.get("title"))
            company = (
                _linkedin_text_field(obj.get("primaryDescription"))
                or _linkedin_text_field((obj.get("companyDetails") or {}).get("companyName"))
                or _linkedin_text_field(((obj.get("companyDetails") or {}).get("companyResolutionResult") or {}).get("name"))
            )
            location = _linkedin_text_field(obj.get("secondaryDescription")) or _linkedin_text_field(obj.get("formattedLocation"))
            if m and title and ("jobposting" in raw_urn.lower() or "jobcard" in str(obj.get("$type") or "").lower()):
                job_id = m.group(1)
                if job_id not in seen_ids:
                    seen_ids.add(job_id)
                    pub_date = ""
                    listed_ms = obj.get("listedAt")
                    if isinstance(listed_ms, (int, float)) and listed_ms > 1_000_000_000:
                        sec = listed_ms / 1000.0 if listed_ms > 10_000_000_000 else float(listed_ms)
                        pub_date = datetime.fromtimestamp(sec, timezone.utc).strftime("%Y-%m-%d")

                    body_parts = []
                    if company:
                        body_parts.append(f"Company: {company}")
                    if location:
                        body_parts.append(f"Location: {location}")
                    desc_snippet = _linkedin_text_field((obj.get("description") or {}).get("text")) or _linkedin_text_field(obj.get("jobInsight"))
                    if desc_snippet:
                        body_parts.append(desc_snippet)
                    jld: dict[str, str] = {}
                    if company:
                        jld["company"] = company
                    if location:
                        jld["location"] = location
                        if "remote" in location.lower():
                            jld["work_type"] = "remote"
                    items.append(
                        ScrapeItem(
                            source="linkedin_jobs",
                            url=f"https://www.linkedin.com/jobs/view/{job_id}/",
                            title=f"{title}" + (f" — {company}" if company else ""),
                            body=" | ".join(body_parts)[:2500] or title,
                            author=company,
                            published_at=pub_date,
                            query_used=query,
                            metadata={"jsonld": jld} if jld else {},
                        )
                    )
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for el in obj:
                _walk(el)

    _walk(payload)
    return items


async def capture_auth_session(platform: str) -> Path:
    """Open a real browser window once to capture Playwright cookies for X or LinkedIn."""
    clean = _validate_platform(platform)
    from playwright.async_api import async_playwright

    target = cookie_path_for(clean)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        try:
            context = await browser.new_context(viewport={"width": 1280, "height": 850})
            page = await context.new_page()
            await page.goto(LOGIN_URLS[clean], wait_until="domcontentloaded")
            await asyncio.to_thread(
                input,
                f"\nComplete login in the browser window for '{clean}', then press Enter here to save session...",
            )
            await context.storage_state(path=str(target))
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
        finally:
            await browser.close()
    return target


class BrowserSocialAdapter(BaseAdapter):
    """Deep live scraper for X, LinkedIn Posts, and LinkedIn Jobs using network JSON + DOM fallback."""

    name = "browser_social"

    def __init__(self, platforms: list[str] | None = None, proxy: str | None = None) -> None:
        super().__init__(proxy=resolve_proxy(proxy))
        raw = platforms or ["x", "linkedin"]
        self.platforms = [p.lower() for p in raw if p.lower() in _PLATFORM_ALIASES]

    async def fetch(self, plan: SearchPlan, limit: int = 100) -> list[ScrapeItem]:
        active = [p for p in self.platforms if has_saved_session(_PLATFORM_ALIASES[p])]
        if not active:
            return []

        from playwright.async_api import async_playwright

        items: list[ScrapeItem] = []
        pw_proxy = playwright_proxy_config(self.proxy)
        async with async_playwright() as p:
            launch_kwargs: dict = {"headless": True}
            if pw_proxy:
                launch_kwargs["proxy"] = pw_proxy
            browser = await p.chromium.launch(**launch_kwargs)
            try:
                async def _run_platform(target: str) -> list[ScrapeItem]:
                    cookie_plat = _PLATFORM_ALIASES[target]
                    state_path = str(cookie_path_for(cookie_plat))
                    context = await browser.new_context(storage_state=state_path)
                    page = await context.new_page()
                    try:
                        if target == "x":
                            return await self._scrape_x(page, plan.raw_query, limit, days=plan.days)
                        if target in ("linkedin", "linkedin_posts"):
                            return await self._scrape_linkedin(page, plan.raw_query, limit, days=plan.days)
                        if target == "linkedin_jobs":
                            wants_remote = "remote" in plan.raw_query.lower()
                            return await self._scrape_linkedin_jobs(page, plan.raw_query, limit, days=plan.days, remote=wants_remote)
                        return []
                    finally:
                        await context.close()

                batches = await asyncio.gather(*(_run_platform(plat) for plat in active), return_exceptions=True)
                errors = []
                for b in batches:
                    if isinstance(b, BaseException):
                        errors.append(str(b))
                    else:
                        items.extend(b)
                if not items and errors:
                    raise RuntimeError("; ".join(errors))
            finally:
                await browser.close()
        return items

    async def _scrape_x(self, page, query: str, limit: int, days: int | None = None) -> list[ScrapeItem]:
        effective_q = query
        if days and days > 0:
            since_str = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
            effective_q = f"{query} since:{since_str}"
        url = f"https://x.com/search?q={quote_plus(effective_q)}&src=typed_query&f=live"
        results: list[ScrapeItem] = []
        seen: set[str] = set()
        pending_tasks: set[asyncio.Task] = set()
        max_scrolls = max(6, min(limit // 4, 80))
        stale_rounds = 0

        async def _on_response(response) -> None:
            if "SearchTimeline" in response.url or "/graphql/" in response.url:
                try:
                    data = await response.json()
                    for item in _extract_tweets_from_graphql(data, query):
                        if item.url not in seen:
                            seen.add(item.url)
                            results.append(item)
                except Exception:
                    pass

        def _schedule(resp) -> None:
            task = asyncio.create_task(_on_response(resp))
            pending_tasks.add(task)
            task.add_done_callback(pending_tasks.discard)

        page.on("response", _schedule)

        try:
            await page.goto(url, timeout=25000, wait_until="domcontentloaded")
            await page.wait_for_timeout(2200)
            if "/i/flow/login" in page.url or "/login" in page.url:
                raise RuntimeError("X session expired (redirected to login); run `widescrape auth x`")

            for _ in range(max_scrolls):
                before = len(results)
                if len(results) >= limit:
                    break

                cards = await page.eval_on_selector_all(
                    'article[data-testid="tweet"]',
                    """els => els.map(el => {
                        const link = el.querySelector('a[href*="/status/"]');
                        const text = el.querySelector('div[data-testid="tweetText"]');
                        const author = el.querySelector('div[data-testid="User-Name"]');
                        const timeEl = el.querySelector('time[datetime]');
                        return {
                            href: link ? link.getAttribute('href') : '',
                            text: text ? text.innerText : '',
                            author: author ? author.innerText.split('\\n')[0] : '',
                            dt: timeEl ? timeEl.getAttribute('datetime') : ''
                        };
                    })""",
                )
                for c in cards or []:
                    href = c.get("href") or ""
                    text = (c.get("text") or "").strip()
                    full_url = f"https://x.com{href}" if href.startswith("/") else href
                    if not full_url or not text or full_url in seen:
                        continue
                    seen.add(full_url)
                    pub_dt = str(c.get("dt") or "")[:10]
                    results.append(
                        ScrapeItem(
                            source="x",
                            url=full_url,
                            title=f"{c.get('author') or 'X User'}: {text[:70]}",
                            body=text[:2500],
                            author=c.get("author") or "",
                            published_at=pub_dt,
                            query_used=query,
                        )
                    )
                    if len(results) >= limit:
                        break

                if len(results) >= limit:
                    break
                stale_rounds = stale_rounds + 1 if len(results) == before else 0
                if stale_rounds >= 3:
                    break
                await page.mouse.wheel(0, 2400)
                await page.wait_for_timeout(1000)
        finally:
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)
        return results[:limit]

    async def _scrape_linkedin(self, page, query: str, limit: int, days: int | None = None) -> list[ScrapeItem]:
        date_filter = ""
        if days and days <= 1:
            date_filter = '&datePosted=%22past-24h%22'
        elif days and days <= 7:
            date_filter = '&datePosted=%22past-week%22'
        elif days and days <= 31:
            date_filter = '&datePosted=%22past-month%22'
        url = f"https://www.linkedin.com/search/results/content/?keywords={quote_plus(query)}&sortBy=%22date_posted%22{date_filter}"
        results: list[ScrapeItem] = []
        seen: set[str] = set()
        pending_tasks: set[asyncio.Task] = set()
        max_scrolls = max(6, min(limit // 4, 80))
        stale_rounds = 0

        async def _on_response(response) -> None:
            if "voyager/api" in response.url:
                try:
                    data = await response.json()
                    for item in _extract_linkedin_from_voyager(data, query):
                        if item.url not in seen:
                            seen.add(item.url)
                            results.append(item)
                except Exception:
                    pass

        def _schedule(resp) -> None:
            task = asyncio.create_task(_on_response(resp))
            pending_tasks.add(task)
            task.add_done_callback(pending_tasks.discard)

        page.on("response", _schedule)

        try:
            await page.goto(url, timeout=25000, wait_until="domcontentloaded")
            await page.wait_for_timeout(2200)
            if "/login" in page.url or "/authwall" in page.url:
                raise RuntimeError("LinkedIn session expired (redirected to login); run `widescrape auth linkedin`")

            for _ in range(max_scrolls):
                before = len(results)
                if len(results) >= limit:
                    break

                posts = await page.eval_on_selector_all(
                    ".feed-shared-update-v2, .reusable-search__result-container",
                    """els => els.map(el => {
                        const urn = el.getAttribute('data-urn') || '';
                        const textEl = el.querySelector('.update-components-text, .feed-shared-inline-show-more-text');
                        const authorEl = el.querySelector('.update-components-actor__title, .entity-result__title-text');
                        const linkEl = el.querySelector('a[href*="/posts/"], a[href*="activity-"]');
                        return {
                            url: linkEl ? linkEl.href : (urn ? 'https://www.linkedin.com/feed/update/' + urn : ''),
                            text: textEl ? textEl.innerText : '',
                            author: authorEl ? authorEl.innerText.split('\\n')[0] : ''
                        };
                    })""",
                )
                for p_item in posts or []:
                    post_url = (p_item.get("url") or "").strip()
                    text = (p_item.get("text") or "").strip()
                    if not post_url or not text or post_url in seen:
                        continue
                    seen.add(post_url)
                    results.append(
                        ScrapeItem(
                            source="linkedin_posts",
                            url=post_url,
                            title=f"{p_item.get('author') or 'LinkedIn'}: {text[:70]}",
                            body=text[:2500],
                            author=p_item.get("author") or "",
                            query_used=query,
                        )
                    )
                    if len(results) >= limit:
                        break

                if len(results) >= limit:
                    break
                stale_rounds = stale_rounds + 1 if len(results) == before else 0
                if stale_rounds >= 3:
                    break
                try:
                    more_btn = page.locator("button:has-text('Show more results')").first
                    if await more_btn.is_visible(timeout=300):
                        await more_btn.click()
                        await page.wait_for_timeout(1000)
                except Exception:
                    pass
                await page.mouse.wheel(0, 2400)
                await page.wait_for_timeout(1100)
        finally:
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)
        return results[:limit]

    async def _scrape_linkedin_jobs(
        self, page, query: str, limit: int, days: int | None = None, remote: bool = False
    ) -> list[ScrapeItem]:
        params = [f"keywords={quote_plus(query)}", "sortBy=DD"]
        if days and days > 0:
            params.append(f"f_TPR=r{days * 86400}")
        if remote:
            params.append("f_WT=2")
        url = f"https://www.linkedin.com/jobs/search/?{'&'.join(params)}"
        results: list[ScrapeItem] = []
        seen: set[str] = set()
        pending_tasks: set[asyncio.Task] = set()
        max_scrolls = max(6, min(limit // 4, 80))
        stale_rounds = 0

        async def _on_response(response) -> None:
            if "voyager" in response.url and ("job" in response.url.lower() or "search" in response.url.lower()):
                try:
                    data = await response.json()
                    for item in _extract_linkedin_jobs_from_voyager(data, query):
                        if item.url not in seen:
                            seen.add(item.url)
                            results.append(item)
                except Exception:
                    pass

        def _schedule(resp) -> None:
            task = asyncio.create_task(_on_response(resp))
            pending_tasks.add(task)
            task.add_done_callback(pending_tasks.discard)

        page.on("response", _schedule)

        try:
            await page.goto(url, timeout=25000, wait_until="domcontentloaded")
            await page.wait_for_timeout(2200)
            if "/login" in page.url or "/authwall" in page.url or "/uas/login" in page.url:
                raise RuntimeError("LinkedIn session expired (redirected to login); run `widescrape auth linkedin`")

            for _ in range(max_scrolls):
                before = len(results)
                if len(results) >= limit:
                    break

                cards = await page.eval_on_selector_all(
                    "li[data-occludable-job-id], .job-card-container, .jobs-search-results__list-item",
                    """els => els.map(el => {
                        const jobId = el.getAttribute('data-occludable-job-id') || el.getAttribute('data-job-id') || '';
                        const linkEl = el.querySelector('a[href*="/jobs/view/"]');
                        const titleEl = el.querySelector('.job-card-list__title, .job-card-container__link, a[href*="/jobs/view/"]');
                        const compEl = el.querySelector('.job-card-container__primary-description, .artdeco-entity-lockup__subtitle');
                        const locEl = el.querySelector('.job-card-container__metadata-wrapper, .artdeco-entity-lockup__caption');
                        const timeEl = el.querySelector('time[datetime]');
                        return {
                            jobId,
                            href: linkEl ? linkEl.href : '',
                            title: titleEl ? titleEl.innerText.split('\\n')[0].trim() : '',
                            company: compEl ? compEl.innerText.trim() : '',
                            location: locEl ? locEl.innerText.trim() : '',
                            dt: timeEl ? timeEl.getAttribute('datetime') : ''
                        };
                    })""",
                )
                for c in cards or []:
                    raw_href = (c.get("href") or "").split("?")[0].strip()
                    jid = (c.get("jobId") or "").strip()
                    if not raw_href and jid:
                        raw_href = f"https://www.linkedin.com/jobs/view/{jid}/"
                    title = (c.get("title") or "").strip()
                    if not raw_href or not title or raw_href in seen:
                        continue
                    seen.add(raw_href)
                    comp = (c.get("company") or "").strip()
                    loc = (c.get("location") or "").strip()
                    body = " | ".join(p for p in (f"Company: {comp}" if comp else "", f"Location: {loc}" if loc else "") if p) or title
                    jld: dict[str, str] = {}
                    if comp:
                        jld["company"] = comp
                    if loc:
                        jld["location"] = loc
                        if "remote" in loc.lower():
                            jld["work_type"] = "remote"
                    results.append(
                        ScrapeItem(
                            source="linkedin_jobs",
                            url=raw_href,
                            title=f"{title}" + (f" — {comp}" if comp else ""),
                            body=body[:2500],
                            author=comp,
                            published_at=str(c.get("dt") or "")[:10],
                            query_used=query,
                            metadata={"jsonld": jld} if jld else {},
                        )
                    )
                    if len(results) >= limit:
                        break

                if len(results) >= limit:
                    break
                stale_rounds = stale_rounds + 1 if len(results) == before else 0
                if stale_rounds >= 3:
                    break
                try:
                    await page.evaluate(
                        """() => {
                            const pane = document.querySelector('.jobs-search-results-list, .scaffold-layout__list');
                            if (pane) pane.scrollTop += 1800;
                        }"""
                    )
                except Exception:
                    pass
                await page.mouse.wheel(0, 1800)
                await page.wait_for_timeout(1100)
        finally:
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)
        return results[:limit]
