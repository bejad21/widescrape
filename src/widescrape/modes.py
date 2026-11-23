"""Custom modes and per-mode website presets for WideScrape."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable
from urllib.parse import urlparse

DEFAULT_MODES_PATH = Path.home() / ".widescrape" / "modes.json"
BUILTIN_MODES = ("jobs", "research", "general")
_NON_LEARNABLE_HOSTS = frozenset({
    "google.com", "bing.com", "yahoo.com", "yandex.com", "duckduckgo.com",
    "reddit.com", "old.reddit.com", "x.com", "twitter.com", "t.co",
    "linkedin.com", "lnkd.in", "github.com", "news.ycombinator.com",
    "arxiv.org", "stackoverflow.com", "stackexchange.com", "news.google.com",
    "youtube.com", "youtu.be", "facebook.com", "instagram.com", "tiktok.com",
    "pinterest.com", "wikipedia.org", "en.wikipedia.org",
})


def resolve_learned_path(
    modes_path: Path | str | None = None, learned_path: Path | str | None = None
) -> Path:
    """Resolve learned sites JSON path (isolated automatically alongside custom WIDESCRAPE_MODES_PATH)."""
    if learned_path is not None:
        return Path(learned_path)
    env_learned = os.environ.get("WIDESCRAPE_LEARNED_PATH", "").strip()
    if env_learned:
        return Path(env_learned)
    return resolve_modes_path(modes_path).with_name("learned_sites.json")


def resolve_modes_path(path: Path | str | None = None) -> Path:
    """Resolve custom modes JSON path cleanly (allowing WIDESCRAPE_MODES_PATH override)."""
    if path is not None:
        return Path(path)
    env_path = os.environ.get("WIDESCRAPE_MODES_PATH", "").strip()
    if env_path:
        return Path(env_path)
    return DEFAULT_MODES_PATH


def normalize_mode_name(name: str) -> str:
    """Normalize a mode name into a lowercase URL/CLI-safe slug."""
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", (name or "").strip().lower()).strip("-_")
    if not cleaned or cleaned == "auto":
        raise ValueError(f"Invalid mode name '{name}'. Use letters/numbers/hyphens (cannot be 'auto').")
    return cleaned


def clean_site_domain(site: str) -> str:
    """Normalize and validate website domain/subpath for `site:` dorks."""
    raw = (site or "").strip().lower()
    if not raw or any(ch.isspace() for ch in raw):
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    from urllib.parse import urlparse

    parsed = urlparse(raw)
    host = (parsed.netloc or "").removeprefix("www.").strip("/")
    if not host or "." not in host:
        return ""
    path = parsed.path.strip("/")
    return f"{host}/{path}" if path else host


def _dedupe_strs(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        clean = v.strip()
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


@dataclass
class ModeConfig:
    """User-defined mode or customization layered on a built-in base mode."""

    name: str
    base_mode: str = "general"  # "jobs", "research", or "general"
    description: str = ""
    sources: list[str] = field(default_factory=list)
    sites: list[str] = field(default_factory=list)
    synonyms: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    signal_keywords: list[str] = field(default_factory=list)
    boost_keywords: list[str] = field(default_factory=list)
    penalty_keywords: list[str] = field(default_factory=list)
    default_days: int | None = None
    default_extract: str = ""
    default_fields: list[str] = field(default_factory=list)
    default_rank_fields: list[str] = field(default_factory=list)


def load_custom_modes(path: Path | str | None = None, strict: bool = False) -> dict[str, ModeConfig]:
    """Load saved custom modes from disk."""
    cfg_path = resolve_modes_path(path)
    if not cfg_path.is_file():
        return {}
    try:
        raw_txt = cfg_path.read_text(encoding="utf-8")
        raw = json.loads(raw_txt) if raw_txt.strip() else {}
    except (OSError, ValueError) as exc:
        if strict:
            raise ValueError(f"Corrupt modes config at {cfg_path}: {exc}. Fix or back it up before writing.") from exc
        return {}
    if not isinstance(raw, dict):
        if strict:
            raise ValueError(f"Corrupt modes config at {cfg_path}: root must be a JSON object.")
        return {}
    entries = raw.get("modes", {})
    if not isinstance(entries, dict):
        return {}
    modes: dict[str, ModeConfig] = {}
    for key, val in entries.items():
        if not isinstance(val, dict):
            continue
        base = str(val.get("base_mode") or (key if key in BUILTIN_MODES else "general")).lower()
        if base not in BUILTIN_MODES:
            base = "general"
        days_val = val.get("default_days")
        valid_days = isinstance(days_val, int) and not isinstance(days_val, bool) and days_val > 0
        modes[key] = ModeConfig(
            name=key,
            base_mode=base,
            description=str(val.get("description") or ""),
            sources=[str(s).strip().lower() for s in (val.get("sources") or []) if str(s).strip()],
            sites=_dedupe_strs([clean_site_domain(str(s)) for s in (val.get("sites") or []) if str(s).strip()]),
            synonyms=_dedupe_strs([str(k).strip().lower() for k in (val.get("synonyms") or []) if str(k).strip()]),
            exclude_keywords=_dedupe_strs([str(k).strip().lower() for k in (val.get("exclude_keywords") or []) if str(k).strip()]),
            signal_keywords=_dedupe_strs([str(k).lower() for k in (val.get("signal_keywords") or [])]),
            boost_keywords=_dedupe_strs([str(k).lower() for k in (val.get("boost_keywords") or [])]),
            penalty_keywords=_dedupe_strs([str(k).lower() for k in (val.get("penalty_keywords") or [])]),
            default_days=int(days_val) if valid_days else None,
            default_extract=str(val.get("default_extract") or "").strip().lower(),
            default_fields=_dedupe_strs([str(f).strip().lower() for f in (val.get("default_fields") or []) if str(f).strip()]),
            default_rank_fields=_dedupe_strs([str(f).strip().lower() for f in (val.get("default_rank_fields") or []) if str(f).strip()]),
        )
    return modes


def save_custom_modes(modes: dict[str, ModeConfig], path: Path | str | None = None) -> Path:
    """Persist custom modes atomically (backing up any existing valid file first)."""
    cfg_path = resolve_modes_path(path)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if cfg_path.is_file():
        cfg_path.with_name(cfg_path.name + ".bak").write_bytes(cfg_path.read_bytes())
    tmp_path = cfg_path.with_name(f".{cfg_path.name}.{os.getpid()}.tmp")
    payload = {"modes": {name: asdict(cfg) for name, cfg in sorted(modes.items())}}
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp_path, cfg_path)
    return cfg_path


def get_mode(name: str, path: Path | str | None = None) -> ModeConfig | None:
    """Return a custom mode config by normalized name, if defined."""
    slug = normalize_mode_name(name)
    return load_custom_modes(path).get(slug)


def upsert_mode(
    name: str,
    base_mode: str | None = None,
    description: str = "",
    sources: list[str] | None = None,
    sites: list[str] | None = None,
    synonyms: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
    signal_keywords: list[str] | None = None,
    boost_keywords: list[str] | None = None,
    penalty_keywords: list[str] | None = None,
    default_days: int | None = None,
    default_extract: str = "",
    default_fields: list[str] | None = None,
    default_rank_fields: list[str] | None = None,
    path: Path | str | None = None,
) -> ModeConfig:
    """Create or update a custom mode configuration while preserving omitted fields."""
    slug = normalize_mode_name(name)
    modes = load_custom_modes(path, strict=True)
    existing = modes.get(slug)

    if base_mode is not None:
        base = base_mode.strip().lower()
    elif existing is not None:
        base = existing.base_mode
    elif slug in BUILTIN_MODES:
        base = slug
    else:
        base = "general"

    if base not in BUILTIN_MODES:
        raise ValueError(f"base_mode must be one of {', '.join(BUILTIN_MODES)}")

    cfg = ModeConfig(
        name=slug,
        base_mode=base,
        description=description if description else (existing.description if existing else ""),
        sources=(
            _dedupe_strs([s.strip().lower() for s in sources if s.strip()])
            if sources is not None
            else (existing.sources if existing else [])
        ),
        sites=(
            _dedupe_strs([clean_site_domain(s) for s in sites if clean_site_domain(s)])
            if sites is not None
            else (existing.sites if existing else [])
        ),
        synonyms=(
            _dedupe_strs([k.strip().lower() for k in synonyms if k.strip()])
            if synonyms is not None
            else (existing.synonyms if existing else [])
        ),
        exclude_keywords=(
            _dedupe_strs([k.strip().lower() for k in exclude_keywords if k.strip()])
            if exclude_keywords is not None
            else (existing.exclude_keywords if existing else [])
        ),
        signal_keywords=(
            _dedupe_strs([k.strip().lower() for k in signal_keywords if k.strip()])
            if signal_keywords is not None
            else (existing.signal_keywords if existing else [])
        ),
        boost_keywords=(
            _dedupe_strs([k.strip().lower() for k in boost_keywords if k.strip()])
            if boost_keywords is not None
            else (existing.boost_keywords if existing else [])
        ),
        penalty_keywords=(
            _dedupe_strs([k.strip().lower() for k in penalty_keywords if k.strip()])
            if penalty_keywords is not None
            else (existing.penalty_keywords if existing else [])
        ),
        default_days=(
            (default_days if default_days > 0 else None)
            if default_days is not None
            else (existing.default_days if existing else None)
        ),
        default_extract=(
            default_extract.strip().lower()
            if default_extract
            else (existing.default_extract if existing else "")
        ),
        default_fields=(
            _dedupe_strs([f.strip().lower() for f in default_fields if f.strip()])
            if default_fields is not None
            else (existing.default_fields if existing else [])
        ),
        default_rank_fields=(
            _dedupe_strs([f.strip().lower() for f in default_rank_fields if f.strip()])
            if default_rank_fields is not None
            else (existing.default_rank_fields if existing else [])
        ),
    )
    modes[slug] = cfg
    save_custom_modes(modes, path)
    return cfg


def add_sites_to_mode(mode_name: str, sites: list[str], path: Path | str | None = None) -> ModeConfig:
    """Append one or more custom websites to an existing or new mode."""
    slug = normalize_mode_name(mode_name)
    modes = load_custom_modes(path, strict=True)
    cleaned = [clean_site_domain(s) for s in sites if clean_site_domain(s)]
    if not cleaned:
        raise ValueError("Provide at least one valid domain (e.g. amazon.ae).")

    existing = modes.get(slug)
    if existing is None:
        base = slug if slug in BUILTIN_MODES else "general"
        existing = ModeConfig(name=slug, base_mode=base)

    existing.sites = _dedupe_strs(existing.sites + cleaned)
    modes[slug] = existing
    save_custom_modes(modes, path)
    return existing


def remove_sites_from_mode(mode_name: str, sites: list[str], path: Path | str | None = None) -> ModeConfig:
    """Remove websites from a custom mode (or mode override)."""
    slug = normalize_mode_name(mode_name)
    modes = load_custom_modes(path, strict=True)
    existing = modes.get(slug)
    if existing is None:
        raise KeyError(f"Mode '{slug}' has no custom websites saved.")

    to_remove = {clean_site_domain(s) for s in sites if clean_site_domain(s)}
    existing.sites = [s for s in existing.sites if s not in to_remove]
    modes[slug] = existing
    save_custom_modes(modes, path)
    return existing


def delete_mode(mode_name: str, path: Path | str | None = None) -> bool:
    """Delete a custom mode (or custom override of a built-in mode)."""
    slug = normalize_mode_name(mode_name)
    modes = load_custom_modes(path, strict=True)
    if slug not in modes:
        return False
    del modes[slug]
    save_custom_modes(modes, path)
    return True


def list_all_modes(path: Path | str | None = None) -> list[dict]:
    """Return combined overview of built-in modes and custom modes/overrides."""
    custom = load_custom_modes(path)
    rows: list[dict] = []
    builtin_descs = {
        "jobs": "Hidden hiring on LinkedIn, X, Reddit & startup ATS boards",
        "research": "Technical workflows, GitHub repos, HN, arXiv, StackExchange & docs",
        "general": "Balanced multi-source search across web, news, Reddit, X, GitHub & HN",
    }
    for name in BUILTIN_MODES:
        ovr = custom.get(name)
        has_ovr = bool(
            ovr
            and (
                ovr.sites
                or ovr.sources
                or ovr.default_days
                or ovr.default_extract
                or ovr.default_fields
                or ovr.default_rank_fields
                or ovr.signal_keywords
                or ovr.boost_keywords
                or ovr.penalty_keywords
            )
        )
        rows.append(
            {
                "name": name,
                "kind": "built-in (+override)" if has_ovr else "built-in",
                "base_mode": name,
                "description": (ovr.description if ovr and ovr.description else builtin_descs[name]),
                "sites": ovr.sites if ovr else [],
                "sources": ovr.sources if ovr else [],
                "default_days": ovr.default_days if ovr else None,
                "default_extract": ovr.default_extract if ovr else "",
                "default_fields": ovr.default_fields if ovr else [],
                "default_rank_fields": ovr.default_rank_fields if ovr else [],
            }
        )
    for name, cfg in sorted(custom.items()):
        if name in BUILTIN_MODES:
            continue
        rows.append(
            {
                "name": name,
                "kind": "custom",
                "base_mode": cfg.base_mode,
                "description": cfg.description or f"Custom mode (base: {cfg.base_mode})",
                "sites": cfg.sites,
                "sources": cfg.sources,
                "synonyms": cfg.synonyms,
                "exclude_keywords": cfg.exclude_keywords,
                "default_days": cfg.default_days,
                "default_extract": cfg.default_extract,
                "default_fields": cfg.default_fields,
                "default_rank_fields": cfg.default_rank_fields,
            }
        )
    return rows


def _extract_host_domain(url: str) -> str:
    """Return canonical public host for `url`, or empty string."""
    raw = (url or "").strip()
    if not raw.startswith(("http://", "https://")):
        return ""
    host = (urlparse(raw).netloc or "").lower().removeprefix("www.").strip("/")
    return host if "." in host else ""


def _is_learnable_domain(host: str) -> bool:
    clean = host.lower().removeprefix("www.")
    if not clean or "." not in clean:
        return False
    return clean not in _NON_LEARNABLE_HOSTS and not any(clean.endswith(f".{b}") for b in _NON_LEARNABLE_HOSTS)


def _topic_bucket_key(tokens: list[str] | tuple[str, ...], base_mode: str) -> str:
    clean = sorted({re.sub(r"[^a-z0-9_-]+", "", t.lower()) for t in tokens if len(t.strip()) >= 2})[:4]
    if not clean:
        clean = ["general"]
    return f"{base_mode}:{'-'.join(clean)}"


def load_learned_topics(
    modes_path: Path | str | None = None, learned_path: Path | str | None = None
) -> dict[str, dict[str, Any]]:
    """Load persisted self-learned topic->site knowledge."""
    lpath = resolve_learned_path(modes_path, learned_path)
    if not lpath.is_file():
        return {}
    try:
        raw = json.loads(lpath.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    topics = raw.get("topics") if isinstance(raw, dict) else None
    return dict(topics) if isinstance(topics, dict) else {}


def save_learned_topics(
    topics: dict[str, dict[str, Any]],
    modes_path: Path | str | None = None,
    learned_path: Path | str | None = None,
) -> Path:
    """Atomically persist learned topic->site knowledge."""
    lpath = resolve_learned_path(modes_path, learned_path)
    lpath.parent.mkdir(parents=True, exist_ok=True)
    tmp = lpath.with_name(f".{lpath.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"topics": topics}, indent=2), encoding="utf-8")
    os.replace(tmp, lpath)
    return lpath


def recall_learned_context(
    tokens: list[str] | tuple[str, ...],
    base_mode: str = "general",
    *,
    modes_path: Path | str | None = None,
    learned_path: Path | str | None = None,
    max_sites: int = 8,
    max_synonyms: int = 12,
) -> tuple[list[str], list[str]]:
    """Recall previously high-yield sites and validated synonyms matching `tokens`."""
    clean_tokens = {t.strip().lower() for t in tokens if len(t.strip()) >= 2}
    if not clean_tokens:
        return [], []
    topics = load_learned_topics(modes_path, learned_path)
    if not topics:
        return [], []

    min_overlap = 1 if len(clean_tokens) == 1 else 2
    site_scores: dict[str, float] = {}
    learned_syns: list[str] = []

    for bucket in topics.values():
        if not isinstance(bucket, dict):
            continue
        b_mode = str(bucket.get("base_mode") or "general").lower()
        if b_mode != base_mode and not (base_mode == "general" or b_mode == "general"):
            continue
        b_tokens = {str(t).strip().lower() for t in (bucket.get("tokens") or []) if str(t).strip()}
        b_syns = [str(s).strip().lower() for s in (bucket.get("synonyms") or []) if str(s).strip()]
        syn_tokens = {w for s in b_syns for w in s.split() if len(w) >= 2}
        overlap = len(clean_tokens & (b_tokens | syn_tokens))
        if overlap < min_overlap and not (len(clean_tokens) >= 2 and overlap >= 1 and overlap / max(len(b_tokens), 1) >= 0.5):
            continue
        mode_mult = 1.25 if b_mode == base_mode else 0.85
        for dom, info in (bucket.get("sites") or {}).items():
            if not isinstance(info, dict):
                continue
            kept = int(info.get("kept") or 0)
            avg_score = float(info.get("avg_score") or 0.5)
            weight = overlap * mode_mult * max(1, kept) * (avg_score + 0.5)
            site_scores[dom] = max(site_scores.get(dom, 0.0), weight)
        for s in b_syns:
            if s not in learned_syns:
                learned_syns.append(s)

    ranked_sites = [dom for dom, _ in sorted(site_scores.items(), key=lambda kv: kv[1], reverse=True)]
    return ranked_sites[:max_sites], learned_syns[:max_synonyms]


def record_learned_run(
    *,
    tokens: list[str] | tuple[str, ...],
    base_mode: str,
    mode_name: str,
    kept_items: Iterable[Any],
    discovered_sites: Iterable[str] = (),
    used_synonyms: Iterable[str] = (),
    learn_mode: str = "",
    modes_path: Path | str | None = None,
    learned_path: Path | str | None = None,
) -> list[str]:
    """Persist high-yield discovered domains & synonyms into learned_sites.json (and target custom mode)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    topics = load_learned_topics(modes_path, learned_path)
    key = _topic_bucket_key(tokens, base_mode)
    existing = topics.get(key) or {
        "base_mode": base_mode,
        "tokens": sorted({t.strip().lower() for t in tokens if len(t.strip()) >= 2})[:6],
        "sites": {},
        "synonyms": [],
        "runs": 0,
        "updated_at": today,
    }
    sites_map: dict[str, dict[str, Any]] = dict(existing.get("sites") or {})
    discovered_set = {clean_site_domain(s) for s in discovered_sites if clean_site_domain(s)}

    domain_counts: dict[str, list[float]] = {}
    for it in kept_items:
        url = getattr(it, "url", "") if not isinstance(it, dict) else str(it.get("url") or "")
        score = float(getattr(it, "score", 1.0) if not isinstance(it, dict) else (it.get("score") or 1.0))
        host = _extract_host_domain(url)
        if not _is_learnable_domain(host):
            continue
        domain_counts.setdefault(host, []).append(score)

    newly_learned: list[str] = []
    for dom, scores in domain_counts.items():
        if len(scores) < 1:
            continue
        prev = dict(sites_map.get(dom) or {})
        prev_kept = int(prev.get("kept") or 0)
        new_kept = prev_kept + len(scores)
        avg = round((float(prev.get("avg_score") or 0.0) * prev_kept + sum(scores)) / max(new_kept, 1), 3)
        sites_map[dom] = {
            "kept": new_kept,
            "hits": int(prev.get("hits") or 0) + len(scores),
            "avg_score": avg,
            "discovered": bool(prev.get("discovered") or dom in discovered_set),
            "last_seen": today,
        }
        if prev_kept == 0:
            newly_learned.append(dom)

    # Bound stored domains and topic buckets so learned_sites.json stays fast and free of tail noise.
    sorted_sites = sorted(
        sites_map.items(),
        key=lambda kv: (int(kv[1].get("kept") or 0) * (float(kv[1].get("avg_score") or 0.5) + 0.5), kv[1].get("last_seen") or ""),
        reverse=True,
    )[:25]
    existing["sites"] = dict(sorted_sites)
    existing["synonyms"] = _dedupe_strs(
        [str(s).strip().lower() for s in (existing.get("synonyms") or [])]
        + [str(s).strip().lower() for s in used_synonyms if str(s).strip()]
    )[:30]
    existing["runs"] = int(existing.get("runs") or 0) + 1
    existing["updated_at"] = today
    topics[key] = existing

    if len(topics) > 40:
        ranked_buckets = sorted(
            topics.items(),
            key=lambda kv: (str(kv[1].get("updated_at") or ""), int(kv[1].get("runs") or 0)),
            reverse=True,
        )[:40]
        topics = dict(ranked_buckets)

    if domain_counts or existing["synonyms"]:
        save_learned_topics(topics, modes_path=modes_path, learned_path=learned_path)

    # Promote high-yield domains directly into modes.json only for custom modes or explicit `learn_mode`.
    target_mode = ""
    if learn_mode.strip():
        target_mode = normalize_mode_name(learn_mode)
    elif mode_name and mode_name not in (*BUILTIN_MODES, "auto", "crawl"):
        target_mode = normalize_mode_name(mode_name)

    if target_mode and (domain_counts or used_synonyms):
        top_kept_sites = [
            dom for dom, _ in sorted(domain_counts.items(), key=lambda kv: len(kv[1]), reverse=True)[:10]
        ]
        existing_mode = get_mode(target_mode, path=modes_path)
        merged_sites = _dedupe_strs((existing_mode.sites if existing_mode else []) + top_kept_sites)[:30]
        merged_syns = _dedupe_strs(
            (existing_mode.synonyms if existing_mode else []) + [str(s).strip().lower() for s in used_synonyms if str(s).strip()]
        )[:30]
        upsert_mode(
            name=target_mode,
            base_mode=existing_mode.base_mode if existing_mode else base_mode,
            sites=merged_sites,
            synonyms=merged_syns,
            path=modes_path,
        )

    return newly_learned


def list_learned_topics(
    modes_path: Path | str | None = None, learned_path: Path | str | None = None
) -> list[dict[str, Any]]:
    """Return human-readable summary rows for all self-learned topic buckets."""
    topics = load_learned_topics(modes_path, learned_path)
    rows: list[dict[str, Any]] = []
    for key, bucket in sorted(topics.items(), key=lambda kv: str(kv[1].get("updated_at") or ""), reverse=True):
        sites = bucket.get("sites") or {}
        top_sites = [
            f"{dom} ({info.get('kept', 0)} kept)"
            for dom, info in list(sites.items())[:8]
            if isinstance(info, dict)
        ]
        rows.append(
            {
                "key": key,
                "base_mode": bucket.get("base_mode", "general"),
                "tokens": bucket.get("tokens", []),
                "top_sites": top_sites,
                "synonyms": list(bucket.get("synonyms") or [])[:10],
                "runs": int(bucket.get("runs") or 0),
                "updated_at": bucket.get("updated_at", ""),
            }
        )
    return rows


def clear_learned_topics(
    topic_key: str = "",
    modes_path: Path | str | None = None,
    learned_path: Path | str | None = None,
) -> int:
    """Clear all learned topics or one specific topic key; returns count removed."""
    topics = load_learned_topics(modes_path, learned_path)
    if not topics:
        return 0
    if not topic_key.strip():
        count = len(topics)
        save_learned_topics({}, modes_path=modes_path, learned_path=learned_path)
        return count
    clean = topic_key.strip().lower()
    removed = 0
    for k in list(topics):
        if k.lower() == clean or clean in k.lower():
            del topics[k]
            removed += 1
    if removed:
        save_learned_topics(topics, modes_path=modes_path, learned_path=learned_path)
    return removed


def promote_learned_to_mode(
    topic_key: str,
    target_mode: str,
    modes_path: Path | str | None = None,
    learned_path: Path | str | None = None,
) -> ModeConfig:
    """Promote learned sites and synonyms for `topic_key` into a reusable custom mode."""
    topics = load_learned_topics(modes_path, learned_path)
    clean = topic_key.strip().lower()
    matched = topics.get(topic_key)
    if matched is None:
        for k, v in topics.items():
            if k.lower() == clean or clean in k.lower():
                matched = v
                break
    if matched is None:
        raise KeyError(f"No learned topic matching '{topic_key}'.")
    sites = list((matched.get("sites") or {}).keys())[:25]
    syns = list(matched.get("synonyms") or [])[:25]
    existing = get_mode(target_mode, path=modes_path)
    return upsert_mode(
        name=target_mode,
        base_mode=existing.base_mode if existing else str(matched.get("base_mode") or "general"),
        sites=_dedupe_strs((existing.sites if existing else []) + sites),
        synonyms=_dedupe_strs((existing.synonyms if existing else []) + syns),
        path=modes_path,
    )
