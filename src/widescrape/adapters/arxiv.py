"""Zero-login arXiv academic preprint Atom API adapter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import xml.etree.ElementTree as ET

import httpx

from widescrape.adapters.base import BaseAdapter
from widescrape.expander import SearchPlan
from widescrape.fetcher import resolve_proxy
from widescrape.models import ScrapeItem

_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
_SAFE_ARXIV_TOK_RE = re.compile(r"[a-zA-Z0-9_-]+")


def build_arxiv_search_query(plan: SearchPlan) -> str:
    """Format multi-term arXiv search_query using valid `all:tok1 AND all:tok2` syntax."""
    raw_tokens = plan.core_terms[:5] if plan.core_terms else plan.raw_query.split()[:5]
    safe = [m.group(0) for tok in raw_tokens if (m := _SAFE_ARXIV_TOK_RE.search(tok))]
    if not safe:
        safe = ["machine", "learning"]
    return " AND ".join(f"all:{t}" for t in safe)


def parse_arxiv_atom(xml_text: str, query: str, days: int | None = None, limit: int = 30) -> list[ScrapeItem]:
    """Parse arXiv Atom XML feed into normalized ScrapeItems, skipping error feeds."""
    if not xml_text or "<feed" not in xml_text[:600].lower():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    now = datetime.now(timezone.utc)
    cutoff_dt = (now - timedelta(days=days)) if (days and days > 0) else None
    items: list[ScrapeItem] = []

    for entry in root.findall("atom:entry", _ATOM_NS):
        title = " ".join((entry.findtext("atom:title", default="", namespaces=_ATOM_NS) or "").split())
        url = (entry.findtext("atom:id", default="", namespaces=_ATOM_NS) or "").strip()
        if not title or not url or title.lower() == "error" or "/api/errors" in url.lower():
            continue
        raw_pub = (entry.findtext("atom:published", default="", namespaces=_ATOM_NS) or "").strip()
        published = raw_pub[:10]
        if cutoff_dt and raw_pub:
            try:
                iso_val = raw_pub.replace("Z", "+00:00")
                dt = datetime.fromisoformat(iso_val)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff_dt:
                    continue
            except ValueError:
                pass
        summary = " ".join((entry.findtext("atom:summary", default="", namespaces=_ATOM_NS) or "").split())
        authors = [
            " ".join((a.findtext("atom:name", default="", namespaces=_ATOM_NS) or "").split())
            for a in entry.findall("atom:author", _ATOM_NS)
        ]
        author_str = ", ".join(a for a in authors[:3] if a)
        items.append(
            ScrapeItem(
                source="arxiv",
                url=url,
                title=title,
                body=summary[:2500],
                author=author_str,
                published_at=published,
                engagement=0,
                query_used=query,
                metadata={"authors": authors},
            )
        )
        if len(items) >= limit:
            break
    return items


class ArxivAdapter(BaseAdapter):
    """Searches recent technical and research papers via the public arXiv Atom API."""

    name = "arxiv"

    async def fetch(self, plan: SearchPlan, limit: int = 100) -> list[ScrapeItem]:
        max_results = min(max(limit, 15), 100)
        search_q = build_arxiv_search_query(plan)
        url = "https://export.arxiv.org/api/query"
        params = {
            "search_query": search_q,
            "start": 0,
            "max_results": max_results,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "widescrape/0.2"},
            proxy=resolve_proxy(self.proxy),
        ) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                raise RuntimeError(f"arXiv HTTP {resp.status_code}")
            items = parse_arxiv_atom(resp.text, query=search_q, days=plan.days, limit=max_results)

            extra_syns = [
                " ".join(_SAFE_ARXIV_TOK_RE.findall(s))
                for s in list(getattr(plan, "extra_queries", ()) or ())[:1] + list(getattr(plan, "synonyms", ()) or ())[:2]
                if _SAFE_ARXIV_TOK_RE.findall(s)
            ]
            if extra_syns and (limit >= 30 or len(items) < limit):
                alt_terms = [t for phrase in extra_syns for t in phrase.split() if len(t) >= 2][:3]
                if alt_terms:
                    alt_q = " OR ".join(f"all:{t}" for t in dict.fromkeys(alt_terms))
                    if alt_q != search_q:
                        resp2 = await client.get(
                            url,
                            params={
                                "search_query": alt_q,
                                "start": 0,
                                "max_results": max_results,
                                "sortBy": "submittedDate",
                                "sortOrder": "descending",
                            },
                        )
                        if resp2.status_code == 200:
                            seen_urls = {it.url for it in items}
                            for it in parse_arxiv_atom(resp2.text, query=alt_q, days=plan.days, limit=max_results):
                                if it.url not in seen_urls:
                                    seen_urls.add(it.url)
                                    items.append(it)
            return items[:limit]
