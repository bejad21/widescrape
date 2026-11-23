"""Zero-login search-dorking, site mapping, `/llms.txt` fast-path, and SPA-aware crawling."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import html as html_mod
import logging
import re
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
from ddgs import DDGS
import httpx

from widescrape.adapters.base import BaseAdapter
from widescrape.expander import SearchPlan
from widescrape.fetcher import (
    canonicalize_url,
    extract_html_published_date,
    extract_items_from_json_payload,
    extract_jsonld_metadata,
    extract_structured_items,
    html_to_markdown,
    is_challenge_page,
    looks_like_spa_shell,
    normalize_http_url,
    playwright_proxy_config,
    resolve_proxy,
    stealth_fetch_document,
    validate_public_url,
)
from widescrape.models import ScrapeItem

logger = logging.getLogger("widescrape")

RELIABLE_BACKENDS = ("bing", "yahoo", "google", "yandex")
_STATIC_EXTS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".zip",
)
_LOC_RE = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?(https?://[^<\s]+?)(?:\]\]>)?\s*</loc>", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_MONTH_NAME_MAP = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MDY_TEXT_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+(\d{1,2}),?\s+(20\d{2})\b",
    re.IGNORECASE,
)
_DMY_TEXT_RE = re.compile(
    r"\b(\d{1,2})\s+"
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r",?\s+(20\d{2})\b",
    re.IGNORECASE,
)
_REL_AGE_RE = re.compile(r"\b(\d{1,2})\s+(hour|day|week|month)s?\s+ago\b", re.IGNORECASE)


def extract_snippet_date(raw_date_or_text: str) -> str:
    """Parse an explicit ISO, month-name, or relative age date string into YYYY-MM-DD (or '')."""
    if not raw_date_or_text:
        return ""
    sample = raw_date_or_text[:500]
    if m := _ISO_DATE_RE.search(sample):
        return m.group(1)
    if m := _MDY_TEXT_RE.search(sample):
        month = _MONTH_NAME_MAP.get(m.group(1).lower(), 0)
        day, year = int(m.group(2)), int(m.group(3))
        try:
            return datetime(year, month, day, tzinfo=timezone.utc).strftime("%Y-%m-%d")
        except ValueError:
            pass
    if m := _DMY_TEXT_RE.search(sample):
        day = int(m.group(1))
        month = _MONTH_NAME_MAP.get(m.group(2).lower(), 0)
        year = int(m.group(3))
        try:
            return datetime(year, month, day, tzinfo=timezone.utc).strftime("%Y-%m-%d")
        except ValueError:
            pass
    if m := _REL_AGE_RE.search(sample):
        qty = int(m.group(1))
        unit = m.group(2).lower()
        delta_days = {"hour": 0, "day": qty, "week": qty * 7, "month": qty * 30}.get(unit, 0)
        return (datetime.now(timezone.utc) - timedelta(days=delta_days)).strftime("%Y-%m-%d")
    return ""


def _extract_readable_html(html: str) -> tuple[str, str]:
    """Backwards-compatible alias returning (title, clean_markdown)."""
    return html_to_markdown(html)


def extract_sitemap_urls(xml_text: str) -> list[str]:
    """Extract and unescape URLs from sitemap.xml <loc> tags (handling XML & CDATA)."""
    raw_urls: list[str] = []
    try:
        root = ET.fromstring(xml_text or "")
        for el in root.iter():
            if el.tag.lower().endswith("loc") and el.text:
                raw_urls.append(html_mod.unescape(el.text.strip()))
    except ET.ParseError:
        raw_urls = [html_mod.unescape(u) for u in _LOC_RE.findall(xml_text or "")]
    return [u for u in raw_urls if u]


def extract_same_host_links(base_url: str, html: str, path_prefix: str = "") -> list[str]:
    """Extract normalized same-host page links from HTML, skipping static assets."""
    root_host = urlparse(canonicalize_url(base_url)).netloc.lower()
    clean_prefix = path_prefix.rstrip("/") if path_prefix and path_prefix != "/" else ""
    soup = BeautifulSoup(html or "", "html.parser")
    links: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        nxt = canonicalize_url(urljoin(base_url, a["href"]))
        parsed = urlparse(nxt)
        if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != root_host:
            continue
        if parsed.path.lower().endswith(_STATIC_EXTS):
            continue
        if clean_prefix and not parsed.path.startswith(clean_prefix):
            continue
        if nxt not in seen:
            seen.add(nxt)
            links.append(nxt)
    return links


async def map_site(
    start_url: str,
    filter_text: str = "",
    limit: int = 100,
    proxy: str | None = None,
) -> list[str]:
    """Cheaply discover same-domain URLs from sitemapindex/sitemap.xml, /llms.txt, and links."""
    norm_start = validate_public_url(start_url)
    if not norm_start:
        return []
    parsed = urlparse(norm_start)
    base_root = f"{parsed.scheme}://{parsed.netloc}"
    sitemap_url = f"{base_root}/sitemap.xml"
    llms_url = f"{base_root}/llms.txt"
    active_proxy = resolve_proxy(proxy)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    async with httpx.AsyncClient(
        timeout=12.0, follow_redirects=False, headers=headers, proxy=active_proxy
    ) as client:
        page_doc, sitemap_doc, llms_doc = await asyncio.gather(
            stealth_fetch_document(norm_start, http_client=client, proxy=active_proxy),
            stealth_fetch_document(sitemap_url, http_client=client, proxy=active_proxy),
            stealth_fetch_document(llms_url, http_client=client, proxy=active_proxy),
        )

        urls: list[str] = []
        seen: set[str] = set()
        candidates: list[str] = []
        child_sitemaps: list[str] = []

        if llms_doc is not None and len(llms_doc[1]) > 40 and "<html" not in llms_doc[2][:400].lower():
            candidates.append(llms_url)
        if page_doc is not None:
            head = page_doc[2][:500].lower()
            if "<sitemapindex" in head:
                child_sitemaps.extend(extract_sitemap_urls(page_doc[2]))
            elif "<urlset" in head:
                candidates.extend(extract_sitemap_urls(page_doc[2]))
            else:
                candidates.extend(extract_same_host_links(norm_start, page_doc[2]))
        if sitemap_doc is not None:
            s_head = sitemap_doc[2][:500].lower()
            if "<sitemapindex" in s_head:
                child_sitemaps.extend(extract_sitemap_urls(sitemap_doc[2]))
            else:
                candidates.extend(extract_sitemap_urls(sitemap_doc[2]))

        if child_sitemaps:
            child_docs = await asyncio.gather(
                *(
                    stealth_fetch_document(cs, http_client=client, proxy=active_proxy)
                    for cs in child_sitemaps[:5]
                    if validate_public_url(cs)
                )
            )
            for cd in child_docs:
                if cd is not None:
                    candidates.extend(extract_sitemap_urls(cd[2]))

    terms = [t.lower() for t in filter_text.split() if t.strip()]
    for u in candidates:
        norm = canonicalize_url(u)
        if norm in seen:
            continue
        if terms and not all(t in norm.lower() for t in terms):
            continue
        seen.add(norm)
        urls.append(norm)
        if len(urls) >= limit:
            break
    return urls


async def _install_ssrf_route(page) -> None:
    """Block Playwright navigations or subrequests targeting private/local/metadata IPs."""
    async def _guard(route) -> None:
        if not validate_public_url(route.request.url):
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", _guard)


async def _attach_json_sniffer(page, captured_json: list[tuple[str, object]]) -> None:
    """Passively capture public JSON XHR/Fetch API responses triggered during page render."""

    async def _on_response(resp) -> None:
        if len(captured_json) >= 12:
            return
        try:
            r_url = str(resp.url or "")
            if resp.status != 200 or not validate_public_url(r_url):
                return
            ct = (resp.headers.get("content-type") or "").lower()
            if "json" not in ct:
                return
            raw = await resp.text()
            if not raw or len(raw) > 800_000:
                return
            import json as _json

            payload = _json.loads(raw)
            if isinstance(payload, (dict, list)):
                captured_json.append((r_url, payload))
        except Exception:
            return

    page.on("response", _on_response)


async def _fetch_html_playwright(
    url: str, proxy: str | None = None, browser=None, captured_json: list[tuple[str, object]] | None = None
) -> str | None:
    if not validate_public_url(url):
        return None
    pw_proxy = playwright_proxy_config(proxy)

    if browser is not None:
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            viewport={"width": 1280, "height": 800},
        )
        try:
            page = await context.new_page()
            await _install_ssrf_route(page)
            if captured_json is not None:
                await _attach_json_sniffer(page, captured_json)
            await page.goto(url, timeout=15000, wait_until="domcontentloaded")
            await page.wait_for_timeout(1200)
            return await page.content()
        except Exception:
            return None
        finally:
            await context.close()

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None
    try:
        async with async_playwright() as p:
            launch_kwargs: dict = {"headless": True}
            if pw_proxy:
                launch_kwargs["proxy"] = pw_proxy
            owned_browser = await p.chromium.launch(**launch_kwargs)
            try:
                page = await owned_browser.new_page(viewport={"width": 1280, "height": 800})
                await _install_ssrf_route(page)
                if captured_json is not None:
                    await _attach_json_sniffer(page, captured_json)
                await page.goto(url, timeout=15000, wait_until="domcontentloaded")
                await page.wait_for_timeout(1200)
                return await page.content()
            finally:
                await owned_browser.close()
    except Exception:
        return None


async def crawl_site(
    start_url: str,
    max_pages: int = 15,
    depth: int = 1,
    strategy: str = "bfs",
    browser_fetch: bool = False,
    auto_spa: bool = True,
    use_llms_txt: bool = True,
    scope_prefix: bool = True,
    proxy: str | None = None,
) -> list[ScrapeItem]:
    """Crawl a public URL (with `/llms.txt` fast-path, `bfs`/`dfs` strategy, and shared SPA fallback)."""
    norm_start = validate_public_url(start_url)
    if not norm_start:
        return []
    parsed_root = urlparse(norm_start)
    root_host = canonicalize_url(norm_start).split("/")[2]
    start_path = parsed_root.path.rstrip("/")
    path_prefix = start_path if (scope_prefix and start_path and "." not in start_path.rsplit("/", 1)[-1]) else ""
    active_proxy = resolve_proxy(proxy)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(canonicalize_url(norm_start), 1)]
    items: list[ScrapeItem] = []
    use_dfs = strategy.strip().lower() == "dfs"
    pw_ctx = None
    shared_browser = None
    pw_sem = asyncio.Semaphore(2)

    async def _ensure_browser():
        nonlocal pw_ctx, shared_browser
        if shared_browser is not None:
            return shared_browser
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return None
        pw_ctx = await async_playwright().start()
        launch_kwargs: dict = {"headless": True}
        pw_proxy = playwright_proxy_config(active_proxy)
        if pw_proxy:
            launch_kwargs["proxy"] = pw_proxy
        shared_browser = await pw_ctx.chromium.launch(**launch_kwargs)
        return shared_browser

    try:
        async with httpx.AsyncClient(
            timeout=12.0, follow_redirects=False, headers=headers, proxy=active_proxy
        ) as client:
            while queue and len(items) < max_pages:
                batch = []
                while queue and len(batch) < 6 and (len(items) + len(batch)) < max_pages:
                    url, cur_depth = queue.pop() if use_dfs else queue.pop(0)
                    norm = canonicalize_url(url)
                    if norm in visited or not validate_public_url(norm):
                        continue
                    visited.add(norm)
                    batch.append((norm, cur_depth))

                if not batch:
                    break

                async def _grab(url: str, cur_depth: int) -> tuple[str, int, tuple[str, str, str] | None, list[ScrapeItem]]:
                    doc = await stealth_fetch_document(url, http_client=client, proxy=active_proxy)
                    xhr_items: list[ScrapeItem] = []
                    needs_browser = (
                        (doc is None and browser_fetch)
                        or (doc is not None and auto_spa and looks_like_spa_shell(doc[2], doc[1]))
                    )
                    if needs_browser:
                        captured_json: list[tuple[str, object]] = []
                        async with pw_sem:
                            browser = await _ensure_browser()
                            if browser is not None:
                                html = await _fetch_html_playwright(
                                    url, proxy=active_proxy, browser=browser, captured_json=captured_json
                                )
                                if html and not is_challenge_page(html):
                                    t, md = html_to_markdown(html)
                                    if len(md) > (len(doc[1]) if doc else 0):
                                        doc = (t, md, html)
                        for src_u, payload in captured_json:
                            xhr_items.extend(
                                extract_items_from_json_payload(src_u or url, payload, source="web", query_used=start_url)
                            )
                    return url, cur_depth, doc, xhr_items

                pages = await asyncio.gather(*(_grab(u, d) for u, d in batch))
                for url, cur_depth, doc, xhr_items in pages:
                    if doc is None and not xhr_items:
                        continue
                    if doc is not None:
                        title, text, raw_body = doc
                        if len(text) >= 40:
                            item = ScrapeItem(
                                source="web",
                                url=url,
                                title=title or url,
                                body=text[:6000],
                                published_at=extract_html_published_date(raw_body),
                                query_used=start_url,
                            )
                            _attach_html_metadata(item, raw_body)
                            items.append(item)
                        if len(items) < max_pages:
                            for sub_item in extract_structured_items(url, raw_body, source="web", query_used=start_url):
                                c_sub = canonicalize_url(sub_item.url)
                                if c_sub not in visited and len(items) < max_pages:
                                    visited.add(c_sub)
                                    items.append(sub_item)
                    else:
                        raw_body = ""
                    if len(items) < max_pages and xhr_items:
                        for sub_item in xhr_items:
                            c_sub = canonicalize_url(sub_item.url)
                            if c_sub not in visited and len(items) < max_pages:
                                visited.add(c_sub)
                                items.append(sub_item)
                    if cur_depth < depth and "<a" in raw_body.lower():
                        child_links = [
                            nxt
                            for nxt in extract_same_host_links(url, raw_body, path_prefix=path_prefix)
                            if nxt not in visited
                        ]
                        if use_dfs:
                            for nxt in reversed(child_links):
                                queue.append((nxt, cur_depth + 1))
                        else:
                            for nxt in child_links:
                                queue.append((nxt, cur_depth + 1))

            if use_llms_txt and len(items) < max_pages and not norm_start.lower().endswith("llms.txt"):
                llms_url = f"{parsed_root.scheme}://{parsed_root.netloc}/llms.txt"
                if canonicalize_url(llms_url) not in visited:
                    llms_doc = await stealth_fetch_document(llms_url, http_client=client, proxy=active_proxy)
                    if llms_doc is not None and len(llms_doc[1]) > 80 and "<html" not in llms_doc[2][:400].lower():
                        items.append(
                            ScrapeItem(
                                source="web",
                                url=llms_url,
                                title=f"llms.txt ({root_host})",
                                body=llms_doc[1][:6000],
                                query_used=norm_start,
                                metadata={"kind": "llms_txt"},
                            )
                        )
    finally:
        if shared_browser is not None:
            await shared_browser.close()
        if pw_ctx is not None:
            await pw_ctx.stop()
    return items[:max_pages]


def _attach_html_metadata(item: ScrapeItem, raw_html: str) -> None:
    """Populate missing publication date and parsed schema.org JSON-LD metadata on `item`."""
    if not raw_html:
        return
    if not item.published_at:
        item.published_at = extract_html_published_date(raw_html)
    jsonld = extract_jsonld_metadata(raw_html)
    if jsonld:
        item.metadata["jsonld"] = jsonld


async def _playwright_enrich(items: list[ScrapeItem], proxy: str | None = None) -> None:
    """Use one shared headless browser to extract SPAs when static HTTP yields thin shells."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return

    active_proxy = resolve_proxy(proxy)
    try:
        async with async_playwright() as p:
            launch_kwargs: dict = {"headless": True}
            pw_proxy = playwright_proxy_config(active_proxy)
            if pw_proxy:
                launch_kwargs["proxy"] = pw_proxy
            browser = await p.chromium.launch(**launch_kwargs)
            try:
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    viewport={"width": 1280, "height": 800},
                )
                page = await context.new_page()
                await _install_ssrf_route(page)
                for item in items:
                    if not validate_public_url(item.url):
                        continue
                    try:
                        await page.goto(item.url, timeout=15000, wait_until="domcontentloaded")
                        await page.wait_for_timeout(800)
                        html = await page.content()
                        if is_challenge_page(html):
                            continue
                        _, text = html_to_markdown(html)
                        if len(text) > len(item.body):
                            item.body = text[:5000]
                        _attach_html_metadata(item, html)
                    except Exception:
                        continue
            finally:
                await browser.close()
    except Exception as exc:
        logger.debug("Playwright enrichment failed: %s", exc)


async def enrich_items(
    items: list[ScrapeItem],
    browser_fetch: bool = False,
    proxy: str | None = None,
) -> None:
    """Fetch full HTML/PDF/Markdown text and dates for top ranked web items."""
    if not items:
        return
    active_proxy = resolve_proxy(proxy)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    sem = asyncio.Semaphore(8)

    async def _fetch_one(client: httpx.AsyncClient, item: ScrapeItem) -> ScrapeItem | None:
        async with sem:
            doc = await stealth_fetch_document(item.url, http_client=client, proxy=active_proxy)
            if doc is not None:
                _, text, raw = doc
                _attach_html_metadata(item, raw)
                if looks_like_spa_shell(raw, text):
                    return item
                if len(text) > max(len(item.body), 200):
                    item.body = text[:5000]
                    return None
                if len(text) >= len(item.body):
                    return None
            return item

    async with httpx.AsyncClient(
        timeout=10.0, follow_redirects=False, headers=headers, proxy=active_proxy
    ) as client:
        checked = await asyncio.gather(*(_fetch_one(client, it) for it in items))

    remaining = [it for it in checked if it is not None]
    if browser_fetch and remaining:
        await _playwright_enrich(remaining[:12], proxy=active_proxy)


def _interleave_unique(batches: list[list[ScrapeItem]], limit: int) -> list[ScrapeItem]:
    """Round-robin interleave dork batches so every source and open-web query gets represented."""
    items: list[ScrapeItem] = []
    seen: set[str] = set()
    max_len = max((len(b) for b in batches), default=0)
    for idx in range(max_len):
        for batch in batches:
            if idx >= len(batch):
                continue
            item = batch[idx]
            c_url = canonicalize_url(item.url)
            if c_url not in seen:
                seen.add(c_url)
                items.append(item)
                if len(items) >= limit:
                    return items
    return items


class WebDorkAdapter(BaseAdapter):
    """Uses search-dorking (SearXNG or multi-backend DDGS) + concurrent stealth extraction."""

    name = "web_dork"

    def __init__(
        self,
        searxng_url: str | None = None,
        fetch_pages: bool = False,
        browser_fetch: bool = False,
        proxy: str | None = None,
        auto_enrich: bool = True,
    ) -> None:
        self.searxng_url = searxng_url
        self.fetch_pages = fetch_pages
        self.browser_fetch = browser_fetch
        self.proxy = resolve_proxy(proxy)
        self.auto_enrich = auto_enrich

    async def fetch(self, plan: SearchPlan, limit: int = 100) -> list[ScrapeItem]:
        items: list[ScrapeItem] = []
        last_err: Exception | None = None
        if self.searxng_url:
            try:
                items = await self._fetch_via_searxng(plan, limit)
            except Exception as exc:
                last_err = exc
                logger.warning("SearXNG fetch failed (%s); falling back to DDGS", exc)
        if not items:
            try:
                items = await self._fetch_via_ddgs(plan, limit)
            except Exception as exc:
                if not items:
                    raise exc from last_err

        if self.auto_enrich and items and (self.fetch_pages or self.browser_fetch):
            await self._enrich_bodies(items[:40])
        return items[:limit]

    async def _fetch_via_searxng(self, plan: SearchPlan, limit: int) -> list[ScrapeItem]:
        pages = max(1, min(limit // 20, 6))
        sem = asyncio.Semaphore(6)
        base = self.searxng_url.rstrip("/")
        time_range = self._searx_time_range(plan.days)
        errors: list[str] = []

        async def _query_page(
            client: httpx.AsyncClient, source_label: str, dork: str, pageno: int
        ) -> list[ScrapeItem]:
            async with sem:
                try:
                    params: dict[str, str | int] = {"q": dork, "format": "json", "pageno": pageno}
                    if time_range:
                        params["time_range"] = time_range
                    resp = await client.get(f"{base}/search", params=params)
                    if resp.status_code != 200:
                        errors.append(f"HTTP {resp.status_code}")
                        return []
                    out: list[ScrapeItem] = []
                    for r in resp.json().get("results", []):
                        url = r.get("url") or ""
                        if not url:
                            continue
                        body = r.get("content") or ""
                        pub = extract_snippet_date(r.get("publishedDate") or "") or extract_snippet_date(body)
                        out.append(
                            ScrapeItem(
                                source=source_label,
                                url=url,
                                title=r.get("title") or source_label,
                                body=body,
                                published_at=pub,
                                query_used=dork,
                            )
                        )
                    return out
                except (httpx.HTTPError, ValueError) as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")
                    return []

        async with httpx.AsyncClient(timeout=15.0, proxy=self.proxy) as client:
            tasks = [
                _query_page(client, source_label, dork, page)
                for source_label, dork in plan.dork_queries
                for page in range(1, pages + 1)
            ]
            batches = await asyncio.gather(*tasks)

        items = _interleave_unique(batches, limit)
        if not items and errors:
            raise RuntimeError(f"SearXNG failed: {errors[0]}")
        return items

    @staticmethod
    def _ddgs_timelimit(days: int | None) -> str | None:
        if not days or days <= 0:
            return None
        if days <= 1:
            return "d"
        if days <= 7:
            return "w"
        if days <= 31:
            return "m"
        return "y"

    @staticmethod
    def _searx_time_range(days: int | None) -> str | None:
        if not days or days <= 0:
            return None
        if days <= 1:
            return "day"
        if days <= 7:
            return "week"
        if days <= 31:
            return "month"
        return "year"

    async def _fetch_via_ddgs(self, plan: SearchPlan, limit: int) -> list[ScrapeItem]:
        n_dorks = max(1, len(plan.dork_queries))
        # Keep total request volume bounded so site-heavy presets (like jobs) do not burst 200-500 queries.
        if n_dorks >= 12:
            pages = 2 if limit >= 180 else 1
            query_backends = [
                (source_label, dork, ("bing", "yahoo")[(idx + page - 1) % 2], page)
                for page in range(1, pages + 1)
                for idx, (source_label, dork) in enumerate(plan.dork_queries)
            ]
        elif n_dorks > 4:
            pages = 1
            query_backends = [
                (source_label, dork, backend, 1)
                for source_label, dork in plan.dork_queries
                for backend in ("bing", "yahoo")
            ]
        else:
            pages = 3 if limit >= 90 else (2 if limit >= 40 else 1)
            backends = RELIABLE_BACKENDS if limit >= 40 else ("bing", "yahoo")
            query_backends = [
                (source_label, dork, backend, page)
                for source_label, dork in plan.dork_queries
                for backend in backends
                for page in range(1, pages + 1)
            ]

        sem = asyncio.Semaphore(6)
        timelimit = self._ddgs_timelimit(plan.days)
        errors: list[str] = []

        def _query_sync(source_label: str, dork: str, backend: str, page: int) -> list[ScrapeItem]:
            collected: list[ScrapeItem] = []
            ddgs = DDGS(proxy=self.proxy) if self.proxy else DDGS()
            base_kwargs: dict = {"max_results": 20, "backend": backend, "page": page}
            if getattr(plan, "region", ""):
                base_kwargs["region"] = plan.region
            results = None
            last_exc: Exception | None = None
            if timelimit:
                try:
                    results = ddgs.text(dork, timelimit=timelimit, **base_kwargs)
                except Exception as exc:
                    last_exc = exc
                    results = None
            # Strict site: dorks with timelimit often return 0; retry without timelimit and filter locally.
            if not results:
                try:
                    results = ddgs.text(dork, **base_kwargs)
                    last_exc = None
                except Exception as exc:
                    errors.append(f"{backend}: {type(exc).__name__}: {exc}")
                    logger.debug("DDGS query failed (%s %r): %s", backend, dork, exc)
                    return []
            if last_exc and results is None:
                errors.append(f"{backend}: {type(last_exc).__name__}: {last_exc}")
            for r in results or []:
                url = r.get("href") or r.get("url") or ""
                if not url:
                    continue
                body = r.get("body") or r.get("snippet") or ""
                pub = (
                    extract_snippet_date(r.get("date") or r.get("published") or "")
                    or extract_snippet_date(body)
                )
                collected.append(
                    ScrapeItem(
                        source=source_label,
                        url=url,
                        title=r.get("title") or source_label,
                        body=body,
                        published_at=pub,
                        query_used=dork,
                    )
                )
            return collected

        async def _bounded_query(source_label: str, dork: str, backend: str, page: int) -> list[ScrapeItem]:
            async with sem:
                return await asyncio.to_thread(_query_sync, source_label, dork, backend, page)

        batches = await asyncio.gather(
            *(_bounded_query(s, d, b, p) for s, d, b, p in query_backends)
        )
        items = _interleave_unique(batches, limit)
        if not items and errors:
            raise RuntimeError(f"DDGS search failed: {errors[0]}")
        return items

    async def _enrich_bodies(self, items: list[ScrapeItem]) -> None:
        await enrich_items(items, browser_fetch=self.browser_fetch, proxy=self.proxy)

    async def _enrich_via_playwright(self, items: list[ScrapeItem]) -> None:
        await _playwright_enrich(items, proxy=self.proxy)
