#!/usr/bin/env python3
"""Incremental contentless CJK-aware SQLite FTS5 + chronological search CLI.

Uses contentless_delete=1 FTS5 to eliminate redundant full-text storage (shrinking
DB size dramatically), updates only files whose size/mtime changed, supports
multi-keyword AND with configurable synonym expansion, and falls back to disk scan.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

ARCHIVE_SOURCES = ("codex", "chatgpt", "cursor", "antigravity", "claude")
REGEX_META_RE = re.compile(r"[|*?+()\[\]{}\\]")
SCHEMA_VERSION = "3"
SYNC_DIR = Path(__file__).resolve().parent
VEC_PYTHON = Path("/usr/local/bin/python3") if Path("/usr/local/bin/python3").is_file() else Path(sys.executable)


def cjk_tokenize(text: str) -> str:
    return re.sub(r"([\u4e00-\u9fff])", r" \1 ", text)


def load_synonyms(root: Path) -> dict[str, list[str]]:
    cfg_path = root / "_同步" / "kb_config.json"
    syn_groups: list[list[str]] = []
    if cfg_path.is_file():
        try:
            syn_groups = json.loads(cfg_path.read_text(encoding="utf-8")).get("synonyms", [])
        except Exception:
            syn_groups = []
    syn_map: dict[str, list[str]] = {}
    for group in syn_groups:
        clean_group = [g.strip() for g in group if isinstance(g, str) and g.strip()]
        for item in clean_group:
            syn_map[item.lower()] = clean_group
    return syn_map


def expand_term_variants(term: str, syn_map: dict[str, list[str]]) -> list[str]:
    return syn_map.get(term.lower(), [term])


def load_manifests(root: Path) -> list[dict]:
    items = []
    for src in ARCHIVE_SOURCES:
        p = root / "_同步" / f"{src}-manifest.json"
        if p.is_file():
            try:
                for item in json.loads(p.read_text(encoding="utf-8")):
                    if "source" not in item:
                        item["source"] = src
                    items.append(item)
            except Exception:
                pass
    items.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    return items


def parse_curated_chunks(md_path: Path, root: Path) -> list[dict]:
    lines = md_path.read_text(encoding="utf-8", errors="replace").splitlines()
    chunks = []
    cur_heading = md_path.stem
    cur_line = 1
    buf: list[str] = []
    rel = md_path.relative_to(root).as_posix()

    def flush():
        body = "\n".join(buf).strip()
        if body:
            chunks.append({
                "tier": "curated",
                "source": "note",
                "path": rel,
                "abs_path": str(md_path),
                "title": md_path.stem,
                "cwd": "",
                "updated_at": "9999-99-99",
                "section": cur_heading,
                "role": "note",
                "time": "",
                "line": cur_line,
                "body": body,
            })

    for i, line in enumerate(lines, 1):
        if line.startswith("#"):
            flush()
            buf = [line]
            cur_heading = line.lstrip("#").strip()
            cur_line = i
        else:
            buf.append(line)
    flush()
    return chunks


def parse_conversation_messages(md_path: Path, root: Path, meta: dict) -> list[dict]:
    lines = md_path.read_text(encoding="utf-8", errors="replace").splitlines()
    rel = md_path.relative_to(root).as_posix()
    chunks = []
    cur_header = "文件头部"
    cur_role = "meta"
    cur_time = meta.get("updated_at", "未知")
    cur_line = 1
    buf: list[str] = []
    last_user_topic = ""
    conv_title = (meta.get("title") or md_path.stem).strip()

    def flush():
        nonlocal last_user_topic
        body = "\n".join(buf).strip()
        if body:
            sec_label = cur_header
            if cur_role == "user" and "会话上下文或控制标记" not in cur_header:
                raw_u = body.split("## My request:", 1)[1].strip() if "## My request:" in body else body
                raw_u = raw_u.replace("[非文本附件：保留在原始会话中，本次文本归档未复制附件。]", "").strip()
                clean_u = re.sub(r"\s+", " ", raw_u).strip()
                if clean_u:
                    last_user_topic = clean_u[:36]
            elif cur_role == "assistant" and last_user_topic and len(chunks) >= 3:
                if last_user_topic[:18].lower() not in conv_title.lower():
                    sec_label = f"{cur_header} · 问:{last_user_topic}"
            chunks.append({
                "tier": "archive",
                "source": meta.get("source", md_path.parent.name.lower()),
                "path": rel,
                "abs_path": str(md_path),
                "title": conv_title,
                "cwd": meta.get("cwd", ""),
                "updated_at": meta.get("updated_at", ""),
                "section": sec_label,
                "role": cur_role,
                "time": cur_time,
                "line": cur_line,
                "body": body,
            })

    for i, line in enumerate(lines, 1):
        m = re.match(r"^#{2,3}\s+(\d+)\.\s+(用户|助手)(.*)$", line)
        if m:
            flush()
            buf = []
            cur_header = line.lstrip("#").strip()
            cur_role = "user" if m.group(2) == "用户" else "assistant"
            cur_line = i
            tail = (m.group(3) or "").strip()
            if "·" in tail:
                inline_ts = tail.split("·", 1)[1].strip()
                if inline_ts:
                    cur_time = inline_ts
            continue
        if line.startswith("时间（UTC）："):
            cur_time = line.split("：", 1)[-1].strip()
            continue
        if line.startswith("<!-- message_id:"):
            continue
        buf.append(line)
    flush()
    return chunks


def _collect_all_targets(root: Path) -> dict[str, tuple[str, Path, dict | None]]:
    """Return {rel_path: (kind, abs_path, meta_or_none)} for all indexable files."""
    targets: dict[str, tuple[str, Path, dict | None]] = {}
    curated_files = [root / "INDEX.md", root / "个人档案.md"]
    for folder in ("笔记", "笔记/历史档案", "共享摘录"):
        d = root / folder
        if d.is_dir():
            curated_files.extend(sorted(d.glob("*.md")))
    for f in curated_files:
        if f.is_file():
            rel = f.relative_to(root).as_posix()
            targets[rel] = ("curated", f, None)

    for meta in load_manifests(root):
        rel = meta.get("path")
        if not rel or rel in targets:
            continue
        md_path = root / rel
        if md_path.is_file():
            targets[rel] = ("archive", md_path, meta)

    dir_map = {
        "codex": root / "对话" / "Codex",
        "chatgpt": root / "对话" / "ChatGPT",
        "cursor": root / "对话" / "Cursor",
        "antigravity": root / "对话" / "Antigravity",
        "claude": root / "对话" / "Claude",
    }
    for src_name, sdir in dir_map.items():
        if not sdir.is_dir():
            continue
        for md_path in sorted(sdir.glob("*.md")):
            rel = md_path.relative_to(root).as_posix()
            if rel not in targets:
                targets[rel] = ("archive", md_path, {"source": src_name, "title": md_path.stem, "cwd": "", "updated_at": ""})
    return targets


def _init_schema_v3(conn: sqlite3.Connection):
    conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
    conn.execute(
        """
        CREATE TABLE files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tier TEXT NOT NULL,
            source TEXT NOT NULL,
            path TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            cwd TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            sig TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX idx_files_filter ON files(tier, source, updated_at DESC)")
    conn.execute(
        """
        CREATE TABLE docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            section TEXT NOT NULL,
            role TEXT NOT NULL,
            time TEXT NOT NULL,
            line INTEGER NOT NULL,
            body TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX idx_docs_file ON docs(file_id)")
    conn.execute(
        "CREATE VIRTUAL TABLE docs_fts USING fts5(title_tok, body_tok, content='', tokenize='unicode61')"
    )


def _schema_is_current(db_path: Path) -> bool:
    if not db_path.is_file():
        return False
    try:
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10.0)
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        fts_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='docs_fts'").fetchone()
        conn.close()
        return bool(
            row
            and row[0] == SCHEMA_VERSION
            and fts_sql
            and "contentless_delete" not in (fts_sql[0] or "")
        )
    except Exception:
        return False


def get_search_db_stats(root: Path) -> dict:
    db_path = root.resolve() / "_同步" / "search_index.sqlite"
    if not db_path.is_file():
        return {"exists": False}
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10.0)
    try:
        schema_ver = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        freelist_count = conn.execute("PRAGMA freelist_count").fetchone()[0]
        auto_vacuum = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
        total_files = conn.execute("SELECT count(*) FROM files").fetchone()[0] if schema_ver and schema_ver[0] == SCHEMA_VERSION else None
        total_docs = conn.execute("SELECT count(*) FROM docs").fetchone()[0]
        return {
            "exists": True,
            "schema_version": schema_ver[0] if schema_ver else "unknown",
            "db_bytes": db_path.stat().st_size,
            "db_mb": round(db_path.stat().st_size / (1024 * 1024), 2),
            "page_size": page_size,
            "page_count": page_count,
            "freelist_pages": freelist_count,
            "auto_vacuum": auto_vacuum,
            "files": total_files,
            "chunks": total_docs,
        }
    finally:
        conn.close()


def _target_file_meta(rel: str, kind: str, fpath: Path, meta: dict | None) -> tuple[str, str, str, str, str]:
    if kind == "curated":
        return "curated", "note", fpath.stem, "", "9999-99-99"
    m = meta or {}
    return (
        "archive",
        m.get("source", fpath.parent.name.lower()),
        m.get("title", fpath.stem),
        m.get("cwd", ""),
        m.get("updated_at", ""),
    )


def _doc_title_tok(file_title: str, section: str) -> str:
    t_tok = cjk_tokenize(file_title)
    if "· 问:" in (section or ""):
        return f"{t_tok} {cjk_tokenize(section)}".strip()
    return t_tok


def _insert_file_and_chunks(
    conn: sqlite3.Connection,
    root: Path,
    rel: str,
    kind: str,
    fpath: Path,
    meta: dict | None,
    sig: str,
):
    tier, source, title, cwd, updated_at = _target_file_meta(rel, kind, fpath, meta)
    cur = conn.execute(
        "INSERT INTO files(tier, source, path, title, cwd, updated_at, sig) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tier, source, rel, title, cwd, updated_at, sig),
    )
    file_id = cur.lastrowid
    chunks = parse_curated_chunks(fpath, root) if kind == "curated" else parse_conversation_messages(fpath, root, meta or {})
    for r in chunks:
        doc_cur = conn.execute(
            "INSERT INTO docs(file_id, section, role, time, line, body) VALUES (?, ?, ?, ?, ?, ?)",
            (file_id, r["section"], r["role"], r["time"], r["line"], r["body"]),
        )
        conn.execute(
            "INSERT INTO docs_fts(rowid, title_tok, body_tok) VALUES (?, ?, ?)",
            (doc_cur.lastrowid, _doc_title_tok(title, r["section"]), cjk_tokenize(r["body"])),
        )


def build_search_index(root: Path, stats_out: dict | None = None, force_compact: bool = False) -> int:
    """Incrementally update normalized contentless FTS5 search index with auto-vacuum reclamation."""
    root = root.resolve()
    (root / "_同步").mkdir(parents=True, exist_ok=True)
    db_path = root / "_同步" / "search_index.sqlite"
    targets = _collect_all_targets(root)

    current_sigs: dict[str, str] = {}
    for rel, (_, fpath, meta) in targets.items():
        st = fpath.stat()
        meta_sig = f"{meta.get('updated_at','')}:{meta.get('title','')}:{meta.get('cwd','')}" if meta else ""
        current_sigs[rel] = f"{st.st_size}:{st.st_mtime_ns}:{meta_sig}"

    if not _schema_is_current(db_path):
        tmp_path = root / "_同步" / "search_index.sqlite.tmp"
        if tmp_path.exists():
            tmp_path.unlink()
        conn = sqlite3.connect(str(tmp_path), timeout=10.0)
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        _init_schema_v3(conn)
        with conn:
            for rel, (kind, fpath, meta) in targets.items():
                _insert_file_and_chunks(conn, root, rel, kind, fpath, meta, current_sigs[rel])
            total_rows = conn.execute("SELECT count(*) FROM docs").fetchone()[0]
        conn.close()
        os.replace(tmp_path, db_path)
        freelist = 0
        if stats_out is not None:
            stats_out.update({
                "mode": "full_rebuild_v3",
                "updated_files": len(targets),
                "removed_files": 0,
                "total_chunks": total_rows,
                "compacted": False,
                "freelist_pages": freelist,
                "db_bytes": db_path.stat().st_size,
            })
        return total_rows

    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.execute("PRAGMA synchronous = NORMAL")
    prev_rows = conn.execute("SELECT id, path, sig FROM files").fetchall()
    prev_map = {path: (fid, sig) for fid, path, sig in prev_rows}

    removed_paths = [p for p in prev_map if p not in current_sigs]
    changed_paths = [p for p, sig in current_sigs.items() if prev_map.get(p, (None, None))[1] != sig]
    deleted_chunks = 0
    compacted = False

    if removed_paths or changed_paths:
        with conn:
            for rel in removed_paths + changed_paths:
                prev = prev_map.get(rel)
                if prev is not None:
                    fid = prev[0]
                    t_row = conn.execute("SELECT title FROM files WHERE id = ?", (fid,)).fetchone()
                    old_title = t_row[0] if t_row else ""
                    doc_rows = conn.execute("SELECT id, section, body FROM docs WHERE file_id = ?", (fid,)).fetchall()
                    deleted_chunks += len(doc_rows)
                    for did, dsec, dbody in doc_rows:
                        conn.execute(
                            "INSERT INTO docs_fts(docs_fts, rowid, title_tok, body_tok) VALUES ('delete', ?, ?, ?)",
                            (did, _doc_title_tok(old_title, dsec), cjk_tokenize(dbody)),
                        )
                    conn.execute("DELETE FROM docs WHERE file_id = ?", (fid,))
                    conn.execute("DELETE FROM files WHERE id = ?", (fid,))

            for rel in changed_paths:
                kind, fpath, meta = targets[rel]
                _insert_file_and_chunks(conn, root, rel, kind, fpath, meta, current_sigs[rel])

    freelist_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
    if force_compact or deleted_chunks >= 40:
        conn.execute("INSERT INTO docs_fts(docs_fts) VALUES('optimize')")
        conn.commit()
        freelist_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
        compacted = True

    total_rows = conn.execute("SELECT count(*) FROM docs").fetchone()[0]
    conn.close()

    if (force_compact or compacted or freelist_pages >= 64) and freelist_pages > 0:
        vac_conn = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
        try:
            for _ in range(freelist_pages + 2):
                cur_fl = vac_conn.execute("PRAGMA freelist_count").fetchone()[0]
                if cur_fl <= 0:
                    break
                vac_conn.execute(f"PRAGMA incremental_vacuum({cur_fl})").fetchall()
                next_fl = vac_conn.execute("PRAGMA freelist_count").fetchone()[0]
                if next_fl >= cur_fl:
                    break
            freelist_pages = vac_conn.execute("PRAGMA freelist_count").fetchone()[0]
            compacted = True
        finally:
            vac_conn.close()
    if stats_out is not None:
        stats_out.update({
            "mode": "incremental_v3",
            "updated_files": len(changed_paths),
            "removed_files": len(removed_paths),
            "total_chunks": total_rows,
            "compacted": compacted,
            "freelist_pages": freelist_pages,
            "db_bytes": db_path.stat().st_size,
        })
    return total_rows


def make_snippet(body: str, variants_flat: list[str], regex_pat: re.Pattern | None) -> str:
    clean_body = re.sub(r"\s+", " ", body).strip()
    pos = -1
    if regex_pat is not None:
        m = regex_pat.search(clean_body)
        if m:
            pos = m.start()
    else:
        low = clean_body.lower()
        for t in variants_flat:
            idx = low.find(t.lower())
            if idx != -1:
                pos = idx
                break
    if pos == -1:
        return clean_body[:320]
    start = max(0, pos - 90)
    end = min(len(clean_body), start + 320)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(clean_body) else ""
    return prefix + clean_body[start:end] + suffix


def _search_via_sqlite(
    root: Path, db_path: Path, query: str, source: str, role: str, cwd: str, since: str, limit: int, per_conv: int
) -> list[dict]:
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    syn_map = load_synonyms(root)
    effective_per_conv = max(1, per_conv)

    where = []
    params: list = []
    if source == "notes":
        where.append("f.tier = 'curated'")
    elif source in ARCHIVE_SOURCES:
        where.append("f.source = ?")
        params.append(source)
    if role != "all":
        where.append("d.role = ?")
        params.append(role)
    if cwd:
        where.append("instr(LOWER(f.cwd), ?) > 0")
        params.append(cwd.lower())
    if since:
        where.append("(f.tier = 'curated' OR substr(f.updated_at, 1, 10) >= ?)")
        params.append(since)

    is_regex = bool(REGEX_META_RE.search(query))
    terms = [t.strip() for t in query.split() if t.strip()]
    term_groups = [expand_term_variants(t, syn_map) for t in terms]
    variants_flat = [v for grp in term_groups for v in grp]

    if not is_regex and term_groups:
        fts_parts = []
        for grp in term_groups:
            or_clauses = []
            for v in grp:
                tok = cjk_tokenize(v).replace('"', " ").strip()
                if tok:
                    or_clauses.append(f'"{tok}"')
            if or_clauses:
                fts_parts.append("(" + " OR ".join(or_clauses) + ")" if len(or_clauses) > 1 else or_clauses[0])

        if fts_parts:
            fts_query = " AND ".join(fts_parts)
            where_sql = ("AND " + " AND ".join(where)) if where else ""
            sql = f"""
                WITH raw AS (
                    SELECT f.tier, f.source, f.path, f.title, f.cwd, f.updated_at,
                           d.file_id, d.section, d.role, d.time, d.line, d.body,
                           bm25(docs_fts) AS rank
                    FROM docs_fts
                    JOIN docs d ON d.id = docs_fts.rowid
                    JOIN files f ON f.id = d.file_id
                    WHERE docs_fts MATCH ? {where_sql}
                ),
                ranked AS (
                    SELECT *, ROW_NUMBER() OVER (PARTITION BY file_id ORDER BY rank, line) AS conv_rn
                    FROM raw
                )
                SELECT * FROM ranked
                WHERE conv_rn <= ?
                ORDER BY (CASE WHEN tier = 'curated' THEN 0 ELSE 1 END), updated_at DESC, rank
                LIMIT ?
            """
            try:
                rows = list(conn.execute(sql, [fts_query, *params, effective_per_conv, limit]).fetchall())
                # Multi-keyword quorum rescue ((N-1)-of-N AND) when strict AND misses curated notes or under-fills limit
                has_curated = any(r["tier"] == "curated" for r in rows)
                if len(fts_parts) >= 2 and (len(rows) < limit or (source in ("all", "notes") and not has_curated)):
                    if len(fts_parts) == 2 and not rows:
                        quorum_q = f"{fts_parts[0]} OR {fts_parts[1]}"
                    elif 3 <= len(fts_parts) <= 6:
                        k = len(fts_parts) - 1
                        quorum_q = " OR ".join("(" + " AND ".join(c) + ")" for c in itertools.combinations(fts_parts, k))
                    else:
                        quorum_q = ""
                    if quorum_q:
                        quorum_sql = f"""
                            WITH raw AS (
                                SELECT f.tier, f.source, f.path, f.title, f.cwd, f.updated_at,
                                       d.file_id, d.section, d.role, d.time, d.line, d.body,
                                       bm25(docs_fts) AS rank
                                FROM docs_fts
                                JOIN docs d ON d.id = docs_fts.rowid
                                JOIN files f ON f.id = d.file_id
                                WHERE docs_fts MATCH ? {where_sql}
                            ),
                            ranked AS (
                                SELECT *, ROW_NUMBER() OVER (PARTITION BY file_id ORDER BY rank, line) AS conv_rn
                                FROM raw
                            )
                            SELECT * FROM ranked
                            WHERE conv_rn <= ?
                            ORDER BY (CASE WHEN tier = 'curated' THEN 0 ELSE 1 END), rank, updated_at DESC
                            LIMIT ?
                        """
                        q_rows = conn.execute(quorum_sql, [quorum_q, *params, effective_per_conv, limit]).fetchall()
                        seen_keys = {(r["path"], r["line"]) for r in rows}
                        file_cnts: dict[str, int] = {}
                        for r in rows:
                            file_cnts[r["path"]] = file_cnts.get(r["path"], 0) + 1
                        curated_added: list = []
                        archive_added: list = []
                        for qr in q_rows:
                            k_id = (qr["path"], qr["line"])
                            if k_id in seen_keys or file_cnts.get(qr["path"], 0) >= effective_per_conv:
                                continue
                            seen_keys.add(k_id)
                            file_cnts[qr["path"]] = file_cnts.get(qr["path"], 0) + 1
                            if qr["tier"] == "curated":
                                curated_added.append(qr)
                            else:
                                archive_added.append(qr)
                        # Place rescued curated notes right after strict-match curated notes, before archive hits
                        strict_curated = [r for r in rows if r["tier"] == "curated"]
                        strict_archive = [r for r in rows if r["tier"] != "curated"]
                        rows = (strict_curated + curated_added + strict_archive + archive_added)[:limit]
                # Rescue continuous unspaced natural-language sentences (>= 6 CJK chars) when exact phrase hits nothing
                if not rows and len(terms) == 1:
                    cjk_only = "".join(re.findall(r"[\u4e00-\u9fff]+", terms[0]))
                    en_words = re.findall(r"[A-Za-z0-9_.-]{2,}", terms[0])
                    if len(cjk_only) >= 6:
                        stop = set("的了着过吗呢啊吧该怎什么如何哪些可以能够一下怎么")
                        meaningful = "".join(ch for ch in cjk_only if ch not in stop)
                        bigrams = [f'"{cjk_tokenize(meaningful[i:i+2])}"' for i in range(max(0, len(meaningful) - 1))]
                        en_clauses = [f'"{w}"' for w in en_words]
                        all_clauses = list(dict.fromkeys(bigrams + en_clauses))[:12]
                        if all_clauses:
                            rescue_sql = f"""
                                WITH raw AS (
                                    SELECT f.tier, f.source, f.path, f.title, f.cwd, f.updated_at,
                                           d.file_id, d.section, d.role, d.time, d.line, d.body,
                                           bm25(docs_fts) AS rank
                                    FROM docs_fts
                                    JOIN docs d ON d.id = docs_fts.rowid
                                    JOIN files f ON f.id = d.file_id
                                    WHERE docs_fts MATCH ? {where_sql}
                                ),
                                ranked AS (
                                    SELECT *, ROW_NUMBER() OVER (PARTITION BY file_id ORDER BY rank, line) AS conv_rn
                                    FROM raw
                                )
                                SELECT * FROM ranked
                                WHERE conv_rn <= ?
                                ORDER BY rank, (CASE WHEN tier = 'curated' THEN 0 ELSE 1 END), updated_at DESC
                                LIMIT ?
                            """
                            rows = conn.execute(rescue_sql, [" OR ".join(all_clauses), *params, effective_per_conv, limit]).fetchall()
                            variants_flat = list(dict.fromkeys([meaningful[i:i+2] for i in range(max(0, len(meaningful) - 1))] + en_words))
                conn.close()
                return [{
                    "tier": r["tier"],
                    "source": r["source"],
                    "path": r["path"],
                    "abs_path": str(root / r["path"]),
                    "title": r["title"],
                    "cwd": r["cwd"],
                    "updated_at": r["updated_at"],
                    "section": r["section"],
                    "role": r["role"],
                    "time": r["time"],
                    "line": r["line"],
                    "snippet": make_snippet(r["body"], variants_flat, None),
                } for r in rows]
            except sqlite3.OperationalError:
                pass

    try:
        pat = re.compile(query, re.I)
    except re.error:
        pat = re.compile(re.escape(query), re.I)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = (
        f"SELECT f.tier, f.source, f.path, f.title, f.cwd, f.updated_at, "
        f"d.section, d.role, d.time, d.line, d.body "
        f"FROM docs d JOIN files f ON f.id = d.file_id {where_sql} "
        f"ORDER BY (CASE WHEN f.tier = 'curated' THEN 0 ELSE 1 END), f.updated_at DESC"
    )
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    matched = []
    counts: dict[str, int] = {}
    for r in rows:
        if counts.get(r["path"], 0) >= effective_per_conv:
            continue
        hay = r["title"] + "\n" + r["body"]
        low = hay.lower()
        ok = (
            all(any(v.lower() in low for v in grp) for grp in term_groups)
            if (not is_regex and term_groups)
            else bool(pat.search(hay))
        )
        if ok:
            counts[r["path"]] = counts.get(r["path"], 0) + 1
            matched.append({
                "tier": r["tier"],
                "source": r["source"],
                "path": r["path"],
                "abs_path": str(root / r["path"]),
                "title": r["title"],
                "cwd": r["cwd"],
                "updated_at": r["updated_at"],
                "section": r["section"],
                "role": r["role"],
                "time": r["time"],
                "line": r["line"],
                "snippet": make_snippet(r["body"], variants_flat, None if not is_regex else pat),
            })
            if len(matched) >= limit:
                break
    return matched


def _chunk_matches(chunk: dict, term_groups: list[list[str]], pat: re.Pattern, is_regex: bool) -> bool:
    hay = chunk["title"] + "\n" + chunk["body"]
    if not is_regex and term_groups:
        low = hay.lower()
        return all(any(v.lower() in low for v in grp) for grp in term_groups)
    return bool(pat.search(hay))


def _query_vectors_subprocess(
    root: Path,
    query: str,
    source: str,
    role: str,
    cwd: str,
    since: str,
    limit: int,
    per_conv: int,
) -> list[dict]:
    vec_db = root / "_同步" / "vectors.sqlite"
    embed_script = SYNC_DIR / "kb_embed.py"
    if not VEC_PYTHON.is_file() or not vec_db.is_file() or not embed_script.is_file():
        return []
    cmd = [
        str(VEC_PYTHON), str(embed_script),
        "--root", str(root),
        "--query", query,
        "--source", source,
        "--role", role,
        "--cwd", cwd,
        "--since", since,
        "--limit", str(limit),
        "--per-conv", str(per_conv),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, check=False)
        if res.returncode != 0 or not res.stdout.strip():
            return []
        return json.loads(res.stdout).get("results", [])
    except Exception:
        return []


def rrf_fuse(fts_hits: list[dict], vec_hits: list[dict], limit: int, per_conv: int) -> list[dict]:
    """Reciprocal Rank Fusion merging lexical FTS5 BM25 hits and semantic ONNX vector hits."""
    fused: dict[tuple[str, int], dict] = {}
    scores: dict[tuple[str, int], float] = {}
    vias: dict[tuple[str, int], set[str]] = {}
    vec_sims: dict[tuple[str, int], float] = {}

    for rank, item in enumerate(fts_hits):
        key = (item["path"], int(item["line"]))
        fused.setdefault(key, dict(item))
        scores[key] = scores.get(key, 0.0) + 1.2 / (40.0 + rank)
        vias.setdefault(key, set()).add("fts")

    for rank, item in enumerate(vec_hits):
        key = (item["path"], int(item["line"]))
        fused.setdefault(key, dict(item))
        scores[key] = scores.get(key, 0.0) + 1.0 / (40.0 + rank)
        vias.setdefault(key, set()).add("vector")
        vec_sims[key] = float(item.get("score") or 0.0)

    for key, item in fused.items():
        if item.get("tier") == "curated":
            if "fts" in vias[key] or vec_sims.get(key, 0.0) >= 0.58:
                scores[key] += 0.015
            elif vec_sims.get(key, 0.0) >= 0.48:
                scores[key] += 0.004

    ranked = sorted(
        fused.items(),
        key=lambda kv: (
            scores[kv[0]],
            kv[1].get("updated_at", ""),
        ),
        reverse=True,
    )

    out: list[dict] = []
    conv_counts: dict[str, int] = {}
    eff_per_conv = max(1, per_conv)
    for key, item in ranked:
        if conv_counts.get(item["path"], 0) >= eff_per_conv:
            continue
        conv_counts[item["path"]] = conv_counts.get(item["path"], 0) + 1
        entry = dict(item)
        entry["match_via"] = "+".join(sorted(vias[key]))
        entry["score"] = round(scores[key], 5)
        out.append(entry)
        if len(out) >= limit:
            break
    return out


def search_kb(
    root: Path,
    query: str,
    source: str = "all",
    role: str = "all",
    cwd: str = "",
    since: str = "",
    limit: int = 15,
    per_conv: int = 2,
    mode: str = "hybrid",
) -> list[dict]:
    root = root.resolve()
    is_regex = bool(REGEX_META_RE.search(query))
    fetch_limit = limit if (mode == "fts" or is_regex) else max(24, limit * 3)

    fts_hits: list[dict] = []
    if mode in ("hybrid", "fts"):
        db_path = root / "_同步" / "search_index.sqlite"
        if db_path.is_file():
            if not _schema_is_current(db_path):
                try:
                    build_search_index(root)
                except Exception:
                    pass
            if _schema_is_current(db_path):
                fts_hits = _search_via_sqlite(root, db_path, query, source, role, cwd, since, fetch_limit, per_conv)
        if not db_path.is_file() or not _schema_is_current(db_path):
            fts_hits = _search_disk_fallback(root, query, source, role, cwd, since, fetch_limit, per_conv)

    if mode == "fts" or is_regex:
        return fts_hits[:limit]

    vec_hits = _query_vectors_subprocess(root, query, source, role, cwd, since, fetch_limit, per_conv)
    if mode == "vector":
        return vec_hits[:limit]
    if not vec_hits:
        return fts_hits[:limit]
    return rrf_fuse(fts_hits, vec_hits, limit=limit, per_conv=per_conv)


def _search_disk_fallback(
    root: Path,
    query: str,
    source: str,
    role: str,
    cwd: str,
    since: str,
    limit: int,
    per_conv: int,
) -> list[dict]:

    syn_map = load_synonyms(root)
    is_regex = bool(REGEX_META_RE.search(query))
    terms = [t.strip() for t in query.split() if t.strip()]
    term_groups = [expand_term_variants(t, syn_map) for t in terms]
    variants_flat = [v for grp in term_groups for v in grp]
    try:
        pat = re.compile(query, re.I)
    except re.error:
        pat = re.compile(re.escape(query), re.I)

    results: list[dict] = []
    if source in ("all", "notes") and role in ("all", "note") and not cwd:
        curated_files = [root / "INDEX.md", root / "个人档案.md"]
        for folder in ("笔记", "笔记/历史档案", "共享摘录"):
            d = root / folder
            if d.is_dir():
                curated_files.extend(sorted(d.glob("*.md")))
        for f in curated_files:
            if not f.is_file():
                continue
            hits = 0
            for ch in parse_curated_chunks(f, root):
                if _chunk_matches(ch, term_groups, pat, is_regex):
                    ch["snippet"] = make_snippet(ch["body"], variants_flat, None if not is_regex else pat)
                    results.append(ch)
                    hits += 1
                    if hits >= per_conv or len(results) >= limit:
                        break
            if len(results) >= limit:
                return results[:limit]

    if source != "notes":
        allowed = {source} if source in ARCHIVE_SOURCES else set(ARCHIVE_SOURCES)
        manifests = load_manifests(root)
        for meta in manifests:
            if meta.get("source") not in allowed:
                continue
            if since and (meta.get("updated_at") or "")[:10] < since:
                continue
            if cwd and cwd.lower() not in (meta.get("cwd") or "").lower():
                continue
            md_path = root / meta.get("path", "")
            if not md_path.is_file():
                continue
            hits = 0
            for ch in parse_conversation_messages(md_path, root, meta):
                if role != "all" and ch["role"] != role:
                    continue
                if _chunk_matches(ch, term_groups, pat, is_regex):
                    ch["snippet"] = make_snippet(ch["body"], variants_flat, None if not is_regex else pat)
                    results.append(ch)
                    hits += 1
                    if hits >= per_conv or len(results) >= limit:
                        break
            if len(results) >= limit:
                break

    return results[:limit]


def main():
    parser = argparse.ArgumentParser(description="Search Agent Memory KB (FTS5 + Local ONNX Hybrid)")
    parser.add_argument("--query", "-q", help="Keywords (space-separated AND + synonyms) or regex")
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("AGENT_MEMORY_KB_ROOT", Path.home() / "personal-kb")))
    parser.add_argument("--source", choices=["all", "notes", "codex", "chatgpt", "cursor", "antigravity", "claude"], default="all")
    parser.add_argument("--role", choices=["all", "user", "assistant"], default="all")
    parser.add_argument("--cwd", default="", help="Substring filter for workspace cwd")
    parser.add_argument("--since", default="", help="Filter conversations updated on or after YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--per-conv", type=int, default=2, help="Max hits returned per file/conversation")
    parser.add_argument("--mode", choices=["hybrid", "fts", "vector"], default="hybrid", help="Retrieval engine mode")
    parser.add_argument("--rebuild-index", action="store_true", help="Incrementally update or build search_index.sqlite")
    parser.add_argument("--vacuum", action="store_true", help="Force FTS5 optimize + incremental_vacuum")
    parser.add_argument("--stats", action="store_true", help="Print SQLite storage and schema statistics")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.stats:
        print(json.dumps(get_search_db_stats(args.root), ensure_ascii=False, indent=2))
        if not args.rebuild_index and not args.vacuum and not args.query:
            return

    if args.rebuild_index or args.vacuum:
        stats: dict = {}
        build_search_index(args.root, stats_out=stats, force_compact=args.vacuum)
        if not args.query:
            print(json.dumps(stats, ensure_ascii=False))
            return

    if not args.query:
        parser.error("the following arguments are required: --query/-q (unless --rebuild-index/--vacuum/--stats)")

    results = search_kb(
        root=args.root,
        query=args.query,
        source=args.source,
        role=args.role,
        cwd=args.cwd,
        since=args.since,
        limit=args.limit,
        per_conv=args.per_conv,
        mode=args.mode,
    )

    if args.json:
        print(json.dumps({"query": args.query, "count": len(results), "results": results}, ensure_ascii=False, indent=2))
        return

    print(f"# 知识库检索结果：`{args.query}`（返回前 {len(results)} 条，按最新时间优先）\n")
    for idx, r in enumerate(results, 1):
        link = f"[{r['path']}#L{r['line']}]({Path(r['abs_path']).resolve().as_uri()}#L{r['line']})"
        if r["tier"] == "curated":
            print(f"{idx}. **[精炼活页/档案]** {link} · 章节：`{r['section']}`")
        else:
            cwd_tag = f" · cwd:`{r['cwd']}`" if r.get("cwd") else ""
            print(f"{idx}. **[{r['source'].upper()}]** {r['title']} ({link}) · `{r['section']}` · {r['time']}{cwd_tag}")
        clean_snip = re.sub(r"\s+", " ", r["snippet"])
        print(f"   > {clean_snip}\n")


if __name__ == "__main__":
    main()
