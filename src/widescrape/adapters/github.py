"""Zero-login GitHub repository & issue/discussion adapter (with optional GITHUB_TOKEN uplift)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os

import httpx

from widescrape.adapters.base import BaseAdapter
from widescrape.expander import SearchPlan
from widescrape.fetcher import resolve_proxy
from widescrape.models import ScrapeItem


def build_github_query(plan: SearchPlan, kind: str = "repo") -> str:
    """Build concise GitHub repo (`pushed:>=`) or issue (`updated:>=`) search queries."""
    words = plan.raw_query.split()
    if len(words) <= 4:
        base = plan.raw_query
    elif plan.core_terms:
        base = " ".join(plan.core_terms[:4])
    else:
        base = " ".join(words[:4])
    if plan.days and plan.days > 0:
        since = (datetime.now(timezone.utc) - timedelta(days=plan.days)).strftime("%Y-%m-%d")
        qualifier = "updated" if kind == "issue" else "pushed"
        return f"{base} {qualifier}:>={since}"
    return base


class GitHubAdapter(BaseAdapter):
    """Searches GitHub repositories and issues via public REST search endpoints."""

    name = "github"

    async def fetch(self, plan: SearchPlan, limit: int = 100) -> list[ScrapeItem]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "widescrape/0.2",
        }
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        per_page = max(10, min(max(limit // 2, 10), 100))
        repo_q = build_github_query(plan, kind="repo")
        issue_q = build_github_query(plan, kind="issue")

        async def _query_endpoint(
            client: httpx.AsyncClient, endpoint: str, kind: str, q_str: str
        ) -> list[ScrapeItem]:
            out: list[ScrapeItem] = []
            resp = await client.get(
                endpoint,
                params={"q": q_str, "sort": "updated", "per_page": per_page},
            )
            if resp.status_code != 200:
                raise RuntimeError(f"GitHub HTTP {resp.status_code} ({kind})")
            for row in resp.json().get("items", []):
                if kind == "repo":
                    out.append(
                        ScrapeItem(
                            source="github",
                            url=row.get("html_url", ""),
                            title=row.get("full_name", "GitHub Repo"),
                            body=row.get("description") or "",
                            author=(row.get("owner") or {}).get("login", ""),
                            published_at=(row.get("pushed_at") or "")[:10],
                            engagement=int(row.get("stargazers_count") or 0),
                            query_used=q_str,
                            metadata={"kind": "repo", "language": row.get("language")},
                        )
                    )
                else:
                    out.append(
                        ScrapeItem(
                            source="github",
                            url=row.get("html_url", ""),
                            title=row.get("title", "GitHub Issue"),
                            body=(row.get("body") or "")[:1500],
                            author=(row.get("user") or {}).get("login", ""),
                            published_at=(row.get("updated_at") or "")[:10],
                            engagement=int(row.get("comments") or 0),
                            query_used=q_str,
                            metadata={"kind": "issue"},
                        )
                    )
            return out

        async with httpx.AsyncClient(timeout=15.0, headers=headers, proxy=resolve_proxy(self.proxy)) as client:
            tasks = [
                _query_endpoint(client, "https://api.github.com/search/repositories", "repo", repo_q),
                _query_endpoint(client, "https://api.github.com/search/issues", "issue", issue_q),
            ]
            if len(plan.api_queries) > 1 and (limit >= 40 or getattr(plan, "synonyms", ())):
                alt_raw = plan.api_queries[1]
                alt_q = (
                    f"{' '.join(alt_raw.split()[:4])} pushed:>={(datetime.now(timezone.utc) - timedelta(days=plan.days)).strftime('%Y-%m-%d')}"
                    if plan.days and plan.days > 0
                    else " ".join(alt_raw.split()[:4])
                )
                if alt_q != repo_q:
                    tasks.append(_query_endpoint(client, "https://api.github.com/search/repositories", "repo", alt_q))
            batches = await asyncio.gather(*tasks, return_exceptions=True)

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
