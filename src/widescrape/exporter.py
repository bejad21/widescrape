"""Human-friendly Markdown, Excel (.xlsx), and interactive HTML table exporters."""

from __future__ import annotations

import html
import json
from pathlib import Path
import re

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from widescrape.extractor import format_extracted_text
from widescrape.models import ScrapeItem, format_md_link

DEFAULT_EXPORT_DIR = Path.home() / ".widescrape" / "exports"

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_HEADER_FILL = PatternFill(start_color="1F3A5F", end_color="1F3A5F", fill_type="solid")
_HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)
_ZEBRA_FILL = PatternFill(start_color="F5F8FC", end_color="F5F8FC", fill_type="solid")
_LINK_FONT = Font(color="0563C1", underline="single")
_TOP_SCORE_FILL = PatternFill(start_color="E6F4EA", end_color="E6F4EA", fill_type="solid")


def _sanitize_excel_text(value: object) -> str:
    """Strip OpenPyXL illegal control chars and defuse Excel formula injection."""
    cleaned = ILLEGAL_CHARACTERS_RE.sub("", str(value or ""))
    if cleaned and cleaned[0] in _FORMULA_PREFIXES:
        return f"'{cleaned}"
    return cleaned


def _safe_http_url(url: str) -> str:
    cleaned = ILLEGAL_CHARACTERS_RE.sub("", (url or "").replace("\xa0", "").strip())
    return cleaned if cleaned.startswith(("http://", "https://")) else ""


def _slugify(text: str, max_len: int = 36) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (text or "results").lower()).strip("_")
    return (slug[:max_len] or "results").strip("_")


def _item_summary_text(item: ScrapeItem, max_len: int = 400) -> str:
    """Prefer query-matched highlight when present, falling back to body/title."""
    hl = item.metadata.get("highlight") if isinstance(item.metadata, dict) else None
    raw = str(hl or item.body or item.title)
    return " ".join(raw.split())[:max_len]


def _item_field_value(item: ScrapeItem, field_name: str) -> str:
    fields_map = item.metadata.get("extracted_fields") if isinstance(item.metadata, dict) else None
    if isinstance(fields_map, dict):
        return str(fields_map.get(field_name) or "")
    return ""


def _write_markdown_report(
    md_path: Path,
    run_id: str,
    query: str,
    mode: str,
    items: list[ScrapeItem],
    stats: dict[str, int],
    adapter_errors: dict[str, str] | None,
    days: int | None,
    extract_mode: str,
    extract_fields: tuple[str, ...],
) -> None:
    """Write ranked search results to a clean Markdown table document."""
    has_extracted = bool(extract_fields) or any(bool(format_extracted_text(it)) for it in items)
    meta_suffix = (f" · **Window:** last `{days}` days" if days else "") + (
        f" · **Extractor:** `{extract_mode}`" if extract_mode else ""
    ) + (f" · **Fields:** `{', '.join(extract_fields)}`" if extract_fields else "")
    lines = [
        f"# WideScrape Report: `{query}`",
        "",
        f"- **Run ID:** `{run_id}`",
        f"- **Mode:** `{mode}`{meta_suffix}",
        f"- **Scraped:** `{stats.get('raw', len(items))}` raw -> `{stats.get('kept', len(items))}` unique kept",
        f"- **Context Saved:** `{stats.get('compression_pct', 0)}%`",
    ]
    if stats.get("dropped_old"):
        lines.append(f"- **Older Than Window Filtered:** `{stats['dropped_old']}`")
    if stats.get("skipped_seen"):
        lines.append(f"- **Previously Seen Skipped (`--new-only`):** `{stats['skipped_seen']}`")
    if adapter_errors:
        notes = ", ".join(f"{k}: {v}" for k, v in adapter_errors.items())
        lines.append(f"- **Adapter Warnings:** {notes}")

    lines.extend(["", "## Ranked Results (Best First)", ""])
    if extract_fields:
        col_headers = ["#", "ID", "Score", "Source", "Date"] + list(extract_fields) + ["Title & Link", "Highlight"]
        aligns = ["---:", ":---", "---:", ":---", ":---"] + [":---"] * len(extract_fields) + [":---", ":---"]
        lines.append("| " + " | ".join(col_headers) + " |")
        lines.append("| " + " | ".join(aligns) + " |")
    elif has_extracted:
        lines.extend([
            "| # | ID | Score | Source | Engagement | Date | Extracted | Title & Link | Highlight |",
            "| ---: | :--- | ---: | :--- | ---: | :--- | :--- | :--- | :--- |",
        ])
    else:
        lines.extend([
            "| # | ID | Score | Source | Engagement | Date | Title & Link | Highlight |",
            "| ---: | :--- | ---: | :--- | ---: | :--- | :--- | :--- |",
        ])

    for idx, it in enumerate(items, start=1):
        title = " ".join(it.title.split()).replace("|", "\\|")[:90]
        md_link = format_md_link(title, it.url)
        gist = _item_summary_text(it, 150).replace("|", "\\|")
        date_str = it.published_at[:10] if it.published_at else "-"
        if extract_fields:
            dyn_vals = [(_item_field_value(it, f) or "-").replace("|", "\\|")[:60] for f in extract_fields]
            row = [str(idx), f"`{it.item_id}`", f"{it.score:.2f}", it.source, date_str]
            row.extend(dyn_vals)
            row.extend([md_link, gist])
            lines.append("| " + " | ".join(row) + " |")
        elif has_extracted:
            ext_txt = format_extracted_text(it).replace("|", "\\|")[:100]
            lines.append(
                f"| {idx} | `{it.item_id}` | {it.score:.2f} | {it.source} | {it.engagement} | "
                f"{date_str} | {ext_txt or '-'} | {md_link} | {gist} |"
            )
        else:
            lines.append(
                f"| {idx} | `{it.item_id}` | {it.score:.2f} | {it.source} | {it.engagement} | "
                f"{date_str} | {md_link} | {gist} |"
            )
    md_path.write_text("\n".join(lines), encoding="utf-8")


def _write_xlsx_workbook(
    xlsx_path: Path,
    run_id: str,
    query: str,
    mode: str,
    items: list[ScrapeItem],
    stats: dict[str, int],
    adapter_counts: dict[str, int] | None,
    adapter_errors: dict[str, str] | None,
    days: int | None,
    extract_mode: str,
    extract_fields: tuple[str, ...],
) -> None:
    """Write ranked results and custom schema columns to a styled Excel workbook."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Results"
    ws.views.sheetView[0].showGridLines = True
    ws.freeze_panes = "A2"

    dyn_cols = [f.replace("_", " ").title() for f in extract_fields]
    headers = (
        ["Rank", "ID", "Score", "Source", "Engagement", "Date", "Author", "Title (Click Link)", "URL", "Extracted"]
        + dyn_cols
        + ["Highlight / Summary"]
    )
    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 24

    summary_col_idx = len(headers)
    for idx, it in enumerate(items, start=1):
        row_num = idx + 1
        title_clean = _sanitize_excel_text(" ".join(it.title.split()))
        gist = _sanitize_excel_text(_item_summary_text(it, 400))
        safe_url = _safe_http_url(it.url)
        display_url = _sanitize_excel_text(safe_url or it.url)
        dyn_vals = [_sanitize_excel_text(_item_field_value(it, f)) for f in extract_fields]
        ws.append(
            [
                idx,
                _sanitize_excel_text(it.item_id),
                round(float(it.score), 2),
                _sanitize_excel_text(it.source),
                int(it.engagement),
                _sanitize_excel_text(it.published_at[:10]),
                _sanitize_excel_text(it.author or ""),
                title_clean,
                display_url,
                _sanitize_excel_text(format_extracted_text(it)),
            ]
            + dyn_vals
            + [gist]
        )

        title_cell = ws.cell(row=row_num, column=8)
        url_cell = ws.cell(row=row_num, column=9)
        if safe_url:
            title_cell.hyperlink = safe_url
            title_cell.font = _LINK_FONT
            url_cell.hyperlink = safe_url
            url_cell.font = _LINK_FONT

        score_cell = ws.cell(row=row_num, column=3)
        if idx <= 5:
            score_cell.fill = _TOP_SCORE_FILL

        if idx % 2 == 0:
            for c_idx in range(1, len(headers) + 1):
                if c_idx == 3 and idx <= 5:
                    continue
                ws.cell(row=row_num, column=c_idx).fill = _ZEBRA_FILL

        for c_idx in range(1, len(headers) + 1):
            wrap = c_idx in (8, 10, summary_col_idx) or (11 <= c_idx < summary_col_idx)
            ws.cell(row=row_num, column=c_idx).alignment = Alignment(wrap_text=wrap, vertical="top")

    if items:
        last_col = get_column_letter(len(headers))
        ws.auto_filter.ref = f"A1:{last_col}{len(items) + 1}"

    fixed_widths = {1: 7, 2: 13, 3: 8, 4: 15, 5: 12, 6: 12, 7: 18, 8: 44, 9: 38, 10: 28}
    for col_idx, width in fixed_widths.items():
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    for col_idx in range(11, summary_col_idx):
        ws.column_dimensions[get_column_letter(col_idx)].width = 22
    ws.column_dimensions[get_column_letter(summary_col_idx)].width = 60

    meta_ws = wb.create_sheet(title="Run Info")
    meta_rows = [
        ("Run ID", run_id),
        ("Query", query),
        ("Mode", mode),
        ("Days Window", str(days) if days else "all"),
        ("Extractor", extract_mode or "none"),
        ("Custom Fields", ", ".join(extract_fields) or "none"),
        ("Raw Items Scraped", stats.get("raw", len(items))),
        ("Duplicates Removed", stats.get("deduped", 0)),
        ("Noise Dropped", stats.get("dropped_noise", 0)),
        ("Older Filtered", stats.get("dropped_old", 0)),
        ("Previously Seen Skipped", stats.get("skipped_seen", 0)),
        ("Unique Kept", len(items)),
        ("Context Saved (%)", stats.get("compression_pct", 0)),
        ("Adapter Counts", ", ".join(f"{k}: {v}" for k, v in (adapter_counts or {}).items()) or "n/a"),
        ("Adapter Warnings", ", ".join(f"{k}: {v}" for k, v in (adapter_errors or {}).items()) or "none"),
    ]
    meta_ws.append(["Field", "Value"])
    for c_idx in (1, 2):
        meta_ws.cell(row=1, column=c_idx).fill = _HEADER_FILL
        meta_ws.cell(row=1, column=c_idx).font = _HEADER_FONT
    for field_name, val in meta_rows:
        meta_ws.append([_sanitize_excel_text(field_name), _sanitize_excel_text(val)])
    meta_ws.column_dimensions["A"].width = 24
    meta_ws.column_dimensions["B"].width = 72

    wb.save(xlsx_path)


def _escape_script_json(obj: object) -> str:
    """Serialize JSON safely for inline HTML <script> tags."""
    raw = json.dumps(obj, ensure_ascii=False)
    return (
        raw.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _write_html_viewer(
    html_path: Path,
    run_id: str,
    query: str,
    mode: str,
    items: list[ScrapeItem],
    stats: dict[str, int],
    extract_fields: tuple[str, ...],
) -> None:
    """Write a self-contained, offline interactive dataset table viewer (.html)."""
    records = []
    sources = sorted({it.source for it in items})
    for idx, it in enumerate(items, start=1):
        ext = it.metadata.get("extracted") if isinstance(it.metadata, dict) else {}
        codes = ext.get("codes", []) if isinstance(ext, dict) else []
        fields_map = {f: _item_field_value(it, f) for f in extract_fields}
        records.append(
            {
                "rank": idx,
                "id": it.item_id,
                "score": round(float(it.score), 2),
                "source": it.source,
                "eng": int(it.engagement),
                "date": it.published_at[:10],
                "title": " ".join(it.title.split()),
                "url": _safe_http_url(it.url) or it.url,
                "highlight": _item_summary_text(it, 240),
                "extracted": format_extracted_text(it),
                "codes": codes,
                "fields": fields_map,
            }
        )

    safe_json = _escape_script_json(records)
    safe_fields = _escape_script_json(list(extract_fields))
    src_options = "".join(
        f'<option value="{html.escape(s)}">{html.escape(s)}</option>' for s in sources
    )
    extra_ths = "".join(f"<th>{html.escape(f)}</th>" for f in extract_fields)

    page_html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>WideScrape · {html.escape(query)}</title>
<style>
  :root {{ --bg:#0b1020; --card:#121931; --border:#24304d; --text:#e8edf7; --muted:#95a3c3; --accent:#4da3ff; --chip:#1b2a4a; }}
  @media (prefers-color-scheme: light) {{
    :root {{ --bg:#f6f8fc; --card:#ffffff; --border:#dce3f0; --text:#182235; --muted:#5a6882; --accent:#0b66d0; --chip:#ebf3ff; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--text); padding:24px; }}
  .wrap {{ max-width:1360px; margin:0 auto; }}
  .header {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:18px 22px; margin-bottom:16px; }}
  .header h1 {{ margin:0 0 8px; font-size:20px; }}
  .meta {{ color:var(--muted); font-size:13px; display:flex; gap:16px; flex-wrap:wrap; }}
  .controls {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:14px; }}
  input, select {{ background:var(--card); color:var(--text); border:1px solid var(--border); border-radius:8px; padding:9px 12px; font-size:14px; }}
  input {{ flex:1; min-width:240px; }}
  .table-wrap {{ background:var(--card); border:1px solid var(--border); border-radius:12px; overflow:auto; }}
  table {{ width:100%; border-collapse:collapse; font-size:13.5px; }}
  th, td {{ padding:10px 12px; border-bottom:1px solid var(--border); text-align:left; vertical-align:top; }}
  th {{ position:sticky; top:0; background:var(--card); cursor:pointer; user-select:none; white-space:nowrap; font-size:12px; text-transform:uppercase; letter-spacing:.03em; color:var(--muted); }}
  tr:hover {{ background:rgba(77,163,255,0.06); }}
  a {{ color:var(--accent); text-decoration:none; font-weight:600; }}
  a:hover {{ text-decoration:underline; }}
  .pill {{ display:inline-block; background:var(--chip); color:var(--accent); border-radius:6px; padding:2px 8px; font-size:12px; font-weight:600; margin:2px 4px 2px 0; cursor:pointer; border:1px solid transparent; }}
  .pill:hover {{ border-color:var(--accent); }}
  .hl {{ color:var(--muted); margin-top:4px; line-height:1.4; }}
  .count {{ font-size:13px; color:var(--muted); align-self:center; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <h1>WideScrape Results: {html.escape(query)}</h1>
    <div class="meta">
      <span>Run ID: <code>{html.escape(run_id)}</code></span>
      <span>Mode: <strong>{html.escape(mode)}</strong></span>
      <span>Kept: <strong>{len(items)}</strong> of {stats.get('raw', len(items))} scraped</span>
      <span>Context Saved: <strong>{stats.get('compression_pct', 0)}%</strong></span>
    </div>
  </div>
  <div class="controls">
    <input id="q" type="search" placeholder="Filter titles, highlights, promo codes, extracted fields..." />
    <select id="src"><option value="">All Sources ({len(sources)})</option>{src_options}</select>
    <select id="sort">
      <option value="rank:asc">Sort: Rank (Best First)</option>
      <option value="score:desc">Sort: Score (High -> Low)</option>
      <option value="eng:desc">Sort: Engagement (High -> Low)</option>
      <option value="date:desc">Sort: Newest Date</option>
    </select>
    <span class="count" id="shown"></span>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr><th data-sort="rank">#</th><th data-sort="score">Score</th><th data-sort="source">Source</th><th data-sort="eng">Eng</th><th data-sort="date">Date</th><th>Extracted</th>{extra_ths}<th data-sort="title">Title & Highlight</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
</div>
<script>
const DATA = {safe_json};
const EXTRA_FIELDS = {safe_fields};
const tbody = document.getElementById('rows');
const qEl = document.getElementById('q');
const srcEl = document.getElementById('src');
const sortEl = document.getElementById('sort');
const shownEl = document.getElementById('shown');
let sortState = {{ key: 'rank', dir: 'asc' }};

function el(tag, cls, txt) {{
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt !== undefined) n.textContent = txt;
  return n;
}}

async function copyText(txt, pill) {{
  let ok = false;
  try {{
    if (navigator.clipboard && window.isSecureContext) {{
      await navigator.clipboard.writeText(txt);
      ok = true;
    }}
  }} catch (_) {{ ok = false; }}
  if (!ok) {{
    try {{
      const ta = document.createElement('textarea');
      ta.value = txt;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      ok = document.execCommand('copy');
      document.body.removeChild(ta);
    }} catch (_) {{ ok = false; }}
  }}
  pill.textContent = ok ? txt + ' (copied)' : txt + ' (copy failed)';
  setTimeout(() => pill.textContent = txt, 1300);
}}

function render() {{
  const term = qEl.value.trim().toLowerCase();
  const src = srcEl.value;
  const key = sortState.key, dir = sortState.dir;
  let list = DATA.filter(r => {{
    if (src && r.source !== src) return false;
    if (!term) return true;
    const blob = (r.title + ' ' + r.highlight + ' ' + r.extracted + ' ' + r.source + ' ' + JSON.stringify(r.fields)).toLowerCase();
    return blob.includes(term);
  }});
  list.sort((a, b) => {{
    const av = a[key], bv = b[key];
    if (av < bv) return dir === 'asc' ? -1 : 1;
    if (av > bv) return dir === 'asc' ? 1 : -1;
    return 0;
  }});
  tbody.innerHTML = '';
  for (const r of list) {{
    const tr = document.createElement('tr');
    tr.appendChild(el('td', '', String(r.rank)));
    tr.appendChild(el('td', '', r.score.toFixed(2)));
    tr.appendChild(el('td', '', r.source));
    tr.appendChild(el('td', '', String(r.eng)));
    tr.appendChild(el('td', '', r.date || '-'));
    const extTd = el('td');
    for (const c of (r.codes || [])) {{
      const p = el('span', 'pill', c);
      p.title = 'Click to copy code';
      p.onclick = () => copyText(c, p);
      extTd.appendChild(p);
    }}
    if (r.extracted) {{
      const extraNote = (r.codes && r.codes.length)
        ? r.extracted.split(' | ').filter(part => !part.startsWith('Codes:')).join(' | ')
        : r.extracted;
      if (extraNote) {{
        const div = el('div', 'hl', extraNote);
        extTd.appendChild(div);
      }}
    }}
    tr.appendChild(extTd);
    for (const f of EXTRA_FIELDS) tr.appendChild(el('td', '', r.fields[f] || ''));
    const mainTd = el('td');
    const a = document.createElement('a');
    a.href = r.url.startsWith('http://') || r.url.startsWith('https://') ? r.url : '#';
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.textContent = r.title;
    mainTd.appendChild(a);
    mainTd.appendChild(el('div', 'hl', r.highlight));
    tr.appendChild(mainTd);
    tbody.appendChild(tr);
  }}
  shownEl.textContent = `Showing ${{list.length}} of ${{DATA.length}}`;
}}
sortEl.addEventListener('input', () => {{
  const [k, d] = sortEl.value.split(':');
  sortState = {{ key: k, dir: d }};
  render();
}});
document.querySelectorAll('th[data-sort]').forEach(th => {{
  th.onclick = () => {{
    const k = th.getAttribute('data-sort');
    const nextDir = (sortState.key === k && sortState.dir === 'desc') ? 'asc' : 'desc';
    sortState = {{ key: k, dir: nextDir }};
    render();
  }};
}});
[qEl, srcEl].forEach(n => n.addEventListener('input', render));
render();
</script>
</body>
</html>"""
    html_path.write_text(page_html, encoding="utf-8")


def export_run_files(
    run_id: str,
    query: str,
    mode: str,
    items: list[ScrapeItem],
    stats: dict[str, int],
    export_dir: Path | str = DEFAULT_EXPORT_DIR,
    adapter_counts: dict[str, int] | None = None,
    adapter_errors: dict[str, str] | None = None,
    days: int | None = None,
    extract_mode: str = "",
    extract_fields: tuple[str, ...] = (),
    filename_stem: str = "",
) -> tuple[Path, Path, Path]:
    """Export ranked run results to Markdown (.md), Excel (.xlsx), and interactive HTML (.html).

    Args:
        run_id: Unique short identifier for the scrape run or consolidated task.
        query: Raw user query string or consolidated task title.
        mode: Resolved search mode name.
        items: Ranked and deduplicated scrape items.
        stats: Pipeline summary counters and compression metrics.
        export_dir: Destination folder for exported files.
        adapter_counts: Raw item counts per adapter.
        adapter_errors: Per-adapter warning messages if any adapter failed.
        days: Optional recency filter window in days.
        extract_mode: Active built-in extractor name, if any.
        extract_fields: Optional custom schema field names extracted per item.
        filename_stem: Optional explicit file stem (replaces `<run_id>_<query>` for consolidated task exports).

    Returns:
        Tuple containing `(md_path, xlsx_path, html_path)`.
    """
    out_dir = Path(export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _slugify(filename_stem, max_len=64) if filename_stem.strip() else f"{run_id}_{_slugify(query)}"
    md_path = out_dir / f"{stem}.md"
    xlsx_path = out_dir / f"{stem}.xlsx"
    html_path = out_dir / f"{stem}.html"

    _write_markdown_report(
        md_path, run_id, query, mode, items, stats, adapter_errors, days, extract_mode, extract_fields
    )
    _write_xlsx_workbook(
        xlsx_path, run_id, query, mode, items, stats, adapter_counts, adapter_errors, days, extract_mode, extract_fields
    )
    _write_html_viewer(html_path, run_id, query, mode, items, stats, extract_fields)
    return md_path, xlsx_path, html_path
