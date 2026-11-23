"""Self-registration (`widescrape install`) and diagnostics (`widescrape doctor`) for WideScrape."""

from __future__ import annotations

from contextlib import ExitStack
from importlib import resources
import json
import os
from pathlib import Path
import shutil
import sys
from urllib.parse import urlparse

import httpx

from widescrape.adapters.browser_social import has_saved_session
from widescrape.storage import DEFAULT_DB_PATH

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_SKILL = PROJECT_ROOT / "SKILL.md"
SOURCE_REFS = PROJECT_ROOT / "references"


def _mask_proxy_url(raw: str | None) -> str:
    """Mask embedded credentials in proxy URLs before printing diagnostics."""
    if not raw:
        return "direct (no WIDESCRAPE_PROXY set)"
    parsed = urlparse(raw)
    if parsed.username or parsed.password:
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://***:***@{parsed.hostname}{port}"
    return raw


def _resolve_skill_sources(stack: ExitStack) -> tuple[Path, Path | None]:
    """Locate SKILL.md and references/ from source checkout or packaged wheel assets."""
    if SOURCE_SKILL.is_file():
        return SOURCE_SKILL, (SOURCE_REFS if SOURCE_REFS.is_dir() else None)
    try:
        pkg_skill = resources.files("widescrape").joinpath("skill_data", "SKILL.md")
        if pkg_skill.is_file():
            skill_path = stack.enter_context(resources.as_file(pkg_skill))
            pkg_refs = resources.files("widescrape").joinpath("skill_data", "references")
            refs_path = stack.enter_context(resources.as_file(pkg_refs)) if pkg_refs.is_dir() else None
            return skill_path, refs_path
    except Exception:
        pass
    raise FileNotFoundError(f"Missing SKILL.md at {SOURCE_SKILL} and package resources.")


def _build_mcp_entry() -> dict:
    """Return a working MCP launch entry for either repo checkout or packaged installs."""
    if (PROJECT_ROOT / "pyproject.toml").is_file() and SOURCE_SKILL.is_file():
        # Pass --no-sync so Windows never tries to overwrite a locked widescrape.exe.
        return {
            "command": "uv",
            "args": ["run", "--no-sync", "--project", str(PROJECT_ROOT).replace("\\", "/"), "widescrape", "mcp"],
        }
    return {
        "command": sys.executable,
        "args": ["-m", "widescrape.cli", "mcp"],
    }


def install_agent_skills(with_mcp: bool = False, home_dir: Path | None = None) -> list[str]:
    """Install SKILL.md, Tier-3 references, and optional MCP configs for AI agents."""
    home = home_dir or Path.home()
    candidates = [
        ("Antigravity", home / ".gemini" / "config" / "skills" / "widescrape" / "SKILL.md", home / ".gemini" / "config"),
        ("Claude Code", home / ".claude" / "skills" / "widescrape" / "SKILL.md", home / ".claude"),
        ("OpenAI Codex", home / ".codex" / "skills" / "widescrape" / "SKILL.md", home / ".codex"),
        ("Shared Agents", home / ".agents" / "skills" / "widescrape" / "SKILL.md", home / ".agents"),
        ("Cursor", home / ".cursor" / "skills" / "widescrape" / "SKILL.md", home / ".cursor"),
    ]

    actions: list[str] = []
    with ExitStack() as stack:
        skill_src, refs_src = _resolve_skill_sources(stack)
        for label, target_file, root_dir in candidates:
            if root_dir.exists() or label == "Antigravity":
                target_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(skill_src, target_file)
                if refs_src is not None and refs_src.is_dir():
                    shutil.copytree(refs_src, target_file.parent / "references", dirs_exist_ok=True)
                actions.append(f"Installed skill for {label}: {target_file}")

    if with_mcp:
        mcp_entry = _build_mcp_entry()
        mcp_targets = [
            ("Antigravity MCP", home / ".gemini" / "antigravity" / "mcp_config.json", True),
            ("Claude MCP", home / ".claude.json", False),
            ("Cursor MCP", home / ".cursor" / "mcp.json", False),
        ]
        for label, cfg_path, allow_if_missing_parent in mcp_targets:
            if not (cfg_path.parent.exists() or (allow_if_missing_parent and home_dir is None)):
                continue
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            data: dict = {}
            if cfg_path.is_file():
                raw_txt = cfg_path.read_text(encoding="utf-8")
                if raw_txt.strip():
                    try:
                        parsed = json.loads(raw_txt)
                    except ValueError as exc:
                        actions.append(f"Skipped {label} ({cfg_path}): invalid JSON ({exc}) — existing file left untouched")
                        continue
                    if not isinstance(parsed, dict):
                        actions.append(f"Skipped {label} ({cfg_path}): root JSON value is not an object")
                        continue
                    data = parsed
                shutil.copy2(cfg_path, cfg_path.with_name(cfg_path.name + ".bak"))

            servers = data.get("mcpServers")
            if servers is None:
                servers = {}
                data["mcpServers"] = servers
            elif not isinstance(servers, dict):
                actions.append(f"Skipped {label} ({cfg_path}): 'mcpServers' is not a JSON object")
                continue

            servers["widescrape"] = mcp_entry
            cfg_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            actions.append(f"Registered MCP server in {label}: {cfg_path}")

    return actions


def collect_diagnostics(check_network: bool = True) -> list[str]:
    """Check runtime dependencies, search backends, Playwright, and saved sessions."""
    report: list[str] = [
        f"python: {sys.version.split()[0]}",
        f"project_root: {PROJECT_ROOT}",
        f"sqlite_cache: {DEFAULT_DB_PATH} ({'exists' if DEFAULT_DB_PATH.exists() else 'will initialize on first search'})",
    ]

    try:
        import primp
        _ = primp.Client(impersonate="random")
        report.append("tls_stealth (primp): OK")
    except Exception as exc:
        report.append(f"tls_stealth (primp): ERROR ({exc})")

    try:
        import pypdf  # noqa: F401
        report.append("pdf_extractor (pypdf): OK")
    except Exception as exc:
        report.append(f"pdf_extractor (pypdf): ERROR ({exc})")

    try:
        import openpyxl  # noqa: F401
        report.append("excel_exporter (openpyxl): OK")
    except Exception as exc:
        report.append(f"excel_exporter (openpyxl): ERROR ({exc})")

    report.append(f"proxy: {_mask_proxy_url(os.environ.get('WIDESCRAPE_PROXY'))}")

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            exe = p.chromium.executable_path
            exists = Path(exe).exists()
        report.append(f"playwright_chromium: {'OK (' + exe + ')' if exists else 'MISSING (run: uv run playwright install chromium)'}")
    except Exception as exc:
        report.append(f"playwright_chromium: NOT READY ({exc})")

    searx = os.environ.get("WIDESCRAPE_SEARXNG_URL")
    if searx:
        try:
            r = httpx.get(f"{searx.rstrip('/')}/search", params={"q": "test", "format": "json"}, timeout=5.0)
            report.append(f"searxng ({searx}): HTTP {r.status_code}")
        except Exception as exc:
            report.append(f"searxng ({searx}): UNREACHABLE ({exc})")
    else:
        report.append("searxng: not configured (using multi-backend ddgs)")

    if check_network:
        try:
            from ddgs import DDGS
            hits = DDGS().text("openai", max_results=3, backend="bing")
            report.append(f"search_live_check (bing via ddgs): OK ({len(hits or [])} hits)")
        except Exception as exc:
            report.append(f"search_live_check (bing via ddgs): ERROR ({exc})")

    for plat in ("x", "linkedin"):
        report.append(f"session_{plat}: {'saved cookies' if has_saved_session(plat) else 'zero-login mode'}")

    return report
