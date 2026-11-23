"""Zero-login remote and startup job boards adapter (Himalayas, Remotive, Jobicy, RemoteOK, WorkingNomads, WWR)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import html as html_mod
import re
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
import httpx

from widescrape.adapters.base import BaseAdapter
from widescrape.expander import SearchPlan
from widescrape.fetcher import canonicalize_url, resolve_proxy
from widescrape.models import ScrapeItem

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, application/xml, text/xml;q=0.9, */*;q=0.8",
}

_META_NOISE_TERMS = {
    "remote", "worldwide", "anywhere", "global", "international", "wfh",
    "job", "jobs", "role", "roles", "hiring", "work", "full", "part", "time",
    "experience", "no", "prior", "without", "entry", "junior", "intern",
    "internship", "graduate", "grad", "associate", "preferred", "required",
    "uae", "dubai", "emea", "or", "and", "in", "months", "month", "years", "year",
}

_TECH_SYNONYMS: dict[str, tuple[str, ...]] = {
    "ai": (
        "ai", "artificial intelligence", "llm", "genai", "gen ai", "agent", "agents",
        "agentic", "machine learning", "ml", "deep learning", "nlp", "rag",
        "automation", "openai", "anthropic", "claude", "langchain", "applied ai",
    ),
    "ml": ("ml", "machine learning", "ai", "deep learning", "pytorch", "tensorflow"),
    "automation": ("automation", "workflow", "n8n", "zapier", "make", "agent", "rpa", "python"),
    "product": ("product", "builder", "full-stack", "fullstack", "founding", "forward deployed"),
    "builder": ("builder", "founding", "product", "full-stack", "fullstack", "engineer", "developer"),
    "engineer": ("engineer", "developer", "swe", "programmer", "architect", "specialist"),
    "developer": ("developer", "engineer", "swe", "programmer", "coder"),
}


def _clean_html_text(raw: str, max_chars: int = 4000) -> str:
    if not raw:
        return ""
    txt = BeautifulSoup(html_mod.unescape(raw), "html.parser").get_text(" ", strip=True)
    return " ".join(txt.split())[:max_chars]


def _extract_topic_tokens(plan: SearchPlan) -> list[str]:
    """Extract substantive topic tokens from the search plan, ignoring location/seniority meta-words."""
    tokens = [
        t.lower()
        for t in (plan.core_terms or ())
        if t and len(t) >= 2 and t.lower() not in _META_NOISE_TERMS and not t.isdigit()
    ]
    return list(dict.fromkeys(tokens))


def _matches_topic(
    text: str,
    topic_tokens: list[str],
    *,
    synonym_groups: dict[str, tuple[str, ...]] | None = None,
    flat_synonyms: tuple[str, ...] = (),
) -> bool:
    """Return True if a job title/body matches substantive query tokens or runtime/domain synonyms."""
    low = (text or "").lower()
    flat_match = any(
        (re.search(rf"\b{re.escape(s.lower())}\b", low) if len(s) <= 3 else s.lower() in low)
        for s in flat_synonyms
        if s and s.strip()
    )
    if not topic_tokens:
        return True if not flat_synonyms else flat_match

    syn_map = synonym_groups or {}
    expanded_hits = 0
    for tok in topic_tokens:
        merged_syns = list(syn_map.get(tok, ())) + list(_TECH_SYNONYMS.get(tok, ())) + [tok]
        if any(
            (re.search(rf"\b{re.escape(s.lower())}\b", low) if len(s) <= 3 else s.lower() in low)
            for s in merged_syns
            if s
        ):
            expanded_hits += 1
    if len(topic_tokens) == 1:
        return expanded_hits == 1 or flat_match
    if flat_match and expanded_hits >= 1:
        return True
    if ("ai" in topic_tokens or "ml" in topic_tokens) and not syn_map:
        ai_ok = any(
            (re.search(rf"\b{re.escape(s)}\b", low) if len(s) <= 3 else s in low)
            for s in _TECH_SYNONYMS["ai"]
        )
        return ai_ok and expanded_hits >= 1
    return expanded_hits >= min(2, len(topic_tokens))


def _cutoff_date_str(days: int | None) -> str:
    if not days or days <= 0:
        return ""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def _within_cutoff(pub_date: str, cutoff: str) -> bool:
    if not cutoff or not pub_date:
        return True
    return pub_date[:10] >= cutoff


def _parse_any_date(raw: object) -> str:
    if raw is None or raw == "":
        return ""
    if isinstance(raw, (int, float)):
        ts = raw / 1000.0 if raw > 1e11 else float(raw)
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, OSError):
            return ""
    s = str(raw).strip()
    if m := re.match(r"^(\d{4}-\d{2}-\d{2})", s):
        return m.group(1)
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, IndexError):
        return ""


def parse_himalayas_jobs(
    payload: dict,
    topic_tokens: list[str],
    cutoff: str,
    query: str,
    *,
    synonym_groups: dict[str, tuple[str, ...]] | None = None,
    flat_synonyms: tuple[str, ...] = (),
) -> list[ScrapeItem]:
    """Parse Himalayas `/jobs/api` JSON payload into normalized `ScrapeItem`s."""
    items: list[ScrapeItem] = []
    for row in payload.get("jobs", []) or []:
        url = (row.get("applicationLink") or row.get("guid") or "").strip()
        title = " ".join(str(row.get("title") or "").split())
        company = " ".join(str(row.get("companyName") or "").split())
        if not url or not title:
            continue
        pub = _parse_any_date(row.get("pubDate") or row.get("updatedAt"))
        if not _within_cutoff(pub, cutoff):
            continue
        desc = _clean_html_text(row.get("description") or row.get("excerpt") or "")
        locs = row.get("locationRestrictions") or []
        loc_str = ", ".join(str(x) for x in locs if x) if isinstance(locs, list) and locs else "Worldwide"
        sens = row.get("seniority") or []
        sen_str = ", ".join(str(x) for x in sens if x) if isinstance(sens, list) else str(sens or "")
        min_s, max_s = row.get("minSalary"), row.get("maxSalary")
        curr = row.get("currency") or "USD"
        sal = f"{curr} {min_s}-{max_s}" if (min_s or max_s) else ""
        emp = row.get("employmentType") or ""
        if not _matches_topic(
            f"{title} {desc[:1800]}", topic_tokens, synonym_groups=synonym_groups, flat_synonyms=flat_synonyms
        ):
            continue
        header = " | ".join(p for p in (company and f"Company: {company}", f"Location: {loc_str}", sen_str and f"Seniority: {sen_str}", sal and f"Salary: {sal}", emp and f"Type: {emp}") if p)
        items.append(
            ScrapeItem(
                source="himalayas",
                url=url,
                title=f"{title}" + (f" — {company}" if company else ""),
                body=f"{header}\n\n{desc}".strip()[:4500],
                author=company,
                published_at=pub,
                query_used=query,
                metadata={"jsonld": {"company": company, "location": loc_str, "salary": sal, "employment_type": emp, "work_type": "remote"}},
            )
        )
    return items


def parse_remotive_jobs(
    payload: dict,
    topic_tokens: list[str],
    cutoff: str,
    query: str,
    *,
    synonym_groups: dict[str, tuple[str, ...]] | None = None,
    flat_synonyms: tuple[str, ...] = (),
) -> list[ScrapeItem]:
    """Parse Remotive `/api/remote-jobs` JSON payload into normalized `ScrapeItem`s."""
    items: list[ScrapeItem] = []
    for row in payload.get("jobs", []) or []:
        url = (row.get("url") or "").strip()
        title = " ".join(str(row.get("title") or "").split())
        company = " ".join(str(row.get("company_name") or "").split())
        if not url or not title:
            continue
        pub = _parse_any_date(row.get("publication_date"))
        if not _within_cutoff(pub, cutoff):
            continue
        desc = _clean_html_text(row.get("description") or "")
        loc_str = str(row.get("candidate_required_location") or "Worldwide").strip()
        sal = str(row.get("salary") or "").strip()
        emp = str(row.get("job_type") or "").strip()
        if not _matches_topic(
            f"{title} {desc[:1800]}", topic_tokens, synonym_groups=synonym_groups, flat_synonyms=flat_synonyms
        ):
            continue
        header = " | ".join(p for p in (company and f"Company: {company}", f"Location: {loc_str}", sal and f"Salary: {sal}", emp and f"Type: {emp}") if p)
        items.append(
            ScrapeItem(
                source="remotive",
                url=url,
                title=f"{title}" + (f" — {company}" if company else ""),
                body=f"{header}\n\n{desc}".strip()[:4500],
                author=company,
                published_at=pub,
                query_used=query,
                metadata={"jsonld": {"company": company, "location": loc_str, "salary": sal, "employment_type": emp, "work_type": "remote"}},
            )
        )
    return items


def parse_jobicy_jobs(
    payload: dict,
    topic_tokens: list[str],
    cutoff: str,
    query: str,
    *,
    synonym_groups: dict[str, tuple[str, ...]] | None = None,
    flat_synonyms: tuple[str, ...] = (),
) -> list[ScrapeItem]:
    """Parse Jobicy `/api/v2/remote-jobs` JSON payload into normalized `ScrapeItem`s."""
    items: list[ScrapeItem] = []
    for row in payload.get("jobs", []) or []:
        url = (row.get("url") or "").strip()
        title = html_mod.unescape(" ".join(str(row.get("jobTitle") or "").split()))
        company = html_mod.unescape(" ".join(str(row.get("companyName") or "").split()))
        if not url or not title:
            continue
        pub = _parse_any_date(row.get("pubDate"))
        if not _within_cutoff(pub, cutoff):
            continue
        desc = _clean_html_text(row.get("jobDescription") or row.get("jobExcerpt") or "")
        loc_str = str(row.get("jobGeo") or "Anywhere").strip()
        lvl = str(row.get("jobLevel") or "").strip()
        s_min, s_max = row.get("annualSalaryMin"), row.get("annualSalaryMax")
        s_cur = row.get("salaryCurrency") or "USD"
        sal = f"{s_cur} {s_min}-{s_max}/yr" if (s_min or s_max) else ""
        emp = ", ".join(row.get("jobType") or []) if isinstance(row.get("jobType"), list) else str(row.get("jobType") or "")
        if not _matches_topic(
            f"{title} {desc[:1800]}", topic_tokens, synonym_groups=synonym_groups, flat_synonyms=flat_synonyms
        ):
            continue
        header = " | ".join(p for p in (company and f"Company: {company}", f"Location: {loc_str}", lvl and f"Level: {lvl}", sal and f"Salary: {sal}", emp and f"Type: {emp}") if p)
        items.append(
            ScrapeItem(
                source="jobicy",
                url=url,
                title=f"{title}" + (f" — {company}" if company else ""),
                body=f"{header}\n\n{desc}".strip()[:4500],
                author=company,
                published_at=pub,
                query_used=query,
                metadata={"jsonld": {"company": company, "location": loc_str, "salary": sal, "employment_type": emp, "work_type": "remote"}},
            )
        )
    return items


def parse_remoteok_jobs(
    payload: list,
    topic_tokens: list[str],
    cutoff: str,
    query: str,
    *,
    synonym_groups: dict[str, tuple[str, ...]] | None = None,
    flat_synonyms: tuple[str, ...] = (),
) -> list[ScrapeItem]:
    """Parse RemoteOK `/api` JSON list into normalized `ScrapeItem`s."""
    items: list[ScrapeItem] = []
    for row in payload or []:
        if not isinstance(row, dict) or not row.get("position"):
            continue
        url = (row.get("apply_url") or row.get("url") or "").strip()
        title = " ".join(str(row.get("position") or "").split())
        company = " ".join(str(row.get("company") or "").split())
        if not url or not title:
            continue
        pub = _parse_any_date(row.get("date") or row.get("epoch"))
        if not _within_cutoff(pub, cutoff):
            continue
        desc = _clean_html_text(row.get("description") or "")
        loc_str = str(row.get("location") or "Worldwide").strip()
        s_min, s_max = row.get("salary_min"), row.get("salary_max")
        sal = f"USD {s_min}-{s_max}/yr" if (s_min or s_max) else ""
        tags = ", ".join(str(t) for t in (row.get("tags") or [])[:6])
        if not _matches_topic(
            f"{title} {tags} {desc[:1500]}", topic_tokens, synonym_groups=synonym_groups, flat_synonyms=flat_synonyms
        ):
            continue
        header = " | ".join(p for p in (company and f"Company: {company}", f"Location: {loc_str}", sal and f"Salary: {sal}", tags and f"Tags: {tags}") if p)
        items.append(
            ScrapeItem(
                source="remoteok",
                url=url,
                title=f"{title}" + (f" — {company}" if company else ""),
                body=f"{header}\n\n{desc}".strip()[:4500],
                author=company,
                published_at=pub,
                query_used=query,
                metadata={"jsonld": {"company": company, "location": loc_str, "salary": sal, "work_type": "remote"}},
            )
        )
    return items


def parse_workingnomads_jobs(
    payload: list,
    topic_tokens: list[str],
    cutoff: str,
    query: str,
    *,
    synonym_groups: dict[str, tuple[str, ...]] | None = None,
    flat_synonyms: tuple[str, ...] = (),
) -> list[ScrapeItem]:
    """Parse WorkingNomads `/api/exposed_jobs/` JSON list into normalized `ScrapeItem`s."""
    items: list[ScrapeItem] = []
    for row in payload or []:
        if not isinstance(row, dict):
            continue
        url = (row.get("url") or "").strip()
        title = " ".join(str(row.get("title") or "").split())
        company = " ".join(str(row.get("company_name") or "").split())
        if not url or not title:
            continue
        pub = _parse_any_date(row.get("pub_date"))
        if not _within_cutoff(pub, cutoff):
            continue
        desc = _clean_html_text(row.get("description") or "")
        loc_str = str(row.get("location") or "Anywhere").strip()
        tags = str(row.get("tags") or "").strip()
        if not _matches_topic(
            f"{title} {tags} {desc[:1500]}", topic_tokens, synonym_groups=synonym_groups, flat_synonyms=flat_synonyms
        ):
            continue
        header = " | ".join(p for p in (company and f"Company: {company}", f"Location: {loc_str}", tags and f"Tags: {tags}") if p)
        items.append(
            ScrapeItem(
                source="workingnomads",
                url=url,
                title=f"{title}" + (f" — {company}" if company else ""),
                body=f"{header}\n\n{desc}".strip()[:4500],
                author=company,
                published_at=pub,
                query_used=query,
                metadata={"jsonld": {"company": company, "location": loc_str, "work_type": "remote"}},
            )
        )
    return items


def parse_wwr_rss(
    xml_text: str,
    topic_tokens: list[str],
    cutoff: str,
    query: str,
    *,
    synonym_groups: dict[str, tuple[str, ...]] | None = None,
    flat_synonyms: tuple[str, ...] = (),
) -> list[ScrapeItem]:
    """Parse WeWorkRemotely RSS feed into normalized `ScrapeItem`s."""
    if not xml_text or "<rss" not in xml_text[:600].lower():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    items: list[ScrapeItem] = []
    for node in root.findall("./channel/item"):
        raw_title = html_mod.unescape(" ".join((node.findtext("title") or "").split()))
        url = (node.findtext("link") or "").strip()
        if not url or not raw_title:
            continue
        company, title = "", raw_title
        if ": " in raw_title:
            company, title = raw_title.split(": ", 1)
        pub = _parse_any_date(node.findtext("pubDate"))
        if not _within_cutoff(pub, cutoff):
            continue
        loc_str = html_mod.unescape((node.findtext("region") or node.findtext("country") or "Anywhere").strip())
        emp = html_mod.unescape((node.findtext("type") or "").strip())
        desc = _clean_html_text(node.findtext("description") or "")
        if not _matches_topic(
            f"{title} {desc[:1800]}", topic_tokens, synonym_groups=synonym_groups, flat_synonyms=flat_synonyms
        ):
            continue
        header = " | ".join(p for p in (company and f"Company: {company}", f"Location: {loc_str}", emp and f"Type: {emp}") if p)
        items.append(
            ScrapeItem(
                source="weworkremotely",
                url=url,
                title=f"{title}" + (f" — {company}" if company else ""),
                body=f"{header}\n\n{desc}".strip()[:4500],
                author=company,
                published_at=pub,
                query_used=query,
                metadata={"jsonld": {"company": company, "location": loc_str, "employment_type": emp, "work_type": "remote"}},
            )
        )
    return items


class RemoteJobBoardsAdapter(BaseAdapter):
    """Queries public zero-login remote/startup job feeds (Himalayas, Remotive, Jobicy, RemoteOK, WorkingNomads, WWR)."""

    name = "remote_boards"

    async def fetch(self, plan: SearchPlan, limit: int = 100) -> list[ScrapeItem]:
        topic_tokens = _extract_topic_tokens(plan)
        synonym_groups = plan.synonym_map() if hasattr(plan, "synonym_map") else {}
        flat_synonyms = tuple(getattr(plan, "synonyms", ()) or ())
        cutoff = _cutoff_date_str(plan.days)
        query = plan.raw_query.strip()
        search_q = " ".join(topic_tokens[:3]) or query
        alt_search = next((s for s in flat_synonyms if s.lower() != search_q.lower()), "")

        async def _get_himalayas(client: httpx.AsyncClient, offset: int) -> list[ScrapeItem]:
            r = await client.get(f"https://himalayas.app/jobs/api?limit=50&offset={offset}")
            r.raise_for_status()
            return parse_himalayas_jobs(
                r.json(), topic_tokens, cutoff, query, synonym_groups=synonym_groups, flat_synonyms=flat_synonyms
            )

        async def _get_remotive(client: httpx.AsyncClient, search_param: str = "") -> list[ScrapeItem]:
            url = (
                f"https://remotive.com/api/remote-jobs?search={quote_plus(search_param)}&limit=100"
                if search_param
                else "https://remotive.com/api/remote-jobs?limit=100"
            )
            r = await client.get(url)
            r.raise_for_status()
            return parse_remotive_jobs(
                r.json(), topic_tokens, cutoff, query, synonym_groups=synonym_groups, flat_synonyms=flat_synonyms
            )

        async def _get_jobicy(client: httpx.AsyncClient, tag: str = "") -> list[ScrapeItem]:
            url = (
                f"https://jobicy.com/api/v2/remote-jobs?count=50&tag={quote_plus(tag)}"
                if tag
                else "https://jobicy.com/api/v2/remote-jobs?count=50"
            )
            r = await client.get(url)
            r.raise_for_status()
            return parse_jobicy_jobs(
                r.json(), topic_tokens, cutoff, query, synonym_groups=synonym_groups, flat_synonyms=flat_synonyms
            )

        async def _get_remoteok(client: httpx.AsyncClient) -> list[ScrapeItem]:
            r = await client.get("https://remoteok.com/api")
            r.raise_for_status()
            return parse_remoteok_jobs(
                r.json(), topic_tokens, cutoff, query, synonym_groups=synonym_groups, flat_synonyms=flat_synonyms
            )

        async def _get_workingnomads(client: httpx.AsyncClient) -> list[ScrapeItem]:
            r = await client.get("https://www.workingnomads.com/api/exposed_jobs/")
            r.raise_for_status()
            return parse_workingnomads_jobs(
                r.json(), topic_tokens, cutoff, query, synonym_groups=synonym_groups, flat_synonyms=flat_synonyms
            )

        async def _get_wwr(client: httpx.AsyncClient) -> list[ScrapeItem]:
            r = await client.get("https://weworkremotely.com/remote-jobs.rss")
            r.raise_for_status()
            return parse_wwr_rss(
                r.text, topic_tokens, cutoff, query, synonym_groups=synonym_groups, flat_synonyms=flat_synonyms
            )

        async with httpx.AsyncClient(
            timeout=18.0,
            follow_redirects=True,
            headers=_HEADERS,
            proxy=resolve_proxy(self.proxy),
        ) as client:
            tasks = [
                _get_himalayas(client, 0),
                _get_himalayas(client, 50),
                _get_himalayas(client, 100),
                _get_remotive(client, search_q),
                _get_remotive(client, ""),
                _get_jobicy(client, topic_tokens[0] if topic_tokens else ""),
                _get_jobicy(client, ""),
                _get_remoteok(client),
                _get_workingnomads(client),
                _get_wwr(client),
            ]
            if alt_search:
                tasks.append(_get_remotive(client, alt_search))
            batches = await asyncio.gather(*tasks, return_exceptions=True)

        seen_urls: set[str] = set()
        collected: list[ScrapeItem] = []
        errors: list[str] = []
        for batch in batches:
            if isinstance(batch, BaseException):
                errors.append(f"{type(batch).__name__}: {batch}")
                continue
            for it in batch:
                canon = canonicalize_url(it.url) or it.url
                if canon in seen_urls:
                    continue
                seen_urls.add(canon)
                collected.append(it)
                if len(collected) >= limit:
                    return collected

        if not collected and errors and len(errors) == len(batches):
            raise RuntimeError(errors[0])
        return collected[:limit]
