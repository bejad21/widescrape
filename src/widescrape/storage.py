"""Normalized SQLite + FTS5 storage for runs, deduplicated items, and incremental diffing."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Iterable

from widescrape.fetcher import canonicalize_url
from widescrape.models import ScrapeItem

DEFAULT_DB_PATH = Path.home() / ".widescrape" / "widescrape_cache.db"
_SAFE_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_FTS_RESERVED = {"and", "or", "not", "near"}


def content_digest(text: str) -> str:
    """Compute stable SHA-256 digest of whitespace-normalized item content."""
    norm = " ".join((text or "").split())
    return hashlib.sha256(norm.encode("utf-8", errors="ignore")).hexdigest()


def sanitize_fts5_query(raw_query: str) -> str:
    """Convert arbitrary user text (Unicode, C++, foo-bar, operators) into safe FTS5 terms."""
    tokens = [t for t in _SAFE_TOKEN_RE.findall(raw_query or "") if t.lower() not in _FTS_RESERVED]
    if not tokens:
        return ""
    return " ".join(f'"{t}"' for t in tokens)


class ScrapeStorage:
    """Persistent SQLite storage keeping historical runs intact alongside unique cached items."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            with conn:
                cols = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(items)").fetchall()
                }
                if cols and "canonical_url" not in cols:
                    self._migrate_legacy_schema(conn)
                else:
                    self._create_v2_tables(conn)

    @staticmethod
    def _create_v2_tables(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                query TEXT DEFAULT '',
                mode TEXT DEFAULT 'general',
                stats_json TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS items (
                item_id TEXT PRIMARY KEY,
                canonical_url TEXT UNIQUE,
                source TEXT NOT NULL,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                author TEXT,
                published_at TEXT,
                engagement INTEGER DEFAULT 0,
                score REAL DEFAULT 0.0,
                query_used TEXT,
                content_hash TEXT DEFAULT '',
                metadata_json TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS run_items (
                run_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                rank INTEGER NOT NULL,
                score REAL DEFAULT 0.0,
                query_used TEXT,
                metadata_json TEXT,
                PRIMARY KEY (run_id, item_id)
            );
            CREATE INDEX IF NOT EXISTS idx_run_items_order ON run_items(run_id, rank ASC);
            CREATE INDEX IF NOT EXISTS idx_items_score ON items(score DESC);
            CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
                item_id UNINDEXED,
                title,
                body,
                source UNINDEXED
            );
            """
        )

    def _migrate_legacy_schema(self, conn: sqlite3.Connection) -> None:
        """Upgrade legacy single-table databases without losing historical rows."""
        legacy_rows = conn.execute("SELECT * FROM items ORDER BY created_at ASC, score DESC").fetchall()
        conn.execute("DROP TABLE IF EXISTS items_fts")
        conn.execute("ALTER TABLE items RENAME TO items_legacy")
        self._create_v2_tables(conn)

        seen_runs: dict[str, tuple[str, str]] = {}
        seen_canon: set[str] = set()
        for rank_idx, r in enumerate(legacy_rows, start=1):
            run_id = r["run_id"] or "legacy"
            if run_id not in seen_runs:
                seen_runs[run_id] = (r["query_used"] or "", r["created_at"] or "")
                conn.execute(
                    "INSERT OR IGNORE INTO runs (run_id, query, mode, created_at) VALUES (?, ?, ?, ?)",
                    (run_id, r["query_used"] or "", "general", r["created_at"]),
                )
            canon = canonicalize_url(r["url"])
            digest = content_digest(f"{r['title']}\n{r['body']}")
            if canon not in seen_canon:
                seen_canon.add(canon)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO items
                    (item_id, canonical_url, source, url, title, body, author, published_at, engagement, score, query_used, content_hash, metadata_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        r["item_id"],
                        canon,
                        r["source"],
                        r["url"],
                        r["title"],
                        r["body"],
                        r["author"] or "",
                        r["published_at"] or "",
                        r["engagement"] or 0,
                        r["score"] or 0.0,
                        r["query_used"] or "",
                        digest,
                        r["metadata_json"] or "{}",
                        r["created_at"],
                    ),
                )
            existing_id_row = conn.execute(
                "SELECT item_id FROM items WHERE canonical_url = ?",
                (canon,),
            ).fetchone()
            target_id = existing_id_row["item_id"] if existing_id_row else r["item_id"]
            conn.execute(
                """
                INSERT OR IGNORE INTO run_items (run_id, item_id, rank, score, query_used, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, target_id, rank_idx, r["score"] or 0.0, r["query_used"] or "", r["metadata_json"] or "{}"),
            )

        conn.execute("DROP TABLE items_legacy")
        conn.execute("INSERT INTO items_fts (item_id, title, body, source) SELECT item_id, title, body, source FROM items")

    def save_run(
        self,
        run_id: str,
        items: Iterable[ScrapeItem],
        query: str = "",
        mode: str = "general",
        stats: dict[str, int] | None = None,
    ) -> int:
        """Persist run metadata, update deduplicated items, and preserve historical run links."""
        item_list = list(items)
        sample_q = query or (item_list[0].query_used if item_list else "")
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO runs (run_id, query, mode, stats_json) VALUES (?, ?, ?, ?)",
                    (run_id, sample_q, mode, json.dumps(stats or {})),
                )
                for rank_idx, item in enumerate(item_list, start=1):
                    canon = canonicalize_url(item.url)
                    digest = content_digest(f"{item.title}\n{item.body}")
                    existing = conn.execute(
                        "SELECT item_id, body, engagement, published_at, metadata_json FROM items WHERE canonical_url = ? OR item_id = ?",
                        (canon, item.item_id),
                    ).fetchone()
                    target_id = existing["item_id"] if existing else item.item_id
                    if existing:
                        item.item_id = target_id
                        # Preserve richer body/engagement if prior capture had more text.
                        best_body = item.body if len(item.body) >= len(existing["body"] or "") else existing["body"]
                        best_eng = max(int(item.engagement), int(existing["engagement"] or 0))
                        best_pub = item.published_at or (existing["published_at"] or "")
                        conn.execute(
                            """
                            UPDATE items
                            SET source = ?, url = ?, title = ?, body = ?, author = COALESCE(NULLIF(?, ''), author),
                                published_at = ?, engagement = ?, score = MAX(score, ?), query_used = ?,
                                content_hash = ?, metadata_json = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE item_id = ?
                            """,
                            (
                                item.source,
                                item.url,
                                item.title,
                                best_body,
                                item.author,
                                best_pub,
                                best_eng,
                                item.score,
                                item.query_used,
                                digest,
                                json.dumps(item.metadata),
                                target_id,
                            ),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO items
                            (item_id, canonical_url, source, url, title, body, author, published_at, engagement, score, query_used, content_hash, metadata_json)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                target_id,
                                canon,
                                item.source,
                                item.url,
                                item.title,
                                item.body,
                                item.author,
                                item.published_at,
                                item.engagement,
                                item.score,
                                item.query_used,
                                digest,
                                json.dumps(item.metadata),
                            ),
                        )

                    conn.execute(
                        """
                        INSERT OR REPLACE INTO run_items (run_id, item_id, rank, score, query_used, metadata_json)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (run_id, target_id, rank_idx, item.score, item.query_used, json.dumps(item.metadata)),
                    )
                    conn.execute("DELETE FROM items_fts WHERE item_id = ?", (target_id,))
                    conn.execute(
                        "INSERT INTO items_fts (item_id, title, body, source) VALUES (?, ?, ?, ?)",
                        (target_id, item.title, item.body, item.source),
                    )
        return len(item_list)

    def get_seen_urls(self) -> set[str]:
        """Return canonicalized URLs from all previously saved items."""
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT canonical_url, url FROM items").fetchall()
        return {str(r["canonical_url"] or canonicalize_url(str(r["url"]))) for r in rows if r["url"]}

    def get_seen_hashes(self) -> dict[str, str]:
        """Return mapping of canonical URL -> stored content SHA-256 digest."""
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT canonical_url, url, content_hash FROM items").fetchall()
        out: dict[str, str] = {}
        for r in rows:
            canon = str(r["canonical_url"] or canonicalize_url(str(r["url"])))
            out[canon] = str(r["content_hash"] or "")
        return out

    def get_items_by_ids(self, item_ids: list[str]) -> list[ScrapeItem]:
        """Retrieve full bodies for specific item IDs chosen by the agent."""
        if not item_ids:
            return []
        placeholders = ",".join("?" for _ in item_ids)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM items WHERE item_id IN ({placeholders})",
                item_ids,
            ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def search_cache(self, keyword_query: str, limit: int = 25) -> list[ScrapeItem]:
        """Search already-scraped local items safely with FTS5 (and LIKE fallback)."""
        clean_raw = " ".join((keyword_query or "").split())
        if not clean_raw:
            return []

        fts_query = sanitize_fts5_query(clean_raw)
        with closing(self._connect()) as conn:
            if fts_query:
                try:
                    rows = conn.execute(
                        """
                        SELECT i.* FROM items i
                        JOIN items_fts f ON i.item_id = f.item_id
                        WHERE items_fts MATCH ?
                        ORDER BY i.score DESC
                        LIMIT ?
                        """,
                        (fts_query, limit),
                    ).fetchall()
                    if rows:
                        return [self._row_to_item(r) for r in rows]
                except sqlite3.OperationalError:
                    pass

            # Safe substring fallback for symbols like 'C++' or bare non-FTS queries.
            pattern = f"%{clean_raw}%"
            rows = conn.execute(
                """
                SELECT * FROM items
                WHERE title LIKE ? OR body LIKE ?
                ORDER BY score DESC
                LIMIT ?
                """,
                (pattern, pattern, limit),
            ).fetchall()
            return [self._row_to_item(r) for r in rows]

    def count_run_items(self, run_id: str) -> int:
        """Count total stored items associated with `run_id`."""
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM run_items WHERE run_id = ?", (run_id,)).fetchone()
        return int(row["cnt"]) if row else 0

    def get_cache_stats(self) -> dict[str, int]:
        """Return counts of stored runs, items, run links, and SQLite database byte size."""
        with closing(self._connect()) as conn:
            runs_cnt = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            items_cnt = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            links_cnt = conn.execute("SELECT COUNT(*) FROM run_items").fetchone()[0]
        db_bytes = self.db_path.stat().st_size if self.db_path.is_file() else 0
        return {
            "runs": int(runs_cnt),
            "items": int(items_cnt),
            "run_items": int(links_cnt),
            "db_bytes": int(db_bytes),
        }

    def prune_cache(self, keep_days: int = 30) -> dict[str, int]:
        """Delete runs older than `keep_days` and garbage-collect unreferenced items."""
        if keep_days < 0:
            raise ValueError("keep_days must be >= 0")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M:%S")
        with closing(self._connect()) as conn:
            with conn:
                old_runs = [
                    r["run_id"]
                    for r in conn.execute("SELECT run_id FROM runs WHERE created_at < ?", (cutoff,)).fetchall()
                ]
                if not old_runs:
                    return {"deleted_runs": 0, "deleted_items": 0}
                placeholders = ",".join("?" for _ in old_runs)
                conn.execute(f"DELETE FROM run_items WHERE run_id IN ({placeholders})", old_runs)
                conn.execute(f"DELETE FROM runs WHERE run_id IN ({placeholders})", old_runs)
                orphan_ids = [
                    r["item_id"]
                    for r in conn.execute(
                        "SELECT item_id FROM items WHERE item_id NOT IN (SELECT DISTINCT item_id FROM run_items)"
                    ).fetchall()
                ]
                if orphan_ids:
                    item_ph = ",".join("?" for _ in orphan_ids)
                    conn.execute(f"DELETE FROM items_fts WHERE item_id IN ({item_ph})", orphan_ids)
                    conn.execute(f"DELETE FROM items WHERE item_id IN ({item_ph})", orphan_ids)
            if orphan_ids:
                conn.execute("VACUUM")
        return {"deleted_runs": len(old_runs), "deleted_items": len(orphan_ids)}

    def get_run_info(self, run_id: str) -> dict | None:
        """Return stored run metadata (`run_id`, `query`, `mode`, `stats`, `created_at`)."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT run_id, query, mode, stats_json, created_at FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            stats = json.loads(row["stats_json"] or "{}")
        except ValueError:
            stats = {}
        return {
            "run_id": row["run_id"],
            "query": row["query"] or "",
            "mode": row["mode"] or "general",
            "stats": stats if isinstance(stats, dict) else {},
            "created_at": row["created_at"],
        }

    def get_run_items(self, run_id: str, offset: int = 0, limit: int = 0) -> list[ScrapeItem]:
        """Return stored items for a run ordered best-first (with SQL pagination)."""
        sql = """
            SELECT i.*, ri.score AS run_score, ri.query_used AS run_query_used, ri.metadata_json AS run_metadata_json
            FROM run_items ri
            JOIN items i ON i.item_id = ri.item_id
            WHERE ri.run_id = ?
            ORDER BY ri.score DESC, ri.rank ASC
        """
        with closing(self._connect()) as conn:
            if limit > 0:
                rows = conn.execute(f"{sql} LIMIT ? OFFSET ?", (run_id, limit, offset)).fetchall()
            elif offset > 0:
                rows = conn.execute(f"{sql} LIMIT -1 OFFSET ?", (run_id, offset)).fetchall()
            else:
                rows = conn.execute(sql, (run_id,)).fetchall()
        return [self._row_to_item(r) for r in rows]

    def list_runs(self, limit: int = 10) -> list[dict]:
        """List recent scrape runs stored in SQLite (including 0-item runs)."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT r.run_id, COUNT(ri.item_id) AS item_count, r.query AS sample_query,
                       r.mode, r.created_at
                FROM runs r
                LEFT JOIN run_items ri ON r.run_id = ri.run_id
                GROUP BY r.run_id
                ORDER BY r.created_at DESC, r.rowid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> ScrapeItem:
        keys = set(row.keys())
        meta_raw = row["run_metadata_json"] if "run_metadata_json" in keys and row["run_metadata_json"] else row["metadata_json"]
        score_val = row["run_score"] if "run_score" in keys and row["run_score"] is not None else row["score"]
        q_used = row["run_query_used"] if "run_query_used" in keys and row["run_query_used"] else row["query_used"]
        try:
            meta = json.loads(meta_raw or "{}")
        except ValueError:
            meta = {}
        return ScrapeItem(
            item_id=row["item_id"],
            source=row["source"],
            url=row["url"],
            title=row["title"],
            body=row["body"],
            author=row["author"] or "",
            published_at=row["published_at"] or "",
            engagement=row["engagement"] or 0,
            score=score_val or 0.0,
            query_used=q_used or "",
            metadata=meta if isinstance(meta, dict) else {},
        )
