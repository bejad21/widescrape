"""Ambitious query expansion, custom modes, and preset recipes for WideScrape."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from widescrape.extractor import normalize_fields, resolve_extract_mode
from widescrape.modes import (
    BUILTIN_MODES,
    clean_site_domain,
    get_mode,
    recall_learned_context,
)

_JOBS_RE = re.compile(
    r"\b(?:jobs?|hiring|careers?|internships?|freelance|opening|openings)\b"
    r"|\bremote\s+(?:job|jobs|role|roles|work|engineer|developer|ai|swe)\b"
    r"|\bwork\s+from\s+(?:home|anywhere)\b",
    re.IGNORECASE,
)
_RESEARCH_RE = re.compile(
    r"\b(?:how\s+to|how\s+i|best|guide|build|workflow|architecture|tutorial|benchmark|vs)\b",
    re.IGNORECASE,
)
_NO_EXP_RE = re.compile(
    r"\b(?:no\s+(?:prior\s+|relevant\s+)?experience(?:\s+(?:needed|required))?|without\s+(?:prior\s+)?experience|"
    r"entry[\s-]level|fresh\s*grad(?:uate)?|fresher|new\s*grad|junior|intern(?:ship)?|"
    r"0[\s-]*(?:-|to)?\s*[12]\s*(?:years?|yrs?)|less\s+than\s+(?:a|1)\s*year|\b\d{1,2}\s*months?\b)\b",
    re.IGNORECASE,
)
_NO_EXP_STRIP_RE = re.compile(
    r"\b(?:with\s+)?(?:no\s+(?:prior\s+|relevant\s+)?experience(?:\s+(?:needed|required))?|without\s+(?:prior\s+)?experience)\b",
    re.IGNORECASE,
)
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have",
    "how", "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "was",
    "what", "when", "where", "which", "who", "will", "with", "vs",
}

JOB_SIGNAL_WORDS = [
    "hiring", "looking for", "we're hiring", "dm me", "apply", "role",
    "remote", "salary", "equity", "founding", "engineer", "openings",
]

RESEARCH_SIGNAL_WORDS = [
    "workflow", "benchmark", "architecture", "repo", "open source", "how i built",
    "lessons", "claude", "mcp", "skill", "tips", "comparison", "guide",
]

DEFAULT_SOURCES = [
    "reddit",
    "linkedin_posts",
    "linkedin_jobs",
    "x",
    "github",
    "hackernews",
    "news",
    "web",
]


@dataclass(frozen=True)
class SearchPlan:
    """Expanded queries tailored per platform, search mode, freshness window, and extractor."""

    raw_query: str
    mode: str
    core_terms: tuple[str, ...]
    dork_queries: tuple[tuple[str, str], ...]  # (source_label, search_query)
    api_queries: tuple[str, ...]
    signal_keywords: tuple[str, ...]
    boost_keywords: tuple[str, ...] = ()
    penalty_keywords: tuple[str, ...] = ()
    base_mode: str = "general"
    days: int | None = None
    strict_days: bool = False
    extract_mode: str = ""
    extract_fields: tuple[str, ...] = ()
    rank_fields: tuple[str, ...] = ()
    effective_sources: tuple[str, ...] = ()
    region: str = ""
    synonyms: tuple[str, ...] = ()
    synonym_groups: tuple[tuple[str, tuple[str, ...]], ...] = ()
    exclude_terms: tuple[str, ...] = ()
    extra_queries: tuple[str, ...] = ()
    learned_sites: tuple[str, ...] = ()
    explicit_sites: tuple[str, ...] = ()
    discover_sites: bool = True
    learn: bool = True
    learn_mode: str = ""

    def synonym_map(self) -> dict[str, tuple[str, ...]]:
        """Return mapping of core key -> expanded synonymous phrases."""
        return dict(self.synonym_groups)


def normalize_sources(sources: list[str] | None, mode: str = "general") -> set[str]:
    """Expand convenience aliases (e.g. 'linkedin', 'stackoverflow', 'boards') and mode defaults."""
    if not sources:
        active = set(DEFAULT_SOURCES)
        if mode == "research":
            active.update({"arxiv", "stackexchange"})
        elif mode == "jobs":
            active.add("boards")
        return active
    resolved: set[str] = set()
    for src in sources:
        s = src.strip().lower()
        if s == "linkedin":
            resolved.update({"linkedin_posts", "linkedin_jobs"})
        elif s in ("stackoverflow", "so"):
            resolved.add("stackexchange")
        elif s == "google_news":
            resolved.add("news")
        elif s in ("boards", "remote_boards", "job_boards"):
            resolved.add("boards")
        elif s:
            resolved.add(s)
    return resolved


def extract_core_terms(query: str) -> tuple[str, ...]:
    """Preserve short domain tokens (AI, Go, ML, C#, JS) while stripping English stopwords."""
    cleaned = query.lower().replace('"', "").replace("'", "")
    tokens = [t.strip(".,!?:;()[]{}") for t in cleaned.split()]
    kept = [t for t in tokens if t and t not in _STOPWORDS]
    return tuple(dict.fromkeys(kept))


def parse_term_list(raw: list[str] | tuple[str, ...] | str | None) -> tuple[str, ...]:
    """Parse JSON array, comma/newline/semicolon-separated string, or iterable into unique clean phrases."""
    if not raw:
        return ()
    parts: list[Any] = []
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return ()
        if stripped.startswith("["):
            try:
                loaded = json.loads(stripped)
                if isinstance(loaded, list):
                    parts = loaded
            except ValueError:
                parts = re.split(r"[,;\n|]+", stripped)
        else:
            parts = re.split(r"[,;\n|]+", stripped)
    else:
        parts = list(raw)
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        clean = " ".join(str(p or "").strip().split())
        low = clean.lower()
        if clean and low not in seen:
            seen.add(low)
            out.append(clean)
    return tuple(out)


def parse_synonyms_input(
    raw: dict[str, list[str] | tuple[str, ...] | str] | list[str] | tuple[str, ...] | str | None,
) -> tuple[dict[str, list[str]], list[str]]:
    """Parse caller-provided synonyms (dict, JSON dict/list, or comma string) into groups + flat list."""
    groups: dict[str, list[str]] = {}
    flat: list[str] = []
    seen_flat: set[str] = set()

    def _add_flat(term: str) -> None:
        clean = " ".join(term.strip().split())
        low = clean.lower()
        if clean and low not in seen_flat:
            seen_flat.add(low)
            flat.append(clean)

    parsed: Any = raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return {}, []
        if s.startswith("{") or s.startswith("["):
            try:
                parsed = json.loads(s)
            except ValueError:
                parsed = list(parse_term_list(s))
        else:
            parsed = list(parse_term_list(s))

    if isinstance(parsed, dict):
        for k, vals in parsed.items():
            key = " ".join(str(k or "").strip().lower().split())
            if not key:
                continue
            val_items = [str(vals)] if isinstance(vals, str) and "," not in vals else list(parse_term_list(vals))
            clean_vals: list[str] = []
            for v in val_items:
                vc = " ".join(str(v).strip().split())
                if vc:
                    clean_vals.append(vc)
                    _add_flat(vc)
            if clean_vals:
                groups[key] = list(dict.fromkeys([key, *clean_vals]))
            _add_flat(key)
    elif isinstance(parsed, (list, tuple, set)):
        for v in parsed:
            _add_flat(str(v or ""))

    return groups, flat


def _quote_dork_term(term: str) -> str:
    clean = " ".join(term.strip().replace('"', "").split())
    if not clean:
        return ""
    return f'"{clean}"' if " " in clean else clean


def _build_synonym_or_clause(terms: list[str] | tuple[str, ...], max_terms: int = 5) -> str:
    quoted = [_quote_dork_term(t) for t in terms[:max_terms] if _quote_dork_term(t)]
    if not quoted:
        return ""
    if len(quoted) == 1:
        return quoted[0]
    return "(" + " OR ".join(quoted) + ")"


def expand_query(
    query: str,
    mode: str = "auto",
    sources: list[str] | None = None,
    sites: list[str] | None = None,
    days: int | None = None,
    strict_days: bool = False,
    extract: str = "",
    fields: list[str] | str | None = None,
    rank_fields: list[str] | str | None = None,
    region: str = "",
    synonyms: dict[str, list[str] | tuple[str, ...] | str] | list[str] | tuple[str, ...] | str | None = None,
    exclude: list[str] | tuple[str, ...] | str | None = None,
    extra_queries: list[str] | tuple[str, ...] | str | None = None,
    discover_sites: bool = True,
    learn: bool = True,
    learn_mode: str = "",
    modes_path: Path | str | None = None,
    learned_path: Path | str | None = None,
) -> SearchPlan:
    """Turn a user query + runtime synonyms/exclusions + built-in/custom/learned mode into ambitious queries."""
    clean = " ".join(query.strip().split())
    lower = clean.lower()

    requested_mode = (mode or "auto").strip().lower()
    custom_cfg = None
    if requested_mode == "auto":
        if _JOBS_RE.search(lower):
            resolved_mode = "jobs"
        elif _RESEARCH_RE.search(lower) or re.search(r"\b(?:claude|mcp|rag|github|arxiv)\b", lower):
            resolved_mode = "research"
        else:
            resolved_mode = "general"
        custom_cfg = get_mode(resolved_mode, path=modes_path)
    else:
        custom_cfg = get_mode(requested_mode, path=modes_path)
        if custom_cfg is not None:
            resolved_mode = custom_cfg.base_mode
        elif requested_mode in BUILTIN_MODES:
            resolved_mode = requested_mode
        else:
            raise ValueError(
                f"Unknown mode '{mode}'. Use auto/jobs/research/general or create it with `widescrape mode add {requested_mode}`."
            )

    core_terms = extract_core_terms(clean)

    recalled_sites: list[str] = []
    recalled_syns: list[str] = []
    if learn and not sites:
        recalled_sites, recalled_syns = recall_learned_context(
            core_terms,
            base_mode=resolved_mode,
            modes_path=modes_path,
            learned_path=learned_path,
        )

    caller_groups, caller_flat_syns = parse_synonyms_input(synonyms)
    mode_syns = list(custom_cfg.synonyms) if custom_cfg is not None else []
    merged_flat_syns: list[str] = []
    seen_syn_low: set[str] = set()
    for s in [*caller_flat_syns, *mode_syns, *recalled_syns]:
        sc = " ".join(s.strip().split())
        if sc and sc.lower() not in seen_syn_low:
            seen_syn_low.add(sc.lower())
            merged_flat_syns.append(sc)

    exclude_terms = tuple(
        dict.fromkeys(
            [
                *(list(custom_cfg.exclude_keywords) if custom_cfg is not None else []),
                *list(parse_term_list(exclude)),
            ]
        )
    )
    extra_q_list = list(parse_term_list(extra_queries))

    effective_sources_input = sources
    if effective_sources_input is None and custom_cfg is not None and custom_cfg.sources:
        effective_sources_input = custom_cfg.sources

    combined_sites: list[str] = []
    seen_sites: set[str] = set()
    for source_list in (
        custom_cfg.sites if custom_cfg is not None else [],
        recalled_sites,
        sites or [],
    ):
        for dom in source_list:
            cd = clean_site_domain(dom)
            if cd and cd not in seen_sites:
                seen_sites.add(cd)
                combined_sites.append(cd)

    # days == 0 explicitly disables mode default_days; days is None inherits custom default_days.
    if days == 0:
        effective_days = None
    elif days is not None and days > 0:
        effective_days = days
    else:
        effective_days = custom_cfg.default_days if custom_cfg else None

    raw_extract = extract if extract else (custom_cfg.default_extract if custom_cfg else "")
    effective_extract = resolve_extract_mode(raw_extract, query=clean, base_mode=resolved_mode)
    raw_fields = fields if fields else (custom_cfg.default_fields if custom_cfg else [])
    effective_fields = tuple(normalize_fields(raw_fields))
    raw_rank = rank_fields if rank_fields else (custom_cfg.default_rank_fields if custom_cfg else [])
    effective_rank_fields = tuple(normalize_fields(raw_rank))

    active = normalize_sources(effective_sources_input, mode=resolved_mode)
    dorks: list[tuple[str, str]] = []
    api_queries: list[str] = [clean]
    signal_words: list[str] = []
    boost_words: list[str] = []
    penalty_words: list[str] = []

    syn_clause = _build_synonym_or_clause(merged_flat_syns, max_terms=4)
    for eq in extra_q_list[:4]:
        if eq.lower() not in {q.lower() for q in api_queries}:
            api_queries.append(eq)
    if merged_flat_syns:
        anchor = " ".join(core_terms[:2])
        for syn in merged_flat_syns[:3]:
            cand = syn if (len(syn.split()) >= 2 or not anchor or anchor in syn.lower()) else f"{syn} {anchor}"
            cand = " ".join(cand.split()).strip()
            if cand and cand.lower() not in {q.lower() for q in api_queries}:
                api_queries.append(cand)

    # Custom mode & learned sites augment web search; explicit `--sites` restricts open-web discovery.
    has_explicit_sites = bool(sites)
    explicit_site_list = [clean_site_domain(s) for s in (sites or []) if clean_site_domain(s)]
    neg_suffix = " ".join(f"-{_quote_dork_term(ex)}" for ex in exclude_terms[:4] if _quote_dork_term(ex))

    for dom in combined_sites:
        q_dom = f"site:{dom} {clean}"
        dorks.append(("web", f"{q_dom} {neg_suffix}".strip()))
        if syn_clause and not has_explicit_sites:
            dorks.append(("web", f"site:{dom} {syn_clause}".strip()))

    wants_remote = bool(re.search(r"\b(?:remote|wfh|distributed)\b|work\s+from\s+(?:home|anywhere)", lower))
    if wants_remote:
        boost_words.extend(["remote", "work from anywhere", "fully remote", "wfh", "async", "distributed"])
        penalty_words.extend(["onsite only", "on-site only", "in-office only"])

    wants_no_exp = bool(_NO_EXP_RE.search(lower))
    dork_clean = clean
    if wants_no_exp:
        boost_words.extend([
            "no experience", "no prior experience", "entry level", "entry-level",
            "0-1 years", "0-2 years", "fresh graduate", "junior", "intern",
        ])
        penalty_words.extend([
            "5+ years", "7+ years", "10+ years", "senior only",
            "staff engineer", "principal engineer",
        ])
        stripped = " ".join(_NO_EXP_STRIP_RE.sub("", clean).split()).strip()
        base_q = stripped or clean
        dork_clean = f'{base_q} ("no experience" OR "entry level" OR "0-1 years" OR "junior")'

    # Open-web ambitious synonym / extra-query expansion shared across ALL modes.
    if "web" in active and not has_explicit_sites:
        if syn_clause:
            dorks.append(("web", f"{clean} {syn_clause} {neg_suffix}".strip()))
            if len(merged_flat_syns) >= 3:
                second_clause = _build_synonym_or_clause(merged_flat_syns[2:6], max_terms=4)
                if second_clause:
                    dorks.append(("web", f"{second_clause} {neg_suffix}".strip()))
        for eq in extra_q_list[:3]:
            dorks.append(("web", f"{eq} {neg_suffix}".strip()))

    if resolved_mode == "jobs":
        signal_words = list(JOB_SIGNAL_WORDS)
        api_queries.extend([
            f"{clean} hiring",
            f"{clean} remote" if not wants_remote else f"{clean} work from anywhere",
        ])
        if "linkedin_posts" in active:
            dorks.extend([
                ("linkedin_posts", f'site:linkedin.com/posts/ {dork_clean} hiring'),
                ("linkedin_posts", f'site:linkedin.com/posts/ {dork_clean} ("we\'re hiring" OR "we are hiring")'),
                ("linkedin_posts", f'site:linkedin.com/posts/ {dork_clean} apply remote'),
                ("linkedin_posts", f'site:linkedin.com/posts/ {dork_clean} founding'),
                ("linkedin_posts", f'site:linkedin.com/posts/ {dork_clean} "DM" hiring'),
            ])
            if syn_clause:
                dorks.append(("linkedin_posts", f"site:linkedin.com/posts/ {syn_clause} hiring"))
            if wants_remote:
                dorks.append(("linkedin_posts", f'site:linkedin.com/posts/ {dork_clean} "work from anywhere"'))
        if "linkedin_jobs" in active:
            dorks.extend([
                ("linkedin_jobs", f'site:linkedin.com/jobs/view/ {dork_clean}'),
                ("linkedin_jobs", f'site:linkedin.com/jobs/view/ {dork_clean} hiring'),
            ])
        if "x" in active:
            dorks.extend([
                ("x", f'site:x.com {dork_clean} hiring'),
                ("x", f'site:x.com {dork_clean} ("we\'re hiring" OR "join our team")'),
                ("x", f'site:x.com {dork_clean} "DM me" hiring'),
                ("x", f'site:x.com {dork_clean} remote role'),
                ("x", f'site:twitter.com {dork_clean} hiring'),
            ])
            if syn_clause:
                dorks.append(("x", f"site:x.com {syn_clause} hiring"))
            if wants_remote:
                dorks.append(("x", f'site:x.com {dork_clean} "work from anywhere"'))
        if "reddit" in active:
            dorks.extend([
                ("reddit", f'site:reddit.com {dork_clean} hiring'),
                ("reddit", f'site:reddit.com {dork_clean} remote job'),
            ])
        if "hackernews" in active:
            api_queries.append(f"Ask HN Who is hiring {clean}")
        if "web" in active and not has_explicit_sites:
            dorks.extend([
                ("web", f'{dork_clean} (careers OR "apply now" OR "join our team") {neg_suffix}'.strip()),
                ("web", f"site:jobs.ashbyhq.com {dork_clean}"),
                ("web", f"site:job-boards.greenhouse.io {dork_clean}"),
                ("web", f"site:boards.greenhouse.io {dork_clean}"),
                ("web", f"site:jobs.lever.co {dork_clean}"),
                ("web", f"site:apply.workable.com {dork_clean}"),
                ("web", f"site:workatastartup.com {dork_clean}"),
                ("web", f"site:wellfound.com/jobs {dork_clean}"),
                ("web", f"site:himalayas.app/companies {dork_clean}"),
            ])

    elif resolved_mode == "research":
        signal_words = list(RESEARCH_SIGNAL_WORDS)
        api_queries.extend([
            f"{clean} workflow",
            f"{clean} github",
        ])
        if "linkedin_posts" in active:
            dorks.extend([
                ("linkedin_posts", f'site:linkedin.com/posts/ {clean}'),
                ("linkedin_posts", f'site:linkedin.com/posts/ {clean} workflow guide'),
                ("linkedin_posts", f'site:linkedin.com/pulse/ {clean}'),
            ])
        if "linkedin_jobs" in active:
            dorks.append(("linkedin_jobs", f'site:linkedin.com/jobs/view/ {clean}'))
        if "x" in active:
            dorks.extend([
                ("x", f'site:x.com {clean} thread'),
                ("x", f'site:x.com {clean} guide workflow'),
                ("x", f'site:x.com {clean} tips'),
                ("x", f'site:twitter.com {clean}'),
            ])
        if "reddit" in active:
            dorks.extend([
                ("reddit", f'site:reddit.com {clean} workflow'),
                ("reddit", f'site:reddit.com {clean} guide tips'),
            ])
        if "web" in active and not has_explicit_sites:
            dorks.extend([
                ("web", f"{clean} guide architecture github {neg_suffix}".strip()),
                ("web", f"{clean} substack blog tutorial {neg_suffix}".strip()),
                ("web", f"{clean} benchmark dataset paper {neg_suffix}".strip()),
            ])

    else:
        signal_words = list(RESEARCH_SIGNAL_WORDS + JOB_SIGNAL_WORDS)
        if "linkedin_posts" in active:
            dorks.extend([
                ("linkedin_posts", f'site:linkedin.com/posts/ "{clean}"'),
                ("linkedin_posts", f'site:linkedin.com/posts/ {clean}'),
                ("linkedin_posts", f'site:linkedin.com/pulse/ {clean}'),
            ])
        if "linkedin_jobs" in active:
            dorks.append(("linkedin_jobs", f'site:linkedin.com/jobs/view/ {clean}'))
        if "x" in active:
            dorks.extend([
                ("x", f'site:x.com "{clean}"'),
                ("x", f'site:x.com {clean}'),
                ("x", f'site:twitter.com {clean}'),
            ])
        if "reddit" in active:
            dorks.append(("reddit", f'site:reddit.com {clean}'))
            if syn_clause:
                dorks.append(("reddit", f"site:reddit.com {syn_clause}"))
        if "web" in active and not has_explicit_sites:
            dorks.append(("web", f"{clean} {neg_suffix}".strip()))

    if merged_flat_syns:
        signal_words.extend([s.lower() for s in merged_flat_syns[:16]])
    if custom_cfg is not None:
        signal_words.extend(custom_cfg.signal_keywords)
        boost_words.extend(custom_cfg.boost_keywords)
        penalty_words.extend(custom_cfg.penalty_keywords)

    deduped_dorks: list[tuple[str, str]] = []
    seen_dork_keys: set[tuple[str, str]] = set()
    for pair in dorks:
        if pair not in seen_dork_keys:
            seen_dork_keys.add(pair)
            deduped_dorks.append(pair)

    syn_group_tuples = tuple(
        (k, tuple(dict.fromkeys(v))) for k, v in sorted(caller_groups.items())
    )
    label_mode = (
        learn_mode.strip().lower()
        if learn_mode.strip() and requested_mode in (*BUILTIN_MODES, "auto")
        else (custom_cfg.name if custom_cfg is not None else resolved_mode)
    )
    return SearchPlan(
        raw_query=clean,
        mode=label_mode,
        base_mode=resolved_mode,
        core_terms=core_terms,
        dork_queries=tuple(deduped_dorks),
        api_queries=tuple(dict.fromkeys(api_queries)),
        signal_keywords=tuple(dict.fromkeys(signal_words)),
        boost_keywords=tuple(dict.fromkeys(boost_words)),
        penalty_keywords=tuple(dict.fromkeys(penalty_words)),
        days=effective_days,
        strict_days=strict_days and bool(effective_days and effective_days > 0),
        extract_mode=effective_extract,
        extract_fields=effective_fields,
        rank_fields=effective_rank_fields,
        effective_sources=tuple(sorted(active)),
        region=region.strip(),
        synonyms=tuple(merged_flat_syns),
        synonym_groups=syn_group_tuples,
        exclude_terms=exclude_terms,
        extra_queries=tuple(extra_q_list),
        learned_sites=tuple(recalled_sites),
        explicit_sites=tuple(explicit_site_list),
        discover_sites=bool(discover_sites and not has_explicit_sites),
        learn=bool(learn),
        learn_mode=learn_mode.strip(),
    )
