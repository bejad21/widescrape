"""Zero-login LinkedIn Job Board adapter via public `/jobs-guest/` HTML endpoints."""

from __future__ import annotations

import asyncio
import random
import re
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from widescrape.adapters.base import BaseAdapter
from widescrape.expander import SearchPlan
from widescrape.fetcher import (
    _sync_primp_get,
    extract_jsonld_metadata,
    resolve_proxy,
)
from widescrape.models import ScrapeItem

_GUEST_SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
_GUEST_DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
_URN_ID_RE = re.compile(r"(?:jobPosting:|/jobs/view/[^/?]*?)(\d{5,15})")
_QUERY_STRIP_RE = re.compile(
    r'\b(?:OR|AND|no\s+(?:prior\s+|relevant\s+)?experience(?:\s+(?:needed|required))?|'
    r'without\s+(?:prior\s+)?experience|work\s+from\s+anywhere|worldwide|anywhere|global|'
    r'preferred|remote|wfh|uae|dubai|abu\s+dhabi|emea)\b|["()]',
    re.IGNORECASE,
)


def build_linkedin_guest_searches(plan: SearchPlan) -> list[dict[str, str | int]]:
    """Build targeted zero-login LinkedIn Guest search parameter sets (clean keywords, `f_E`, `location`, `f_WT`, `f_TPR`)."""
    raw = (plan.raw_query or "").strip()
    if not raw:
        return []
    low = raw.lower()
    wants_remote = bool(re.search(r"\b(?:remote|wfh|distributed|worldwide|anywhere)\b", low)) and "not remote" not in low and "onsite" not in low
    cleaned_kw = " ".join(_QUERY_STRIP_RE.sub(" ", raw).split()).strip()
    keywords = cleaned_kw or raw

    base: dict[str, str | int] = {"keywords": keywords}
    if plan.days and plan.days > 0:
        base["f_TPR"] = f"r{plan.days * 86400}"
    if wants_remote:
        base["f_WT"] = "2"

    # Map junior / intern / entry intent to LinkedIn experience filter (`f_E`: 1=Intern, 2=Entry, 3=Associate).
    wants_intern = bool(re.search(r"\b(?:intern|internship|grad(?:uate)?|new grad|trainee)\b", low))
    wants_entry = bool(
        re.search(
            r"\b(?:no\s+(?:prior\s+)?experience|entry[\s-]?level|junior|jr\.?|0[\s-]*(?:-|to)\s*[12]|8\s*months?|associate)\b",
            low,
        )
    )
    f_e = ""
    if wants_intern and wants_entry:
        f_e = "1,2"
    elif wants_intern:
        f_e = "1,2"
    elif wants_entry:
        f_e = "2" if "preferred" not in low and "associate" not in low else "1,2,3"

    locations: list[str | None] = []
    if re.search(r"\b(?:uae|united arab emirates|dubai|abu dhabi)\b", low):
        locations.append("United Arab Emirates")
    if re.search(r"\b(?:emea|middle east)\b", low):
        locations.append("Europe, Middle East, and Africa")
    if re.search(r"\b(?:worldwide|anywhere|global)\b", low):
        locations.append("Worldwide")

    variants: list[dict[str, str | int]] = []
    if f_e and locations:
        for loc in locations:
            variants.append({**base, "f_E": f_e, "location": loc})
        variants.append({**base, "location": locations[0]})
    elif f_e:
        variants.append({**base, "f_E": f_e})
        variants.append(dict(base))
    elif locations:
        for loc in locations:
            variants.append({**base, "location": loc})
        variants.append(dict(base))
    else:
        variants.append(dict(base))

    # Expand additional keyword searches from agent-supplied `extra_queries` or multi-word `synonyms`.
    extra_keywords: list[str] = []
    for eq in tuple(getattr(plan, "extra_queries", ()) or ())[:2]:
        c_eq = " ".join(_QUERY_STRIP_RE.sub(" ", eq).split()).strip()
        if c_eq and c_eq.lower() != keywords.lower():
            extra_keywords.append(c_eq)
    for syn in tuple(getattr(plan, "synonyms", ()) or ()):
        if " " in syn.strip():
            c_syn = " ".join(_QUERY_STRIP_RE.sub(" ", syn).split()).strip()
            if c_syn and c_syn.lower() != keywords.lower() and c_syn.lower() not in {x.lower() for x in extra_keywords}:
                extra_keywords.append(c_syn)
                if len(extra_keywords) >= 2:
                    break
    for ek in extra_keywords:
        v = {**base, "keywords": ek}
        if f_e:
            v["f_E"] = f_e
        if locations:
            v["location"] = locations[0]
        variants.append(v)

    seen_keys: set[tuple[tuple[str, str], ...]] = set()
    unique: list[dict[str, str | int]] = []
    for v in variants:
        key = tuple(sorted((k, str(val)) for k, val in v.items()))
        if key not in seen_keys:
            seen_keys.add(key)
            unique.append(v)
    return unique[:6]


def _extract_job_id(raw_urn: str, href: str) -> str:
    for val in (raw_urn, href):
        m = _URN_ID_RE.search(val or "")
        if m:
            return m.group(1)
    # Slugged URLs place the numeric job ID at the tail (e.g., /jobs/view/python-dev-at-company-4492061521).
    if m_end := re.search(r"-(\d{6,15})(?:$|[/?])", href or ""):
        return m_end.group(1)
    return ""


def parse_linkedin_guest_cards(html_text: str, query: str = "") -> list[ScrapeItem]:
    """Parse LinkedIn `seeMoreJobPostings` HTML fragments into normalized `ScrapeItem`s."""
    if not html_text or "<" not in html_text:
        return []
    soup = BeautifulSoup(html_text, "html.parser")
    cards = soup.select("div.base-card, div.job-search-card")
    if not cards:
        cards = [li for li in soup.select("li") if li.select_one("a[href*='/jobs/view/']")]

    items: list[ScrapeItem] = []
    seen_ids: set[str] = set()
    for card in cards:
        urn = str(card.get("data-entity-urn") or "")
        link_el = card.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
        raw_href = str(link_el.get("href") or "").strip() if link_el else ""
        job_id = _extract_job_id(urn, raw_href)
        clean_url = (
            raw_href.split("?")[0].strip()
            if raw_href.startswith("http")
            else (f"https://www.linkedin.com/jobs/view/{job_id}/" if job_id else "")
        )
        if not clean_url or (job_id and job_id in seen_ids):
            continue
        if job_id:
            seen_ids.add(job_id)

        title_el = card.select_one("h3.base-search-card__title, h3")
        title = " ".join(title_el.get_text(" ", strip=True).split()) if title_el else ""
        if not title:
            continue

        comp_el = card.select_one("h4.base-search-card__subtitle, a.hidden-nested-link")
        company = " ".join(comp_el.get_text(" ", strip=True).split()) if comp_el else ""

        loc_el = card.select_one("span.job-search-card__location")
        location = " ".join(loc_el.get_text(" ", strip=True).split()) if loc_el else ""

        sal_el = card.select_one("span.job-search-card__salary-info")
        salary = " ".join(sal_el.get_text(" ", strip=True).split()) if sal_el else ""

        time_el = card.select_one("time")
        pub_date = str(time_el.get("datetime") or "").strip()[:10] if time_el else ""

        jld: dict[str, str] = {}
        body_parts = []
        if company:
            jld["company"] = company
            body_parts.append(f"Company: {company}")
        if location:
            jld["location"] = location
            body_parts.append(f"Location: {location}")
            loc_low = location.lower()
            if "remote" in loc_low and "not remote" not in loc_low:
                jld["work_type"] = "remote"
        if salary:
            jld["salary"] = salary
            body_parts.append(f"Salary: {salary}")

        meta: dict = {}
        if jld:
            meta["jsonld"] = jld
        if job_id:
            meta["linkedin_job_id"] = job_id

        items.append(
            ScrapeItem(
                source="linkedin_jobs",
                url=clean_url,
                title=f"{title}" + (f" — {company}" if company else ""),
                body=" | ".join(body_parts) or title,
                author=company,
                published_at=pub_date,
                query_used=query,
                metadata=meta,
            )
        )
    return items


def parse_linkedin_guest_detail(html_text: str, item: ScrapeItem) -> None:
    """Enrich a LinkedIn Jobs item with full description, seniority, employment type, and JSON-LD."""
    if not html_text or "<" not in html_text:
        return
    soup = BeautifulSoup(html_text, "html.parser")

    jld = dict(item.metadata.get("jsonld") or {}) if isinstance(item.metadata, dict) else {}
    parsed_jld = extract_jsonld_metadata(html_text)
    for k, v in parsed_jld.items():
        jld.setdefault(k, v)
    if not item.published_at and parsed_jld.get("published_at"):
        item.published_at = parsed_jld["published_at"]

    criteria_parts: list[str] = []
    for li in soup.select("li.description__job-criteria-item"):
        h3 = li.select_one("h3")
        sp = li.select_one("span")
        key = " ".join(h3.get_text(" ", strip=True).split()) if h3 else ""
        val = " ".join(sp.get_text(" ", strip=True).split()) if sp else ""
        if key and val and val.lower() != "not applicable":
            criteria_parts.append(f"{key}: {val}")

    sal_el = soup.select_one("div.salary, span.salary, div.compensation__salary")
    if sal_el:
        sal_txt = " ".join(sal_el.get_text(" ", strip=True).split())
        if sal_txt and not jld.get("salary"):
            jld["salary"] = sal_txt
            criteria_parts.append(f"Salary: {sal_txt}")

    desc_el = soup.select_one("div.show-more-less-html__markup, div.description__text")
    desc_text = " ".join(desc_el.get_text(" ", strip=True).split()) if desc_el else ""

    if jld:
        item.metadata["jsonld"] = jld

    header = item.body.strip()
    if criteria_parts:
        header = f"{header} | {' | '.join(criteria_parts)}" if header else " | ".join(criteria_parts)
    combined = f"{header}\n\n{desc_text}".strip() if desc_text else header
    if combined:
        item.body = combined[:5000]


async def _fetch_guest_html(url: str, proxy: str | None = None) -> tuple[int, str]:
    """Fetch LinkedIn public guest HTML using thread-local `primp` TLS stealth."""
    outcome = await asyncio.to_thread(_sync_primp_get, url, 12, resolve_proxy(proxy))
    text = outcome.payload.decode("utf-8", errors="replace") if outcome.payload else ""
    return outcome.status, text


class LinkedInJobsGuestAdapter(BaseAdapter):
    """Scrapes LinkedIn Job Board search and full job descriptions via `/jobs-guest/` without login."""

    name = "linkedin_jobs_guest"

    def __init__(self, fetch_details: bool = True, proxy: str | None = None) -> None:
        super().__init__(proxy=resolve_proxy(proxy))
        self.fetch_details = fetch_details

    async def fetch(self, plan: SearchPlan, limit: int = 100) -> list[ScrapeItem]:
        query = plan.raw_query.strip()
        if not query:
            return []

        search_variants = build_linkedin_guest_searches(plan)
        if not search_variants:
            return []

        items: list[ScrapeItem] = []
        seen_urls: set[str] = set()
        errors: list[str] = []
        max_pages = max(1, min((limit // (10 * len(search_variants))) + 2, 12))
        rate_limited = False

        for v_idx, base_params in enumerate(search_variants):
            if len(items) >= limit or rate_limited:
                break
            start = 0
            for page_idx in range(max_pages):
                if len(items) >= limit:
                    break
                params = {**base_params, "start": start}
                url = f"{_GUEST_SEARCH_URL}?{urlencode(params)}"
                if v_idx > 0 or page_idx > 0:
                    await asyncio.sleep(random.uniform(0.25, 0.55))

                status, html_text = await _fetch_guest_html(url, proxy=self.proxy)
                if status == 429:
                    errors.append("LinkedIn guest rate limit (HTTP 429)")
                    rate_limited = True
                    break
                if status != 200 or not html_text:
                    errors.append(f"LinkedIn guest HTTP {status}")
                    break

                batch = parse_linkedin_guest_cards(html_text, query=query)
                if not batch:
                    break
                added = 0
                for it in batch:
                    if it.url not in seen_urls:
                        seen_urls.add(it.url)
                        items.append(it)
                        added += 1
                        if len(items) >= limit:
                            break
                start += max(len(batch), 10)
                if added == 0:
                    break

        if not items and errors:
            raise RuntimeError(errors[0])

        if self.fetch_details and items:
            detail_cap = min(len(items), max(25, min(limit, 80)))
            sem = asyncio.Semaphore(5)

            async def _enrich_one(it: ScrapeItem) -> None:
                job_id = str(it.metadata.get("linkedin_job_id") or "")
                if not job_id:
                    return
                async with sem:
                    await asyncio.sleep(random.uniform(0.08, 0.22))
                    st, detail_html = await _fetch_guest_html(
                        _GUEST_DETAIL_URL.format(job_id=job_id),
                        proxy=self.proxy,
                    )
                    if st == 200 and detail_html:
                        parse_linkedin_guest_detail(detail_html, it)

            await asyncio.gather(*(_enrich_one(it) for it in items[:detail_cap]))

        return items[:limit]
