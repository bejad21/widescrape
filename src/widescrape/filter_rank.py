"""Noise/bot filtering, merge-deduplication, Unicode BM25 ranking, and query highlighting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import re

from rank_bm25 import BM25Okapi

from widescrape.expander import SearchPlan
from widescrape.fetcher import canonicalize_url, is_challenge_page
from widescrape.models import ScrapeItem

SPAM_PATTERNS = [
    r"buy followers",
    r"crypto airdrop",
    r"whatsapp me",
    r"click link in bio",
    r"make \$\d+ daily",
    r"dm for promo",
    r"something went wrong, but don.?t fret",
]
LOGIN_WALL_PATTERNS = [
    r"^sign in to view",
    r"^log in or sign up to view",
    r"join linkedin to see",
    r"^you must log in to continue",
]
STUB_BODIES = {"[removed]", "[deleted]", "none", "null", "n/a"}
AD_URL_MARKERS = (
    "bing.com/aclick",
    "ad.doubleclick.net",
    "googleadservices.com",
    "analytics.twitter.com/mob_idsync_click",
)
_API_SOURCES = {"github", "hackernews", "arxiv", "stackexchange", "reddit"}

_SPAM_RE = re.compile("|".join(SPAM_PATTERNS), re.IGNORECASE)
_LOGIN_RE = re.compile("|".join(LOGIN_WALL_PATTERNS), re.IGNORECASE)
_TOKEN_RE = re.compile(r"[^\W_]+(?:[+#.-][^\W_]+)*", re.UNICODE)
_JOB_SEEKER_RE = re.compile(
    r"\b(?:i\s+am|i['’]?m|im)\s+(?:currently\s+|actively\s+)?looking\s+for\s+(?:a\s+|my\s+next\s+|remote\s+)?(?:job|role|position|work|internship|opportunity|opportunities)\b"
    r"|\[\s*for\s+hire\s*\]"
    r"|\bfor\s+hire\b"
    r"|#?opentowork\b|\bopen\s+to\s+work\b"
    r"|\bhire\s+me\b"
    r"|\bactively\s+(?:seeking|looking\s+for)\s+(?:a\s+|new\s+|full[- ]time\s+)?(?:role|job|position|internship)\b",
    re.IGNORECASE,
)
_EMPLOYER_OVERRIDE_RE = re.compile(
    r"\b(?:we\s+are\s+hiring|we['’]?re\s+hiring|our\s+team\s+is\s+hiring|join\s+our\s+team|apply\s+now|open\s+roles?)\b",
    re.IGNORECASE,
)


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _normalize_url(url: str) -> str:
    return canonicalize_url(url)


def _is_low_quality(item: ScrapeItem, base_mode: str = "general") -> bool:
    """Detect ad redirects, removed/deleted stubs, spam, job-seeker noise, and short login walls."""
    url_lower = (item.url or "").lower()
    if any(ad in url_lower for ad in AD_URL_MARKERS):
        return True
    body_clean = " ".join((item.body or "").split()).strip()
    title_clean = " ".join((item.title or "").split()).strip()
    if body_clean.lower() in STUB_BODIES and len(title_clean) < 120:
        return True
    combined = f"{title_clean} {body_clean}".strip()
    min_len = 8 if item.source in _API_SOURCES else 20
    if len(combined) < min_len:
        return True
    if _SPAM_RE.search(combined) or is_challenge_page(combined):
        return True
    # Only flag login walls on short stub captures so long documentation pages mentioning login aren't dropped.
    if len(combined) < 400 and _LOGIN_RE.search(combined):
        return True
    if base_mode == "jobs" and _JOB_SEEKER_RE.search(combined) and not _EMPLOYER_OVERRIDE_RE.search(combined):
        return True
    return False


def _chunk_long_text(blk: str, chunk_cap: int) -> list[str]:
    """Split oversized unpunctuated text into word-boundary chunks <= chunk_cap."""
    chunks: list[str] = []
    start = 0
    n = len(blk)
    while start < n:
        end = min(start + chunk_cap, n)
        if end < n:
            space = blk.rfind(" ", start, end)
            if space > start + (chunk_cap // 3):
                end = space
        piece = blk[start:end].strip()
        if piece:
            chunks.append(piece)
        start = end
    return chunks


def prune_text_bm25(text: str, focus_query: str, max_chars: int = 2500) -> str:
    """Intra-page BM25 pruning: keep only paragraphs/lines most relevant to `focus_query`."""
    if not focus_query.strip() or len(text) <= max_chars:
        return text[:max_chars] if len(text) > max_chars else text

    raw_blocks = [b.strip() for b in re.split(r"\n{2,}", text) if len(b.strip()) >= 15]
    blocks: list[str] = []
    chunk_cap = max(120, min(600, max_chars // 2))
    for blk in raw_blocks:
        if len(blk) <= chunk_cap:
            blocks.append(blk)
        elif "\n" in blk:
            lines = [ln.strip() for ln in blk.splitlines() if ln.strip()]
            buf = ""
            for ln in lines:
                if len(ln) > chunk_cap:
                    if buf:
                        blocks.append(buf)
                        buf = ""
                    blocks.extend(_chunk_long_text(ln, chunk_cap))
                elif buf and len(buf) + len(ln) + 1 > chunk_cap:
                    blocks.append(buf)
                    buf = ln
                else:
                    buf = f"{buf}\n{ln}" if buf else ln
            if buf:
                blocks.append(buf)
        else:
            sentences = re.split(r"(?<=[.!?])\s+", blk)
            if len(sentences) <= 1:
                blocks.extend(_chunk_long_text(blk, chunk_cap))
            else:
                for i in range(0, len(sentences), 4):
                    seg = " ".join(sentences[i : i + 4]).strip()
                    if len(seg) > chunk_cap:
                        blocks.extend(_chunk_long_text(seg, chunk_cap))
                    elif seg:
                        blocks.append(seg)

    if not blocks:
        return text[:max_chars]

    q_tokens = _tokenize(focus_query)
    if not q_tokens:
        return text[:max_chars]

    if len(blocks) <= 2:
        q_set = set(q_tokens)
        scores = [sum(1.0 for t in _tokenize(b) if t in q_set) for b in blocks]
    else:
        bm25 = BM25Okapi([_tokenize(b) for b in blocks])
        scores = bm25.get_scores(q_tokens)
    ranked_indices = sorted(range(len(blocks)), key=lambda i: scores[i], reverse=True)

    chosen: list[int] = []
    total = 0
    for idx in ranked_indices:
        b_len = len(blocks[idx]) + (7 if chosen else 0)
        if chosen and total + b_len > max_chars:
            continue
        if not chosen and len(blocks[idx]) > max_chars:
            return blocks[idx][:max_chars]
        chosen.append(idx)
        total += b_len
        if total >= max_chars:
            break

    chosen.sort()
    joined = "\n\n...\n\n".join(blocks[i] for i in chosen)
    return joined[:max_chars]


def select_best_highlight(text: str, query: str, max_chars: int = 160) -> str:
    """Pick the sentence in `text` most relevant to `query` (Exa-style smart highlights)."""
    flat = " ".join((text or "").split())
    if not flat:
        return ""
    if len(flat) <= max_chars:
        return flat

    q_tokens = {t for t in _tokenize(query) if len(t) > 1}
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]
    sentences = [" ".join(s.split()) for s in sentences if len(" ".join(s.split())) >= 15]

    def _safe_clip(txt: str, cap: int) -> str:
        if len(txt) <= cap:
            return txt
        if cap <= 4:
            return txt[:cap]
        raw_cut = txt[: cap - 3]
        space = raw_cut.rfind(" ")
        if space >= max(4, int(len(raw_cut) * 0.4)):
            return raw_cut[:space].rstrip() + "..."
        return raw_cut.rstrip() + "..."

    if not sentences:
        return _safe_clip(flat, max_chars)

    best_idx = 0
    best_score = -1.0
    for idx, sent in enumerate(sentences):
        s_tokens = set(_tokenize(sent))
        overlap = sum(1 for qt in q_tokens if qt in s_tokens)
        has_concrete = bool(re.search(r"(?i)(\d+%|aed|usd|eur|gbp|sar|\$|€)|\b[A-Z0-9]{5,}\b", sent))
        score = (overlap * 2.0) + (0.6 if has_concrete else 0.0) + (0.15 if 35 <= len(sent) <= 220 else 0.0)
        if score > best_score:
            best_score = score
            best_idx = idx

    chosen = sentences[best_idx]
    prefix = "... " if best_idx > 0 and max_chars > 12 else ""
    budget = max(4, max_chars - len(prefix))
    return f"{prefix}{_safe_clip(chosen, budget)}"


def _matches_exclude_term(text_lower: str, term: str) -> bool:
    clean = " ".join(term.strip().lower().split())
    if not clean:
        return False
    if len(clean) <= 4 and " " not in clean:
        return bool(re.search(rf"\b{re.escape(clean)}\b", text_lower))
    return clean in text_lower


def filter_and_rank(
    items: list[ScrapeItem],
    plan: SearchPlan,
    apply_ranking: bool = True,
    enforce_strict_days: bool = True,
) -> tuple[list[ScrapeItem], dict[str, int]]:
    """Merge-deduplicate, enforce recency & exclude cutoffs, strip noise, and rank best-first."""
    raw_chars = sum(len(it.title) + len(it.body) for it in items)
    stats = {
        "raw": len(items),
        "deduped": 0,
        "dropped_noise": 0,
        "dropped_excluded": 0,
        "dropped_old": 0,
        "skipped_seen": 0,
        "skipped_unchanged": 0,
        "kept": 0,
        "raw_chars": raw_chars,
        "summary_chars": 0,
        "compression_pct": 0,
    }

    now_utc = datetime.now(timezone.utc)
    cutoff_date = (
        (now_utc - timedelta(days=plan.days)).strftime("%Y-%m-%d")
        if plan.days and plan.days > 0
        else None
    )
    exclude_terms = tuple(getattr(plan, "exclude_terms", ()) or ())

    by_url: dict[str, ScrapeItem] = {}
    seen_titles: dict[str, str] = {}
    cleaned: list[ScrapeItem] = []

    for item in items:
        if cutoff_date:
            item_date = (item.published_at or "")[:10]
            has_valid_date = bool(re.match(r"^\d{4}-\d{2}-\d{2}$", item_date))
            if has_valid_date and item_date < cutoff_date:
                stats["dropped_old"] += 1
                continue
            if plan.strict_days and enforce_strict_days and not has_valid_date:
                stats["dropped_old"] += 1
                continue

        if _is_low_quality(item, base_mode=plan.base_mode):
            stats["dropped_noise"] += 1
            continue

        if exclude_terms:
            item_text_low = f"{item.title} {item.body}".lower()
            if any(_matches_exclude_term(item_text_low, ex) for ex in exclude_terms):
                stats["dropped_excluded"] += 1
                continue

        norm_url = _normalize_url(item.url)
        norm_title = " ".join(item.title.lower().split())[:80]

        existing = by_url.get(norm_url)
        if existing is None and norm_title and norm_title in seen_titles and len(item.body) < 80:
            stats["deduped"] += 1
            continue

        if existing is not None:
            stats["deduped"] += 1
            # Merge-dedup: keep richer body/metadata and higher engagement (so snippets never shadow rich API hits).
            existing_sources = set(existing.metadata.get("all_sources") or [existing.source])
            existing_sources.add(item.source)
            item_richer = (len(item.body) > len(existing.body) + 40) or (
                item.engagement > existing.engagement and len(item.body) >= len(existing.body) - 40
            )
            if item_richer:
                winner, loser = item, existing
                idx_pos = cleaned.index(existing)
                cleaned[idx_pos] = winner
                by_url[norm_url] = winner
            else:
                winner, loser = existing, item
            winner.engagement = max(winner.engagement, loser.engagement)
            if not winner.published_at and loser.published_at:
                winner.published_at = loser.published_at
            if not winner.author and loser.author:
                winner.author = loser.author
            winner.metadata["all_sources"] = sorted(existing_sources)
            continue

        by_url[norm_url] = item
        if norm_title:
            seen_titles[norm_title] = norm_url
        cleaned.append(item)

    if not cleaned:
        return [], stats

    if not apply_ranking:
        for idx, item in enumerate(cleaned):
            item.score = round(max(0.1, 1.0 - (idx * 0.01)), 3)
    else:
        query_tokens = _tokenize(plan.raw_query) or list(plan.core_terms)
        core_terms = plan.core_terms or tuple(_tokenize(plan.raw_query))
        if len(cleaned) <= 2:
            q_set = set(query_tokens)
            raw_bm25 = [
                float(sum(1 for tok in _tokenize(f"{it.title} {it.body}") if tok in q_set))
                for it in cleaned
            ]
        else:
            corpus = [_tokenize(f"{it.title} {it.body}") for it in cleaned]
            bm25 = BM25Okapi(corpus)
            raw_bm25 = list(bm25.get_scores(query_tokens)) if query_tokens else [0.0] * len(cleaned)
            # If BM25 IDF turned <= 0 because terms appear across all docs, fall back to TF overlap.
            if max(raw_bm25, default=0.0) <= 0.0 and query_tokens:
                q_set = set(query_tokens)
                raw_bm25 = [
                    float(sum(1 for tok in _tokenize(f"{it.title} {it.body}") if tok in q_set))
                    for it in cleaned
                ]

        max_bm25 = max(raw_bm25) if max(raw_bm25, default=0.0) > 0 else 1.0
        fresh_7d = (now_utc - timedelta(days=7)).strftime("%Y-%m-%d")
        fresh_30d = (now_utc - timedelta(days=30)).strftime("%Y-%m-%d")

        plan_synonyms = tuple(str(s).lower() for s in (getattr(plan, "synonyms", ()) or ()) if s)
        for idx, item in enumerate(cleaned):
            text_lower = f"{item.title} {item.body}".lower()
            bm25_norm = max(0.0, raw_bm25[idx]) / max_bm25
            term_hits = sum(1 for t in core_terms if re.search(rf"\b{re.escape(t)}\b", text_lower) or (len(t) > 3 and t in text_lower))
            term_coverage = (term_hits / len(core_terms)) if core_terms else 0.0
            syn_hits = sum(
                1
                for s in plan_synonyms
                if (re.search(rf"\b{re.escape(s)}\b", text_lower) if len(s) <= 3 else s in text_lower)
            )
            synonym_bonus = min(syn_hits * 0.14, 0.42)

            signal_hits = sum(1 for kw in plan.signal_keywords if kw in text_lower)
            signal_bonus = min(signal_hits * 0.12, 0.6)
            boost_hits = sum(1 for kw in plan.boost_keywords if kw in text_lower)
            intent_bonus = min(boost_hits * 0.25, 0.75)
            penalty_hits = sum(1 for kw in plan.penalty_keywords if kw in text_lower)
            intent_penalty = min(penalty_hits * 0.4, 0.8)
            eng_bonus = min(math.log1p(max(item.engagement, 0)) * 0.08, 0.5)
            substance_bonus = 0.15 if len(item.body) > 200 else 0.0

            recency_bonus = 0.0
            if item.published_at and len(item.published_at) >= 10:
                if item.published_at[:10] >= fresh_7d:
                    recency_bonus = 0.2
                elif item.published_at[:10] >= fresh_30d:
                    recency_bonus = 0.1

            item.score = round(
                max(
                    0.0,
                    (bm25_norm * 1.0)
                    + (term_coverage * 0.8)
                    + synonym_bonus
                    + signal_bonus
                    + intent_bonus
                    + eng_bonus
                    + substance_bonus
                    + recency_bonus
                    - intent_penalty,
                ),
                3,
            )

        cleaned.sort(key=lambda x: x.score, reverse=True)

    for it in cleaned:
        hl = select_best_highlight(it.body or it.title, plan.raw_query, max_chars=165)
        if hl:
            it.metadata["highlight"] = hl

    recompute_stats(cleaned, stats)
    return cleaned, stats


def _item_has_field_value(it: ScrapeItem, field_name: str) -> bool:
    """Check whether `field_name` has a populated value in extracted fields, JSON-LD, or item metadata."""
    meta = it.metadata if isinstance(it.metadata, dict) else {}
    ext_fields = meta.get("extracted_fields") if isinstance(meta.get("extracted_fields"), dict) else {}
    if ext_fields.get(field_name):
        return True
    ext = meta.get("extracted") if isinstance(meta.get("extracted"), dict) else {}
    if ext.get(field_name):
        return True
    jld = meta.get("jsonld") if isinstance(meta.get("jsonld"), dict) else {}
    if jld.get(field_name):
        return True
    # Common field-alias checks across built-in extractors (`remote`/`location` -> `work_type`).
    if field_name in ("location", "remote", "work", "work_type") and (ext.get("work_type") or jld.get("location")):
        return True
    if field_name in ("exp", "years", "seniority", "level") and (ext.get("experience") or ext.get("seniority")):
        return True
    if field_name in ("comp", "pay", "compensation") and ext.get("salary"):
        return True
    if field_name in ("date", "published_at") and it.published_at:
        return True
    if field_name == "author" and it.author:
        return True
    return False


def apply_field_ranking_boost(items: list[ScrapeItem], plan: SearchPlan) -> None:
    """Boost items with populated `rank_fields`, `--fields`, or preset signals while preserving relevance."""
    custom_rank = list(getattr(plan, "rank_fields", ()) or ())
    if not items or (not custom_rank and not plan.extract_fields and not plan.extract_mode):
        return
    for it in items:
        if it.score < 0.2 and (plan.core_terms or plan.raw_query):
            continue
        meta = it.metadata if isinstance(it.metadata, dict) else {}
        ext = meta.get("extracted") if isinstance(meta.get("extracted"), dict) else {}

        bonus = 0.0
        if custom_rank:
            # Weight earlier listed `rank_fields` slightly higher so agent priority order matters.
            for idx, fname in enumerate(custom_rank):
                if _item_has_field_value(it, fname):
                    weight = max(0.14, 0.30 - (idx * 0.05))
                    bonus += weight
            bonus = min(bonus, 0.90)
        else:
            hits = 0
            if plan.extract_fields:
                hits += sum(1 for f in plan.extract_fields if _item_has_field_value(it, f))
            if plan.extract_mode == "jobs":
                hits += sum(1 for k in ("salary", "experience", "work_type") if ext.get(k))
            elif plan.extract_mode == "codes":
                hits += sum(1 for k in ("codes", "discounts", "bank_card") if ext.get(k))
            bonus = min(hits * 0.18, 0.65)

        if bonus > 0:
            it.score = round(it.score + bonus, 3)
    items.sort(key=lambda x: x.score, reverse=True)


def recompute_stats(items: list[ScrapeItem], stats: dict[str, int]) -> None:
    """Recompute final kept count, summary size, and compression % after all post-filters."""
    summary_chars = sum(len(it.compact_summary()) for it in items)
    stats["kept"] = len(items)
    stats["summary_chars"] = summary_chars
    raw_chars = stats.get("raw_chars", 0)
    if raw_chars > 0:
        stats["compression_pct"] = max(0, round((1 - (summary_chars / raw_chars)) * 100))
    else:
        stats["compression_pct"] = 0
