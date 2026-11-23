# AI Agent & MCP Setup Guide

Step-by-step instructions to connect **WideScrape** (`SKILL.md` + 7-tool MCP server) to Google Antigravity, Claude Code, OpenAI Codex, Cursor, VS Code (GitHub Copilot), and Windsurf.

---

## 1. Prerequisites (Run Once)

```bash
git clone https://github.com/bejad21/widescrape.git && cd widescrape
uv sync
uv run widescrape doctor
```

> **Automatic Installer (Antigravity, Claude Code, Cursor, Codex Skills):**
> ```bash
> uv run widescrape install --with-mcp
> ```
> This installs `SKILL.md` + `references/` for **Antigravity, Claude Code, OpenAI Codex, Cursor, and `~/.agents/skills/`**, and automatically registers `"mcpServers.widescrape"` (with a `.bak` backup) in **Antigravity**, **Claude Code**, and **Cursor**.  
> For **OpenAI Codex MCP** (`config.toml`), **VS Code**, or **Windsurf**, follow the per-agent steps below.

---

## 2. Per-Agent Configuration

Replace `/path/to/widescrape` with your local cloned folder path (on Windows, use forward slashes like `C:/Users/<you>/widescrape`).

> **Why `--no-sync`?** Passing `--no-sync` to `uv run` prevents Windows file-lock errors (`os error 32` on `widescrape.exe`) when multiple agent sessions run concurrently.

### Google Antigravity

* **Automatic:** `uv run widescrape install --with-mcp`
* **Skill Path:** `~/.gemini/config/skills/widescrape/`
* **MCP Config Path:** `~/.gemini/antigravity/mcp_config.json`

```json
{
  "mcpServers": {
    "widescrape": {
      "command": "uv",
      "args": [
        "run",
        "--no-sync",
        "--project",
        "/path/to/widescrape",
        "widescrape",
        "mcp"
      ]
    }
  }
}
```

Restart or reload MCP servers in Antigravity, then test with `widescrape_modes(action="list")`.

---

### Claude Code

* **Automatic:** `uv run widescrape install --with-mcp`
* **Skill Path:** `~/.claude/skills/widescrape/`
* **CLI Registration (Recommended):**
  ```bash
  claude mcp add --transport stdio widescrape -- uv run --no-sync --project /path/to/widescrape widescrape mcp
  ```
* **Manual JSON Config:** Global `~/.claude.json` or project-local `.mcp.json`:
  ```json
  {
    "mcpServers": {
      "widescrape": {
        "command": "uv",
        "args": [
          "run",
          "--no-sync",
          "--project",
          "/path/to/widescrape",
          "widescrape",
          "mcp"
        ]
      }
    }
  }
  ```
* **Verify:** Run `claude mcp list` (look for `✓ Connected`) or `/mcp` inside a Claude Code session.

---

### OpenAI Codex (CLI & IDE Extension)

* **Skill Path (installed by `widescrape install`):** `~/.codex/skills/widescrape/`
* **CLI Registration:**
  ```bash
  codex mcp add widescrape --command uv --args run --no-sync --project /path/to/widescrape widescrape mcp
  ```
* **Manual TOML Config:** Global `~/.codex/config.toml` or project-local `.codex/config.toml`:
  ```toml
  [mcp_servers.widescrape]
  command = "uv"
  args = ["run", "--no-sync", "--project", "/path/to/widescrape", "widescrape", "mcp"]
  tool_timeout_sec = 120
  ```
* **Verify:** Run `codex mcp list`, `codex mcp show widescrape`, or `/mcp` inside a session.

---

### Cursor

* **Automatic:** `uv run widescrape install --with-mcp`
* **Skill Path:** `~/.cursor/skills/widescrape/`
* **MCP Config Path:** Global `~/.cursor/mcp.json` or workspace `<repo>/.cursor/mcp.json`

```json
{
  "mcpServers": {
    "widescrape": {
      "command": "uv",
      "args": [
        "run",
        "--no-sync",
        "--project",
        "/path/to/widescrape",
        "widescrape",
        "mcp"
      ]
    }
  }
}
```

Reload Cursor (`Settings -> Tools & MCP`) and verify the 7 `widescrape_*` tools appear.

---

### VS Code (GitHub Copilot)

* **Skill Path:** `~/.agents/skills/widescrape/` (installed automatically by `widescrape install`)
* **MCP Config Path:** Global `~/.copilot/mcp-config.json` or workspace `.mcp.json`

```json
{
  "mcpServers": {
    "widescrape": {
      "command": "uv",
      "args": [
        "run",
        "--no-sync",
        "--project",
        "/path/to/widescrape",
        "widescrape",
        "mcp"
      ]
    }
  }
}
```

*(If using an older VS Code workspace `.vscode/mcp.json`, place the same entry under `"servers"` instead of `"mcpServers"`.)*

---

### Windsurf

* **Skill Path:** `~/.agents/skills/widescrape/`
* **MCP Config Path:** `~/.codeium/windsurf/mcp_config.json`

```json
{
  "mcpServers": {
    "widescrape": {
      "command": "uv",
      "args": [
        "run",
        "--no-sync",
        "--project",
        "/path/to/widescrape",
        "widescrape",
        "mcp"
      ]
    }
  }
}
```

---

## 3. CLI-Only / No-MCP Usage

If an agent only has shell access without MCP support, it still picks up `SKILL.md` (`~/.agents/skills/widescrape/`) and can call the CLI directly:

```bash
uv run --no-sync --project /path/to/widescrape widescrape search "your topic" --synonyms "alt1,alt2" --quiet --no-export
```

---

## 4. Quick Reference Table

| Agent | Skill Directory | MCP Config Location | Config Format |
| :--- | :--- | :--- | :--- |
| **Antigravity** | `~/.gemini/config/skills/widescrape/` | `~/.gemini/antigravity/mcp_config.json` | JSON (`mcpServers`) |
| **Claude Code** | `~/.claude/skills/widescrape/` | `~/.claude.json` or `.mcp.json` | JSON (`mcpServers`) / `claude mcp add` |
| **OpenAI Codex** | `~/.codex/skills/widescrape/` | `~/.codex/config.toml` or `.codex/config.toml` | TOML (`[mcp_servers.widescrape]`) / `codex mcp add` |
| **Cursor** | `~/.cursor/skills/widescrape/` | `~/.cursor/mcp.json` or `.cursor/mcp.json` | JSON (`mcpServers`) |
| **VS Code (Copilot)** | `~/.agents/skills/widescrape/` | `~/.copilot/mcp-config.json` or `.mcp.json` | JSON (`mcpServers`) |
| **Windsurf** | `~/.agents/skills/widescrape/` | `~/.codeium/windsurf/mcp_config.json` | JSON (`mcpServers`) |
