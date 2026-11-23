"""Zero-login Hacker News Algolia search adapter."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import html

from bs4 import BeautifulSoup
import httpx

from widescrape.adapters.base import BaseAdapter
from widescrape.expander import SearchPlan
from widescrape.fetcher import resolve_proxy
from widescrape.models import ScrapeItem


class HackerNewsAdapter(BaseAdapter):
    """Queries Hacker News stories and comments via the free Algolia HN API."""

    name = "hackernews"

    async def fetch(self, plan: SearchPlan, limit: int = 100) -> list[ScrapeItem]:
        use_date_sort = bool(plan.days and 0 < plan.days <= 30)
        url = "https://hn.algolia.com/api/v1/search_by_date" if use_date_sort else "https://hn.algolia.com/api/v1/search"
        hits_per_page = min(max(limit, 10), 100)
        cutoff_epoch = (
            int((datetime.now(timezone.utc) - timedelta(days=plan.days)).timestamp())
            if plan.days and plan.days > 0
            else None
        )

        async def _query_one(client: httpx.AsyncClient, q: str) -> list[ScrapeItem]:
            out: list[ScrapeItem] = []
            params: dict[str, str | int] = {"query": q, "tags": "(story,ask_hn,show_hn)", "hitsPerPage": hits_per_page}
            if cutoff_epoch is not None:
                params["numericFilters"] = f"created_at_i>{cutoff_epoch}"
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                raise RuntimeError(f"Algolia HN HTTP {resp.status_code}")
            for hit in resp.json().get("hits", []):
                oid = hit.get("objectID", "")
                hn_url = f"https://news.ycombinator.com/item?id={oid}" if oid else (hit.get("url") or "")
                raw_body = html.unescape(hit.get("story_text") or hit.get("comment_text") or hit.get("url") or "")
                body = " ".join(BeautifulSoup(raw_body, "html.parser").get_text(" ").split())
                out.append(
                    ScrapeItem(
                        source="hackernews",
                        url=hn_url,
                        title=html.unescape(hit.get("title") or "HN Discussion"),
                        body=body[:2000],
                        author=hit.get("author") or "",
                        published_at=(hit.get("created_at") or "")[:10],
                        engagement=int(hit.get("points") or 0) + int(hit.get("num_comments") or 0),
                        query_used=q,
                        metadata={"external_url": hit.get("url")},
                    )
                )
            return out

        q_list = plan.api_queries[:4] if len(plan.api_queries) > 2 else plan.api_queries[:2]
        async with httpx.AsyncClient(timeout=15.0, proxy=resolve_proxy(self.proxy)) as client:
            batches = await asyncio.gather(
                *(_query_one(client, q) for q in q_list),
                return_exceptions=True,
            )

        items: list[ScrapeItem] = []
        errors: list[str] = []
        for b in batches:
            if isinstance(b, BaseException):
                errors.append(str(b))
            else:
                items.extend(b)
        if not items and errors:
            raise RuntimeError("; ".join(errors[:2]))
        return items[:limit]
