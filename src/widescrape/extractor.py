"""Targeted structured data extractors (promo codes, jobs, and custom schema fields)."""

from __future__ import annotations

import re
from typing import Iterable

from widescrape.models import ScrapeItem

# Capture promo codes case-sensitively inside case-insensitive context phrases.
_CODE_CONTEXT_RE = re.compile(
    r"(?i)\b(?:(?:promo|coupon|discount|voucher|use|apply|with|enter)\s+code|code\s*(?:is|[:=]))(?:\s+|[:=]+\s*)[`'\"\[]*(?-i:([A-Z0-9][A-Z0-9_-]{3,15}))\b"
)
_BACKTICK_CODE_RE = re.compile(r"[`'\"]([A-Z0-9]{4,14})[`'\"]")
_CODE_STOPWORDS = {
    "CODE", "CODES", "PROMO", "COUPON", "SALE", "DEAL", "DEALS", "FREE", "SAVE",
    "OFFER", "OFFERS", "WHITE", "BLACK", "FRIDAY", "AMAZON", "PRIME", "HTTP",
    "HTTPS", "HTML", "JSON", "TRUE", "FALSE", "NULL", "NONE", "AED", "USD",
    "UAE", "DUBAI", "SHOP", "LINK", "CLICK", "HERE", "APPLY", "ONLY", "MORE",
    "VISA", "BANK", "CARD", "AVAILABLE", "FROM", "TODAY", "WITH", "BEST",
}

_DISCOUNT_RE = re.compile(
    r"((?:up\s+to\s+)?\d{1,3}%\s*off|(?:AED|USD|EUR|GBP|SAR|\$|€|£)\s*\d[\d,]*(?:\.\d+)?\s+(?:off|discount|cashback)\b)",
    re.IGNORECASE,
)

_BANK_CARDS = (
    "ADCB", "ENBD", "Emirates NBD", "Emirates Islamic", "FAB", "First Abu Dhabi",
    "HSBC", "Citi", "Citibank", "Mashreq", "RAKBANK", "DIB", "CBD", "Mastercard",
    "Visa", "Amex", "Prime", "Tabby", "Tamara",
)
# Keep uppercase acronyms case-sensitive so words like "fab", "cbd", or "dib" do not false-positive.
_BANK_PATTERNS = [
    (b, re.compile(rf"\b{re.escape(b)}\b", 0 if b.isupper() else re.IGNORECASE))
    for b in _BANK_CARDS
]

_SALARY_RE = re.compile(
    r"((?:\$|USD|EUR|GBP|AED)\s?(?:\d{2,3}(?:,\d{3})+|\d{2,3}k|\d{5,6}|\d[\d,]*(?:\.\d+)?(?:\s*(?:/yr|/year|/hr|/hour|/mo|/month)))(?:\s*(?:-|–|\bto\b)\s*(?:\$|USD|EUR|GBP|AED)?\s?(?:\d{2,3}(?:,\d{3})+|\d{2,3}k|\d{5,6}|\d[\d,]*(?:\.\d+)?))?(?:\s*(?:/yr|/year|/hr|/hour|/mo|/month|per\s+year|per\s+month|annually))?|\b\d{2,3}k\s*(?:-|–|\bto\b)\s*\d{2,3}k\b|\b\d{2,3}k\s*(?:USD|EUR|GBP|AED|SAR)\b)",
    re.IGNORECASE,
)
_WORK_NEGATED_RE = re.compile(r"\b(?:not\s+remote|no\s+remote|onsite\s+only|on-site\s+only|in-office\s+only)\b", re.IGNORECASE)
_WORK_TYPE_PATTERNS = (
    ("remote", re.compile(r"\b(?:fully\s+remote|work\s+from\s+anywhere|100%\s+remote|remote)\b", re.IGNORECASE)),
    ("hybrid", re.compile(r"\bhybrid\b", re.IGNORECASE)),
    ("onsite", re.compile(r"\b(?:on-site|onsite|in-office)\b", re.IGNORECASE)),
)
_SENIORITY_TERMS = (
    "founding", "principal", "staff", "lead", "senior",
    "mid-level", "junior", "entry-level", "entry level",
    "fresh graduate", "fresh grad", "fresher", "new grad", "intern",
)
_SENIORITY_PATTERNS = [(s, re.compile(rf"\b{re.escape(s)}\b", re.IGNORECASE)) for s in _SENIORITY_TERMS]
_NO_EXP_MATCH_RE = re.compile(
    r"\b(?:no\s+(?:prior\s+|relevant\s+)?experience(?:\s+(?:needed|required))?|"
    r"without\s+(?:prior\s+)?experience|entry[\s-]level|fresh\s*grad(?:uate)?|fresher|new\s*grad)\b",
    re.IGNORECASE,
)
_YEARS_EXP_RE = re.compile(
    r"(?:\b(?:\d{1,2}\s*(?:-|–|to)\s*\d{1,2}\s*\+?\s*(?:years?|yrs?)|"
    r"\d{1,2}\s*\+\s*(?:years?|yrs?))"
    r"(?:\s+(?:of\s+)?(?:[\w-]+\s+){0,2}experience\b)?(?:\s*(?:\((?:preferred|plus|ideal|nice to have|optional)\)|\b(?:preferred|plus|ideal|nice to have|optional)\b))?|"
    r"\b0\s*(?:-|–|to)\s*[12]\s*(?:years?|yrs?)\b|"
    r"\b\d{1,2}\+?\s*months?\s+(?:of\s+)?experience\b|"
    r"\bminimum\s+(?:of\s+)?\d{1,2}\s*(?:years?|yrs?)(?:\s+of\s+experience)?\b)",
    re.IGNORECASE,
)
_LOCATION_HINT_RE = re.compile(
    r"\b(?:worldwide|global|work\s+from\s+anywhere|emea|apac|latam|north\s+america|"
    r"us[\s-]only|eu[\s-]only|uk|united\s+kingdom|united\s+states|europe|canada|germany|"
    r"dubai|uae|abu\s+dhabi|london|berlin|singapore|india)\b",
    re.IGNORECASE,
)

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")
_PRICE_RE = re.compile(
    r"(?:\b(?:AED|USD|EUR|GBP|SAR)\s?\d[\d,]*(?:\.\d+)?k?|\$\s?\d[\d,]*(?:\.\d+)?k?(?:\s*(?:-|–)\s*\$?\d[\d,]*(?:\.\d+)?k?)?)",
    re.IGNORECASE,
)
_DATE_PHRASE_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|(?:valid\s+(?:until|till|through)|expires?|deadline|ends?)\s*[:\-]?\s*[A-Za-z]{3,9}\s+\d{1,2}(?:,?\s*\d{4})?|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,?\s*\d{4})?)\b",
    re.IGNORECASE,
)
_EXCLUDED_CODE_FIELDS = {"zip_code", "postal_code", "status_code", "error_code", "area_code", "country_code"}


def resolve_extract_mode(extract: str, query: str = "", base_mode: str = "general") -> str:
    """Resolve `auto` or empty extract setting into `'codes'`, `'jobs'`, or `''`."""
    clean = (extract or "").strip().lower()
    if clean in ("none", "off", "false"):
        return ""
    if clean in ("codes", "promo", "coupons", "deals"):
        return "codes"
    if clean in ("jobs", "hiring"):
        return "jobs"
    if clean == "auto":
        q_lower = query.lower()
        if any(re.search(rf"\b{w}\b", q_lower) for w in ("promo", "promos", "coupon", "coupons", "voucher", "discount code", "deal", "deals", "sale")):
            return "codes"
        if base_mode == "jobs":
            return "jobs"
    return ""


def _is_valid_promo_token(cand: str) -> bool:
    if not (4 <= len(cand) <= 15) or cand in _CODE_STOPWORDS or cand.isdigit():
        return False
    # Require mixed letters+digits or underscore/hyphen, unless at least 5 uppercase chars.
    return bool(re.search(r"[A-Z]", cand) and (re.search(r"[0-9_-]", cand) or len(cand) >= 5))


def _extract_codes_fields(text: str) -> dict:
    codes: list[str] = []
    seen: set[str] = set()
    for pat in (_CODE_CONTEXT_RE, _BACKTICK_CODE_RE):
        for match in pat.findall(text):
            cand = match.strip().upper()
            if _is_valid_promo_token(cand) and cand not in seen:
                seen.add(cand)
                codes.append(cand)

    discounts = []
    for d in _DISCOUNT_RE.findall(text):
        cleaned = " ".join(d.split())
        if cleaned.lower() not in {x.lower() for x in discounts}:
            discounts.append(cleaned)

    banks = [label for label, pat in _BANK_PATTERNS if pat.search(text)]

    out: dict = {}
    if codes:
        out["codes"] = codes[:5]
    if discounts:
        out["discounts"] = discounts[:3]
    if banks:
        out["bank_card"] = banks[:5]
    return out


def _extract_experience_tags(text: str) -> list[str]:
    """Extract experience level hints such as 'no experience', 'entry-level', or '2-4 years'."""
    tags: list[str] = []
    seen: set[str] = set()
    for pat in (_NO_EXP_MATCH_RE, _YEARS_EXP_RE):
        for m in pat.findall(text):
            cleaned = " ".join(m.split()).strip()
            low = cleaned.lower()
            if cleaned and low not in seen:
                seen.add(low)
                tags.append(cleaned)
    return tags[:3]


def _extract_jobs_fields(text: str) -> dict:
    salaries: list[str] = []
    for m in _SALARY_RE.findall(text):
        val = " ".join(m.split())
        nums = [int(n.replace(",", "")) for n in re.findall(r"\d+", val.replace(",", "")) if n.isdigit()]
        has_scale = any(k in val.lower() for k in ("k", "/yr", "year", "/hr", "hour", "/mo", "month", ","))
        if not has_scale and nums and max(nums) < 1000:
            continue
        if len(val) >= 3 and val not in salaries:
            salaries.append(val)

    work_type = ""
    if _WORK_NEGATED_RE.search(text):
        work_type = "onsite"
    else:
        for label, pat in _WORK_TYPE_PATTERNS:
            if pat.search(text):
                work_type = label
                break

    seniority = [label for label, pat in _SENIORITY_PATTERNS if pat.search(text)][:2]
    experience = _extract_experience_tags(text)

    out: dict = {}
    if salaries:
        out["salary"] = salaries[:2]
    if work_type:
        out["work_type"] = work_type
    if seniority:
        out["seniority"] = seniority
    if experience:
        out["experience"] = experience
    return out


def normalize_fields(fields: list[str] | str | None) -> list[str]:
    """Normalize comma-separated or list field names cleanly."""
    if not fields:
        return []
    raw_list = fields.split(",") if isinstance(fields, str) else fields
    seen: set[str] = set()
    out: list[str] = []
    for f in raw_list:
        clean = re.sub(r"[^a-z0-9_]+", "_", str(f).strip().lower()).strip("_")
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def _jsonld_field_lookup(item: ScrapeItem, ftokens: set[str], fname: str) -> str:
    """Return structured schema.org JSON-LD value matching the requested custom field name."""
    jld = item.metadata.get("jsonld") if isinstance(item.metadata, dict) else None
    if not isinstance(jld, dict) or not jld:
        return ""
    if (ftokens & {"company", "org", "employer", "brand", "seller"}) and jld.get("company"):
        return str(jld["company"])
    if (ftokens & {"salary", "comp", "compensation", "pay"}) and jld.get("salary"):
        return str(jld["salary"])
    if (ftokens & {"price", "cost", "fee", "budget", "spend"}) and jld.get("price"):
        return str(jld["price"])
    if (ftokens & {"deadline", "expiry", "expires", "valid"} or fname.startswith("expir")) and jld.get("deadline"):
        return str(jld["deadline"])
    if (ftokens & {"remote", "work", "type"}) and jld.get("work_type"):
        return str(jld["work_type"])
    if (ftokens & {"location", "office", "city", "country"}) and jld.get("location"):
        return str(jld["location"])
    if fname not in _EXCLUDED_CODE_FIELDS and (ftokens & {"code", "codes", "promo", "coupon", "voucher"}) and jld.get("code"):
        return str(jld["code"])
    return ""


def _extract_single_custom_field(item: ScrapeItem, field_name: str, combined: str) -> str:
    """Extract a requested field cleanly, prioritizing JSON-LD and Key: Value before regexes."""
    fname = field_name.lower()
    ftokens = set(fname.split("_"))

    if jval := _jsonld_field_lookup(item, ftokens, fname):
        return jval

    # 1. Explicit Key: Value match first (e.g. "Company: Acme Corp", "Min spend: AED 500").
    label_words = re.escape(fname).replace("_", r"[\s_-]+")
    kv_match = re.search(rf"(?i)\b{label_words}\s*[:=]\s*([^\n.;|]{{2,65}})", combined)
    if kv_match:
        return " ".join(kv_match.group(1).split()).strip()

    # 2. Word-token semantic detectors (avoiding 'company'->'comp' or 'office'->'off').
    if fname not in _EXCLUDED_CODE_FIELDS and (ftokens & {"code", "codes", "promo", "coupon", "coupons", "voucher"}):
        return ", ".join((_extract_codes_fields(combined).get("codes") or [])[:3])
    if ftokens & {"discount", "discounts", "saving", "savings", "off"}:
        return ", ".join((_extract_codes_fields(combined).get("discounts") or [])[:2])
    if ftokens & {"bank", "banks", "card", "cards", "tier"}:
        return ", ".join((_extract_codes_fields(combined).get("bank_card") or [])[:4])
    if ftokens & {"salary", "comp", "compensation", "pay"}:
        return ", ".join((_extract_jobs_fields(combined).get("salary") or [])[:2])
    if ftokens & {"email", "emails", "contact"}:
        return ", ".join(list(dict.fromkeys(_EMAIL_RE.findall(combined)))[:2])
    if ftokens & {"price", "cost", "fee", "budget", "spend", "min"} and "price" in ftokens or ftokens & {"cost", "fee", "budget", "spend"}:
        prices = [" ".join(m.split()) for m in _PRICE_RE.findall(combined)]
        return ", ".join(list(dict.fromkeys(prices))[:2])
    if ftokens & {"date", "deadline", "expiry", "expires", "valid"} or fname.startswith("expir"):
        dates = [" ".join(m.split()) for m in _DATE_PHRASE_RE.findall(combined)]
        return ", ".join(list(dict.fromkeys(dates))[:2])
    if ftokens & {"experience", "exp", "years", "seniority", "level"}:
        job_sig = _extract_jobs_fields(combined)
        vals = list(job_sig.get("experience") or []) or list(job_sig.get("seniority") or [])
        return ", ".join(vals[:3])
    if ftokens & {"remote", "work", "location"}:
        wt = _extract_jobs_fields(combined).get("work_type")
        locs = list(dict.fromkeys(" ".join(m.split()) for m in _LOCATION_HINT_RE.findall(combined)))[:2]
        if wt and locs:
            return f"{wt} ({', '.join(locs)})"
        if locs:
            return ", ".join(locs)
        return str(wt) if wt else ""
    if fname in ("author", "handle", "user"):
        return item.author or ""
    if fname == "source":
        return item.source

    # 3. Generic sentence fallback matching meaningful field tokens.
    sig_tokens = [t for t in fname.split("_") if len(t) > 2]
    if sig_tokens:
        for sent in re.split(r"(?<=[.!?])\s+|\n+", combined):
            s_low = sent.lower()
            if all(re.search(rf"\b{re.escape(t)}", s_low) for t in sig_tokens):
                return " ".join(sent.split())[:75]
    return ""


def apply_custom_fields(items: Iterable[ScrapeItem], fields: list[str] | str | None) -> list[str]:
    """Extract custom schema fields on each item under `item.metadata['extracted_fields']`."""
    norm = normalize_fields(fields)
    if not norm:
        return []
    for it in items:
        combined = f"{it.title}\n{it.body}"
        extracted: dict[str, str] = {}
        for fname in norm:
            val = _extract_single_custom_field(it, fname, combined)
            if val:
                extracted[fname] = val
        if extracted:
            it.metadata["extracted_fields"] = extracted
    return norm


def apply_extractors(items: Iterable[ScrapeItem], extract_mode: str) -> str:
    """Annotate items in-place with structured fields under `item.metadata['extracted']`."""
    mode = (extract_mode or "").strip().lower()
    if not mode:
        return ""
    for it in items:
        combined = f"{it.title}\n{it.body}"
        jld = it.metadata.get("jsonld") if isinstance(it.metadata, dict) else None
        if mode == "codes":
            extracted = _extract_codes_fields(combined)
            if isinstance(jld, dict) and jld.get("code"):
                codes = list(extracted.get("codes") or [])
                if jld["code"] not in codes:
                    extracted["codes"] = [str(jld["code"]), *codes][:5]
        elif mode == "jobs":
            extracted = _extract_jobs_fields(combined)
            if isinstance(jld, dict):
                if not extracted.get("salary") and jld.get("salary"):
                    extracted["salary"] = [str(jld["salary"])]
                if not extracted.get("work_type") and jld.get("work_type"):
                    extracted["work_type"] = str(jld["work_type"])
        else:
            extracted = {}
        if extracted:
            it.metadata["extracted"] = extracted
    return mode


def format_extracted_text(item: ScrapeItem) -> str:
    """Format built-in and custom extracted fields cleanly for Markdown, CLI, and Excel."""
    if not isinstance(item.metadata, dict):
        return ""
    ext = item.metadata.get("extracted")
    ext_fields = item.metadata.get("extracted_fields")
    parts: list[str] = []
    if isinstance(ext, dict) and ext:
        if ext.get("codes"):
            parts.append("Codes: " + ", ".join(ext["codes"]))
        if ext.get("discounts"):
            parts.append("Discount: " + ", ".join(ext["discounts"]))
        if ext.get("bank_card"):
            parts.append("Card/Tier: " + ", ".join(ext["bank_card"]))
        if ext.get("salary"):
            parts.append("Comp: " + ", ".join(ext["salary"]))
        if ext.get("work_type"):
            parts.append("Type: " + str(ext["work_type"]))
        if ext.get("seniority"):
            parts.append("Level: " + ", ".join(ext["seniority"]))
        if ext.get("experience"):
            parts.append("Exp: " + ", ".join(ext["experience"]))
    if isinstance(ext_fields, dict) and ext_fields:
        for k, v in ext_fields.items():
            if v:
                parts.append(f"{k}: {v}")
    return " | ".join(parts)
