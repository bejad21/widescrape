"""Zero-login Google News RSS adapter with configurable locale support."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import html
import os
import re
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
import httpx

from widescrape.adapters.base import BaseAdapter
from widescrape.expander import SearchPlan
from widescrape.fetcher import resolve_proxy
from widescrape.models import ScrapeItem


def _parse_rss_date(raw_date: str) -> tuple[str, datetime | None]:
    if not raw_date:
        return "", None
    try:
        dt = parsedate_to_datetime(raw_date.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d"), dt.astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        return "", None


def _resolve_news_locale(region: str = "") -> tuple[str, str, str]:
    raw = (region or os.environ.get("WIDESCRAPE_NEWS_REGION", "US")).strip() or "US"
    lang = os.environ.get("WIDESCRAPE_NEWS_LANG", "en").strip().lower() or "en"
    if "-" in raw or "_" in raw:
        parts = [p.strip() for p in re.split(r"[-_]", raw, maxsplit=1) if p.strip()]
        if len(parts) == 2:
            left, right = parts
            if left.lower() in ("en", "ar", "fr", "de", "es", "pt", "it", "ja", "zh", "hi") and len(right) == 2:
                lang, reg = left.lower(), right.upper()
            else:
                reg, lang = left.upper(), right.lower()
            reg = "GB" if reg == "UK" else reg
            return f"{lang}-{reg}", reg, f"{reg}:{lang}"
    reg = "GB" if raw.upper() == "UK" else raw.upper()
    return f"{lang}-{reg}", reg, f"{reg}:{lang}"


def parse_google_news_rss(xml_text: str, query: str, days: int | None = None, limit: int = 40) -> list[ScrapeItem]:
    """Parse Google News RSS XML into normalized ScrapeItems."""
    if not xml_text or "<rss" not in xml_text[:600].lower():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    now = datetime.now(timezone.utc)
    items: list[ScrapeItem] = []
    for node in root.findall("./channel/item"):
        raw_title = html.unescape((node.findtext("title") or "").strip())
        link = (node.findtext("link") or "").strip()
        if not link or not raw_title:
            continue
        pub_str, dt = _parse_rss_date(node.findtext("pubDate") or "")
        if days and dt is not None and (now - dt).total_seconds() > (days * 86400):
            continue
        source_el = node.find("source")
        publisher = (source_el.text or "").strip() if source_el is not None else ""
        title = raw_title
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(f" - {publisher}")].strip()

        desc_raw = html.unescape(node.findtext("description") or "")
        desc_text = " ".join(BeautifulSoup(desc_raw, "html.parser").get_text(" ").split())
        clean_body = re.sub(r"\s+", " ", desc_text or title)[:2000]
        if title and clean_body.startswith(title):
            trimmed = clean_body[len(title) :].lstrip(" -·:")
            if len(trimmed) >= 15:
                clean_body = trimmed

        items.append(
            ScrapeItem(
                source="news",
                url=link,
                title=title,
                body=clean_body,
                author=publisher,
                published_at=pub_str,
                engagement=0,
                query_used=query,
                metadata={"publisher": publisher},
            )
        )
        if len(items) >= limit:
            break
    return items


class GoogleNewsAdapter(BaseAdapter):
    """Fetches fresh news coverage via public Google News RSS feeds."""

    name = "news"

    async def fetch(self, plan: SearchPlan, limit: int = 100) -> list[ScrapeItem]:
        per_query = min(max(limit, 15), 100)
        max_q = 4 if (limit >= 60 or len(plan.api_queries) > 2) else 2
        queries = plan.api_queries[:max_q] if plan.api_queries else (plan.raw_query,)
        hl, gl, ceid = _resolve_news_locale(plan.region)
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

        async def _query_one(client: httpx.AsyncClient, q: str) -> list[ScrapeItem]:
            rss_q = f"{q} when:{plan.days}d" if plan.days and plan.days > 0 else q
            url = f"https://news.google.com/rss/search?q={quote_plus(rss_q)}&hl={hl}&gl={gl}&ceid={ceid}"
            resp = await client.get(url)
            if resp.status_code != 200:
                raise RuntimeError(f"Google News RSS HTTP {resp.status_code}")
            return parse_google_news_rss(resp.text, query=q, days=plan.days, limit=per_query)

        async with httpx.AsyncClient(
            timeout=12.0, follow_redirects=True, headers=headers, proxy=resolve_proxy(self.proxy)
        ) as client:
            batches = await asyncio.gather(*(_query_one(client, q) for q in queries), return_exceptions=True)

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
