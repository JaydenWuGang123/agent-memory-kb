#!/usr/bin/env python3
"""Incrementally sync DLP-redacted curated notes & MinerU docs from agent-memory-kb to WeKnora."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BASE_DIR = Path(os.environ.get("WEKNORA_RAG_DIR", str(Path.home() / "weknora-rag"))).expanduser()
DEFAULT_ENV_FILE = BASE_DIR / ".sync.env"
DEFAULT_STATE_FILE = BASE_DIR / ".sync-state.json"
DEFAULT_KB_ROOT = Path(os.environ.get("AGENT_MEMORY_KB_ROOT", str(Path.home() / "personal-kb"))).expanduser()


def load_dotenv(env_file: Path) -> None:
    """Load KEY=VALUE lines without overriding already-set environment variables."""
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def load_dlp(kb_root: Path):
    sync_dir = kb_root / "_同步"
    if str(sync_dir) not in sys.path:
        sys.path.insert(0, str(sync_dir))
    from kb_filters import find_raw_secrets, redact_secrets  # type: ignore
    return redact_secrets, find_raw_secrets


def collect_curated_files(kb_root: Path, explicit_files: list[str] | None, include_archive: bool) -> list[Path]:
    root = kb_root.resolve()
    files: list[Path] = []
    if explicit_files:
        for raw in explicit_files:
            p = Path(raw).expanduser().resolve()
            if not p.is_file():
                raise FileNotFoundError(f"File not found: {raw}")
            if root not in p.parents and p != root:
                raise ValueError(f"Security block: file {p} is outside KB root {root}")
            files.append(p)
        return files

    for top in ("INDEX.md", "个人档案.md"):
        p = root / top
        if p.is_file():
            files.append(p)
    for folder in ("笔记", "共享摘录"):
        d = root / folder
        if d.is_dir():
            files.extend(sorted(d.glob("*.md")))
    docs_dir = root / "资料"
    if docs_dir.is_dir():
        files.extend(sorted(docs_dir.rglob("*.md")))
    if include_archive:
        hist = root / "笔记" / "历史档案"
        if hist.is_dir():
            files.extend(sorted(hist.glob("*.md")))
    return [f for f in files if f.is_file() and not f.name.startswith(".")]


class WeKnoraClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 60):
        parsed = urllib.parse.urlparse(base_url.rstrip("/"))
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"Invalid WEKNORA_BASE_URL: {base_url}")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.timeout = timeout
        host = (parsed.hostname or "").lower()
        if host in ("localhost", "127.0.0.1", "::1", "host.docker.internal"):
            self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        else:
            self._opener = urllib.request.build_opener()

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"X-API-Key": self.api_key}
        if extra:
            h.update(extra)
        return h

    def _json_request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        url = f"{self.base_url}{path}"
        data = None
        headers = self._headers()
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return resp.status, json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = {"raw": body}
            return exc.code, parsed

    def health(self) -> bool:
        try:
            with self._opener.open(f"{self.base_url}/health", timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def list_knowledge_bases(self) -> list[dict]:
        status, data = self._json_request("GET", "/api/v1/knowledge-bases")
        if status != 200:
            raise RuntimeError(f"Failed to list knowledge bases (HTTP {status}): {data}")
        return data.get("data") or []

    def create_knowledge_base(self, name: str, description: str) -> dict:
        status, data = self._json_request(
            "POST",
            "/api/v1/knowledge-bases",
            {"name": name, "description": description, "type": "document"},
        )
        if status not in (200, 201):
            raise RuntimeError(f"Failed to create knowledge base (HTTP {status}): {data}")
        return data.get("data") or {}

    def list_knowledge_items(self, kb_id: str) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            qs = urllib.parse.urlencode({"page": page, "page_size": 100})
            status, data = self._json_request("GET", f"/api/v1/knowledge-bases/{kb_id}/knowledge?{qs}")
            if status != 200:
                raise RuntimeError(f"Failed to list KB documents (HTTP {status}): {data}")
            batch = data.get("data") or []
            items.extend(batch)
            total = int(data.get("total") or len(items))
            if len(items) >= total or not batch:
                break
            page += 1
        return items

    def delete_knowledge(self, knowledge_id: str, wait_sec: float = 30.0) -> None:
        status, data = self._json_request("DELETE", f"/api/v1/knowledge/{knowledge_id}")
        if status not in (200, 202, 204, 404):
            raise RuntimeError(f"Failed to delete old doc (id={knowledge_id}, HTTP {status}): {data}")
        if status == 404:
            return
        # WeKnora DELETE /api/v1/knowledge/:id is asynchronous; wait for 404 so immediate
        # re-upload does not race into 409 Duplicate against a dying row.
        deadline = time.monotonic() + wait_sec
        while time.monotonic() < deadline:
            st, _ = self._json_request("GET", f"/api/v1/knowledge/{knowledge_id}")
            if st == 404:
                return
            time.sleep(0.4)

    def upload_markdown(self, kb_id: str, rel_path: str, content_bytes: bytes, summary_enabled: bool = False) -> tuple[int, dict]:
        boundary = f"----WeKnoraSync{uuid.uuid4().hex}"
        safe_leaf = Path(rel_path).name
        cfg_json = json.dumps({"summary_enabled": summary_enabled})
        parts: list[bytes] = [
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="fileName"\r\n\r\n'
                f"{rel_path}\r\n"
            ).encode("utf-8"),
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="process_config"\r\n\r\n'
                f"{cfg_json}\r\n"
            ).encode("utf-8"),
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{safe_leaf}"\r\n'
                f"Content-Type: text/markdown; charset=utf-8\r\n\r\n"
            ).encode("utf-8")
            + content_bytes
            + b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
        body = b"".join(parts)

        req = urllib.request.Request(
            f"{self.base_url}/api/v1/knowledge-bases/{kb_id}/knowledge/file",
            data=body,
            headers=self._headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}),
            method="POST",
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                txt = resp.read().decode("utf-8", errors="replace")
                return resp.status, json.loads(txt) if txt.strip() else {}
        except urllib.error.HTTPError as exc:
            txt = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(txt)
            except Exception:
                parsed = {"raw": txt}
            return exc.code, parsed


def load_state(state_file: Path) -> dict:
    if not state_file.is_file():
        return {"files": {}}
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return {"files": {}}


def save_state(state_file: Path, state: dict) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_name(f".{state_file.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, state_file)


def main() -> int:
    parser = argparse.ArgumentParser(description="Incrementally sync DLP-redacted notes to WeKnora RAG")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--kb-root", type=Path, default=None)
    parser.add_argument("--file", dest="files", action="append", help="Sync only specified file(s)")
    parser.add_argument("--include-archive", action="store_true", help="Include 笔记/历史档案/*.md")
    parser.add_argument("--create-kb", action="store_true", help="Create target KB if missing")
    parser.add_argument("--enable-summary", action="store_true", help="Enable remote LLM summary generation during upload")
    parser.add_argument("--force", action="store_true", help="Ignore local SHA-256 cache and re-upload all")
    parser.add_argument("--dry-run", action="store_true", help="Preview pending files & DLP checks without network calls")
    args = parser.parse_args()

    load_dotenv(args.env_file)
    kb_root = (args.kb_root or Path(os.environ.get("PERSONAL_KB_ROOT", str(DEFAULT_KB_ROOT)))).expanduser().resolve()
    base_url = os.environ.get("WEKNORA_BASE_URL", "http://127.0.0.1:8080").strip()
    api_key = os.environ.get("WEKNORA_API_KEY", "").strip()
    kb_id = os.environ.get("WEKNORA_KB_ID", "").strip()
    kb_name = os.environ.get("WEKNORA_KB_NAME", "个人知识库").strip()

    redact_secrets, find_raw_secrets = load_dlp(kb_root)
    targets = collect_curated_files(kb_root, args.files, args.include_archive)
    state = load_state(args.state_file)
    synced_map: dict[str, dict] = state.setdefault("files", {})

    pending: list[tuple[str, bytes, str]] = []
    skipped = 0
    for fpath in targets:
        rel = fpath.relative_to(kb_root).as_posix()
        raw_text = fpath.read_text(encoding="utf-8", errors="replace")
        clean_text = redact_secrets(raw_text)
        residual = find_raw_secrets(clean_text)
        if residual:
            print(f"[DLP BLOCK] {rel} still contains suspected secret ({', '.join(residual)}); skipped.", file=sys.stderr)
            continue
        payload_bytes = clean_text.encode("utf-8")
        digest = hashlib.sha256(payload_bytes).hexdigest()
        prev = synced_map.get(rel) or {}
        if not args.force and prev.get("sha256") == digest and prev.get("knowledge_id"):
            skipped += 1
            continue
        pending.append((rel, payload_bytes, digest))

    if args.dry_run:
        print(f"[kb_sync_weknora] Scanned {len(targets)} docs: pending {len(pending)}, unchanged {skipped}.")
        for rel, data, digest in pending:
            print(f"  [DRY-RUN] {rel} ({len(data)} bytes, sha256={digest[:12]})")
        return 0

    if not api_key:
        print("[kb_sync_weknora] WEKNORA_API_KEY not configured. Copy .sync.env.example to .sync.env.", file=sys.stderr)
        return 2

    client = WeKnoraClient(base_url=base_url, api_key=api_key)
    if not client.health():
        print(f"[kb_sync_weknora] WeKnora backend unreachable ({base_url}/health). Run weknora-ctl start.", file=sys.stderr)
        return 1

    if not kb_id:
        kbs = client.list_knowledge_bases()
        matched = next((kb for kb in kbs if kb.get("name") == kb_name), None)
        if matched:
            kb_id = str(matched["id"])
        elif args.create_kb:
            created = client.create_knowledge_base(kb_name, "DLP-redacted personal curated notes & parsed documents")
            kb_id = str(created["id"])
            print(f"[kb_sync_weknora] Created KB: {kb_name} (id={kb_id})")
        else:
            print(f"[kb_sync_weknora] KB '{kb_name}' not found (pass --create-kb or set WEKNORA_KB_ID).", file=sys.stderr)
            return 1

    remote_map: dict[str, str] = {}
    remote_ids: set[str] = set()
    for item in client.list_knowledge_items(kb_id):
        folder = (item.get("folder_path") or "").strip("/")
        fname = (item.get("file_name") or "").strip()
        kid = str(item.get("id") or "")
        if kid:
            remote_ids.add(kid)
        if fname and kid:
            key = f"{folder}/{fname}" if folder else fname
            remote_map[key] = kid

    pending_rels = {rel for rel, _, _ in pending}
    if not args.force:
        for fpath in targets:
            rel = fpath.relative_to(kb_root).as_posix()
            if rel in pending_rels:
                continue
            prev = synced_map.get(rel) or {}
            if prev.get("kb_id") != kb_id or prev.get("knowledge_id") not in remote_ids:
                raw_text = fpath.read_text(encoding="utf-8", errors="replace")
                clean_text = redact_secrets(raw_text)
                if not find_raw_secrets(clean_text):
                    payload_bytes = clean_text.encode("utf-8")
                    pending.append((rel, payload_bytes, hashlib.sha256(payload_bytes).hexdigest()))
                    pending_rels.add(rel)

    print(f"[kb_sync_weknora] Scanned {len(targets)} docs: uploading {len(pending)} (target KB: {kb_id}).")
    if not pending:
        print("[kb_sync_weknora] All documents are up to date.")
        return 0

    uploaded = 0
    for rel, payload_bytes, digest in pending:
        prev_entry = synced_map.get(rel) or {}
        prev_id = prev_entry.get("knowledge_id") if prev_entry.get("kb_id") == kb_id else None
        old_id = remote_map.get(rel) or prev_id
        if old_id:
            client.delete_knowledge(old_id)
        status, resp = client.upload_markdown(kb_id, rel, payload_bytes, summary_enabled=args.enable_summary)
        if status == 409 and old_id and str((resp.get("data") or {}).get("id") or "") == old_id:
            time.sleep(1.5)
            status, resp = client.upload_markdown(kb_id, rel, payload_bytes, summary_enabled=args.enable_summary)
        if status in (200, 201):
            kid = str((resp.get("data") or {}).get("id") or "")
            synced_map[rel] = {"sha256": digest, "knowledge_id": kid, "kb_id": kb_id}
            uploaded += 1
            print(f"  [OK] {rel} -> knowledge_id={kid}")
        elif status == 409:
            kid = str((resp.get("data") or {}).get("id") or "")
            synced_map[rel] = {"sha256": digest, "knowledge_id": kid, "kb_id": kb_id}
            print(f"  [DUP] {rel} already exists (knowledge_id={kid}); state updated")
        else:
            print(f"  [FAIL] {rel} upload failed (HTTP {status}): {resp}", file=sys.stderr)

    save_state(args.state_file, state)
    print(f"[kb_sync_weknora] Done: {uploaded}/{len(pending)} uploaded (target KB: {kb_id}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
