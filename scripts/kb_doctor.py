#!/usr/bin/env python3
"""Automated integrity, link, citation, and context-budget doctor for the Personal Knowledge Base."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote

CST_TZ = timezone(timedelta(hours=8), "CST")

SYNC_DIR = Path(__file__).resolve().parent
if str(SYNC_DIR) not in sys.path:
    sys.path.insert(0, str(SYNC_DIR))

from kb_filters import atomic_write_text, find_raw_secrets  # noqa: E402

PUBLIC_SCAN_EXTS = {".md", ".txt", ".json", ".html", ".htm", ".yml", ".yaml", ".js", ".ts", ".css"}

SENSITIVE_PATTERNS = [
    ("内网或私有 IP", r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3})\b"),
    ("本机绝对路径 (/Users/... 或 C:\\Users\\...)", r"(?:/(?:Users|home)/|[A-Za-z]:\\Users\\)[A-Za-z0-9_.\u4e00-\u9fff-]+[\\/]"),
    ("疑似密钥或 Token", r"\b(?:ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b"),
    ("个人手机号", r"\b1[3-9]\d{9}\b"),
]

BUDGET_LIMITS = {
    "INDEX.md": 90,
    "README.md": 80,
    "个人档案.md": 300,
    "笔记/前端设计与验收.md": 120,
    "笔记/进行中的项目.md": 220,
    "笔记/AI协作错题本.md": 220,
    "笔记/财富与投资纪律.md": 200,
    "笔记/个人事务与成长看板.md": 200,
    "笔记/知识库架构与治理.md": 200,
    "笔记/技术分享-AI原生个人知识库实践.md": 250,
}


def check_links(root: Path, skip_external_links: bool = False) -> list[dict]:
    broken = []
    root_resolved = root.resolve()
    curated = [
        root / "INDEX.md",
        root / "README.md",
        root / "个人档案.md",
        root / "同步规则.md",
        root / "跨设备配置.md",
        root / "对话索引.md",
    ]
    for sub in ("笔记", "笔记/历史档案", "共享摘录", "主题"):
        d = root / sub
        if d.is_dir():
            curated.extend(sorted(d.glob("*.md")))

    link_re = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    for md in curated:
        if not md.is_file():
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        in_fence = False
        for idx, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith(("```", "~~~")):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for m in link_re.finditer(line):
                target_raw = m.group(2).strip()
                if target_raw.startswith(("http://", "https://", "codex://", "conversation://", "chatgpt-conversation://", "sandbox:", "#")):
                    continue
                clean_target = unquote(target_raw.split("#", 1)[0])
                if not clean_target:
                    continue
                if clean_target.startswith("file://"):
                    fp = clean_target.replace("file://", "")
                    if re.match(r"^/[A-Za-z]:/", fp):
                        fp = fp[1:]
                    resolved = Path(fp).resolve()
                else:
                    resolved = (md.parent / clean_target).resolve()
                if skip_external_links:
                    try:
                        resolved.relative_to(root_resolved)
                    except ValueError:
                        continue
                if not resolved.exists():
                    broken.append({
                        "file": md.relative_to(root).as_posix(),
                        "line": idx,
                        "target": target_raw,
                    })
    return broken


def check_citations(root: Path) -> list[str]:
    missing = []
    targets = [root / "个人档案.md"]
    hist_dir = root / "笔记" / "历史档案"
    if hist_dir.is_dir():
        targets.extend(sorted(hist_dir.glob("*.md")))

    for f in targets:
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        sections = re.split(r"(?=^##\s+202\d)", text, flags=re.M)
        for sec in sections[1:]:
            header = sec.splitlines()[0].lstrip("#").strip()
            if "来源：" not in sec and "](对话/" not in sec and "](../../对话/" not in sec:
                missing.append(f"{f.name}: {header}")
    return missing


def check_budgets(root: Path) -> tuple[dict[str, int], list[str]]:
    counts = {}
    warnings = []
    for rel, limit in BUDGET_LIMITS.items():
        p = root / rel
        if p.is_file():
            n = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
            counts[rel] = n
            if n > limit:
                warnings.append(f"{rel} 当前 {n} 行（超过预算上限 {limit} 行）")
    return counts, warnings


def check_sanitization(root: Path, extra_public_dirs: list[Path] | None = None) -> list[dict]:
    dirs_to_scan = [root / "共享摘录"]
    patterns = list(SENSITIVE_PATTERNS)
    cfg_path = root / "_同步" / "kb_config.json"
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            for item in cfg.get("sanitization_extra_patterns", []):
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    patterns.append((str(item[0]), str(item[1])))
            if extra_public_dirs is None:
                for p in cfg.get("extra_public_dirs", []):
                    dirs_to_scan.append(Path(p).expanduser())
        except Exception:
            pass
    if extra_public_dirs is not None:
        dirs_to_scan.extend(extra_public_dirs)

    violations = []
    for d in dirs_to_scan:
        if not d.is_dir():
            continue
        for candidate in sorted(d.rglob("*")):
            if not candidate.is_file():
                continue
            if ".git" in candidate.parts or candidate.suffix.lower() not in PUBLIC_SCAN_EXTS:
                continue
            text = candidate.read_text(encoding="utf-8", errors="replace")
            for label, pat in patterns:
                m = re.search(pat, text, re.I)
                if m:
                    violations.append({
                        "file": str(candidate),
                        "rule": label,
                        "match": m.group(0),
                    })
    return violations


def check_config_integrity(root: Path) -> list[str]:
    errors: list[str] = []
    sync_dir = root / "_同步"
    if not sync_dir.is_dir():
        return errors
    targets: list[Path] = []
    cfg = sync_dir / "kb_config.json"
    if cfg.is_file():
        targets.append(cfg)
    targets.extend(sorted(sync_dir.glob("*-manifest.json")))
    for p in targets:
        try:
            json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"{p.relative_to(root).as_posix()}: JSON 解析失败 ({exc})")
    return errors


def check_archive_secrets(root: Path) -> list[dict]:
    """Scan local KB files (对话/, 笔记/, 个人档案.md) for raw unredacted tokens or private keys."""
    leaks: list[dict] = []
    targets: list[Path] = []
    if (root / "个人档案.md").is_file():
        targets.append(root / "个人档案.md")
    for sub in ("笔记", "笔记/历史档案", "对话"):
        d = root / sub
        if d.is_dir():
            targets.extend(sorted(d.rglob("*.md")))
    for p in targets:
        text = p.read_text(encoding="utf-8", errors="replace")
        matched_rules = find_raw_secrets(text)
        if matched_rules:
            leaks.append({
                "file": p.relative_to(root).as_posix(),
                "rules": matched_rules,
            })
    return leaks


def check_staleness(root: Path, max_age_days: int = 14, today: date | None = None) -> list[dict]:
    """Flag living L3 notes or tracked pending states whose latest confirmation date exceeds max_age_days."""
    ref_today = today or datetime.now(CST_TZ).date()
    stale: list[dict] = []
    targets = [
        "笔记/进行中的项目.md",
        "笔记/财富与投资纪律.md",
        "笔记/个人事务与成长看板.md",
    ]
    date_re = re.compile(r"\b(202\d-\d{2}-\d{2})\b")
    pending_re = re.compile(r"(待完成|待联调|等待|阻塞|暂停|到期)")
    for rel in targets:
        p = root / rel
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        # Strip AUTO_GIT_STATUS block so git snapshot dates don't mask manual confirmation age
        body = re.sub(r"<!-- AUTO_GIT_STATUS_START -->.*?<!-- AUTO_GIT_STATUS_END -->", "", text, flags=re.S)
        all_dates = []
        for m in date_re.finditer(body):
            try:
                all_dates.append(date.fromisoformat(m.group(1)))
            except ValueError:
                continue
        if not all_dates:
            continue
        latest = max(all_dates)
        age = (ref_today - latest).days
        if age > max_age_days:
            stale.append({
                "file": rel,
                "section": "整页最新日期",
                "date": latest.isoformat(),
                "age_days": age,
                "reason": f"超过 {max_age_days} 天未更新核对",
            })
            continue
        for sec in re.split(r"(?=^#{2,3}\s+)", body, flags=re.M):
            lines = sec.splitlines()
            if not lines or not lines[0].startswith(("## ", "### ")):
                continue
            header = lines[0].lstrip("#").strip()
            sec_dates = []
            for m in date_re.finditer(sec):
                try:
                    sec_dates.append(date.fromisoformat(m.group(1)))
                except ValueError:
                    continue
            if sec_dates and pending_re.search(sec):
                sec_latest = max(sec_dates)
                sec_age = (ref_today - sec_latest).days
                if sec_age > max_age_days:
                    stale.append({
                        "file": rel,
                        "section": header[:48],
                        "date": sec_latest.isoformat(),
                        "age_days": sec_age,
                        "reason": f"含待办/暂停/到期节点且已 {sec_age} 天未复核",
                    })
    return stale


def run_doctor(
    root: Path,
    extra_public_dirs: list[Path] | None = None,
    skip_external_links: bool = False,
    staleness_days: int = 14,
) -> dict:
    broken_links = check_links(root, skip_external_links=skip_external_links)
    missing_citations = check_citations(root)
    line_counts, budget_warnings = check_budgets(root)
    sanitization_violations = check_sanitization(root, extra_public_dirs=extra_public_dirs)
    config_errors = check_config_integrity(root)
    secret_leaks = check_archive_secrets(root)
    stale_warnings = check_staleness(root, max_age_days=staleness_days)
    healthy = not (broken_links or missing_citations or budget_warnings or sanitization_violations or config_errors or secret_leaks)
    return {
        "healthy": healthy,
        "broken_links": broken_links,
        "missing_citations": missing_citations,
        "budget_warnings": budget_warnings,
        "line_counts": line_counts,
        "sanitization_violations": sanitization_violations,
        "config_errors": config_errors,
        "secret_leaks": secret_leaks,
        "stale_warnings": stale_warnings,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("AGENT_MEMORY_KB_ROOT", Path.home() / "personal-kb")))
    parser.add_argument("--skip-external-checks", action="store_true")
    parser.add_argument("--ignore-budget", action="store_true")
    parser.add_argument("--staleness-days", type=int, default=14)
    parser.add_argument("--strict-staleness", action="store_true")
    args = parser.parse_args()
    extra = [] if args.skip_external_checks else None
    res = run_doctor(
        args.root.resolve(),
        extra_public_dirs=extra,
        skip_external_links=args.skip_external_checks,
        staleness_days=args.staleness_days,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    failed = bool(res["broken_links"] or res["missing_citations"] or res["sanitization_violations"] or res["config_errors"] or res["secret_leaks"])
    if not args.ignore_budget and res["budget_warnings"]:
        failed = True
    if args.strict_staleness and res["stale_warnings"]:
        failed = True
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
