"""Zero-login StackExchange / StackOverflow Q&A search adapter."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import html
import os

from bs4 import BeautifulSoup
import httpx

from widescrape.adapters.base import BaseAdapter
from widescrape.expander import SearchPlan
from widescrape.fetcher import resolve_proxy
from widescrape.models import ScrapeItem


class StackExchangeAdapter(BaseAdapter):
    """Queries public StackOverflow Q&A via StackExchange API v2.3 (with body text)."""

    name = "stackexchange"

    async def fetch(self, plan: SearchPlan, limit: int = 100) -> list[ScrapeItem]:
        endpoint = "https://api.stackexchange.com/2.3/search/advanced"
        pagesize = min(max(limit, 15), 100)
        terms = list(plan.core_terms) if plan.core_terms else plan.raw_query.split()
        q_primary = " ".join(terms[:5]) if terms else plan.raw_query
        q_short = " ".join(terms[:2]) if len(terms) > 2 else ""
        se_key = os.environ.get("STACKEXCHANGE_KEY", "").strip()

        async def _query(client: httpx.AsyncClient, q_str: str) -> tuple[list[dict], int]:
            params: dict[str, str | int] = {
                "order": "desc",
                "sort": "relevance",
                "q": q_str,
                "site": "stackoverflow",
                "pagesize": pagesize,
                "filter": "withbody",
            }
            if se_key:
                params["key"] = se_key
            if plan.days and plan.days > 0:
                cutoff = int((datetime.now(timezone.utc) - timedelta(days=plan.days)).timestamp())
                params["fromdate"] = cutoff
            resp = await client.get(endpoint, params=params)
            if resp.status_code != 200:
                raise RuntimeError(f"StackExchange HTTP {resp.status_code}")
            payload = resp.json() if resp.content else {}
            return payload.get("items", []) or [], int(payload.get("backoff") or 0)

        used_query = q_primary
        async with httpx.AsyncClient(
            timeout=12.0,
            headers={"User-Agent": "widescrape/0.2"},
            proxy=resolve_proxy(self.proxy),
        ) as client:
            raw_rows, backoff = await _query(client, q_primary)
            # StackExchange ANDs all terms; retry top 2 terms when fromdate returns empty.
            if not raw_rows and q_short:
                if backoff > 0:
                    await asyncio.sleep(min(backoff, 5))
                used_query = q_short
                raw_rows, backoff = await _query(client, q_short)
            extra_syns = list(getattr(plan, "extra_queries", ()) or ())[:1] + list(getattr(plan, "synonyms", ()) or ())[:1]
            if extra_syns and len(raw_rows) < limit:
                alt_q = " ".join(extra_syns[0].split()[:4])
                if alt_q and alt_q.lower() != used_query.lower():
                    if backoff > 0:
                        await asyncio.sleep(min(backoff, 5))
                    alt_rows, _ = await _query(client, alt_q)
                    seen_ids = {r.get("link") for r in raw_rows}
                    for r in alt_rows:
                        if r.get("link") not in seen_ids:
                            seen_ids.add(r.get("link"))
                            raw_rows.append(r)

        items: list[ScrapeItem] = []
        for row in raw_rows:
            url = row.get("link") or ""
            if not url:
                continue
            ts = row.get("last_activity_date") or row.get("creation_date") or 0
            pub = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d") if ts else ""
            tags = ", ".join(row.get("tags") or [])
            answered = "Answered" if row.get("is_answered") else "Open"
            raw_html = html.unescape(row.get("body") or "")
            plain_body = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(" ").split())[:2000]
            header = f"[{answered} · {row.get('answer_count', 0)} answers · Tags: {tags}]"
            full_body = f"{header} {plain_body}".strip()
            items.append(
                ScrapeItem(
                    source="stackexchange",
                    url=url,
                    title=html.unescape(row.get("title") or "StackOverflow Question"),
                    body=full_body,
                    author=(row.get("owner") or {}).get("display_name", ""),
                    published_at=pub,
                    engagement=int(row.get("score") or 0) + int(row.get("answer_count") or 0),
                    query_used=used_query,
                    metadata={"is_answered": bool(row.get("is_answered")), "tags": row.get("tags") or []},
                )
            )
        return items[:limit]
