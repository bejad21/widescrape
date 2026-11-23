"""Shared SSRF-guarded TLS-stealth fetcher, HTML sanitizer, PDF extractor, and URL utilities."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import html as html_mod
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
import httpx

CHALLENGE_SIGNATURES = (
    "attention required! | cloudflare",
    "cf-browser-verification",
    "just a moment...",
    "enable javascript and cookies to continue",
    "checking your browser before accessing",
    "verify you are human",
    "/sp/captcha-block",
)

TEXT_CONTENT_TYPES = (
    "text/html",
    "text/plain",
    "text/markdown",
    "text/x-markdown",
    "application/json",
    "application/xml",
    "text/xml",
)

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "ref",
}

_SPA_MARKERS = (
    'id="root"',
    'id="app"',
    'id="__next"',
    "data-reactroot",
    "enable javascript to run this app",
    "you need to enable javascript",
)

_HIDDEN_OPACITY_RE = re.compile(r"(?:^|;)\s*opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)", re.IGNORECASE)
_ZERO_FONT_RE = re.compile(r"(?:^|;)\s*font-size\s*:\s*0(?:px|em|rem|%)?\s*(?:;|$)", re.IGNORECASE)
_TRANSPARENT_RE = re.compile(r"(?:^|;)\s*color\s*:\s*transparent\s*(?:;|$)", re.IGNORECASE)
_HIDDEN_CLASSES = {"sr-only", "visually-hidden", "screen-reader-text", "assistive-text"}
_BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "metadata.google.internal"}

MAX_FETCH_BYTES = 5 * 1024 * 1024  # 5 MB payload guard
MAX_REDIRECT_HOPS = 5


@dataclass(frozen=True)
class _FetchOutcome:
    """Internal transport result preserving terminal status to skip redundant retries."""

    ok: bool
    status: int = 0
    content_type: str = ""
    payload: bytes = b""
    terminal: bool = False


def resolve_proxy(proxy: str | None = None) -> str | None:
    """Return explicit proxy or `WIDESCRAPE_PROXY` environment variable."""
    val = (proxy if proxy is not None else os.environ.get("WIDESCRAPE_PROXY", "")).strip()
    return val or None


def playwright_proxy_config(proxy: str | None) -> dict | None:
    """Convert a proxy URL into Playwright's dict, separating username/password if present."""
    resolved = resolve_proxy(proxy)
    if not resolved:
        return None
    parsed = urlparse(resolved)
    cfg: dict[str, str] = {"server": f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")}
    if parsed.username is not None:
        cfg["username"] = parsed.username
    if parsed.password is not None:
        cfg["password"] = parsed.password
    return cfg


def format_untrusted_block(url: str, body: str) -> str:
    """Wrap scraped content in randomized nonce boundaries to defeat tag-breakout injection."""
    nonce = secrets.token_hex(4)
    safe_url = html_mod.escape((url or "").strip(), quote=True)
    safe_body = (body or "").replace("</untrusted_web_data", "&lt;/untrusted_web_data")
    return f'<untrusted_web_data id="ws_{nonce}" url="{safe_url}">\n{safe_body}\n</untrusted_web_data id="ws_{nonce}">'


def normalize_http_url(url: str) -> str | None:
    """Normalize bare domains to https://, strip NBSP/whitespace, and reject non-HTTP schemes."""
    raw = (url or "").replace("\xa0", "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return None
    return raw


def _is_non_public_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )


def validate_public_url(url: str) -> str | None:
    """Validate URL scheme and reject loopback, private RFC1918, link-local, or metadata hosts."""
    norm = normalize_http_url(url)
    if not norm:
        return None
    if os.environ.get("WIDESCRAPE_ALLOW_PRIVATE_URLS", "").strip() == "1":
        return norm

    parsed = urlparse(norm)
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or host in _BLOCKED_HOSTS or host.endswith((".localhost", ".local", ".internal")):
        return None

    try:
        ip_literal = ipaddress.ip_address(host)
        return None if _is_non_public_ip(ip_literal) else norm
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        for info in infos:
            addr_raw = info[4][0].split("%")[0]
            try:
                ip_resolved = ipaddress.ip_address(addr_raw)
                if _is_non_public_ip(ip_resolved):
                    return None
            except ValueError:
                return None
    except OSError:
        # Allow offline test hosts when DNS is unavailable; literal IPs and local names are already blocked.
        return norm
    return norm


def canonicalize_url(url: str) -> str:
    """Strip fragments, tracking params, leading `www.`, and normalize param ordering."""
    cleaned = (url or "").replace("\xa0", "").strip()
    parsed = urlparse(cleaned)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    drop_keys = set(TRACKING_PARAMS)
    if host in ("x.com", "twitter.com"):
        drop_keys.add("s")

    kept_params = sorted(
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=False) if k.lower() not in drop_keys
    )
    clean_query = urlencode(kept_params)
    clean_path = parsed.path.rstrip("/") or "/"
    if clean_path == "/" and not clean_query:
        clean_path = ""
    return urlunparse((parsed.scheme.lower(), host, clean_path, "", clean_query, ""))


def is_challenge_page(text: str) -> bool:
    """Detect Cloudflare/CAPTCHA/bot-wall HTML pages so we never save garbage."""
    sample = text[:3000].lower()
    return any(sig in sample for sig in CHALLENGE_SIGNATURES)


def looks_like_spa_shell(raw_html: str, extracted_md: str) -> bool:
    """Detect empty JS SPA shells that need headless browser rendering."""
    if len(extracted_md.strip()) >= 250:
        return False
    lower = raw_html[:8000].lower()
    return any(marker in lower for marker in _SPA_MARKERS)


def _strip_hidden_and_comments(soup: BeautifulSoup) -> None:
    """Prompt-injection defense: remove HTML comments, inert templates, and CSS-hidden nodes."""
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()
    for tag in list(soup.find_all(True)):
        if not isinstance(tag, Tag) or tag.attrs is None:
            continue
        tname = (tag.name or "").lower()
        if tname in ("template", "iframe", "object", "embed"):
            tag.decompose()
            continue
        if tag.has_attr("hidden") or str(tag.get("aria-hidden", "")).lower() == "true":
            tag.decompose()
            continue
        classes = {str(c).lower() for c in (tag.get("class") or [])}
        if classes & _HIDDEN_CLASSES:
            tag.decompose()
            continue
        style = str(tag.get("style") or "").replace(" ", "").lower()
        if (
            "display:none" in style
            or "visibility:hidden" in style
            or "visibility:collapse" in style
            or "text-indent:-999" in style
            or _HIDDEN_OPACITY_RE.search(style)
            or _ZERO_FONT_RE.search(style)
            or _TRANSPARENT_RE.search(style)
        ):
            tag.decompose()


def _node_to_md(node: Tag | NavigableString, inside_content_root: bool = False) -> str:
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""
    name = (node.name or "").lower()
    if name in ("script", "style", "nav", "footer", "aside", "noscript", "svg", "template", "iframe"):
        return ""
    # Drop top-level site chrome <header>, but keep article/section headers inside content roots.
    if name == "header" and not inside_content_root:
        return ""
    if name in ("h1", "h2", "h3", "h4"):
        level = "#" * int(name[1])
        txt = " ".join(node.get_text(" ", strip=True).split())
        return f"\n\n{level} {txt}\n\n" if txt else ""
    if name == "pre":
        code = node.get_text("\n", strip=True)
        return f"\n\n```\n{code}\n```\n\n" if code else ""
    if name == "code" and node.parent and node.parent.name != "pre":
        txt = node.get_text(" ", strip=True)
        return f"`{txt}`" if txt else ""
    if name == "li":
        txt = " ".join("".join(_node_to_md(c, inside_content_root) for c in node.children).split())
        return f"\n- {txt}" if txt else ""
    if name in ("th", "td"):
        cell = " ".join("".join(_node_to_md(c, True) for c in node.children).split())
        return f"{cell} | " if cell else ""
    if name == "tr":
        cells = [
            " ".join("".join(_node_to_md(cell, True) for cell in c.children).split())
            for c in node.find_all(["th", "td"], recursive=False)
        ]
        if not cells:
            row_txt = "".join(_node_to_md(c, True) for c in node.children).strip(" |")
        else:
            row_txt = " | ".join(c for c in cells if c)
        return f"\n{row_txt}\n" if row_txt else ""
    if name == "a":
        txt = " ".join(node.get_text(" ", strip=True).split())
        href = (node.get("href") or "").strip()
        if txt and href.startswith(("http://", "https://")):
            return f"[{txt}]({href})"
        return txt
    is_content = inside_content_root or name in ("main", "article")
    if name in ("p", "div", "section", "article", "blockquote", "table", "header"):
        inner = "".join(_node_to_md(c, is_content) for c in node.children)
        cleaned = re.sub(r"[ \t]+", " ", inner).strip()
        return f"\n\n{cleaned}\n\n" if cleaned else ""
    return "".join(_node_to_md(c, is_content) for c in node.children)


_JSONLD_SCRIPT_RE = re.compile(
    r'(?is)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>'
)


def _walk_jsonld_nodes(obj: object) -> list[dict]:
    nodes: list[dict] = []
    if isinstance(obj, dict):
        nodes.append(obj)
        for key in ("@graph", "itemListElement", "mainEntity"):
            sub = obj.get(key)
            if isinstance(sub, (list, dict)):
                nodes.extend(_walk_jsonld_nodes(sub))
    elif isinstance(obj, list):
        for entry in obj:
            nodes.extend(_walk_jsonld_nodes(entry))
    return nodes


def _norm_iso_date(val: object) -> str:
    if isinstance(val, str):
        m = re.match(r"^\s*(\d{4}-\d{2}-\d{2})", val)
        if m:
            return m.group(1)
    return ""


def _extract_salary_from_jsonld(base_salary: object) -> str:
    if not isinstance(base_salary, dict):
        return str(base_salary).strip() if isinstance(base_salary, (str, int, float)) and str(base_salary).strip() else ""
    currency = str(base_salary.get("currency") or "").strip().upper()
    val = base_salary.get("value")
    unit = ""
    low = high = None
    if isinstance(val, dict):
        low = val.get("minValue")
        high = val.get("maxValue")
        single = val.get("value")
        unit_raw = str(val.get("unitText") or "").strip().lower()
        if unit_raw in ("year", "yr", "annual", "annually"):
            unit = "/yr"
        elif unit_raw in ("month", "mo", "monthly"):
            unit = "/mo"
        elif unit_raw in ("hour", "hr", "hourly"):
            unit = "/hr"
        if low is None and high is None and single is not None:
            low = single
    elif isinstance(val, (int, float, str)) and str(val).strip():
        low = val

    if low is None and high is None:
        return ""
    cur_prefix = f"{currency} " if currency else "$"
    if low is not None and high is not None and str(low) != str(high):
        return f"{cur_prefix}{low}-{high}{unit}"
    return f"{cur_prefix}{low if low is not None else high}{unit}"


def extract_jsonld_metadata(raw_html: str) -> dict[str, str]:
    """Extract schema.org JobPosting, Offer, Product, and Article fields from JSON-LD scripts."""
    if not raw_html or "ld+json" not in raw_html.lower():
        return {}
    out: dict[str, str] = {}
    for block in _JSONLD_SCRIPT_RE.findall(raw_html[:180000]):
        txt = block.strip()
        if not txt:
            continue
        try:
            parsed = json.loads(txt)
        except ValueError:
            continue
        for node in _walk_jsonld_nodes(parsed):
            if pub := (_norm_iso_date(node.get("datePublished")) or _norm_iso_date(node.get("datePosted"))):
                out.setdefault("published_at", pub)
            if deadline := (_norm_iso_date(node.get("validThrough")) or _norm_iso_date(node.get("priceValidUntil"))):
                out.setdefault("deadline", deadline)
            org = node.get("hiringOrganization") or node.get("brand") or node.get("seller")
            if isinstance(org, dict) and org.get("name"):
                out.setdefault("company", " ".join(str(org["name"]).split())[:80])
            elif isinstance(org, str) and org.strip():
                out.setdefault("company", " ".join(org.split())[:80])

            if str(node.get("jobLocationType") or "").upper() == "TELECOMMUTE":
                out.setdefault("work_type", "remote")
            jloc = node.get("jobLocation")
            if isinstance(jloc, list) and jloc:
                jloc = jloc[0]
            if isinstance(jloc, dict):
                addr = jloc.get("address")
                if isinstance(addr, dict):
                    parts = [str(addr.get(k) or "").strip() for k in ("addressLocality", "addressCountry") if addr.get(k)]
                    if parts:
                        out.setdefault("location", ", ".join(parts)[:80])

            if sal := _extract_salary_from_jsonld(node.get("baseSalary")):
                out.setdefault("salary", sal)

            offers = node.get("offers")
            offer_nodes = offers if isinstance(offers, list) else ([offers] if isinstance(offers, dict) else [])
            if str(node.get("@type") or "").lower().endswith("offer"):
                offer_nodes = [node, *offer_nodes]
            for off in offer_nodes:
                if not isinstance(off, dict):
                    continue
                price = off.get("price") or off.get("lowPrice")
                cur = str(off.get("priceCurrency") or "").strip().upper()
                if price is not None and str(price).strip():
                    out.setdefault("price", f"{cur} {price}".strip() if cur else str(price).strip())
                if off_dl := (_norm_iso_date(off.get("priceValidUntil")) or _norm_iso_date(off.get("validThrough"))):
                    out.setdefault("deadline", off_dl)
                for ckey in ("couponCode", "discountCode"):
                    if off.get(ckey):
                        out.setdefault("code", str(off[ckey]).strip()[:32])
    return out


_STRUCTURED_ENTITY_TYPES = frozenset({
    "jobposting", "product", "offer", "article", "newsarticle", "blogposting",
    "scholarlyarticle", "techarticle", "dataset", "event", "course",
    "softwareapplication", "webapplication", "listitem", "review",
})
_HYDRATION_SCRIPT_RE = re.compile(
    r'(?is)<script[^>]*(?:id=["\']__NEXT_DATA__["\']|type=["\']application/json["\'])[^>]*>(.*?)</script>'
)
_FEED_LINK_RE = re.compile(
    r'(?is)<link[^>]+rel=["\'][^"\']*alternate[^"\']*["\'][^>]+href=["\']([^"\']+)["\'][^>]*>'
)
_ATS_HINT_PATTERNS = (
    re.compile(r'https?://(?:boards|job-boards)\.greenhouse\.io/([a-zA-Z0-9_-]{2,60})', re.IGNORECASE),
    re.compile(r'https?://jobs\.ashbyhq\.com/([a-zA-Z0-9_.-]{2,60})', re.IGNORECASE),
    re.compile(r'https?://jobs\.lever\.co/([a-zA-Z0-9_.-]{2,60})', re.IGNORECASE),
)


def _strip_markup(raw: object, max_chars: int = 3500) -> str:
    txt = str(raw or "").strip()
    if not txt:
        return ""
    if "<" in txt and ">" in txt:
        txt = BeautifulSoup(txt, "html.parser").get_text(" ", strip=True)
    return " ".join(txt.split())[:max_chars]


def extract_jsonld_items(
    base_url: str,
    raw_html: str,
    *,
    source: str = "web",
    query_used: str = "",
    max_items: int = 40,
) -> list:
    """Extract list-level Schema.org entities (JobPosting, Product, Article, Dataset, Event) as ScrapeItems."""
    from widescrape.models import ScrapeItem

    if not raw_html or "ld+json" not in raw_html.lower():
        return []
    out: list[ScrapeItem] = []
    seen_urls: set[str] = set()

    for block in _JSONLD_SCRIPT_RE.findall(raw_html[:350000]):
        txt = block.strip()
        if not txt:
            continue
        try:
            parsed = json.loads(txt)
        except ValueError:
            continue
        for raw_node in _walk_jsonld_nodes(parsed):
            # ItemList entries wrap the actual entity inside `.item`.
            node = raw_node.get("item") if isinstance(raw_node.get("item"), dict) else raw_node
            if not isinstance(node, dict):
                continue
            raw_type = node.get("@type")
            types = [t.lower() for t in raw_type] if isinstance(raw_type, list) else [str(raw_type or "").lower()]
            if not any(t.rsplit("/", 1)[-1] in _STRUCTURED_ENTITY_TYPES for t in types):
                continue
            title = " ".join(str(node.get("title") or node.get("headline") or node.get("name") or "").split())
            raw_link = str(node.get("url") or node.get("sameAs") or raw_node.get("url") or "").strip()
            if not title or len(title) < 4:
                continue
            abs_url = urljoin(base_url, raw_link) if raw_link else ""
            if not abs_url.startswith(("http://", "https://")):
                continue
            canon = canonicalize_url(abs_url)
            if canon in seen_urls:
                continue

            desc = _strip_markup(node.get("description") or node.get("articleBody") or node.get("abstract") or "")
            pub = _norm_iso_date(node.get("datePublished")) or _norm_iso_date(node.get("datePosted"))
            org = node.get("hiringOrganization") or node.get("brand") or node.get("publisher") or node.get("seller")
            org_name = (
                " ".join(str(org.get("name")).split())[:80]
                if isinstance(org, dict) and org.get("name")
                else (" ".join(str(org).split())[:80] if isinstance(org, str) and org.strip() else "")
            )
            sal = _extract_salary_from_jsonld(node.get("baseSalary"))
            work_type = "remote" if str(node.get("jobLocationType") or "").upper() == "TELECOMMUTE" else ""
            loc = ""
            jloc = node.get("jobLocation")
            if isinstance(jloc, list) and jloc:
                jloc = jloc[0]
            if isinstance(jloc, dict) and isinstance(jloc.get("address"), dict):
                addr = jloc["address"]
                loc = ", ".join(str(addr.get(k) or "").strip() for k in ("addressLocality", "addressCountry") if addr.get(k))
            price = ""
            offers = node.get("offers")
            offer_obj = offers[0] if isinstance(offers, list) and offers else (offers if isinstance(offers, dict) else None)
            if isinstance(offer_obj, dict) and (offer_obj.get("price") or offer_obj.get("lowPrice")):
                p_val = offer_obj.get("price") or offer_obj.get("lowPrice")
                p_cur = str(offer_obj.get("priceCurrency") or "").strip().upper()
                price = f"{p_cur} {p_val}".strip() if p_cur else str(p_val).strip()

            jld_meta = {k: v for k, v in {
                "company": org_name, "salary": sal, "work_type": work_type, "location": loc, "price": price
            }.items() if v}
            seen_urls.add(canon)
            out.append(
                ScrapeItem(
                    source=source,
                    url=abs_url,
                    title=f"{title} — {org_name}" if org_name and org_name.lower() not in title.lower() else title,
                    body=desc or f"{org_name} {loc} {sal} {price}".strip() or title,
                    author=org_name,
                    published_at=pub,
                    query_used=query_used,
                    metadata={"jsonld": jld_meta} if jld_meta else {},
                )
            )
            if len(out) >= max_items:
                return out
    return out


_LIST_RECORD_TITLE_KEYS = ("title", "name", "headline", "position", "jobTitle", "role", "label")
_LIST_RECORD_URL_KEYS = ("url", "applyUrl", "apply_url", "applicationLink", "link", "href", "canonicalUrl", "permalink")
_LIST_RECORD_DESC_KEYS = ("description", "summary", "excerpt", "snippet", "body", "abstract", "content")


def extract_items_from_json_payload(
    base_url: str,
    payload: object,
    *,
    source: str = "web",
    query_used: str = "",
    max_items: int = 40,
) -> list:
    """Walk arbitrary JSON responses or hydration blobs to extract structured list records generically."""
    from widescrape.models import ScrapeItem

    out: list[ScrapeItem] = []
    seen_urls: set[str] = set()
    visited_nodes = 0

    def _first_val(d: dict, keys: tuple[str, ...]) -> object:
        for k in keys:
            if d.get(k) not in (None, ""):
                return d[k]
        return None

    def _walk(obj: object, depth: int) -> None:
        nonlocal visited_nodes
        if visited_nodes > 2500 or len(out) >= max_items or depth > 8:
            return
        visited_nodes += 1
        if isinstance(obj, list):
            dict_rows = [r for r in obj[:80] if isinstance(r, dict)]
            if len(dict_rows) >= 2:
                for row in dict_rows:
                    if len(out) >= max_items:
                        return
                    title_raw = _first_val(row, _LIST_RECORD_TITLE_KEYS)
                    url_raw = _first_val(row, _LIST_RECORD_URL_KEYS)
                    if not isinstance(title_raw, str) or len(title_raw.strip()) < 4:
                        continue
                    title = " ".join(title_raw.split())[:180]
                    abs_url = ""
                    if isinstance(url_raw, str) and url_raw.strip():
                        cand = urljoin(base_url, url_raw.strip())
                        if cand.startswith(("http://", "https://")):
                            abs_url = cand
                    if not abs_url:
                        continue
                    desc_raw = _first_val(row, _LIST_RECORD_DESC_KEYS)
                    desc = _strip_markup(desc_raw)
                    org = row.get("company") or row.get("companyName") or row.get("organization") or row.get("brand") or row.get("publisher")
                    if isinstance(org, dict):
                        org = org.get("name") or org.get("title") or ""
                    org_str = " ".join(str(org or "").split())[:80]
                    pub = (
                        _norm_iso_date(row.get("publishedAt"))
                        or _norm_iso_date(row.get("published_at"))
                        or _norm_iso_date(row.get("createdAt"))
                        or _norm_iso_date(row.get("datePosted"))
                        or _norm_iso_date(row.get("date"))
                    )
                    loc = str(row.get("location") or row.get("locationName") or row.get("country") or "").strip()[:80]
                    sal = str(row.get("salary") or row.get("compensation") or row.get("price") or "").strip()[:60]
                    # Guard against navbar link arrays by requiring substance or domain signal.
                    has_substance = bool(desc and len(desc) >= 25) or bool(org_str or pub or sal or row.get("isRemote") or row.get("remote"))
                    if not has_substance:
                        continue
                    canon = canonicalize_url(abs_url)
                    if canon in seen_urls:
                        continue
                    seen_urls.add(canon)
                    jld = {k: v for k, v in {"company": org_str, "location": loc, "salary": sal}.items() if v}
                    if row.get("isRemote") or row.get("remote") is True:
                        jld["work_type"] = "remote"
                    out.append(
                        ScrapeItem(
                            source=source,
                            url=abs_url,
                            title=f"{title} — {org_str}" if org_str and org_str.lower() not in title.lower() else title,
                            body=desc or " | ".join(p for p in (org_str, loc, sal, title) if p),
                            author=org_str,
                            published_at=pub,
                            query_used=query_used,
                            metadata={"jsonld": jld} if jld else {},
                        )
                    )
            for elem in obj[:60]:
                if isinstance(elem, (dict, list)):
                    _walk(elem, depth + 1)
        elif isinstance(obj, dict):
            for v in list(obj.values())[:50]:
                if isinstance(v, (dict, list)):
                    _walk(v, depth + 1)

    _walk(payload, 0)
    return out


def extract_hydration_items_from_html(
    base_url: str,
    raw_html: str,
    *,
    source: str = "web",
    query_used: str = "",
    max_items: int = 40,
) -> list:
    """Extract list records embedded in `__NEXT_DATA__` or JSON `<script>` hydration blocks."""
    if not raw_html or "<script" not in raw_html.lower():
        return []
    collected: list = []
    seen: set[str] = set()
    for block in _HYDRATION_SCRIPT_RE.findall(raw_html[:600000]):
        txt = block.strip()
        if not txt or len(txt) < 30 or "ld+json" in txt[:40].lower():
            continue
        try:
            parsed = json.loads(txt)
        except ValueError:
            continue
        for it in extract_items_from_json_payload(
            base_url, parsed, source=source, query_used=query_used, max_items=max_items
        ):
            c = canonicalize_url(it.url)
            if c not in seen:
                seen.add(c)
                collected.append(it)
                if len(collected) >= max_items:
                    return collected
    return collected


def extract_feed_and_ats_hints(base_url: str, raw_html: str) -> list[str]:
    """Detect RSS/Atom/JSON feeds and public ATS API endpoints embedded on any webpage."""
    if not raw_html:
        return []
    hints: list[str] = []
    seen: set[str] = set()

    def _add(u: str) -> None:
        safe = validate_public_url(u)
        if safe and safe not in seen:
            seen.add(safe)
            hints.append(safe)

    for tag_match in re.finditer(r'(?is)<link\b[^>]+>', raw_html[:60000]):
        tag = tag_match.group(0)
        if not re.search(r'(?i)type=["\']application/(?:rss\+xml|atom\+xml|feed\+json)["\']', tag):
            continue
        href_m = re.search(r'(?i)href=["\']([^"\']+)["\']', tag)
        if href_m:
            _add(urljoin(base_url, href_m.group(1).strip()))

    sample = raw_html[:250000]
    for m in _ATS_HINT_PATTERNS[0].findall(sample):
        _add(f"https://boards-api.greenhouse.io/v1/boards/{m}/jobs?content=true")
    for m in _ATS_HINT_PATTERNS[1].findall(sample):
        _add(f"https://api.ashbyhq.com/posting-api/job-board/{m}")
    for m in _ATS_HINT_PATTERNS[2].findall(sample):
        _add(f"https://api.lever.co/v0/postings/{m}?mode=json")
    return hints[:8]


def extract_structured_items(
    base_url: str,
    raw_html: str,
    *,
    source: str = "web",
    query_used: str = "",
    max_items: int = 40,
) -> list:
    """Unified generic extractor: JSON-LD lists + Next/Nuxt hydration JSON from any unseen site."""
    items = extract_jsonld_items(
        base_url, raw_html, source=source, query_used=query_used, max_items=max_items
    )
    seen = {canonicalize_url(it.url) for it in items}
    if len(items) < max_items:
        for it in extract_hydration_items_from_html(
            base_url, raw_html, source=source, query_used=query_used, max_items=max_items - len(items)
        ):
            c = canonicalize_url(it.url)
            if c not in seen:
                seen.add(c)
                items.append(it)
                if len(items) >= max_items:
                    break
    return items


def extract_html_published_date(raw_html: str) -> str:
    """Extract YYYY-MM-DD publication date from HTML meta tags, <time>, or JSON-LD."""
    if not raw_html:
        return ""
    sample = raw_html[:60000]
    for pat in (
        r'(?i)<meta[^>]+(?:property|name)=["\'](?:article:published_time|pubdate|publishdate|date|dc\.date\.issued)["\'][^>]+content=["\'](\d{4}-\d{2}-\d{2})',
        r'(?i)<time[^>]+datetime=["\'](\d{4}-\d{2}-\d{2})',
        r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})',
    ):
        m = re.search(pat, sample)
        if m:
            return m.group(1)
    return extract_jsonld_metadata(raw_html).get("published_at", "")


def html_to_markdown(raw_html: str) -> tuple[str, str]:
    """Convert HTML into clean Markdown while stripping comments and hidden prompt-injection nodes."""
    soup = BeautifulSoup(raw_html, "html.parser")
    _strip_hidden_and_comments(soup)
    title = soup.title.get_text(" ", strip=True) if soup.title else ""

    main_root = soup.find("main")
    articles = soup.find_all("article")
    if main_root is not None:
        raw_md = "".join(_node_to_md(child, inside_content_root=True) for child in main_root.children)
    elif articles:
        raw_md = "\n\n".join(
            "".join(_node_to_md(child, inside_content_root=True) for child in art.children)
            for art in articles
        )
    else:
        body_root = soup.body or soup
        raw_md = "".join(_node_to_md(child, inside_content_root=False) for child in body_root.children)

    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw_md.splitlines()]
    compact = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return title, compact


def extract_pdf_text(raw_bytes: bytes, url: str = "") -> tuple[str, str] | None:
    """Extract clean text from a PDF payload using pypdf (first 15 pages, noting truncation)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        reader = PdfReader(io.BytesIO(raw_bytes))
        total_pages = len(reader.pages)
        pages_text: list[str] = []
        for page in reader.pages[:15]:
            txt = (page.extract_text() or "").strip()
            if txt:
                pages_text.append(txt)
        combined = "\n\n".join(pages_text).strip()
        if not combined:
            return None
        if total_pages > 15:
            combined += f"\n\n[Note: Extracted first 15 of {total_pages} PDF pages.]"
        meta_title = ""
        if reader.metadata and getattr(reader.metadata, "title", None):
            meta_title = str(reader.metadata.title).strip()
        fallback_name = url.rsplit("/", 1)[-1] or "PDF Document"
        return meta_title or fallback_name, combined
    except Exception:
        return None


def _is_supported_content_type(url: str, content_type: str) -> bool:
    ct = (content_type or "").lower()
    return (
        not ct
        or "application/pdf" in ct
        or url.lower().split("?")[0].endswith(".pdf")
        or any(t in ct for t in TEXT_CONTENT_TYPES)
    )


def _format_fetched_payload(url: str, content_type: str, body: str | bytes) -> tuple[str, str, str] | None:
    """Normalize HTML, Markdown, plain text, JSON, or PDF payloads; return None on challenge pages."""
    ct = content_type.lower()
    raw_bytes = body if isinstance(body, bytes) else body.encode("utf-8", errors="ignore")

    if "application/pdf" in ct or url.lower().split("?")[0].endswith(".pdf") or raw_bytes[:5] == b"%PDF-":
        pdf_res = extract_pdf_text(raw_bytes, url=url)
        if pdf_res is None:
            return None
        title, pdf_text = pdf_res
        return title, pdf_text, ""

    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    if is_challenge_page(text):
        return None

    if "text/html" in ct:
        title, md = html_to_markdown(text)
        return title, md, text

    if "application/json" in ct:
        try:
            pretty = json.dumps(json.loads(text), indent=2)
            return url.rsplit("/", 1)[-1] or url, pretty, text
        except ValueError:
            return url, text.strip(), text

    first_heading = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            first_heading = s.lstrip("#").strip()
            break
    return first_heading or (url.rsplit("/", 1)[-1] or url), text.strip(), text


_PRIMP_TLS = threading.local()


def _get_primp_client(primp_mod, timeout: int, proxy: str | None):
    """Reuse a thread-local primp.Client per (timeout, proxy) to pool TLS/HTTP2 connections safely."""
    clients = getattr(_PRIMP_TLS, "clients", None)
    if clients is None:
        clients = {}
        _PRIMP_TLS.clients = clients
    key = (timeout, proxy or "")
    client = clients.get(key)
    if client is None:
        kwargs: dict = {"impersonate": "random", "timeout": timeout, "follow_redirects": False}
        if proxy:
            kwargs["proxy"] = proxy
        client = primp_mod.Client(**kwargs)
        clients[key] = client
    return client


def _sync_primp_get(url: str, timeout: int = 12, proxy: str | None = None) -> _FetchOutcome:
    """Fetch with pooled primp following validated public redirects only."""
    try:
        import primp
    except ImportError:
        return _FetchOutcome(ok=False)

    key = (timeout, proxy or "")
    try:
        client = _get_primp_client(primp, timeout, proxy)
        current_url = url
        for _ in range(MAX_REDIRECT_HOPS + 1):
            if not validate_public_url(current_url):
                return _FetchOutcome(ok=False, terminal=True)
            resp = client.get(current_url)
            status = int(resp.status_code)
            headers = {str(k).lower(): str(v) for k, v in (resp.headers or {}).items()}
            if status in (301, 302, 303, 307, 308) and headers.get("location"):
                next_url = urljoin(current_url, headers["location"])
                if not validate_public_url(next_url):
                    return _FetchOutcome(ok=False, terminal=True)
                current_url = next_url
                continue

            ct = headers.get("content-type", "")
            cl_header = headers.get("content-length", "")
            if cl_header.isdigit() and int(cl_header) > MAX_FETCH_BYTES:
                return _FetchOutcome(ok=False, status=status, content_type=ct, terminal=True)
            if status in (404, 410):
                return _FetchOutcome(ok=False, status=status, content_type=ct, terminal=True)
            if status == 200 and not _is_supported_content_type(current_url, ct):
                return _FetchOutcome(ok=False, status=status, content_type=ct, terminal=True)

            content = (
                resp.content
                if hasattr(resp, "content") and resp.content is not None
                else (resp.text or "").encode("utf-8", errors="ignore")
            )
            if len(content) > MAX_FETCH_BYTES:
                return _FetchOutcome(ok=False, status=status, content_type=ct, terminal=True)
            return _FetchOutcome(ok=(status == 200), status=status, content_type=ct, payload=bytes(content))
    except Exception:
        clients = getattr(_PRIMP_TLS, "clients", None)
        if isinstance(clients, dict):
            clients.pop(key, None)
        return _FetchOutcome(ok=False)
    return _FetchOutcome(ok=False, terminal=True)


async def _httpx_stream_get(http_client: httpx.AsyncClient, start_url: str) -> tuple[str, str, bytes] | None:
    """Fetch via httpx streaming with per-hop SSRF validation and byte-limit guard."""
    current_url = start_url
    for _ in range(MAX_REDIRECT_HOPS + 1):
        if not validate_public_url(current_url):
            return None
        async with http_client.stream("GET", current_url, follow_redirects=False) as resp:
            status = resp.status_code
            location = resp.headers.get("location")
            if status in (301, 302, 303, 307, 308) and location:
                next_url = urljoin(current_url, location)
                if not validate_public_url(next_url):
                    return None
                current_url = next_url
                continue
            if status != 200:
                return None
            ct = resp.headers.get("content-type", "")
            if not _is_supported_content_type(current_url, ct):
                return None
            cl = resp.headers.get("content-length", "")
            if cl.isdigit() and int(cl) > MAX_FETCH_BYTES:
                return None

            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_FETCH_BYTES:
                    return None
            return current_url, ct, bytes(buf)
    return None


async def stealth_fetch_document(
    url: str,
    http_client: httpx.AsyncClient | None = None,
    timeout: int = 12,
    proxy: str | None = None,
) -> tuple[str, str, str] | None:
    """Fetch public URL using SSRF-guarded TLS stealth (`primp`) with streaming `httpx` fallback."""
    active_proxy = resolve_proxy(proxy)
    safe_url = validate_public_url(url)
    if not safe_url:
        return None

    outcome = await asyncio.to_thread(_sync_primp_get, safe_url, timeout, active_proxy)
    if outcome.ok:
        formatted = _format_fetched_payload(safe_url, outcome.content_type or "text/plain", outcome.payload)
        if formatted is not None:
            return formatted
        return None
    if outcome.terminal:
        return None

    if http_client is not None:
        try:
            streamed = await _httpx_stream_get(http_client, safe_url)
            if streamed is not None:
                final_url, ct, raw_bytes = streamed
                return _format_fetched_payload(final_url, ct or "text/plain", raw_bytes)
        except httpx.HTTPError:
            return None
    return None
