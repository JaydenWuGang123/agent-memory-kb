#!/usr/bin/env python3
"""Parse local PDF/Office/Image files via local MinerU 4.2+ into <kb_root>/资料/<category>/."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8), "CST")
SUPPORTED_EXTS = {
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".html", ".htm",
}
VALID_TIERS = ("flash", "basic", "standard", "advanced")
DEFAULT_RAG_DIR = Path(os.environ.get("WEKNORA_RAG_DIR", str(Path.home() / "weknora-rag"))).expanduser()
MINERU_BIN = Path(os.environ.get("MINERU_BIN", str(DEFAULT_RAG_DIR / "mineru-venv" / "bin" / "mineru"))).expanduser()
DEFAULT_KB_ROOT = Path(os.environ.get("AGENT_MEMORY_KB_ROOT", str(Path.home() / "personal-kb"))).expanduser()
SYNC_SCRIPT = Path(__file__).resolve().parent / "kb_sync_weknora.py"


def sanitize_segment(name: str, fallback: str = "未分类") -> str:
    """Prevent path traversal and illegal filename characters."""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", (name or "").strip()).strip(". ")
    if not cleaned or cleaned in (".", ".."):
        return fallback
    return cleaned[:80]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_mineru_server() -> None:
    if not MINERU_BIN.is_file():
        raise FileNotFoundError(f"MinerU binary not found: {MINERU_BIN} (set MINERU_BIN or WEKNORA_RAG_DIR)")
    env = dict(os.environ)
    env["MINERU_TELEMETRY_DISABLED"] = "1"
    probe = subprocess.run(
        [str(MINERU_BIN), "server", "status"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )
    if probe.returncode == 0 and "PID" in probe.stdout:
        return
    print("[kb_parse_mineru] Starting local MinerU Doclib server...", file=sys.stderr)
    res = subprocess.run(
        [str(MINERU_BIN), "server", "start"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Failed to start MinerU server: {res.stderr.strip() or res.stdout.strip()}")


def load_redact_fn(kb_root: Path):
    sync_dir = kb_root / "_同步"
    if str(sync_dir) not in sys.path:
        sys.path.insert(0, str(sync_dir))
    try:
        from kb_filters import redact_secrets  # type: ignore
        return redact_secrets
    except Exception:
        return lambda s: s


def parse_one_file(
    src: Path,
    kb_root: Path,
    category: str,
    tier: str,
    pages: str,
    wait_sec: int,
    force: bool,
    redact: bool,
) -> Path:
    src = src.resolve()
    if not src.is_file():
        raise FileNotFoundError(f"Source file does not exist: {src}")
    if src.suffix.lower() not in SUPPORTED_EXTS:
        raise ValueError(f"Unsupported file extension: {src.suffix}")

    safe_cat = sanitize_segment(category, fallback="未分类")
    safe_stem = sanitize_segment(src.stem, fallback="document")
    docs_root = (kb_root.expanduser().resolve() / "资料").resolve()
    out_dir = (docs_root / safe_cat).resolve()
    if docs_root not in out_dir.parents and out_dir != docs_root:
        raise ValueError(f"Blocked output path outside docs root: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_md = (out_dir / f"{safe_stem}.md").resolve()
    if out_dir not in out_md.parents:
        raise ValueError(f"Blocked output file outside target dir: {out_md}")

    env = dict(os.environ)
    env["MINERU_TELEMETRY_DISABLED"] = "1"

    with tempfile.TemporaryDirectory(prefix="mineru_parse_") as tmp_dir:
        raw_out = Path(tmp_dir) / f"{safe_stem}.md"
        cmd = [
            str(MINERU_BIN),
            "parse",
            str(src),
            "--tier",
            tier,
            "-p",
            pages,
            "-o",
            str(raw_out),
            "--wait",
            str(wait_sec),
        ]
        if force:
            cmd.append("--force")

        print(f"[kb_parse_mineru] Parsing {src.name} (tier={tier}, pages={pages})...")
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, check=False)
        if res.returncode != 0 or not raw_out.is_file():
            detail = (res.stderr or res.stdout or "").strip()
            raise RuntimeError(f"MinerU parse failed (code={res.returncode}): {detail}")
        body = raw_out.read_text(encoding="utf-8", errors="replace").strip()

    if redact:
        body = load_redact_fn(kb_root)(body)

    digest = sha256_file(src)
    now_str = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S +08:00")
    header = "\n".join([
        f"# {src.stem}",
        "",
        f"> **来源文件**：`{src.name}`",
        f"> **解析引擎**：本地 MinerU（`tier={tier}`，`pages={pages}`）",
        f"> **解析时间**：{now_str}",
        f"> **分类目录**：`资料/{safe_cat}`",
        f"> **源文件 SHA-256**：`{digest[:16]}`",
        "",
        "---",
        "",
    ])
    final_content = header + body + "\n"
    tmp_target = out_md.with_name(f".{out_md.name}.tmp.{os.getpid()}")
    tmp_target.write_text(final_content, encoding="utf-8")
    os.replace(tmp_target, out_md)
    print(f"[kb_parse_mineru] Saved: {out_md} ({len(final_content)} chars)")
    return out_md


def rebuild_personal_kb_indexes(kb_root: Path, skip_vectors: bool = False) -> None:
    search_py = kb_root / "_同步" / "kb_search.py"
    embed_py = kb_root / "_同步" / "kb_embed.py"
    if search_py.is_file():
        print("[kb_parse_mineru] Rebuilding local FTS5 index...")
        subprocess.run([sys.executable, str(search_py), "--root", str(kb_root), "--rebuild-index"], check=False)
    if not skip_vectors and embed_py.is_file():
        vec_py = kb_root / "_同步" / ".venv-embed" / "bin" / "python"
        py_bin = str(vec_py) if vec_py.is_file() else sys.executable
        print("[kb_parse_mineru] Incrementally updating local ONNX semantic index...")
        subprocess.run([py_bin, str(embed_py), "--root", str(kb_root), "--sync"], check=False)


def collect_inputs(paths: list[str]) -> list[Path]:
    found: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser().resolve()
        if p.is_file():
            found.append(p)
        elif p.is_dir():
            for child in sorted(p.rglob("*")):
                if child.is_file() and child.suffix.lower() in SUPPORTED_EXTS and not child.name.startswith("."):
                    found.append(child)
        else:
            raise FileNotFoundError(f"Input path does not exist: {raw}")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse PDF/Office/Images via local MinerU into personal-kb/资料/")
    parser.add_argument("inputs", nargs="+", help="Files or directories to parse")
    parser.add_argument("--category", "-c", default="未分类", help="Subdirectory under <kb_root>/资料/")
    parser.add_argument("--tier", choices=VALID_TIERS, default="flash", help="MinerU tier (flash for text PDF, standard for scans/math)")
    parser.add_argument("--pages", "-p", default="all", help="Page range, e.g. all or 1-5,8")
    parser.add_argument("--wait", type=int, default=300, help="Timeout per file in seconds")
    parser.add_argument("--force", action="store_true", help="Ignore MinerU cache and re-parse")
    parser.add_argument("--redact", action="store_true", help="Apply kb_filters secret redaction before writing")
    parser.add_argument("--no-index", action="store_true", help="Skip local FTS5/ONNX index refresh")
    parser.add_argument("--no-vectors", action="store_true", help="Only refresh FTS5, skip ONNX vectors")
    parser.add_argument("--sync-weknora", action="store_true", help="Sync parsed outputs to WeKnora RAG")
    parser.add_argument("--kb-root", type=Path, default=DEFAULT_KB_ROOT, help="Local agent-memory-kb root")
    args = parser.parse_args()

    files = collect_inputs(args.inputs)
    if not files:
        print("[kb_parse_mineru] No supported input files found.", file=sys.stderr)
        return 1

    ensure_mineru_server()
    outputs: list[Path] = []
    for src in files:
        outputs.append(
            parse_one_file(
                src=src,
                kb_root=args.kb_root,
                category=args.category,
                tier=args.tier,
                pages=args.pages,
                wait_sec=args.wait,
                force=args.force,
                redact=args.redact,
            )
        )

    if not args.no_index:
        rebuild_personal_kb_indexes(args.kb_root, skip_vectors=args.no_vectors)

    if args.sync_weknora and SYNC_SCRIPT.is_file():
        cmd = [sys.executable, str(SYNC_SCRIPT), "--kb-root", str(args.kb_root)]
        for o in outputs:
            cmd.extend(["--file", str(o)])
        print("[kb_parse_mineru] Syncing parsed outputs to WeKnora...")
        subprocess.run(cmd, check=False)

    print(f"[kb_parse_mineru] Done! Parsed {len(outputs)} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
