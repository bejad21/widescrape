"""Zero-login Reddit adapter (PullPush + Arctic Shift global/subreddit + Reddit RSS fallback)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import html
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
import httpx

from widescrape.adapters.base import BaseAdapter
from widescrape.expander import SearchPlan
from widescrape.fetcher import resolve_proxy
from widescrape.models import ScrapeItem

DEFAULT_SUBREDDITS = {
    "jobs": ["forhire", "cscareerquestions", "AI_Agents", "RemoteJobs"],
    "research": ["LocalLLaMA", "ClaudeAI", "MachineLearning", "webdev", "webscraping"],
    "general": ["LocalLLaMA", "MachineLearning", "technology", "AskReddit"],
}
_STUB_TEXT = {"[removed]", "[deleted]", ""}


def _parse_iso_dt(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def parse_reddit_rss(xml_text: str, query: str, days: int | None = None, limit: int = 50) -> list[ScrapeItem]:
    """Parse public Reddit Atom/RSS search feed into normalized ScrapeItems."""
    if not xml_text or ("<feed" not in xml_text[:600].lower() and "<rss" not in xml_text[:600].lower()):
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    now = datetime.now(timezone.utc)
    cutoff_dt = (now - timedelta(days=days)) if (days and days > 0) else None
    items: list[ScrapeItem] = []

    for entry in root.findall("atom:entry", ns):
        title = html.unescape((entry.findtext("atom:title", default="", namespaces=ns) or "").strip())
        link_el = entry.find("atom:link", ns)
        href = link_el.attrib.get("href", "").strip() if link_el is not None else ""
        if not href or not title:
            continue
        raw_date = entry.findtext("atom:updated", default="", namespaces=ns) or ""
        pub = raw_date[:10]
        dt = _parse_iso_dt(raw_date)
        if cutoff_dt and dt is not None and dt < cutoff_dt:
            continue
        author = (entry.findtext("atom:author/atom:name", default="", namespaces=ns) or "").strip()
        # Avoid lstrip('/u/') because it strips leading 'u' from names like uae_shopper.
        author = author.removeprefix("/u/").removeprefix("u/")
        content_raw = html.unescape(entry.findtext("atom:content", default="", namespaces=ns) or "")
        body = " ".join(BeautifulSoup(content_raw, "html.parser").get_text(" ").split())[:2500]
        cat_el = entry.find("atom:category", ns)
        sub = cat_el.attrib.get("term", "") if cat_el is not None else ""
        items.append(
            ScrapeItem(
                source="reddit",
                url=href,
                title=title,
                body=body or title,
                author=author,
                published_at=pub,
                engagement=0,
                query_used=query,
                metadata={"subreddit": sub},
            )
        )
        if len(items) >= limit:
            break

    if not items:
        for node in root.findall("./channel/item"):
            title = html.unescape((node.findtext("title") or "").strip())
            href = (node.findtext("link") or "").strip()
            if not href or not title:
                continue
            pub = ""
            raw_pub = node.findtext("pubDate") or ""
            if raw_pub:
                try:
                    dt = parsedate_to_datetime(raw_pub)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    dt = dt.astimezone(timezone.utc)
                    pub = dt.strftime("%Y-%m-%d")
                    if cutoff_dt and dt < cutoff_dt:
                        continue
                except (TypeError, ValueError, IndexError):
                    pass
            desc = html.unescape(node.findtext("description") or "")
            body = " ".join(BeautifulSoup(desc, "html.parser").get_text(" ").split())[:2500]
            items.append(
                ScrapeItem(
                    source="reddit",
                    url=href,
                    title=title,
                    body=body or title,
                    published_at=pub,
                    query_used=query,
                )
            )
            if len(items) >= limit:
                break
    return items


class RedditArchiveAdapter(BaseAdapter):
    """Fetches Reddit posts via PullPush, Arctic Shift (global + subreddits), and Reddit RSS fallback."""

    name = "reddit"

    @staticmethod
    def _usable_count(items: list[ScrapeItem]) -> int:
        return sum(1 for it in items if (it.body or "").strip().lower() not in _STUB_TEXT)

    async def fetch(self, plan: SearchPlan, limit: int = 100) -> list[ScrapeItem]:
        errors: list[str] = []
        items: list[ScrapeItem] = []

        for fetcher in (self._fetch_pullpush, self._fetch_arctic_shift, self._fetch_reddit_rss):
            if self._usable_count(items) >= 5:
                break
            try:
                batch = await fetcher(plan, limit)
                items.extend(batch)
            except Exception as exc:
                errors.append(str(exc))

        if not items and errors:
            raise RuntimeError("; ".join(errors[:2]))
        return items[:limit]

    async def _fetch_pullpush(self, plan: SearchPlan, limit: int) -> list[ScrapeItem]:
        endpoint = "https://api.pullpush.io/reddit/search/submission/"
        size = min(max(limit // 2, 15), 100)
        after_epoch = (
            int((datetime.now(timezone.utc) - timedelta(days=plan.days)).timestamp())
            if plan.days and plan.days > 0
            else None
        )

        async def _query_one(client: httpx.AsyncClient, q: str) -> list[ScrapeItem]:
            out: list[ScrapeItem] = []
            params: dict[str, str | int] = {"q": q, "size": size, "sort": "desc"}
            if after_epoch is not None:
                params["after"] = after_epoch
            resp = await client.get(endpoint, params=params)
            if resp.status_code != 200:
                raise RuntimeError(f"PullPush HTTP {resp.status_code}")
            payload = resp.json()
            rows = payload.get("data", []) if isinstance(payload, dict) else []
            for post in rows:
                item = self._post_to_item(post, q)
                if item is not None:
                    out.append(item)
            return out

        async with httpx.AsyncClient(
            timeout=12.0,
            headers={"User-Agent": "widescrape/0.2"},
            proxy=resolve_proxy(self.proxy),
        ) as client:
            batches = await asyncio.gather(
                *(_query_one(client, q) for q in plan.api_queries[:3]),
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
            raise RuntimeError(errors[0])
        return items

    async def _fetch_arctic_shift(self, plan: SearchPlan, limit: int) -> list[ScrapeItem]:
        endpoint = "https://arctic-shift.photon-reddit.com/api/posts/search"
        after_param = f"{plan.days}d" if plan.days and plan.days > 0 else "90d"
        short_q = " ".join(plan.core_terms[:3]) or plan.raw_query
        subreddits = DEFAULT_SUBREDDITS.get(plan.base_mode, DEFAULT_SUBREDDITS["general"])
        per_sub = max(5, min(limit // max(len(subreddits), 1), 25))

        async def _query_params(client: httpx.AsyncClient, params: dict, label: str) -> list[ScrapeItem]:
            out: list[ScrapeItem] = []
            resp = await client.get(endpoint, params=params)
            if resp.status_code != 200:
                raise RuntimeError(f"ArcticShift HTTP {resp.status_code}")
            payload = resp.json()
            rows = payload.get("data", []) if isinstance(payload, dict) else []
            for post in rows:
                item = self._post_to_item(post, label)
                if item is not None:
                    out.append(item)
            return out

        async with httpx.AsyncClient(
            timeout=12.0,
            headers={"User-Agent": "widescrape/0.2"},
            proxy=resolve_proxy(self.proxy),
        ) as client:
            # Global query catches non-tech topics missing from DEFAULT_SUBREDDITS.
            tasks = [
                _query_params(
                    client,
                    {"title": short_q, "after": after_param, "limit": min(limit, 30), "sort": "desc"},
                    short_q,
                ),
            ]
            for sub in subreddits:
                tasks.append(
                    _query_params(
                        client,
                        {
                            "subreddit": sub,
                            "title": " ".join(plan.core_terms[:2]) or short_q,
                            "after": after_param,
                            "limit": per_sub,
                            "sort": "desc",
                        },
                        f"r/{sub}: {short_q}",
                    )
                )
            batches = await asyncio.gather(*tasks, return_exceptions=True)

        items: list[ScrapeItem] = []
        errors: list[str] = []
        for b in batches:
            if isinstance(b, BaseException):
                errors.append(str(b))
            else:
                items.extend(b)
        if not items and errors:
            raise RuntimeError(errors[0])
        return items

    async def _fetch_reddit_rss(self, plan: SearchPlan, limit: int) -> list[ScrapeItem]:
        t_param = (
            "day"
            if plan.days and plan.days <= 1
            else ("week" if plan.days and plan.days <= 7 else ("month" if plan.days and plan.days <= 31 else "year"))
        )
        q = " ".join(plan.core_terms[:3]) or plan.raw_query
        rss_url = f"https://www.reddit.com/search.rss?q={quote_plus(q)}&sort=new&t={t_param}"
        active_proxy = resolve_proxy(self.proxy)

        def _fetch_sync() -> tuple[int, str]:
            try:
                import primp

                kwargs: dict = {"impersonate": "random", "timeout": 12, "follow_redirects": True}
                if active_proxy:
                    kwargs["proxy"] = active_proxy
                client = primp.Client(**kwargs)
                resp = client.get(rss_url, headers={"Accept": "application/rss+xml, application/atom+xml, text/xml"})
                return int(getattr(resp, "status_code", 0)), str(getattr(resp, "text", ""))
            except Exception:
                return 0, ""

        status, xml_text = await asyncio.to_thread(_fetch_sync)
        if status == 200 and xml_text:
            return parse_reddit_rss(xml_text, query=q, days=plan.days, limit=limit)
        if status in (403, 429):
            raise RuntimeError(f"Reddit RSS HTTP {status}")

        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; widescrape/0.2)"},
            proxy=active_proxy,
        ) as client:
            resp = await client.get(rss_url)
            if resp.status_code != 200:
                raise RuntimeError(f"Reddit RSS HTTP {resp.status_code}")
            return parse_reddit_rss(resp.text, query=q, days=plan.days, limit=limit)

    @staticmethod
    def _post_to_item(post: dict, query_used: str) -> ScrapeItem | None:
        permalink = post.get("permalink") or ""
        full_url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else (post.get("url") or "")
        if not full_url:
            return None
        created = post.get("created_utc")
        pub = ""
        if isinstance(created, str):
            dt = _parse_iso_dt(created)
            if dt is not None:
                pub = dt.strftime("%Y-%m-%d")
        elif created:
            try:
                pub = datetime.fromtimestamp(float(created), tz=timezone.utc).strftime("%Y-%m-%d")
            except (TypeError, ValueError, OSError):
                pub = ""
        return ScrapeItem(
            source="reddit",
            url=full_url,
            title=post.get("title") or "Reddit Post",
            body=(post.get("selftext") or post.get("url") or "")[:3000],
            author=post.get("author") or "",
            published_at=pub,
            engagement=int(post.get("score") or 0) + int(post.get("num_comments") or 0),
            query_used=query_used,
            metadata={"subreddit": post.get("subreddit")},
        )
