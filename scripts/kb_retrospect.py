#!/usr/bin/env python3
"""Automatic multi-turn AI correction detector & mistake-book queue manager.

Scans archived user turns in search_index.sqlite across Codex, ChatGPT, Cursor,
Antigravity, and Claude Code, strips IDE/heartbeat wrappers, identifies sessions
where the user had to repeatedly push back or correct the AI (>= 2 turns), and
keeps 笔记/AI协作错题本.md's pending review block + top guardrails up to date.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

CST_TZ = timezone(timedelta(hours=8), "CST")

SYNC_DIR = Path(__file__).resolve().parent
if str(SYNC_DIR) not in sys.path:
    sys.path.insert(0, str(SYNC_DIR))

from kb_filters import atomic_write_text  # noqa: E402

QUEUE_REL = "_同步/retrospect-queue.json"
MISTAKE_BOOK_REL = "笔记/AI协作错题本.md"
MAX_DISPLAY_CANDIDATES = 5

STRONG_CORR_RE = re.compile(
    r"(你理解错|理解错了|弄错了|搞错了|不是这个意思|不是这样|不是让你|不是这个|"
    r"你的修改不对|还是不对|完全不对|没改对|还是有(?:点|问题|报错)|还是报错|还是没有|还是不行|仍然不行|"
    r"你根本没|根本没有|完全没|并没有|没对齐|"
    r"为什么又|怎么又|怎么还是|为啥还是|为什么没有|"
    r"别猜|不要猜|先停一下|先停手|不要在错误的|"
    r"我刚才说|我说的是|按我说的执行|按照我说的|"
    r"遗漏了吧|漏了|没看到你|没有生效|未生效|不生效|还在展示)"
)


def extract_real_user_text(body: str) -> str:
    """Strip heartbeat, environment XML, and IDE context wrappers from archived user text."""
    txt = (body or "").strip()
    if not txt or txt.startswith(("<heartbeat>", "<environment_context>", "# AGENTS.md", "<system")):
        return ""
    if "## My request:" in txt:
        txt = txt.split("## My request:", 1)[1].strip()
    if txt.startswith("<send_user_message_question_reply>"):
        answers = re.findall(r'"answers"\s*:\s*\[([^\]]+)\]', txt)
        txt = " ".join(answers) if answers else ""
    return txt.strip()


def _clean_md_cell(text: str, limit: int = 72) -> str:
    collapsed = " ".join((text or "").split()).replace("|", "\\|").replace("[", "(").replace("]", ")")
    return (collapsed[: limit - 1] + "…") if len(collapsed) > limit else collapsed


def _load_queue(root: Path) -> dict:
    qpath = root / QUEUE_REL
    if qpath.is_file():
        try:
            data = json.loads(qpath.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("resolved", {})
                return data
        except Exception:
            pass
    return {"resolved": {}}


def scan_correction_candidates(
    root: Path,
    since: str | None = None,
    min_hits: int = 2,
    db_path: Path | None = None,
) -> dict:
    """Scan SQLite archive turns for sessions with repeated AI corrections."""
    prev = _load_queue(root)
    resolved: dict = prev.get("resolved", {})
    sqlite_file = db_path or (root / "_同步" / "search_index.sqlite")
    if not sqlite_file.is_file():
        return {
            "updated_at": prev.get("updated_at"),
            "total_detected": 0,
            "unresolved_count": 0,
            "candidates": [],
            "resolved": resolved,
        }

    conn = sqlite3.connect(sqlite_file.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        sql = (
            "SELECT f.id, f.source, f.path, f.title, f.updated_at, d.body "
            "FROM docs d JOIN files f ON d.file_id = f.id "
            "WHERE f.tier = 'archive' AND d.role = 'user' "
        )
        params: list = []
        if since:
            sql += "AND f.updated_at >= ? "
            params.append(since)
        sql += "ORDER BY f.updated_at DESC, d.id ASC"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    conv_hits: dict[int, list[dict]] = defaultdict(list)
    meta: dict[int, tuple[str, str, str, str]] = {}
    turn_counts: dict[int, int] = defaultdict(int)

    for fid, src, path, title, upd, body in rows:
        meta[fid] = (src, path, title, upd)
        real_txt = extract_real_user_text(body)
        if not real_txt:
            continue
        turn_counts[fid] += 1
        head = "\n".join(real_txt.splitlines()[:6]) if len(real_txt) > 1000 else real_txt
        matches = STRONG_CORR_RE.findall(head)
        if matches:
            conv_hits[fid].append({
                "signals": list(dict.fromkeys(matches)),
                "excerpt": _clean_md_cell(head, limit=96),
            })

    candidates = []
    total_detected = 0
    for fid, hits in conv_hits.items():
        if len(hits) < min_hits:
            continue
        total_detected += 1
        src, path, title, upd = meta[fid]
        res_entry = resolved.get(path)
        if isinstance(res_entry, dict) and len(hits) <= int(res_entry.get("hit_count", 0)):
            continue
        candidates.append({
            "source": src,
            "path": path,
            "title": _clean_md_cell(title, limit=56),
            "updated_at": upd,
            "hit_count": len(hits),
            "total_user_turns": turn_counts[fid],
            "sample_excerpts": [h["excerpt"] for h in hits[:3]],
            "signals": sorted({sig for h in hits for sig in h["signals"]}),
        })

    candidates.sort(key=lambda item: (item["updated_at"], item["hit_count"]), reverse=True)
    return {
        "updated_at": datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "total_detected": total_detected,
        "unresolved_count": len(candidates),
        "candidates": candidates,
        "resolved": resolved,
    }


def _queue_signature(queue: dict) -> dict:
    return {
        "total_detected": queue.get("total_detected", 0),
        "unresolved_count": queue.get("unresolved_count", 0),
        "candidates": queue.get("candidates", []),
        "resolved": queue.get("resolved", {}),
    }


def save_queue(root: Path, queue: dict) -> bool:
    """Persist queue JSON only when substantive fields changed (avoid noisy mtime churn)."""
    qpath = root / QUEUE_REL
    if qpath.is_file():
        try:
            old_data = json.loads(qpath.read_text(encoding="utf-8"))
            if _queue_signature(old_data) == _queue_signature(queue):
                return False
        except Exception:
            pass
    atomic_write_text(qpath, json.dumps(queue, ensure_ascii=False, indent=2) + "\n")
    return True


def update_mistake_book_candidates(root: Path, queue: dict) -> bool:
    """Replace AUTO_RETROSPECT block inside 笔记/AI协作错题本.md if content changed."""
    book_path = root / MISTAKE_BOOK_REL
    if not book_path.is_file():
        return False
    text = book_path.read_text(encoding="utf-8")
    pattern = re.compile(r"(<!-- AUTO_RETROSPECT_START -->).*?(<!-- AUTO_RETROSPECT_END -->)", re.S)
    if not pattern.search(text):
        return False

    cands = queue.get("candidates", [])
    if not cands:
        block = "\n- 当前多轮高摩擦待复盘对话已全部消化，暂无新增未处理条目。\n"
    else:
        shown = cands[:MAX_DISPLAY_CANDIDATES]
        lines = [
            "",
            f"检测到 **{len(cands)}** 条待提炼多轮纠错会话（仅展示最近 {len(shown)} 条；沉淀或 Codex 定时任务消化后运行 `kb_retrospect.py resolve` 自动移出）：",
            "",
        ]
        for item in shown:
            rel_link = f"../{item['path']}"
            snip = item["sample_excerpts"][0] if item.get("sample_excerpts") else ""
            lines.append(
                f"- `{item['updated_at'][:10]}` · `{item['source']}` · **[{item['title']}]({rel_link})** "
                f"（纠偏 {item['hit_count']} 轮 · 关键词：{' / '.join(item['signals'][:4])}）"
                + (f" — 摘录：`{snip}`" if snip else "")
            )
        lines.append("")
        block = "\n".join(lines)

    new_text = pattern.sub(lambda m: f"{m.group(1)}{block}{m.group(2)}", text)
    if new_text == text:
        return False
    atomic_write_text(book_path, new_text)
    return True


def refresh_retrospect(root: Path, since: str | None = None, db_path: Path | None = None) -> tuple[dict, bool]:
    """Scan candidates, persist queue cache, and sync 笔记/AI协作错题本.md pending block."""
    queue = scan_correction_candidates(root, since=since, db_path=db_path)
    save_queue(root, queue)
    book_changed = update_mistake_book_candidates(root, queue)
    return queue, book_changed


def resolve_candidate(root: Path, path: str, note: str = "") -> dict:
    """Mark an archived conversation path as reviewed/absorbed into mistake book or Skill."""
    queue = _load_queue(root)
    rel = path.strip().lstrip("./")
    if rel.startswith("../"):
        rel = rel[3:]

    # Re-scan single hit count cleanly so future additional pushbacks can re-open it
    fresh = scan_correction_candidates(root)
    hit_count = 0
    upd_at = ""
    for item in fresh.get("candidates", []):
        if item["path"] == rel:
            hit_count = int(item["hit_count"])
            upd_at = item["updated_at"]
            break
    if hit_count == 0:
        # Fallback if already counted or filtered
        hit_count = max(2, int((queue.get("resolved", {}).get(rel) or {}).get("hit_count", 2)))

    queue.setdefault("resolved", {})[rel] = {
        "resolved_at": datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "hit_count": hit_count,
        "updated_at": upd_at,
        "note": note,
    }
    save_queue(root, queue)
    final_q, _ = refresh_retrospect(root)
    return final_q


def extract_top_guardrails(root: Path, max_items: int = 8) -> list[str]:
    """Extract compact guardrail bullets from GUARDRAILS block in 笔记/AI协作错题本.md."""
    book_path = root / MISTAKE_BOOK_REL
    if not book_path.is_file():
        return []
    text = book_path.read_text(encoding="utf-8")
    m = re.search(r"<!-- GUARDRAILS_START -->\s*(.*?)\s*<!-- GUARDRAILS_END -->", text, re.S)
    if not m:
        return []
    bullets = []
    for line in m.group(1).splitlines():
        s = line.strip()
        if re.match(r"^(?:\d+\.|-)\s+", s):
            bullets.append(s)
        if len(bullets) >= max_items:
            break
    return bullets


def main() -> None:
    parser = argparse.ArgumentParser(description="KB automatic AI mistake-book scanner & queue manager.")
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("AGENT_MEMORY_KB_ROOT", Path.home() / "personal-kb")))
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan")
    p_scan.add_argument("--since", default=None)

    p_res = sub.add_parser("resolve")
    p_res.add_argument("--path", required=True, help="Relative archive path e.g. 对话/Codex/xxx.md")
    p_res.add_argument("--note", default="")

    sub.add_parser("list")
    sub.add_parser("guardrails")

    args = parser.parse_args()
    root = args.root.resolve()
    if args.cmd == "scan":
        q, changed = refresh_retrospect(root, since=args.since)
        print(json.dumps({"changed_book": changed, "unresolved_count": q["unresolved_count"], "candidates": q["candidates"]}, ensure_ascii=False, indent=2))
    elif args.cmd == "resolve":
        q = resolve_candidate(root, args.path, note=args.note)
        print(json.dumps({"unresolved_count": q["unresolved_count"], "resolved_count": len(q.get("resolved", {}))}, ensure_ascii=False, indent=2))
    elif args.cmd == "list":
        q = _load_queue(root)
        print(json.dumps({"unresolved_count": q.get("unresolved_count", len(q.get("candidates", []))), "candidates": q.get("candidates", [])}, ensure_ascii=False, indent=2))
    elif args.cmd == "guardrails":
        print("\n".join(extract_top_guardrails(root)))


if __name__ == "__main__":
    main()
