#!/usr/bin/env python3
"""Bootstrap a fresh local Agent Memory KB repository from templates and engine scripts."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parent


GENERAL_PROJECTS_TEMPLATE = """# 进行中的目标与事项看板

> 用于记录你当前并行推进的工作项目、个人副业、内容创作或学习计划；每次新开 Codex / Claude 窗口时，AI 会先读这张表直接接上你的进度。

---

## 1. 工作主线（示例：产品方案 / 季度复盘）
- **当前目标**：（简述本周或本月最想交付的结果）
- **已确认口径**：（关键结论、受众定位或重要前提）
- **卡点与待确认**：（还在等谁回复、哪些信息还没核实）
- **下一步动作**：（下次打开 AI 时直接从这里继续）

## 2. 个人项目 / 内容创作（示例：自媒体选题 / 副业）
- **当前目标**：（例如每周更新 2 篇深度内容）
- **偏好与风格**：（大白话、重实测、少废话）
- **下一步动作**：（待打磨的选题或草稿）

## 3. 长期学习与生活规划（示例：英语 / 读书 / 理财纪律）
- **当前阶段**：（正在进行的习惯或学习主题）
- **核心原则**：（不想忘记的长期纪律）
"""

GENERAL_MISTAKE_BOOK_TEMPLATE = """# AI 协作避坑与表达偏好本（通用非研发版）

行预算上限 `220` 行。同类问题按根因合并并递增 `[复发 xN]`，由 `_同步/kb_retrospect.py` 自动扫描多轮纠错会话进入待复盘队列。

---

## 一、 高频防重犯铁律（每次动手前 10 秒自检）

<!-- GUARDRAILS_START -->
1. **先对齐受众、场景和目标再动笔**：没搞清是发群聊、写汇报还是做自媒体脚本前，先问清楚，禁止自顾自按模板硬写。
2. **严禁编造没查证过的数据、案例或来源**：拿不准的事实直接标“待确认”，绝不把猜测写成已验证结论。
3. **说人话，少用官话套话和空洞排比**：优先给具体动作、清晰对比和直接可复制的成品，不堆砌正确但无用的废话。
4. **方向偏了立刻停手回归原意**：连续改两轮还不满意时，先重新核对核心诉求，不要在偏题草稿上继续叠补丁。
5. **严守隐私边界**：不把真实手机号、证件号、账号密码或内部敏感信息写入公开共享目录。
<!-- GUARDRAILS_END -->

---

## 二、 高频错题与偏好台账

> 每次会话经历 >= 2 次纠偏解决后，在此按根因归并沉淀。

---

## 三、 自动扫描待复盘队列（由 `kb_retrospect.py` 后台刷新）

<!-- AUTO_RETROSPECT_START -->
- 当前暂无未处理的待复盘对话。
<!-- AUTO_RETROSPECT_END -->
"""


def init_kb(target_root: Path, force: bool = False, profile: str = "dev") -> dict:
    target_root = target_root.expanduser().resolve()
    sync_dir = target_root / "_同步"
    notes_dir = target_root / "笔记"
    shared_dir = target_root / "共享摘录"
    chats_dir = target_root / "对话"

    for d in (sync_dir, notes_dir, shared_dir, chats_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Copy engine scripts into <target_root>/_同步/
    scripts_src = REPO_ROOT / "scripts"
    copied_scripts = 0
    for py_file in sorted(scripts_src.glob("*.py")):
        dst = sync_dir / py_file.name
        shutil.copy2(py_file, dst)
        copied_scripts += 1

    # Copy starter templates (without overwriting existing user notes unless --force)
    templates_src = REPO_ROOT / "templates"
    created_templates = []
    for src in sorted(templates_src.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(templates_src)
        if rel.name == "kb_config.example.json":
            dst = sync_dir / "kb_config.json"
        else:
            dst = target_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if force or not dst.exists():
            shutil.copy2(src, dst)
            created_templates.append(str(dst.relative_to(target_root)))

    # Apply general (non-developer) living-note templates when requested
    if profile == "general":
        for rel_note, content in (
            ("笔记/进行中的项目.md", GENERAL_PROJECTS_TEMPLATE),
            ("笔记/AI协作错题本.md", GENERAL_MISTAKE_BOOK_TEMPLATE),
        ):
            note_path = target_root / rel_note
            if force or rel_note in created_templates or not note_path.exists():
                note_path.write_text(content, encoding="utf-8")

    # Create initial stubs for auto-generated index/report so kb_doctor passes immediately
    for rel_stub, body in (
        ("对话索引.md", "# 对话索引\n\n尚未运行首次同步。运行 `python3 _同步/build_kb_index.py` 后自动生成。\n"),
        ("_同步/同步报告.md", "# 同步报告\n\n尚未运行首次同步。运行 `python3 _同步/build_kb_index.py` 后自动生成。\n"),
    ):
        stub_path = target_root / rel_stub
        if force or not stub_path.exists():
            stub_path.write_text(body, encoding="utf-8")

    # Write KB .gitignore
    kb_gitignore = target_root / ".gitignore"
    if force or not kb_gitignore.exists():
        shutil.copy2(REPO_ROOT / ".gitignore", kb_gitignore)

    # Initialize Git repository if not already initialized
    git_inited = False
    if not (target_root / ".git").exists():
        subprocess.run(["git", "init", "-b", "main", str(target_root)], check=True, stdout=subprocess.DEVNULL)
        git_inited = True

    # Install pre-commit hard gate
    sys.path.insert(0, str(sync_dir))
    import kb_backup  # noqa: E402
    kb_backup.ensure_pre_commit_hook(target_root)

    return {
        "kb_root": str(target_root),
        "profile": profile,
        "copied_scripts": copied_scripts,
        "created_templates": created_templates,
        "git_inited": git_inited,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize a local Agent Memory KB repository.")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("AGENT_MEMORY_KB_ROOT", Path.home() / "personal-kb")),
        help="Target directory for your personal knowledge base (default: ~/personal-kb)",
    )
    parser.add_argument(
        "--profile",
        choices=("dev", "general"),
        default="dev",
        help="Starter template profile: 'dev' for software engineers (default), 'general' for PMs/creators/non-coders",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing starter template files")
    args = parser.parse_args()
    res = init_kb(args.root, force=args.force, profile=args.profile)
    print(f"Initialized Agent Memory KB at {res['kb_root']} (profile={res['profile']}, {res['copied_scripts']} scripts, {len(res['created_templates'])} templates).")
    print("Next steps:")
    if args.profile == "dev":
        print(f"  1. Edit {args.root / '_同步' / 'kb_config.json'} to add your active_repos and category keywords.")
    else:
        print(f"  1. Open {args.root} in Obsidian (optional) and edit 笔记/进行中的项目.md with your active goals.")
    print(f"  2. Run: python3 '{args.root / '_同步' / 'build_kb_index.py'}' --output-root '{args.root}' --skip-icloud")
    print(f"  3. Run: python3 '{args.root / '_同步' / 'kb_doctor.py'}' --root '{args.root}'")


if __name__ == "__main__":
    main()
