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


def init_kb(target_root: Path, force: bool = False) -> dict:
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
    parser.add_argument("--force", action="store_true", help="Overwrite existing starter template files")
    args = parser.parse_args()
    res = init_kb(args.root, force=args.force)
    print(f"Initialized Agent Memory KB at {res['kb_root']} ({res['copied_scripts']} scripts, {len(res['created_templates'])} templates).")
    print("Next steps:")
    print(f"  1. Edit {args.root / '_同步' / 'kb_config.json'} to add your active_repos and category keywords.")
    print(f"  2. Run: python3 '{args.root / '_同步' / 'build_kb_index.py'}' --output-root '{args.root}' --skip-icloud")
    print(f"  3. Run: python3 '{args.root / '_同步' / 'kb_doctor.py'}' --root '{args.root}'")


if __name__ == "__main__":
    main()
