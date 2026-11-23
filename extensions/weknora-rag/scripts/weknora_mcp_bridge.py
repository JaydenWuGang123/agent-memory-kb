#!/usr/bin/env python3
"""Zero-dependency MCP Stdio-to-Streamable-HTTP bridge for local WeKnora MCP Server.

Reads endpoint URL and Bearer token from $WEKNORA_RAG_DIR/.mcp.env (chmod 600)
so credentials are never duplicated into Codex / Claude Code / Cursor / Antigravity configs.
Bypasses system HTTP proxy for loopback hosts and degrades gracefully when WeKnora Docker is stopped.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

BASE_DIR = Path(os.environ.get("WEKNORA_RAG_DIR", str(Path.home() / "weknora-rag"))).expanduser()
ENV_FILE = Path(os.environ.get("WEKNORA_MCP_ENV_FILE", str(BASE_DIR / ".mcp.env"))).expanduser()
TOOLS_CACHE_FILE = BASE_DIR / ".mcp-tools-cache.json"
DEFAULT_TIMEOUT = int(os.environ.get("WEKNORA_MCP_TIMEOUT", "240"))


def load_mcp_config() -> tuple[str, str]:
    cfg: dict[str, str] = {}
    if ENV_FILE.is_file():
        for raw in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    url = (os.environ.get("WEKNORA_MCP_URL") or cfg.get("WEKNORA_MCP_URL") or "").strip()
    token = (os.environ.get("WEKNORA_MCP_TOKEN") or cfg.get("WEKNORA_MCP_TOKEN") or "").strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"Invalid WEKNORA_MCP_URL; please check {ENV_FILE}")
    return url, token


def build_opener(url: str) -> urllib.request.OpenerDirector:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1", "host.docker.internal"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def save_tools_cache(tools_result: dict[str, Any]) -> None:
    try:
        TOOLS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = TOOLS_CACHE_FILE.with_name(f".{TOOLS_CACHE_FILE.name}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(tools_result, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, TOOLS_CACHE_FILE)
    except Exception:
        pass


def load_tools_cache() -> dict[str, Any]:
    if TOOLS_CACHE_FILE.is_file():
        try:
            data = json.loads(TOOLS_CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("tools"), list):
                return data
        except Exception:
            pass
    return {"tools": []}


def forward_to_weknora(
    opener: urllib.request.OpenerDirector, url: str, token: str, msg: dict[str, Any]
) -> dict[str, Any] | None:
    payload = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with opener.open(req, timeout=DEFAULT_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", errors="replace").strip()
        if not raw:
            return None
        ct = (resp.headers.get("Content-Type") or "").lower()
        if "text/event-stream" in ct:
            last_obj: dict[str, Any] | None = None
            for line in raw.splitlines():
                if line.startswith("data:"):
                    chunk = line[5:].strip()
                    if not chunk:
                        continue
                    try:
                        obj = json.loads(chunk)
                        if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                            last_obj = obj
                    except json.JSONDecodeError:
                        continue
            return last_obj
        return json.loads(raw)


def write_response(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def offline_fallback(req_id: Any, method: str, reason: str) -> dict[str, Any]:
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "weknora-kb", "version": "1.1.0-offline"},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": load_tools_cache()}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/call":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "isError": True,
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Local WeKnora RAG service is currently stopped or unreachable ({reason}).\n"
                            "To enable deep multimodal RAG, start it with:\n"
                            "  weknora-ctl start\n"
                            "For lightweight daily notes and chat history, use the always-online personal-kb MCP tools."
                        ),
                    }
                ],
            },
        }
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": f"WeKnora offline: {reason}"}}


def main() -> int:
    url, token = load_mcp_config()
    opener = build_opener(url)

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            write_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {exc}"}})
            continue

        if not isinstance(msg, dict):
            continue
        method = str(msg.get("method") or "")
        has_id = "id" in msg and msg["id"] is not None
        req_id = msg.get("id")

        if not has_id:
            try:
                forward_to_weknora(opener, url, token, msg)
            except Exception:
                pass
            continue

        if method == "ping":
            write_response({"jsonrpc": "2.0", "id": req_id, "result": {}})
            continue

        try:
            resp = forward_to_weknora(opener, url, token, msg)
            if isinstance(resp, dict):
                resp["id"] = req_id
                if method == "tools/list" and isinstance(resp.get("result"), dict):
                    save_tools_cache(resp["result"])
                write_response(resp)
            else:
                write_response({"jsonrpc": "2.0", "id": req_id, "result": {}})
        except urllib.error.HTTPError as exc:
            write_response(offline_fallback(req_id, method, f"HTTP {exc.code}"))
        except Exception as exc:
            write_response(offline_fallback(req_id, method, str(exc)))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
