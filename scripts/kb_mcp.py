#!/usr/bin/env python3
"""Read-only local MCP server exposing Agent Memory KB to Coding Agents."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - allow importing data helpers on runtimes without mcp
    class FastMCP:  # type: ignore[no-redef]
        def __init__(self, name: str):
            self.name = name

        def tool(self):
            def _decorator(fn):
                return fn
            return _decorator

        def run(self, transport: str = "stdio"):
            raise RuntimeError("mcp package is not installed in this Python interpreter")

SYNC_DIR = Path(__file__).resolve().parent
DEFAULT_KB_ROOT = Path(os.environ.get("AGENT_MEMORY_KB_ROOT") or SYNC_DIR.parent).expanduser().resolve()
if str(SYNC_DIR) not in sys.path:
    sys.path.insert(0, str(SYNC_DIR))

import kb_embed  # noqa: E402
import kb_retrospect  # noqa: E402
import kb_search  # noqa: E402

SYS_PYTHON = "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else sys.executable
_EMBEDDER: kb_embed.LocalOnnxEmbedder | None = None

mcp = FastMCP("personal-kb")


def _get_embedder() -> kb_embed.LocalOnnxEmbedder:
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = kb_embed.LocalOnnxEmbedder()
    return _EMBEDDER


def _fts_via_sys_python(
    root: Path,
    query: str,
    source: str,
    role: str,
    cwd: str,
    since: str,
    limit: int,
    per_conv: int = 2,
) -> list[dict]:
    db_path = root / "_同步" / "search_index.sqlite"
    if db_path.is_file():
        try:
            return kb_search._search_via_sqlite(root, db_path, query, source, role, cwd, since, limit, per_conv)
        except Exception:
            pass
    cmd = [
        SYS_PYTHON,
        str(SYNC_DIR / "kb_search.py"),
        "--root", str(root),
        "--query", query,
        "--source", source,
        "--role", role,
        "--cwd", cwd,
        "--since", since,
        "--limit", str(limit),
        "--per-conv", str(per_conv),
        "--mode", "fts",
        "--json",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, check=False)
    if res.returncode != 0 or not res.stdout.strip():
        return []
    return json.loads(res.stdout).get("results", [])


def kb_overview_data(root: Path = DEFAULT_KB_ROOT) -> dict:
    root = root.resolve()
    index_path = root / "INDEX.md"
    report_path = root / "_同步" / "同步报告.md"
    health_lines: list[str] = []
    if report_path.is_file():
        in_health = False
        for line in report_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("## 知识库健康度巡检"):
                in_health = True
                continue
            if in_health and line.startswith("## "):
                break
            if in_health and line.strip():
                health_lines.append(line.strip())
    alert_lines = [
        ln for ln in health_lines
        if any(bad in ln for bad in ("未通过", "失败", "告警", "超标", "ERROR", "WARN"))
        or re.search(r"\*\*[1-9]\d*\s*(?:个|处)", ln)
    ]
    if health_lines and not alert_lines:
        health_lines = ["healthy"]
    elif alert_lines:
        health_lines = alert_lines
    return {
        "kb_root": str(root),
        "index_md": index_path.read_text(encoding="utf-8", errors="replace") if index_path.is_file() else "",
        "guardrails": kb_retrospect.extract_top_guardrails(root),
        "health_summary": health_lines,
        "next_hop_hint": (
            "Hop 2: Call kb_project_context(cwd_or_project) for active repo breakpoints & recent turns, "
            "kb_mistakes_and_guardrails() for full mistake patterns, or kb_search_query(query) for historical quotes."
        ),
    }


def _compact_json(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _trim_hits(hits: list[dict]) -> list[dict]:
    cleaned: list[dict] = []
    for h in hits:
        cleaned.append({k: v for k, v in h.items() if k not in ("abs_path", "tier") and v not in ("", None, [])})
    return cleaned


def kb_search_data(
    query: str,
    mode: str = "hybrid",
    source: str = "all",
    role: str = "all",
    cwd: str = "",
    since: str = "",
    limit: int = 8,
    per_conv: int = 2,
    root: Path = DEFAULT_KB_ROOT,
) -> dict:
    root = root.resolve()
    eff_limit = max(1, min(30, int(limit)))
    eff_per_conv = max(1, min(5, int(per_conv)))
    is_regex = bool(kb_search.REGEX_META_RE.search(query))

    fts_hits: list[dict] = []
    if mode in ("hybrid", "fts") or is_regex:
        fts_hits = _fts_via_sys_python(
            root, query, source, role, cwd, since,
            eff_limit if (mode == "fts" or is_regex) else max(24, eff_limit * 3),
            eff_per_conv,
        )
    if mode == "fts" or is_regex:
        trimmed = _trim_hits(fts_hits[:eff_limit])
        return {"query": query, "mode": "fts", "count": len(trimmed), "results": trimmed}

    emb = _get_embedder()
    vec_hits = kb_embed.query_vectors(
        root, query,
        source=source, role=role, cwd=cwd, since=since,
        limit=max(24, eff_limit * 3), per_conv=eff_per_conv,
        embedder=emb if emb.is_available() else None,
    )
    if mode == "vector":
        trimmed = _trim_hits(vec_hits[:eff_limit])
        return {"query": query, "mode": "vector", "count": len(trimmed), "results": trimmed}
    if not vec_hits:
        trimmed = _trim_hits(fts_hits[:eff_limit])
        return {"query": query, "mode": "fts_fallback", "count": len(trimmed), "results": trimmed}
    fused = kb_search.rrf_fuse(fts_hits, vec_hits, limit=eff_limit, per_conv=eff_per_conv)
    trimmed = _trim_hits(fused)
    return {"query": query, "mode": "hybrid", "count": len(trimmed), "results": trimmed}


def kb_project_context_data(
    cwd_or_project: str,
    root: Path = DEFAULT_KB_ROOT,
    include_recent: bool = True,
) -> dict:
    root = root.resolve()
    norm_cwd = (cwd_or_project or "").replace("\\", "/").rstrip("/")
    key = norm_cwd.rsplit("/", 1)[-1].lower() if "/" in norm_cwd else norm_cwd.strip().lower()
    syn_map = kb_search.load_synonyms(root)
    key_variants = list(dict.fromkeys([key] + [v.lower() for v in kb_search.expand_term_variants(key, syn_map)])) if key else []
    proj_file = root / "笔记" / "进行中的项目.md"
    git_rows: list[str] = []
    matched_sections: list[str] = []

    known_projects = set()
    if proj_file.is_file() and key_variants:
        text = proj_file.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            low_ln = line.lower()
            if line.startswith("| `"):
                known_projects.add(low_ln.split("`", 2)[1])
                if any(kv in low_ln for kv in key_variants):
                    git_rows.append(line)
            elif line.startswith("| **") and any(kv in low_ln for kv in key_variants):
                matched_sections.append(line.strip())
        for sec in re.split(r"(?=^###\s+)", text, flags=re.M):
            if sec.startswith("### ") and any(kv in sec.lower() for kv in key_variants):
                matched_sections.append(sec.strip())

    def _is_valid_project_chat_hit(h: dict) -> bool:
        if h.get("source") == "note" or h.get("tier") == "curated":
            return False
        if h.get("role") == "meta" or h.get("section") == "文件头部":
            return False
        t_low = (h.get("title") or "").strip().lower()
        if t_low in ("你好", "您好", "hi", "hello", "未命名对话", "未命名 antigravity 对话"):
            return False
        title_matches_key = any(kv in t_low for kv in key_variants)
        # Rule A: If conversation has a specific repo cwd, it must not conflict with the queried project
        h_cwd = (h.get("cwd") or "").strip()
        if h_cwd and not title_matches_key:
            h_cwd_norm = h_cwd.replace("\\", "/").rstrip("/")
            cwd_slug = h_cwd_norm.rsplit("/", 1)[-1].lower() if "/" in h_cwd_norm else h_cwd_norm.lower()
            if cwd_slug not in ("", Path.home().name.lower(), "工作", "个人") and not any(kv in h_cwd_norm.lower() for kv in key_variants):
                return False
        # Rule B: Exclude cross-project overview/inventory dumps that list many unrelated projects
        if not title_matches_key:
            if any(meta_kw in t_low for meta_kw in ("个人知识库", "调用我的知识库", "作为codex的影子", "这是被封了吗")):
                return False
            snip_low = (h.get("snippet") or "").lower()
            other_hits = sum(1 for kp in known_projects if kp not in key_variants and kp in snip_low)
            if other_hits >= 2:
                return False
        return True

    recent_res: list[dict] = []
    if key and include_recent:
        seen_paths: set[str] = set()
        for kv in key_variants:
            hits = _fts_via_sys_python(root, kv, "all", "all", kv, "", 12, 2)
            for h in hits:
                if _is_valid_project_chat_hit(h) and h.get("path") not in seen_paths:
                    recent_res.append(h)
                    seen_paths.add(h.get("path", ""))
                    if len(recent_res) >= 4:
                        break
            if len(recent_res) >= 4:
                break
        if len(recent_res) < 4:
            for kv in key_variants:
                broad_hits = _fts_via_sys_python(root, kv, "all", "all", "", "", 12, 2)
                for h in broad_hits:
                    if _is_valid_project_chat_hit(h) and h.get("path") not in seen_paths:
                        recent_res.append(h)
                        seen_paths.add(h.get("path", ""))
                        if len(recent_res) >= 4:
                            break
                if len(recent_res) >= 4:
                    break
        if not recent_res and not matched_sections:
            hyb_hits = kb_search_data(key, mode="hybrid", limit=12, per_conv=1, root=root).get("results", [])
            for h in hyb_hits:
                if "fts" not in (h.get("match_via") or "") and float(h.get("score") or 0.0) < 0.55:
                    continue
                if _is_valid_project_chat_hit(h) and h.get("path") not in seen_paths:
                    recent_res.append(h)
                    seen_paths.add(h.get("path", ""))
                    if len(recent_res) >= 4:
                        break

    payload: dict = {
        "query": cwd_or_project,
        "matched_key": key,
        "git_status_rows": git_rows,
        "project_sections": matched_sections,
    }
    if recent_res:
        payload["recent_hits"] = _trim_hits(recent_res[:4])
    payload["next_hop_hint"] = (
        "Call kb_read_note(rel_path, start_line, max_lines, section) to inspect any matched note or conversation in full, "
        "or kb_search_query(query) to search deeper across all 5 archived AI sources."
    )
    return payload


def kb_mistakes_data(
    include_pending: bool = True,
    full_book: bool = True,
    root: Path = DEFAULT_KB_ROOT,
) -> dict:
    root = root.resolve()
    book = root / "笔记" / "AI协作错题本.md"
    qfile = root / "_同步" / "retrospect-queue.json"
    pending = []
    if include_pending and qfile.is_file():
        try:
            pending = json.loads(qfile.read_text(encoding="utf-8")).get("candidates", [])
        except Exception:
            pending = []
    payload: dict = {"guardrails": kb_retrospect.extract_top_guardrails(root)}
    if full_book and book.is_file():
        payload["mistake_book"] = book.read_text(encoding="utf-8", errors="replace")
    if include_pending:
        payload["pending_candidates"] = pending
    return payload


def kb_read_note_data(
    rel_path: str,
    start_line: int = 1,
    max_lines: int = 160,
    section: str = "",
    root: Path = DEFAULT_KB_ROOT,
) -> dict:
    root = root.resolve()
    raw = (rel_path or "").replace("\\", "/").strip().lstrip("/")
    target = (root / raw).resolve()
    try:
        rel_clean = target.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"Access denied: path outside KB root ({rel_path})") from exc
    if target.suffix.lower() != ".md" or any(part.startswith(".") for part in target.relative_to(root).parts):
        raise ValueError("Only Markdown notes outside hidden directories are readable")
    if not target.is_file():
        raise FileNotFoundError(f"KB note not found: {rel_clean}")
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    s_idx = max(1, int(start_line))
    sec_q = (section or "").lstrip("#").strip().lower()
    if sec_q and s_idx == 1:
        matched_line = 0
        for idx, ln in enumerate(lines, 1):
            if ln.startswith("#") and sec_q in ln.lstrip("#").strip().lower():
                matched_line = idx
                break
        if not matched_line:
            for idx, ln in enumerate(lines, 1):
                if sec_q in ln.lower():
                    matched_line = idx
                    break
        if matched_line:
            s_idx = matched_line
    e_idx = min(len(lines), s_idx + max(1, min(400, int(max_lines))) - 1)
    sliced = [f"{i}: {lines[i - 1]}" for i in range(s_idx, e_idx + 1)]
    return {
        "path": str(rel_clean),
        "total_lines": len(lines),
        "start_line": s_idx,
        "end_line": e_idx,
        "content": "\n".join(sliced),
    }


@mcp.tool()
def kb_overview() -> str:
    """WHEN TO USE: Call first at the start of a task to load the progressive-disclosure routing map (INDEX.md), Top guardrails, and KB health status.
    RETURNS: Compact JSON with index_md, guardrails, health_summary, and next_hop_hint."""
    return _compact_json(kb_overview_data())


@mcp.tool()
def kb_search_query(
    query: str,
    mode: str = "hybrid",
    source: str = "all",
    role: str = "all",
    cwd: str = "",
    since: str = "",
    limit: int = 8,
) -> str:
    """WHEN TO USE: Call when you need historical decisions, exact user quotes, API contracts, or past bug-fix discussions across all 5 archived AI tools (Codex, Cursor, Claude Code, Antigravity, ChatGPT) and curated notes.
    ARGS: query (keywords or natural-language question), mode ('hybrid' RRF default, 'fts' for exact tokens/regex, 'vector' for semantic), source ('all'|'notes'|'codex'|'cursor'|'antigravity'|'claude'|'chatgpt'), limit (default 8).
    RETURNS: Ranked hits with relative path, line number, updated_at date, and grounded snippet (follow up with kb_read_note(path, start_line) for full context)."""
    return _compact_json(
        kb_search_data(query=query, mode=mode, source=source, role=role, cwd=cwd, since=since, limit=limit)
    )


@mcp.tool()
def kb_project_context(cwd_or_project: str, include_recent: bool = True) -> str:
    """WHEN TO USE: Call when starting or resuming work on a specific repository or project (pass the workspace directory name or project slug, e.g. 'my-app').
    RETURNS: Live Git branch/dirty probe rows, curated project breakpoints (confirmed contracts, verification boundaries, next steps), and top 4 recent archived conversation hits for that project."""
    return _compact_json(kb_project_context_data(cwd_or_project, include_recent=include_recent))


@mcp.tool()
def kb_mistakes_and_guardrails(include_pending: bool = True, full_book: bool = True) -> str:
    """WHEN TO USE: Call before multi-file edits, UI/DOM/style changes, or external publishing, or when reviewing recurring AI collaboration mistakes.
    RETURNS: Top guardrails, the complete mistake book with [复发 xN] root-cause patterns, and any pending high-friction sessions awaiting retrospect."""
    return _compact_json(kb_mistakes_data(include_pending=include_pending, full_book=full_book))


@mcp.tool()
def kb_read_note(rel_path: str, start_line: int = 1, max_lines: int = 160, section: str = "") -> str:
    """WHEN TO USE: Call to read a specific living note (e.g. '笔记/进行中的项目.md', '笔记/财富与投资纪律.md') or an archived conversation file returned by kb_search_query.
    ARGS: rel_path (path relative to KB root), start_line (1-based), max_lines (default 160, max 400), section (optional heading or keyword to jump directly to).
    RETURNS: Line-numbered plain text strictly confined inside the KB root."""
    d = kb_read_note_data(rel_path=rel_path, start_line=start_line, max_lines=max_lines, section=section)
    return f"# {d['path']} (lines {d['start_line']}-{d['end_line']}/{d['total_lines']})\n{d['content']}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
