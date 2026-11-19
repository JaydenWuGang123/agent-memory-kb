#!/usr/bin/env python3
"""Cold-archive dated sections of 个人档案.md older than a given YYYY-MM cutoff.

Keeps the Top Hot Snapshot table, undated baseline rules, and recent entries in
个人档案.md, while moving older dated entries into 笔记/历史档案/<YYYY-MM>.md
and adjusting relative Markdown links so zero links break.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re


def rewrite_links_for_subdir(text: str) -> str:
    def _repl(m: re.Match) -> str:
        label, target = m.group(1), m.group(2).strip()
        if target.startswith(("http://", "https://", "codex://", "conversation://", "chatgpt-conversation://", "file://", "#", "/")):
            return m.group(0)
        if target.startswith("../"):
            return f"[{label}](../../{target})"
        return f"[{label}](../../{target})"

    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _repl, text)


def archive_profile(root: Path, before_month: str) -> dict:
    profile_path = root / "个人档案.md"
    if not profile_path.is_file():
        return {"archived_sections": 0, "files": []}

    text = profile_path.read_text(encoding="utf-8")
    # Remove any existing trailing archive index section before splitting
    archive_marker = "\n## 历史流水归档索引"
    existing_archive_block = ""
    if archive_marker in text:
        text, existing_archive_block = text.split(archive_marker, 1)
        existing_archive_block = archive_marker + existing_archive_block

    parts = re.split(r"(?=^## )", text, flags=re.M)
    header = parts[0]
    sections = parts[1:]

    kept_sections = []
    to_archive: dict[str, list[str]] = {}

    for sec in sections:
        first_line = sec.splitlines()[0]
        m = re.match(r"^##\s+(\d{4}-\d{2})-\d{2}\b", first_line)
        if m and m.group(1) < before_month:
            ym = m.group(1)
            to_archive.setdefault(ym, []).append(sec.strip())
        else:
            kept_sections.append(sec)

    if not to_archive:
        return {"archived_sections": 0, "files": []}

    archive_dir = root / "笔记" / "历史档案"
    archive_dir.mkdir(parents=True, exist_ok=True)
    updated_files = []

    for ym, secs in sorted(to_archive.items()):
        out_path = archive_dir / f"{ym}.md"
        rebased = [rewrite_links_for_subdir(s) for s in secs]
        if out_path.is_file():
            old_body = out_path.read_text(encoding="utf-8").strip()
            existing_headers = set(re.findall(r"^##\s+.+$", old_body, flags=re.M))
            new_secs = [s for s in rebased if s.splitlines()[0] not in existing_headers]
            if new_secs:
                out_path.write_text(old_body + "\n\n" + "\n\n".join(new_secs) + "\n", encoding="utf-8")
        else:
            doc = [
                f"# 个人档案历史流水归档（{ym}）",
                "",
                "本页保存从 [个人档案](../../个人档案.md) 按月冷归档的历史原话记录与状态变更依据。当前有效最新状态请优先查看主档案顶部的《当前有效状态基线总表》。",
                "",
                *rebased,
                "",
            ]
            out_path.write_text("\n\n".join(doc[:4]) + "\n" + "\n\n".join(rebased) + "\n", encoding="utf-8")
        updated_files.append(f"笔记/历史档案/{ym}.md")

    all_archives = sorted(p.stem for p in archive_dir.glob("*.md"))
    archive_lines = [
        "## 历史流水归档索引（冷数据分层）",
        "",
        "超过保留窗口的旧流水已按月冷归档至以下文件，保留完整原话来源与日期：",
        "",
    ] + [f"- [{ym} 历史档案流水](笔记/历史档案/{ym}.md)" for ym in all_archives] + [""]

    new_profile = header + "".join(kept_sections).rstrip() + "\n\n" + "\n".join(archive_lines)
    profile_path.write_text(new_profile, encoding="utf-8")

    return {
        "archived_sections": sum(len(v) for v in to_archive.values()),
        "files": updated_files,
        "remaining_lines": len(new_profile.splitlines()),
    }


def main():
    parser = argparse.ArgumentParser(description="Archive old dated sections from 个人档案.md")
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("AGENT_MEMORY_KB_ROOT", Path.home() / "personal-kb")))
    parser.add_argument("--before", required=True, help="Archive sections strictly before YYYY-MM (e.g. 2026-11)")
    args = parser.parse_args()
    res = archive_profile(args.root.resolve(), args.before)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
