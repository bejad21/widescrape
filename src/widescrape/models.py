"""Core data models and Markdown link sanitization helpers for WideScrape."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib


def format_md_link(title: str, url: str) -> str:
    """Render a Markdown link safely, escaping brackets and rejecting non-HTTP URLs."""
    clean_title = " ".join((title or url or "Link").split()).replace("[", "\\[").replace("]", "\\]")
    clean_url = (url or "").replace("\xa0", "").strip()
    if clean_url.startswith(("http://", "https://")):
        return f"[{clean_title}]({clean_url})"
    return clean_title


@dataclass
class ScrapeItem:
    """Normalized item scraped from any source."""

    source: str          # e.g., "github", "reddit", "linkedin", "x", "hackernews", "web"
    url: str
    title: str
    body: str
    author: str = ""
    published_at: str = ""
    engagement: int = 0  # stars, upvotes, likes, comments, etc.
    query_used: str = ""
    metadata: dict = field(default_factory=dict)
    score: float = 0.0
    item_id: str = ""

    def __post_init__(self) -> None:
        self.url = (self.url or "").replace("\xa0", "").strip()
        if not self.item_id:
            digest = hashlib.sha1(self.url.encode("utf-8", errors="ignore")).hexdigest()[:12]
            prefix = self.source[:2].lower()
            self.item_id = f"{prefix}_{digest}"

    def compact_summary(self, max_gist_chars: int = 160) -> str:
        """Return a token-efficient 1-line summary for agent context."""
        highlight = self.metadata.get("highlight") if isinstance(self.metadata, dict) else None
        gist_source = " ".join(str(highlight or self.body or self.title).split())
        if len(gist_source) > max_gist_chars:
            gist = gist_source[: max_gist_chars - 3].rstrip() + "..."
        else:
            gist = gist_source
        title_clean = " ".join(self.title.split())[:90]
        ext = self.metadata.get("extracted") if isinstance(self.metadata, dict) else None
        ext_fields = self.metadata.get("extracted_fields") if isinstance(self.metadata, dict) else None
        bits: list[str] = []
        if isinstance(ext, dict) and ext:
            if ext.get("codes"):
                bits.append("codes=" + ",".join(ext["codes"][:3]))
            if ext.get("discounts"):
                bits.append("off=" + ",".join(ext["discounts"][:2]))
            if ext.get("salary"):
                bits.append("comp=" + ",".join(ext["salary"][:1]))
        if isinstance(ext_fields, dict) and ext_fields:
            for k, v in list(ext_fields.items())[:3]:
                if v:
                    bits.append(f"{k}={v}")
        ext_tag = (" | " + " ".join(bits)) if bits else ""
        eng_tag = f" | eng={self.engagement}" if self.engagement > 0 else ""
        date_tag = f" | {self.published_at[:10]}" if self.published_at else ""
        return (
            f"[{self.item_id}] score={self.score:.2f} | src={self.source}"
            f"{eng_tag}{date_tag}{ext_tag} | "
            f"{format_md_link(title_clean, self.url)} — {gist}"
        )
