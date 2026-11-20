#!/usr/bin/env python3
"""Incremental local ONNX Transformer vector indexer & semantic search engine.

Uses the cached multilingual bi-encoder (paraphrase-multilingual-MiniLM-L12-v2, dim=384)
to embed all curated L2/L3/L4 knowledge sections and representative archive conversation
turns into _同步/vectors.sqlite, keeping sync incremental by file signature.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

try:
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer
except ImportError:  # pragma: no cover - graceful fallback when runtime lacks ONNX
    np = None
    ort = None
    Tokenizer = None

DEFAULT_MODEL_DIR = Path(os.environ.get(
    "AGENT_MEMORY_KB_MODEL_DIR",
    Path.home() / ".cache" / "magpie" / "paraphrase-multilingual-MiniLM-L12-v2" / "2c4055b12046",
)).expanduser()
EMBED_DIM = 384
MAX_CHUNKS_PER_ARCHIVE_CONV = 24
VECTOR_SCHEMA_VERSION = "2"
_VEC_CACHE: dict[str, tuple[int, list[tuple], "np.ndarray"]] = {}


def _clean_for_embedding(raw: str) -> str:
    txt = (raw or "").strip()
    if "## My request:" in txt:
        txt = txt.split("## My request:", 1)[1].strip()
    txt = txt.replace("[非文本附件：保留在原始会话中，本次文本归档未复制附件。]", "").strip()
    txt = re.sub(r"\s+", " ", txt)
    return txt[:800]


class LocalOnnxEmbedder:
    def __init__(self, model_dir: Path = DEFAULT_MODEL_DIR, max_length: int = 256):
        self.model_dir = Path(model_dir)
        self.max_length = max_length
        self._tok = None
        self._sess = None
        self._input_names: list[str] = []

    def is_available(self) -> bool:
        return (
            np is not None
            and ort is not None
            and Tokenizer is not None
            and (self.model_dir / "model_int8.onnx").is_file()
            and (self.model_dir / "tokenizer.json").is_file()
        )

    def _ensure_loaded(self) -> None:
        if self._sess is not None:
            return
        if not self.is_available():
            raise RuntimeError(f"ONNX semantic model unavailable at {self.model_dir}")
        self._tok = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
        self._tok.enable_truncation(max_length=self.max_length)
        self._tok.enable_padding(length=None)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 4
        self._sess = ort.InferenceSession(
            str(self.model_dir / "model_int8.onnx"),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        self._input_names = [i.name for i in self._sess.get_inputs()]

    def encode(self, texts: list[str], batch_size: int = 32) -> "np.ndarray":
        self._ensure_loaded()
        if not texts:
            return np.zeros((0, EMBED_DIM), dtype=np.float32)
        # Sort by char length to minimize batch padding waste, then restore original order
        indexed = sorted(enumerate(texts), key=lambda x: len(x[1]))
        out_vecs = np.zeros((len(texts), EMBED_DIM), dtype=np.float32)
        for start in range(0, len(indexed), batch_size):
            chunk = indexed[start : start + batch_size]
            encs = self._tok.encode_batch([t for _, t in chunk])
            ids = np.array([e.ids for e in encs], dtype=np.int64)
            mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
            feed = {}
            for name in self._input_names:
                if "input_ids" in name:
                    feed[name] = ids
                elif "attention_mask" in name:
                    feed[name] = mask
                elif "token_type" in name:
                    feed[name] = np.zeros_like(ids)
            hidden = self._sess.run(None, feed)[0]
            m_exp = mask[:, :, None].astype(np.float32)
            pooled = (hidden * m_exp).sum(axis=1) / np.clip(m_exp.sum(axis=1), 1e-9, None)
            norms = np.linalg.norm(pooled, axis=1, keepdims=True)
            normed = (pooled / np.clip(norms, 1e-9, None)).astype(np.float32)
            for row_idx, (orig_idx, _) in enumerate(chunk):
                out_vecs[orig_idx] = normed[row_idx]
        return out_vecs


def _init_vec_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (VECTOR_SCHEMA_VERSION,))
    conn.execute(
        """
        CREATE TABLE synced_files (
            path TEXT PRIMARY KEY,
            sig TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE doc_vecs (
            line INTEGER NOT NULL,
            path TEXT NOT NULL,
            tier TEXT NOT NULL,
            source TEXT NOT NULL,
            role TEXT NOT NULL,
            cwd TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            dim INTEGER NOT NULL,
            vec BLOB NOT NULL,
            PRIMARY KEY(path, line)
        )
        """
    )


def _vec_schema_current(vpath: Path) -> bool:
    if not vpath.is_file():
        return False
    try:
        c = sqlite3.connect(vpath.resolve().as_uri() + "?mode=ro", uri=True, timeout=10.0)
        row = c.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        c.close()
        return bool(row and row[0] == VECTOR_SCHEMA_VERSION)
    except Exception:
        return False


def _sample_timeline(items: list[tuple], cap: int) -> list[tuple]:
    if len(items) <= cap or cap <= 0:
        return items[:cap]
    if cap <= 2:
        return [items[0], items[-1]][:cap]
    head = items[:2]
    tail = items[-2:]
    middle = items[2:-2]
    rem = cap - len(head) - len(tail)
    if rem <= 0 or not middle:
        return (head + tail)[:cap]
    step = len(middle) / float(rem)
    picked_mid = [middle[min(len(middle) - 1, int(i * step))] for i in range(rem)]
    return head + picked_mid + tail


def _select_representative_chunks(chunks: list[tuple]) -> list[tuple[int, str]]:
    """Select curated chunks or up to MAX_CHUNKS_PER_ARCHIVE_CONV timeline-distributed turns for embedding."""
    selected: list[tuple[int, str]] = []
    for line, tier, title, section, role, body in chunks:
        cleaned = _clean_for_embedding(body)
        if len(cleaned) < 14:
            continue
        if tier == "curated":
            selected.append((line, f"{title} / {section}: {cleaned}"))
        else:
            if role == "meta" or "会话上下文或控制标记" in (section or ""):
                continue
            label = f"{title} / {section}" if "· 问:" in (section or "") else title
            selected.append((line, f"{label}: {cleaned}"))
            if len(selected) >= MAX_CHUNKS_PER_ARCHIVE_CONV:
                break
    return selected


def sync_vectors(
    root: Path,
    embedder: LocalOnnxEmbedder | None = None,
    encode_fn=None,
) -> dict:
    """Incrementally sync vector embeddings from search_index.sqlite into _同步/vectors.sqlite."""
    root = root.resolve()
    search_db = root / "_同步" / "search_index.sqlite"
    vec_db = root / "_同步" / "vectors.sqlite"
    if not search_db.is_file():
        return {"status": "missing_search_db", "embedded": 0, "total_vectors": 0}

    eng = embedder or LocalOnnxEmbedder()
    if encode_fn is None and not eng.is_available():
        return {"status": "model_unavailable", "embedded": 0, "total_vectors": 0}

    s_conn = sqlite3.connect(search_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10.0)
    try:
        file_rows = s_conn.execute("SELECT id, tier, source, path, title, cwd, updated_at, sig FROM files").fetchall()
        current_files = {r[3]: r for r in file_rows}
    except Exception as exc:
        s_conn.close()
        return {"status": f"read_error:{exc}", "embedded": 0, "total_vectors": 0}

    need_full = not _vec_schema_current(vec_db)
    if need_full:
        tmp_vec = root / "_同步" / "vectors.sqlite.tmp"
        tmp_vec.unlink(missing_ok=True)
        v_conn = sqlite3.connect(str(tmp_vec), timeout=10.0)
        _init_vec_schema(v_conn)
        prev_sigs: dict[str, str] = {}
    else:
        v_conn = sqlite3.connect(str(vec_db), timeout=10.0)
        prev_sigs = dict(v_conn.execute("SELECT path, sig FROM synced_files").fetchall())

    removed = [p for p in prev_sigs if p not in current_files]
    changed = [p for p, row in current_files.items() if prev_sigs.get(p) != row[7]]

    pending_docs: list[tuple[int, str, str, str, str, str, str, str]] = []
    for p in changed:
        fid, tier, source, path, title, cwd, updated_at, sig = current_files[p]
        doc_rows = s_conn.execute(
            "SELECT line, section, role, body FROM docs WHERE file_id = ? ORDER BY id ASC",
            (fid,),
        ).fetchall()
        raw_list = [(line, tier, title, sec, role, body) for line, sec, role, body in doc_rows]
        # Build timeline-distributed user + assistant sampling for archive files (covers multi-topic switches)
        if tier == "archive":
            user_turns = [x for x in raw_list if x[4] == "user" and "会话上下文或控制标记" not in (x[3] or "") and len(_clean_for_embedding(x[5])) >= 14]
            asst_turns = [x for x in raw_list if x[4] == "assistant" and len(_clean_for_embedding(x[5])) >= 40]
            if not asst_turns:
                asst_turns = [x for x in raw_list if x[4] == "assistant" and len(_clean_for_embedding(x[5])) >= 14]
            ordered = _sample_timeline(user_turns, 14) + _sample_timeline(asst_turns, 10)
        else:
            ordered = raw_list
        role_map = {line: role for line, _, _, _, role, _ in raw_list}
        for line, embed_txt in _select_representative_chunks(ordered):
            pending_docs.append((line, path, tier, source, role_map.get(line, "note"), cwd, updated_at, embed_txt))
    s_conn.close()

    vectors = None
    if pending_docs:
        texts = [item[7] for item in pending_docs]
        vectors = encode_fn(texts) if encode_fn is not None else eng.encode(texts)

    with v_conn:
        for p in removed + changed:
            v_conn.execute("DELETE FROM doc_vecs WHERE path = ?", (p,))
            v_conn.execute("DELETE FROM synced_files WHERE path = ?", (p,))
        if pending_docs and vectors is not None:
            for idx, (line, path, tier, source, role, cwd, updated_at, _) in enumerate(pending_docs):
                blob = np.asarray(vectors[idx], dtype=np.float16).tobytes()
                v_conn.execute(
                    "INSERT INTO doc_vecs(line, path, tier, source, role, cwd, updated_at, dim, vec) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (line, path, tier, source, role, cwd, updated_at, int(vectors.shape[1]), blob),
                )
        for p in changed:
            v_conn.execute("INSERT INTO synced_files(path, sig) VALUES (?, ?)", (p, current_files[p][7]))

    total_vecs = v_conn.execute("SELECT count(*) FROM doc_vecs").fetchone()[0]
    freelist = v_conn.execute("PRAGMA freelist_count").fetchone()[0]
    v_conn.close()
    if need_full:
        os.replace(tmp_vec, vec_db)
    elif freelist >= 32:
        vc = sqlite3.connect(str(vec_db), timeout=10.0, isolation_level=None)
        vc.execute(f"PRAGMA incremental_vacuum({freelist})").fetchall()
        vc.close()

    return {
        "status": "ok",
        "mode": "full_vec" if need_full else "incremental_vec",
        "updated_files": len(changed),
        "removed_files": len(removed),
        "embedded": len(pending_docs),
        "total_vectors": total_vecs,
        "db_bytes": vec_db.stat().st_size if vec_db.is_file() else 0,
    }


def query_vectors(
    root: Path,
    query: str,
    source: str = "all",
    role: str = "all",
    cwd: str = "",
    since: str = "",
    limit: int = 20,
    per_conv: int = 2,
    embedder: LocalOnnxEmbedder | None = None,
    encode_fn=None,
) -> list[dict]:
    """Return top semantic cosine matches from _同步/vectors.sqlite joined with search_index.sqlite."""
    root = root.resolve()
    vec_db = root / "_同步" / "vectors.sqlite"
    search_db = root / "_同步" / "search_index.sqlite"
    if np is None or not _vec_schema_current(vec_db) or not search_db.is_file():
        return []

    eng = embedder or LocalOnnxEmbedder()
    if encode_fn is None:
        if not eng.is_available():
            return []
        qvec = eng.encode([query])[0]
    else:
        qvec = np.asarray(encode_fn([query])[0], dtype=np.float32)

    cache_key = str(vec_db)
    mtime_ns = vec_db.stat().st_mtime_ns
    cached = _VEC_CACHE.get(cache_key)
    if cached is not None and cached[0] == mtime_ns:
        all_meta, full_mat = cached[1], cached[2]
    else:
        v_conn = sqlite3.connect(vec_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10.0)
        try:
            raw_rows = v_conn.execute(
                "SELECT line, path, tier, source, role, cwd, updated_at, sig, vec "
                "FROM doc_vecs JOIN synced_files USING(path)"
            ).fetchall()
        finally:
            v_conn.close()
        if not raw_rows:
            return []
        all_meta = [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]) for r in raw_rows]
        full_mat = np.vstack([np.frombuffer(r[8], dtype=np.float16).astype(np.float32) for r in raw_rows])
        _VEC_CACHE[cache_key] = (mtime_ns, all_meta, full_mat)

    if not all_meta or full_mat.shape[1] != qvec.shape[0]:
        return []

    cwd_low = cwd.lower() if cwd else ""
    if source == "all" and role == "all" and not cwd_low and not since:
        rows = all_meta
        mat = full_mat
    else:
        keep_idx = []
        rows = []
        for i, m in enumerate(all_meta):
            _, _, tier, src, d_role, d_cwd, upd, _ = m
            if source == "notes" and tier != "curated":
                continue
            if source not in ("all", "notes") and src != source:
                continue
            if role != "all" and d_role != role:
                continue
            if cwd_low and cwd_low not in (d_cwd or "").lower():
                continue
            if since and tier != "curated" and (upd or "")[:10] < since:
                continue
            keep_idx.append(i)
            rows.append(m)
        if not rows:
            return []
        mat = full_mat[keep_idx]

    sims = mat @ qvec.astype(np.float32)
    order = np.argsort(-sims)

    # Fetch rich doc metadata/body cleanly from search_index.sqlite
    s_conn = sqlite3.connect(search_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10.0)
    s_conn.row_factory = sqlite3.Row
    results = []
    conv_counts: dict[str, int] = {}
    eff_per_conv = max(1, per_conv)
    try:
        for idx in order:
            sim = float(sims[idx])
            if sim <= 0.15:
                break
            line, path, tier, src, d_role, d_cwd, upd, sig = rows[idx]
            if conv_counts.get(path, 0) >= eff_per_conv:
                continue
            drow = s_conn.execute(
                "SELECT d.id, f.title, d.section, d.time, d.line, d.body "
                "FROM docs d JOIN files f ON d.file_id = f.id WHERE f.path = ? AND f.sig = ? AND d.line = ?",
                (path, sig, line),
            ).fetchone()
            if not drow:
                continue
            conv_counts[path] = conv_counts.get(path, 0) + 1
            clean_body = re.sub(r"\s+", " ", drow["body"]).strip()
            results.append({
                "doc_id": drow["id"],
                "tier": tier,
                "source": src,
                "path": path,
                "abs_path": str(root / path),
                "title": drow["title"],
                "cwd": d_cwd,
                "updated_at": upd,
                "section": drow["section"],
                "role": d_role,
                "time": drow["time"],
                "line": drow["line"],
                "snippet": clean_body[:320],
                "score": round(sim, 4),
            })
            if len(results) >= limit:
                break
    finally:
        s_conn.close()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Local ONNX vector indexer & semantic search helper.")
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("AGENT_MEMORY_KB_ROOT", Path.home() / "personal-kb")))
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--query", "-q", default="")
    parser.add_argument("--source", default="all")
    parser.add_argument("--role", default="all")
    parser.add_argument("--cwd", default="")
    parser.add_argument("--since", default="")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--per-conv", type=int, default=2)
    args = parser.parse_args()

    if args.sync:
        print(json.dumps(sync_vectors(args.root), ensure_ascii=False))
    elif args.query:
        res = query_vectors(
            args.root, args.query,
            source=args.source, role=args.role, cwd=args.cwd, since=args.since,
            limit=args.limit, per_conv=args.per_conv,
        )
        print(json.dumps({"results": res}, ensure_ascii=False))
    else:
        vec_db = args.root.resolve() / "_同步" / "vectors.sqlite"
        cnt = 0
        if vec_db.is_file():
            c = sqlite3.connect(vec_db.resolve().as_uri() + "?mode=ro", uri=True)
            cnt = c.execute("SELECT count(*) FROM doc_vecs").fetchone()[0]
            c.close()
        print(json.dumps({"available": LocalOnnxEmbedder().is_available(), "total_vectors": cnt}, ensure_ascii=False))


if __name__ == "__main__":
    main()
