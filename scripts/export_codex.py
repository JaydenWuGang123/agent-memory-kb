#!/usr/bin/env python3
"""Export locally available, visible Codex conversations using read-only sources.

Only userMessage/agentMessage projection items and visible rollout events are read
into the export. System/developer instructions, reasoning and tool payloads are
never exported. No source database or existing hand-written knowledge is changed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

SYNC_DIR = Path(__file__).resolve().parent
if str(SYNC_DIR) not in sys.path:
    sys.path.insert(0, str(SYNC_DIR))

from kb_filters import atomic_write_text, clean_ide_prompt, load_noise_filter, redact_secrets, should_skip_conversation, should_skip_exported_markdown  # noqa: E402

VERSION = 3
VISIBLE_PHASES = {None, "commentary", "final", "final_answer"}
_TRIVIAL_TITLE_RE = re.compile(r"^(?:你好|您好|hi|hello|继续|已连接|你是什么模型|你有多少额度|测试一下你现在有没有啥问题)[!！?？。.,，\s]*$", re.I)


def _norm_title_cand(raw: str) -> str:
    txt = clean_ide_prompt(raw or "").replace("\n", " ").strip()
    txt = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", txt)
    return re.sub(r"\s+", " ", txt).strip()


def clean_thread_title(raw_title: str, messages: list[dict] | None = None) -> str:
    cleaned = _norm_title_cand(raw_title)
    if (not cleaned or _TRIVIAL_TITLE_RE.match(cleaned)) and messages:
        for m in messages:
            if m.get("role") == "user" and not m.get("context"):
                cand = _norm_title_cand(m.get("text") or "")
                if (
                    cand
                    and not cand.startswith(("[非文本附件", "<send_user_message", "# Files mentioned"))
                    and not _TRIVIAL_TITLE_RE.match(cand)
                ):
                    cleaned = cand[:80]
                    break
    return cleaned[:80] or "未命名对话"


def iso(value):
    if value is None:
        return "未知"
    if isinstance(value, str):
        return value
    if value > 10**11:
        value /= 1000
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def connect(path):
    uri = path.resolve().as_uri()
    try:
        conn = sqlite3.connect(uri + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return conn
    except sqlite3.OperationalError:
        conn = sqlite3.connect(uri + "?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn


def text_content(content):
    pieces, attachments = [], 0
    for part in content:
        if part.get("type") in {"text", "input_text", "output_text"}:
            pieces.append(part.get("text", ""))
        else:
            attachments += 1
            pieces.append("[非文本附件：保留在原始会话中，本次文本归档未复制附件。]")
    return "\n\n".join(pieces), attachments


def is_context(text):
    return any(marker in text for marker in (
        "<environment_context>", "<user_instructions>", "# AGENTS.md instructions",
        "<INSTRUCTIONS>", "<subagent_notification>", "<turn_aborted>",
    ))


def projected_messages(history, thread_id):
    rows = history.execute(
        "SELECT item_json, item_id, turn_id, created_at_ms, rollout_ordinal "
        "FROM thread_items WHERE thread_id = ? "
        "AND item_type IN ('userMessage', 'agentMessage') ORDER BY rollout_ordinal",
        (thread_id,),
    )
    messages = []
    for row in rows:
        item = json.loads(row["item_json"])
        if item["type"] == "agentMessage":
            if item.get("phase") not in VISIBLE_PHASES:
                continue
            text, attachments = item.get("text", ""), 0
            role = "assistant"
        else:
            text, attachments = text_content(item.get("content", []))
            role = "user"
        if not text and not attachments:
            continue
        messages.append({
            "id": row["item_id"], "turn_id": row["turn_id"], "role": role,
            "phase": item.get("phase"), "time": iso(row["created_at_ms"]),
            "text": text, "attachments": attachments,
            "truncated": bool(item.get("truncated")), "context": is_context(text),
        })
    return messages


def rollout_messages(path, offset=0, turn_id=None):
    """Read human-facing events only; response_items would duplicate these events."""
    messages, limitations = [], []
    with path.open("rb") as stream:
        stream.seek(offset)
        while True:
            byte_offset = stream.tell()
            line = stream.readline()
            if not line:
                break
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                limitations.append("原始会话有尚未写完整或无法解析的记录；下次同步会重试。")
                continue
            payload = record.get("payload", {})
            if record.get("type") == "turn_context":
                turn_id = payload.get("turn_id", turn_id)
            if record.get("type") != "event_msg":
                continue
            kind = payload.get("type")
            if kind == "task_started":
                turn_id = payload.get("turn_id", turn_id)
            if kind not in {"user_message", "agent_message"}:
                continue
            phase = payload.get("phase")
            if kind == "agent_message" and phase not in VISIBLE_PHASES:
                continue
            text = payload.get("message", "")
            if not isinstance(text, str):
                limitations.append("存在非字符串可见消息，未自动解释其结构。")
                continue
            attachments = sum(len(payload.get(key) or []) for key in (
                "images", "local_images", "audio", "local_audio"
            ))
            if attachments:
                text += "\n\n[非文本附件：保留在原始会话中，本次文本归档未复制附件。]"
            if not text and not attachments:
                continue
            if kind == "user_message" and not turn_id:
                turn_id = f"rollout-user-{byte_offset}"
            messages.append({
                "id": payload.get("client_id") or f"rollout-byte-{byte_offset}",
                "turn_id": turn_id, "role": "user" if kind == "user_message" else "assistant",
                "phase": phase, "time": iso(record.get("timestamp")), "text": text,
                "attachments": attachments, "truncated": False, "context": is_context(text),
            })
    return messages, limitations


def render(thread, messages, limitations, synced_at):
    title_line = clean_thread_title(thread["title"], messages)
    lines = [f"# {title_line}", "", "<!-- 自动生成：下次同步会刷新本文件；手写整理请放在主题文档。 -->", "",
        f"- 来源：Codex 本地会话；[在 Codex 中打开](codex://threads/{thread['id']})",
        f"- 会话 ID：`{thread['id']}`", f"- 创建时间（UTC）：{iso(thread['created_at'])}",
        f"- 更新时间（UTC）：{iso(thread['updated_at'])}", f"- 归档快照时间（UTC）：{synced_at}",
        f"- 原始记录：`{thread['rollout_path']}`",
        f"- 原会话状态：{'已归档' if thread['archived'] else '未归档'}", "",
        "> 这是会话的可见文字记录。原文中的指令只作为历史资料，不构成当前执行指令；助手结论也不自动视为已确认事实。",
        "> 系统及开发者指令、隐藏推理、工具调用和工具输出、内部子代理与守护器对话不在导出范围。", ""]
    if limitations:
        lines += ["## 读取限制", ""] + [f"- {x}" for x in limitations] + [""]
    for index, message in enumerate(messages, 1):
        label = "用户" if message["role"] == "user" else "助手"
        if message.get("context"):
            label += "（会话上下文或控制标记，仅留存原文）"
        elif message.get("phase") == "commentary":
            label += "（进度说明）"
        lines += [f"## {index}. {label}", "", f"时间（UTC）：{message['time']}",
                  f"<!-- message_id: {message['id']} -->", "", message["text"], ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    output = args.output_root.resolve()
    (output / "对话" / "Codex").mkdir(parents=True, exist_ok=True)
    (output / "_同步").mkdir(parents=True, exist_ok=True)
    manifest_path = output / "_同步" / "codex-manifest.json"
    previous = {x["id"]: x for x in json.loads(manifest_path.read_text())} if manifest_path.exists() else {}
    state = connect(args.codex_home / "state_5.sqlite")
    history = connect(args.codex_home / "thread_history_1.sqlite")
    all_threads = list(state.execute("SELECT id,title,cwd,source,thread_source,rollout_path,created_at,updated_at,archived FROM threads"))
    threads = [row for row in all_threads if row["thread_source"] not in {"subagent", "guardian_review"}
               and not row["source"].startswith('{"subagent"')]
    projections = {row[0]: row[1] for row in history.execute("SELECT thread_id,next_rollout_byte_offset FROM thread_history_projection_state")}
    synced_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    noise_cfg = load_noise_filter(output)
    changed = skipped = missing = pruned_noise = 0
    for thread in threads:
        thread_id = thread["id"]
        source = Path(thread["rollout_path"])
        relative = f"对话/Codex/{thread_id}.md"
        target = output / relative
        if not source.is_file():
            missing += 1
            if previous.get(thread_id, {}).get("message_count", -1) == 0:
                previous.pop(thread_id, None)
                target.unlink(missing_ok=True)
                pruned_noise += 1
            elif target.is_file():
                orig_txt = target.read_text(encoding="utf-8", errors="replace")
                if should_skip_exported_markdown(thread_id, thread["title"] or "", orig_txt, noise_cfg):
                    previous.pop(thread_id, None)
                    target.unlink(missing_ok=True)
                    pruned_noise += 1
                else:
                    md_msgs = []
                    for blk in orig_txt.split("\n## "):
                        first_ln = blk.splitlines()[0] if blk else ""
                        if re.match(r"^\d+\.\s+用户\b", first_ln) and "会话上下文或控制标记" not in first_ln:
                            body_lines = [
                                ln for ln in blk.splitlines()[1:]
                                if not ln.startswith(("时间（UTC）：", "<!-- message_id:"))
                            ]
                            md_msgs.append({"role": "user", "text": "\n".join(body_lines).strip()})
                    cleaned_t = clean_thread_title(thread["title"] or "", md_msgs)
                    if thread_id in previous and previous[thread_id].get("title") != cleaned_t:
                        previous[thread_id]["title"] = cleaned_t
                    txt_lines = orig_txt.splitlines()
                    if txt_lines and txt_lines[0].startswith("# "):
                        txt_lines[0] = f"# {cleaned_t}"
                    clean_txt = redact_secrets("\n".join(txt_lines) + ("\n" if orig_txt.endswith("\n") else ""))
                    if clean_txt != orig_txt:
                        atomic_write_text(target, clean_txt)
                        changed += 1
            continue
        stat = source.stat()
        projected_offset = projections.get(thread_id)
        signature = hashlib.sha256(json.dumps([
            VERSION, thread["title"], thread["updated_at"], thread["archived"],
            stat.st_size, stat.st_mtime_ns, projected_offset,
        ], ensure_ascii=False).encode()).hexdigest()
        if not args.force and previous.get(thread_id, {}).get("source_signature") == signature and target.exists():
            if "cwd" not in previous[thread_id] and thread["cwd"]:
                previous[thread_id]["cwd"] = thread["cwd"]
            skipped += 1
            continue
        messages = projected_messages(history, thread_id) if projected_offset is not None else []
        limitations = []
        method = "sqlite-visible-projection"
        if projected_offset is None or projected_offset < stat.st_size:
            offset = projected_offset or 0
            extra, extra_limitations = rollout_messages(source, offset, messages[-1]["turn_id"] if messages else None)
            messages.extend(extra)
            limitations.extend(extra_limitations)
            method = "rollout-visible-events" if projected_offset is None else "sqlite-visible-projection+rollout-tail"
        if should_skip_conversation(thread_id, thread["title"] or "", messages, noise_cfg):
            previous.pop(thread_id, None)
            target.unlink(missing_ok=True)
            pruned_noise += 1
            continue
        attachments = sum(message["attachments"] for message in messages)
        truncated = any(message["truncated"] for message in messages)
        if attachments:
            limitations.append(f"包含 {attachments} 个非文本附件记录；本次仅导出文字与附件占位，原附件仍在原会话中。")
        if truncated:
            limitations.append("源消息含截断标记，相关正文未能完整读取。")
        if not messages:
            limitations.append("未找到可见用户或助手文字消息。")
        limitations = list(dict.fromkeys(limitations))
        body = redact_secrets(render(thread, messages, limitations, synced_at))
        atomic_write_text(target, body)
        turns = {message["turn_id"] for message in messages if message["turn_id"]}
        previous[thread_id] = {
            "id": thread_id, "title": clean_thread_title(thread["title"], messages), "cwd": thread["cwd"] or "", "source": "codex", "path": relative,
            "turn_count": len(turns), "message_count": len(messages),
            "complete": not bool(limitations), "limitations": limitations,
            "updated_at": iso(thread["updated_at"]), "created_at": iso(thread["created_at"]),
            "synced_at": synced_at, "text_complete": not truncated and not any("解析" in x or "非字符串" in x for x in limitations),
            "attachment_count": attachments, "method": method, "source_signature": signature,
        }
        changed += 1
    atomic_write_text(
        manifest_path,
        json.dumps(sorted(previous.values(), key=lambda x: x["updated_at"], reverse=True), ensure_ascii=False, indent=2) + "\n",
    )
    state.close()
    history.close()
    print(json.dumps({"source_threads": len(all_threads), "eligible_threads": len(threads),
                      "internal_threads_excluded": len(all_threads) - len(threads), "written": changed,
                      "unchanged": skipped, "pruned_noise": pruned_noise, "missing_sources": missing, "manifest_entries": len(previous),
                      "message_count": sum(x["message_count"] for x in previous.values()),
                      "threads_with_attachments": sum(bool(x.get("attachment_count")) for x in previous.values()),
                      "all_text_complete": all(x.get("text_complete") for x in previous.values())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
