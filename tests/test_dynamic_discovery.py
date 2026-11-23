"""Tests for domain-agnostic synonyms, open-web discovery, generic extractors, and self-learning modes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from widescrape.adapters.base import BaseAdapter
from widescrape.adapters.linkedin_jobs import build_linkedin_guest_searches
from widescrape.adapters.remote_boards import (
    _extract_topic_tokens,
    _matches_topic,
    parse_remotive_jobs,
)
from widescrape.cli import app as cli_app
from widescrape.discovery import (
    build_discovery_queries,
    discover_sites_and_items,
    extract_candidate_domains_from_html,
)
from widescrape.expander import expand_query, parse_synonyms_input
from widescrape.fetcher import (
    extract_feed_and_ats_hints,
    extract_hydration_items_from_html,
    extract_items_from_json_payload,
    extract_jsonld_items,
    extract_structured_items,
)
from widescrape.filter_rank import filter_and_rank
from widescrape.mcp_server import widescrape_modes, widescrape_search
from widescrape.models import ScrapeItem
from widescrape.modes import (
    clear_learned_topics,
    get_mode,
    list_learned_topics,
    promote_learned_to_mode,
    recall_learned_context,
    record_learned_run,
)
from widescrape.orchestrator import run_scrape


@pytest.fixture(autouse=True)
def _isolated_modes(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WIDESCRAPE_MODES_PATH", str(tmp_path / "modes.json"))
    monkeypatch.setenv("WIDESCRAPE_LEARNED_PATH", str(tmp_path / "learned_sites.json"))


def test_ambitious_synonyms_and_exclude_across_modes() -> None:
    groups, flat = parse_synonyms_input('{"nurse": ["registered nurse", "telehealth rn", "clinical reviewer"]}')
    assert "registered nurse" in groups["nurse"]
    assert "telehealth rn" in flat

    # Non-tech job search works with runtime synonyms cleanly without _TECH_SYNONYMS
    job_plan = expand_query(
        "remote nurse",
        mode="jobs",
        synonyms='{"nurse": ["registered nurse", "clinical reviewer", "triage coordinator"]}',
        exclude="US only, unpaid",
        extra_queries="virtual care nurse, telehealth clinical specialist",
    )
    assert "virtual care nurse" in job_plan.api_queries
    assert "us only" in {e.lower() for e in job_plan.exclude_terms}
    assert any("OR" in d and "registered nurse" in d for _, d in job_plan.dork_queries)

    li_variants = build_linkedin_guest_searches(job_plan)
    assert any("virtual care nurse" in str(v.get("keywords", "")).lower() for v in li_variants)

    remotive_payload = {
        "jobs": [
            {
                "url": "https://clinic.example.org/jobs/1",
                "title": "Clinical Reviewer (EMEA Remote)",
                "company_name": "MedOps",
                "description": "Review patient intake remotely. Full time clinical coordinator role.",
                "candidate_required_location": "EMEA",
            },
            {
                "url": "https://clinic.example.org/jobs/2",
                "title": "Registered Nurse — US Only",
                "company_name": "StatesHealth",
                "description": "Must reside in US only.",
                "candidate_required_location": "US only",
            },
        ]
    }
    toks = _extract_topic_tokens(job_plan)
    parsed = parse_remotive_jobs(
        remotive_payload,
        toks,
        "",
        job_plan.raw_query,
        synonym_groups=job_plan.synonym_map(),
        flat_synonyms=job_plan.synonyms,
    )
    assert len(parsed) == 2

    kept, stats = filter_and_rank(parsed, job_plan)
    assert len(kept) == 1
    assert kept[0].url == "https://clinic.example.org/jobs/1"
    assert stats["dropped_excluded"] == 1

    # Non-job general/research domain expansion also utilizes synonyms & exclusions
    res_plan = expand_query(
        "battery recycling breakthroughs",
        mode="research",
        synonyms=["hydrometallurgy", "direct lithium extraction", "black mass recovery"],
        exclude="sponsored press release",
        extra_queries="LFP cathode recycling startup benchmarks",
    )
    assert "hydrometallurgy" in res_plan.synonyms
    assert any("hydrometallurgy" in d for _, d in res_plan.dork_queries)
    assert "LFP cathode recycling startup benchmarks" in res_plan.api_queries


def test_generic_jsonld_hydration_and_xhr_extractors() -> None:
    product_html = """<html><head>
    <link rel="alternate" type="application/rss+xml" href="/feed.xml">
    <script type="application/ld+json">
    {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [
      {"@type": "ListItem", "item": {
        "@type": "Product", "name": "Espresso Grinder Pro", "url": "/products/grinder-pro",
        "description": "64mm flat burr grinder with stepless adjustment.",
        "brand": {"@type": "Brand", "name": "RoastLab"},
        "offers": {"@type": "Offer", "price": "399", "priceCurrency": "USD"}
      }},
      {"@type": "ListItem", "item": {
        "@type": "JobPosting", "title": "Telehealth RN", "url": "https://care.example.com/rn",
        "description": "Remote patient triage and clinical coordination.",
        "hiringOrganization": {"@type": "Organization", "name": "CareFirst"},
        "jobLocationType": "TELECOMMUTE", "datePosted": "2026-11-20"
      }}
    ]}
    </script></head><body>
    <a href="https://boards.greenhouse.io/carefirst/jobs/123">Apply</a>
    </body></html>"""

    jld_items = extract_jsonld_items("https://shop.example.com/deals", product_html)
    assert len(jld_items) == 2
    assert jld_items[0].url == "https://shop.example.com/products/grinder-pro"
    assert jld_items[0].metadata["jsonld"]["price"] == "USD 399"
    assert jld_items[1].metadata["jsonld"]["work_type"] == "remote"

    hints = extract_feed_and_ats_hints("https://shop.example.com/deals", product_html)
    assert "https://shop.example.com/feed.xml" in hints
    assert "https://boards-api.greenhouse.io/v1/boards/carefirst/jobs?content=true" in hints

    next_html = """<html><head><script id="__NEXT_DATA__" type="application/json">
    {"props": {"pageProps": {
      "nav": [{"title": "About", "url": "/about"}, {"title": "Contact", "url": "/contact"}],
      "listings": [
        {"title": "Clinical Research Associate", "url": "/roles/cra-1", "company": "BioTrial",
         "description": "Coordinate multi-center observational trial protocols remotely.", "remote": true, "publishedAt": "2026-11-21"},
        {"title": "Regulatory Medical Writer", "url": "/roles/mw-2", "company": "MedDocs",
         "description": "Author clinical evaluation reports and regulatory submissions.", "remote": true, "publishedAt": "2026-11-22"}
      ]
    }}}
    </script></head><body></body></html>"""
    hydrated = extract_structured_items("https://medboard.example.org/", next_html)
    assert len(hydrated) == 2
    assert all("/about" not in it.url and "/contact" not in it.url for it in hydrated)
    assert hydrated[0].url == "https://medboard.example.org/roles/cra-1"
    assert hydrated[0].metadata["jsonld"]["work_type"] == "remote"

    xhr_payload = {
        "hits": [
            {"headline": "Open-Access Legal Citation Dataset 2026", "href": "https://lexdata.example.org/ds/2026",
             "summary": "Structured corpus of 1.2M appellate decisions and case summaries."},
            {"headline": "EU Regulatory Compliance Corpus", "href": "https://lexdata.example.org/ds/eu",
             "summary": "Annotated directives and national transposition records across member states."},
        ]
    }
    xhr_items = extract_items_from_json_payload("https://lexdata.example.org", xhr_payload)
    assert len(xhr_items) == 2


def test_open_web_discovery_and_directory_mining() -> None:
    roundup_html = """<html><body>
    <h1>Best Remote Healthcare & Nursing Job Boards</h1>
    <p>Find vetted telehealth nursing and clinical coordinator roles on
       <a href="https://nurseconnect.example.org/jobs">NurseConnect Board</a> and
       <a href="https://telehealthcareers.example.com/remote">TelehealthCareers</a>.</p>
    <p>Ignore navigation: <a href="https://other.example.com/privacy">Privacy Policy</a></p>
    </body></html>"""

    candidates = extract_candidate_domains_from_html(
        "https://guide.example.net/best-nursing-boards",
        roundup_html,
        anchor_terms=("nurse", "telehealth", "clinical"),
    )
    hosts = [h for h, _, _ in candidates]
    assert "nurseconnect.example.org" in hosts
    assert "telehealthcareers.example.com" in hosts
    assert "other.example.com" not in hosts

    plan = expand_query(
        "remote nurse triage",
        mode="jobs",
        synonyms=["telehealth nurse", "clinical coordinator"],
    )
    disc_qs = build_discovery_queries(plan)
    assert any("best job boards" in q for q in disc_qs)

    def fake_search(q: str):
        if q.startswith("site:nurseconnect.example.org"):
            return [
                {
                    "url": "https://nurseconnect.example.org/jobs/triage-rn",
                    "title": "Remote Triage RN — CareLine",
                    "body": "Telehealth triage nurse supporting remote clinics worldwide.",
                    "published_at": "2026-11-22",
                }
            ]
        return [
            {
                "url": "https://guide.example.net/best-nursing-boards",
                "title": "Top 10 Best Websites & Job Boards for Telehealth Nurses",
                "body": "Curated directory of nursing portals and clinical coordinator boards.",
                "published_at": "2026-11-20",
            }
        ]

    def fake_fetch(url: str):
        if "guide.example.net" in url:
            return ("Best Nursing Boards", "Top nursing job boards overview.", roundup_html)
        if "nurseconnect.example.org" in url:
            portal_html = """<html><head><script type="application/ld+json">
            {"@type": "JobPosting", "title": "Virtual Care Nurse", "url": "https://nurseconnect.example.org/jobs/vc-2",
             "description": "Remote nurse care coordinator.", "jobLocationType": "TELECOMMUTE", "datePosted": "2026-11-21"}
            </script></head><body><div>Listings</div></body></html>"""
            return ("NurseConnect", "NurseConnect listings.", portal_html)
        return None

    res = asyncio.run(
        discover_sites_and_items(plan, search_fn=fake_search, fetch_fn=fake_fetch, max_sites=5, max_items=20)
    )
    assert "nurseconnect.example.org" in res.discovered_sites
    urls = {it.url for it in res.items}
    assert "https://nurseconnect.example.org/jobs/triage-rn" in urls
    assert "https://nurseconnect.example.org/jobs/vc-2" in urls


def test_self_learning_recall_isolation_and_mode_promotion(monkeypatch, tmp_path: Path) -> None:
    kept = [
        ScrapeItem(
            source="discovered",
            url="https://nurseconnect.example.org/jobs/1",
            title="Telehealth Nurse",
            body="Remote triage nurse position.",
            score=2.4,
        ),
        ScrapeItem(
            source="web",
            url="https://telehealthcareers.example.com/role/2",
            title="Clinical Coordinator",
            body="Work from anywhere RN clinical coordinator.",
            score=1.9,
        ),
    ]
    newly_learned = record_learned_run(
        tokens=("nurse", "telehealth", "triage"),
        base_mode="jobs",
        mode_name="jobs",
        kept_items=kept,
        discovered_sites=["nurseconnect.example.org"],
        used_synonyms=["clinical coordinator", "virtual rn"],
    )
    assert "nurseconnect.example.org" in newly_learned

    recalled_sites, recalled_syns = recall_learned_context(("nurse", "telehealth", "remote"), base_mode="jobs")
    assert "nurseconnect.example.org" in recalled_sites
    assert "clinical coordinator" in recalled_syns

    plan_next = expand_query("telehealth nurse coordinator", mode="jobs")
    assert "nurseconnect.example.org" in plan_next.learned_sites
    assert any("site:nurseconnect.example.org" in d for _, d in plan_next.dork_queries)

    # Unrelated topics must stay isolated from healthcare-learned sites.
    pizza_sites, _ = recall_learned_context(("pizza", "oven", "outdoor"), base_mode="general")
    assert pizza_sites == []

    topics = list_learned_topics()
    assert len(topics) == 1
    out_learned = widescrape_modes(action="learned")
    assert "nurseconnect.example.org" in out_learned

    out_promote = widescrape_modes(action="promote", name="jobs:nurse", target_mode="healthcare-remote")
    assert "healthcare-remote" in out_promote
    cfg = get_mode("healthcare-remote")
    assert cfg is not None and "nurseconnect.example.org" in cfg.sites

    runner = CliRunner()
    res_cli = runner.invoke(cli_app, ["mode", "learned"])
    assert res_cli.exit_code == 0 and "nurseconnect.example.org" in res_cli.stdout
    assert clear_learned_topics("nurse") == 1
    assert list_learned_topics() == []


def test_orchestrator_wave2_discovery_backfill_and_learning(monkeypatch, tmp_path: Path) -> None:
    class _ThinWebAdapter(BaseAdapter):
        name = "web_dork"

        async def fetch(self, plan, limit: int = 100):
            return [
                ScrapeItem(
                    source="web",
                    url="https://initial.example.org/grant/1",
                    title="Clean Energy Community Grant 2026",
                    body="Funding opportunity for renewable microgrid research teams.",
                    published_at="2026-11-20",
                )
            ]

    async def fake_discover(plan, **kwargs):
        from widescrape.discovery import DiscoveryResult

        return DiscoveryResult(
            discovered_sites=["energygrants.example.org", "climateresearch.example.net"],
            discovered_urls=["https://energygrants.example.org/calls"],
            items=[
                ScrapeItem(
                    source="discovered",
                    url="https://energygrants.example.org/calls/microgrid-2026",
                    title="Global Microgrid Innovation Fund",
                    body="Supports non-profit and academic solar microgrid pilots worldwide.",
                    published_at="2026-11-21",
                ),
                ScrapeItem(
                    source="discovered",
                    url="https://climateresearch.example.net/fellowships/2",
                    title="Excluded US Only Energy Grant",
                    body="Restricted to US only institutions.",
                    published_at="2026-11-21",
                ),
            ],
        )

    monkeypatch.setattr("widescrape.orchestrator.build_adapters", lambda **kw: [_ThinWebAdapter()])
    monkeypatch.setattr("widescrape.orchestrator.discover_sites_and_items", fake_discover)
    monkeypatch.setattr("widescrape.orchestrator.ScrapeStorage", lambda: __import__("widescrape.storage", fromlist=["ScrapeStorage"]).ScrapeStorage(db_path=tmp_path / "run.db"))

    out_text = asyncio.run(
        widescrape_search(
            "renewable microgrid grants",
            mode="research",
            limit=5,
            synonyms="solar microgrid funding, clean energy fellowship",
            exclude="US only",
            learn_mode="climate-grants",
            max_show=5,
        )
    )
    assert "Discovered sites: energygrants.example.org" in out_text
    assert "Newly learned sites saved:" in out_text
    assert "Excluded US Only Energy Grant" not in out_text

    saved_mode = get_mode("climate-grants")
    assert saved_mode is not None
    assert "energygrants.example.org" in saved_mode.sites
    assert "solar microgrid funding" in saved_mode.synonyms
