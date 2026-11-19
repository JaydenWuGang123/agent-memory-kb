#!/usr/bin/env python3
"""End-to-end disaster recovery verification drill for the Personal Knowledge Base."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

CST_TZ = timezone(timedelta(hours=8), "CST")

SYNC_DIR = Path(__file__).resolve().parent
if str(SYNC_DIR) not in sys.path:
    sys.path.insert(0, str(SYNC_DIR))

from kb_backup import KEYCHAIN_ENC_SERVICE, _get_keychain_passphrase  # noqa: E402
from kb_doctor import run_doctor  # noqa: E402
from kb_search import search_kb  # noqa: E402

DEFAULT_BUNDLE_PATH = Path.home() / ".local" / "state" / "agent-memory-kb" / "kb-git-mirror.bundle"
DEFAULT_ICLOUD_PATH = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "agent-memory-kb-backup" / "kb-git-mirror.bundle"


def _bundle_verify_and_head(git_ctx: Path, bundle_file: Path) -> tuple[bool, str | None, str | None]:
    vres = subprocess.run(
        ["git", "-C", str(git_ctx), "bundle", "verify", str(bundle_file)],
        capture_output=True, text=True, check=False,
    )
    if vres.returncode != 0:
        return False, None, (vres.stderr or vres.stdout or "bundle verify failed").strip()[:200]
    hres = subprocess.run(
        ["git", "-C", str(git_ctx), "bundle", "list-heads", str(bundle_file)],
        capture_output=True, text=True, check=False,
    )
    head_sha = None
    for line in (hres.stdout or "").splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1] in ("HEAD", "refs/heads/main", "refs/heads/master"):
            head_sha = parts[0]
            if parts[1] in ("refs/heads/main", "HEAD"):
                break
    if not head_sha and (hres.stdout or "").strip():
        head_sha = hres.stdout.strip().splitlines()[0].split()[0]
    return True, head_sha, None


def verify_icloud_mirror(
    root: Path,
    icloud_bundle_path: Path,
    expected_head: str | None,
    passphrase: str | None = None,
    keychain_service: str = KEYCHAIN_ENC_SERVICE,
) -> dict:
    enc_path = icloud_bundle_path.with_suffix(".bundle.enc")
    try:
        has_enc = enc_path.is_file()
        has_plain = icloud_bundle_path.is_file()
    except PermissionError as exc:
        return {"status": "tcc_permission_denied", "ok": False, "error": str(exc)}

    if not has_enc and not has_plain:
        return {"status": "missing", "ok": False, "path": str(icloud_bundle_path)}

    target = enc_path if has_enc else icloud_bundle_path
    with tempfile.TemporaryDirectory() as td:
        tmp_dir = Path(td)
        verify_target = target
        encrypted = target.suffix == ".enc"
        if encrypted:
            pw = passphrase if passphrase is not None else _get_keychain_passphrase(keychain_service)
            if not pw:
                return {"status": "encrypted_no_passphrase", "ok": False, "path": str(target), "encrypted": True}
            decrypted = tmp_dir / "icloud-decrypted.bundle"
            try:
                _ = target.stat()
            except PermissionError as exc:
                return {"status": "tcc_permission_denied", "ok": False, "path": str(target), "error": str(exc)}
            import shutil
            openssl_bin = shutil.which("openssl") or (
                r"C:\Program Files\Git\usr\bin\openssl.exe"
                if Path(r"C:\Program Files\Git\usr\bin\openssl.exe").is_file()
                else "openssl"
            )
            dres = subprocess.run(
                [openssl_bin, "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-in", str(target), "-out", str(decrypted), "-pass", "stdin"],
                input=pw + "\n", text=True, capture_output=True, check=False,
            )
            if dres.returncode != 0 or not decrypted.is_file():
                return {"status": "decrypt_failed", "ok": False, "path": str(target), "error": (dres.stderr or "")[:200]}
            verify_target = decrypted
        else:
            try:
                verify_target = tmp_dir / "icloud-copy.bundle"
                verify_target.write_bytes(target.read_bytes())
            except PermissionError as exc:
                return {"status": "tcc_permission_denied", "ok": False, "path": str(target), "error": str(exc)}

        ok, icloud_head, err = _bundle_verify_and_head(root, verify_target)
        if not ok:
            return {"status": "verify_failed", "ok": False, "path": str(target), "encrypted": encrypted, "error": err}
        head_match = bool(expected_head and icloud_head == expected_head)
        behind_commits = None
        if expected_head and icloud_head and icloud_head != expected_head and (root / ".git").exists():
            cnt = subprocess.run(
                ["git", "-C", str(root), "rev-list", "--count", f"{icloud_head}..{expected_head}"],
                capture_output=True, text=True, check=False,
            )
            if cnt.returncode == 0 and (cnt.stdout or "").strip().isdigit():
                behind_commits = int(cnt.stdout.strip())
        return {
            "status": "ok" if head_match else "stale_head",
            "ok": True,
            "path": str(target),
            "encrypted": encrypted,
            "head": icloud_head,
            "head_match": head_match,
            "behind_commits": behind_commits,
        }


def verify_restore(
    root: Path,
    bundle_path: Path,
    icloud_bundle_path: Path | None = None,
    check_icloud: bool = False,
    skip_external_checks: bool = False,
    ignore_budget: bool = True,
    passphrase_override: str | None = None,
) -> dict:
    """Clone local bundle into an isolated temp directory and verify full integrity."""
    root = root.resolve()
    now = datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    if not bundle_path.is_file():
        return {"ok": False, "verified_at": now, "error": f"本地备份包不存在: {bundle_path}"}

    expected_head = None
    if (root / ".git").exists():
        hres = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
        if hres.returncode == 0:
            expected_head = (hres.stdout or "").strip()

    ok, bundle_head, verr = _bundle_verify_and_head(root if (root / ".git").exists() else bundle_path.parent, bundle_path)
    if not ok:
        return {"ok": False, "verified_at": now, "bundle_path": str(bundle_path), "error": f"bundle verify 失败: {verr}"}

    with tempfile.TemporaryDirectory() as td:
        restored = Path(td) / "restored-kb"
        cres = subprocess.run(
            ["git", "clone", "--quiet", str(bundle_path), str(restored)],
            capture_output=True, text=True, check=False,
        )
        if cres.returncode != 0 or not restored.is_dir():
            return {"ok": False, "verified_at": now, "bundle_path": str(bundle_path), "error": f"git clone 还原失败: {(cres.stderr or cres.stdout)[:200]}"}

        rhead_res = subprocess.run(["git", "-C", str(restored), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
        restored_head = (rhead_res.stdout or "").strip() if rhead_res.returncode == 0 else None
        head_match = True if expected_head is None else (restored_head == expected_head)

        required = [name for name in ("INDEX.md", "README.md", "个人档案.md", "同步规则.md") if (root / name).is_file()]
        missing_files = [name for name in required if not (restored / name).is_file() or (restored / name).stat().st_size == 0]

        extra_dirs = [] if skip_external_checks else None
        doc_res = run_doctor(restored, extra_public_dirs=extra_dirs, skip_external_links=True)
        doc_fatal = bool(
            doc_res["broken_links"]
            or doc_res["missing_citations"]
            or doc_res["sanitization_violations"]
            or doc_res["config_errors"]
            or (not ignore_budget and doc_res["budget_warnings"])
        )

        # Confirm pure disk search works immediately on restored clone without prebuilt SQLite
        disk_hits = search_kb(restored, "档案", source="notes", limit=2)
        disk_ok = isinstance(disk_hits, list)

    icloud_res = None
    if check_icloud and icloud_bundle_path is not None:
        icloud_res = verify_icloud_mirror(root, icloud_bundle_path, expected_head, passphrase=passphrase_override)

    overall_ok = bool(head_match and not missing_files and not doc_fatal and disk_ok)
    error_reasons = []
    if not head_match:
        error_reasons.append(f"HEAD 不一致 ({restored_head} != {expected_head})")
    if missing_files:
        error_reasons.append(f"缺失关键文件: {', '.join(missing_files)}")
    if doc_res["broken_links"]:
        error_reasons.append(f"仓内死链 {len(doc_res['broken_links'])} 处")
    if doc_res["missing_citations"]:
        error_reasons.append(f"来源引用缺失 {len(doc_res['missing_citations'])} 处")
    if doc_res["sanitization_violations"]:
        error_reasons.append(f"脱敏违规 {len(doc_res['sanitization_violations'])} 处")
    if doc_res["config_errors"]:
        error_reasons.append(f"配置/清单 JSON 异常 {len(doc_res['config_errors'])} 处")
    if not ignore_budget and doc_res["budget_warnings"]:
        error_reasons.append(f"预算超限 {len(doc_res['budget_warnings'])} 处")

    return {
        "ok": overall_ok,
        "error": "; ".join(error_reasons) if error_reasons else None,
        "verified_at": now,
        "bundle_path": str(bundle_path),
        "expected_head": expected_head,
        "bundle_head": bundle_head,
        "restored_head": restored_head,
        "head_match": head_match,
        "missing_files": missing_files,
        "doctor_healthy": not doc_fatal,
        "broken_links": len(doc_res["broken_links"]),
        "missing_citations": len(doc_res["missing_citations"]),
        "sanitization_violations": len(doc_res["sanitization_violations"]),
        "config_errors": len(doc_res["config_errors"]),
        "budget_warnings": doc_res["budget_warnings"],
        "disk_search_probe_hits": len(disk_hits),
        "icloud_mirror": icloud_res,
    }


def main():
    parser = argparse.ArgumentParser(description="Verify full Git bundle disaster recovery restore.")
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("AGENT_MEMORY_KB_ROOT", Path.home() / "personal-kb")))
    parser.add_argument("--bundle-path", type=Path, default=DEFAULT_BUNDLE_PATH)
    parser.add_argument("--icloud-bundle-path", type=Path, default=DEFAULT_ICLOUD_PATH)
    parser.add_argument("--check-icloud", action="store_true")
    parser.add_argument("--skip-external-checks", action="store_true")
    parser.add_argument("--strict-budget", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    res = verify_restore(
        root=args.root,
        bundle_path=args.bundle_path,
        icloud_bundle_path=args.icloud_bundle_path,
        check_icloud=args.check_icloud,
        skip_external_checks=args.skip_external_checks,
        ignore_budget=not args.strict_budget,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if not res["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
