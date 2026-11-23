"""Deterministic benchmark measuring context compression & noise filtering."""

from __future__ import annotations

import time
from widescrape.expander import expand_query
from widescrape.filter_rank import filter_and_rank
from widescrape.models import ScrapeItem


def build_synthetic_corpus() -> list[ScrapeItem]:
    items: list[ScrapeItem] = []
    # 50 rich technical/hiring posts (~1,400 chars each)
    for i in range(50):
        body = (
            f"We are hiring an AI Engineer (#{i}) to build production RAG, BM25 hybrid retrieval, "
            "and MCP tool servers. Remote friendly. DM me or apply via repo. " + ("Architecture notes and benchmarks. " * 35)
        )
        items.append(
            ScrapeItem(
                source=["linkedin", "x", "github", "reddit", "hackernews"][i % 5],
                url=f"https://example.com/post/{i}",
                title=f"AI Engineer Workflow & Hiring Thread #{i}",
                body=body,
                engagement=(i * 7) % 150,
            )
        )
    # 30 duplicate URLs with tracking params
    for i in range(30):
        items.append(
            ScrapeItem(
                source="web",
                url=f"https://example.com/post/{i}?utm_source=social",
                title=f"AI Engineer Workflow & Hiring Thread #{i}",
                body="Duplicate copy with tracking query.",
            )
        )
    # 40 noise/bot/removed stubs
    for i in range(40):
        if i % 3 == 0:
            body = "[removed]"
            title = "Removed post"
        elif i % 3 == 1:
            body = "Join LinkedIn to see this full post and connect."
            title = "LinkedIn"
        else:
            body = "Make $500 daily! Click link in bio for crypto airdrop."
            title = "Promo bot"
        items.append(
            ScrapeItem(
                source="x",
                url=f"https://example.com/noise/{i}",
                title=title,
                body=body,
            )
        )
    return items


def main() -> None:
    corpus = build_synthetic_corpus()
    plan = expand_query("hiring AI engineer", mode="jobs")

    t0 = time.perf_counter()
    ranked, stats = filter_and_rank(corpus, plan)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    raw_est_tokens = stats["raw_chars"] // 4
    summary_est_tokens = stats["summary_chars"] // 4
    top15_chars = sum(len(it.compact_summary()) for it in ranked[:15])
    top15_est_tokens = top15_chars // 4

    print(f"Raw items:            {stats['raw']}")
    print(f"Deduplicated:         -{stats['deduped']}")
    print(f"Noise/bots dropped:   -{stats['dropped_noise']}")
    print(f"Kept ranked items:    {stats['kept']}")
    print(f"Raw payload size:     {stats['raw_chars']:,} chars (~{raw_est_tokens:,} tokens)")
    print(f"Full summary index:   {stats['summary_chars']:,} chars (~{summary_est_tokens:,} tokens)")
    print(f"Top-15 index slice:   {top15_chars:,} chars (~{top15_est_tokens:,} tokens)")
    print(f"Context saved (full): {stats['compression_pct']}%")
    print(f"Pipeline latency:     {elapsed_ms:.2f} ms")


if __name__ == "__main__":
    main()
