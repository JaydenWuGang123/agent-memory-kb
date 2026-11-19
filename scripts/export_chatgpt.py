#!/usr/bin/env python3
"""Render tool-exported visible ChatGPT messages into local Markdown."""
import json
import datetime as dt
import argparse
import hashlib
from pathlib import Path
import sys

SYNC_DIR = Path(__file__).resolve().parent
if str(SYNC_DIR) not in sys.path:
    sys.path.insert(0, str(SYNC_DIR))

from kb_filters import atomic_write_text, load_noise_filter, redact_secrets, should_skip_conversation  # noqa: E402

KB = Path(__file__).resolve().parent.parent
DATA = KB / '_同步/chatgpt-raw'
TZ = dt.timezone(dt.timedelta(hours=8))


def date(value):
    if isinstance(value, (float, int)):
        return dt.datetime.fromtimestamp(value, TZ).isoformat(timespec='seconds')
    return value or '未知'


def json_value(value):
    return json.dumps(value, ensure_ascii=False)


def ingest(input_path, data_dir: Path = DATA):
    """Save an immutable API snapshot before merging, never replace old pages."""
    data = json.loads(Path(input_path).read_text(encoding='utf-8'))
    if 'thread' not in data and isinstance(data.get('content'), list):
        data = json.loads(next(block['text'] for block in data['content'] if block.get('type') == 'text'))
    thread = data.get('thread', {})
    thread_id = thread.get('id', '')
    if thread.get('kind') != 'chatgpt' or not thread_id or any(c not in '0123456789abcdef-' for c in thread_id):
        raise ValueError('Expected read_thread ChatGPT JSON with a safe thread UUID')
    if not isinstance(data.get('turns'), list):
        raise ValueError('Missing visible turns array')
    raw = json.dumps(data, ensure_ascii=False, indent=2)
    digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]
    folder = data_dir / thread_id
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ('snapshot-' + digest + '.json')
    if not target.exists():
        target.write_text(raw, encoding='utf-8')
    return target


def merge_turns(pages):
    """Merge by stable turn/message IDs so a new five-turn slice loses nothing."""
    merged = {}
    for page in pages:
        source_turns = page.get('turns', [])
        if page.get('page', {}).get('order') == 'newest_first':
            source_turns = list(reversed(source_turns))
        for turn in source_turns:
            key = turn.get('id')
            existing = merged.setdefault(key, {**turn, 'items': []})
            by_id = {item.get('id'): item for item in existing['items']}
            order = [item.get('id') for item in existing['items']]
            for item in turn.get('items', []):
                item_id = item.get('id')
                # Visible API messages have IDs. Unknown nonvisible items are
                # irrelevant to the transcript and must not collide at None.
                if not item_id or item.get('type') not in ('userMessage', 'agentMessage'):
                    continue
                if item_id not in by_id:
                    order.append(item_id)
                incoming_truncated = any(v for k,v in item.items() if 'truncat' in k.lower())
                previous = by_id.get(item_id)
                previous_truncated = previous and any(v for k,v in previous.items() if 'truncat' in k.lower())
                if not (previous and incoming_truncated and not previous_truncated):
                    by_id[item_id] = item
            existing.update({k:v for k,v in turn.items() if k != 'items'})
            existing['items'] = [by_id[item_id] for item_id in order]
    return sorted(merged.values(), key=lambda turn: turn.get('startedAt') or 0)


def render_all(kb_root: Path = KB):
    data_dir = kb_root / '_同步/chatgpt-raw'
    manifest = []
    noise_cfg = load_noise_filter(kb_root)
    skipped_noise = 0
    if not data_dir.is_dir():
        target = kb_root / '_同步/chatgpt-manifest.json'
        atomic_write_text(target, '[]')
        print(json.dumps({'threads': 0, 'skipped_noise': 0, 'turns': 0, 'messages': 0, 'incomplete': []}, ensure_ascii=False))
        return
    for folder in sorted(data_dir.iterdir()):
        if not folder.is_dir():
            continue
        raw_paths = sorted([*folder.glob('page-*.json'), *folder.glob('snapshot-*.json')], key=lambda p: (p.stat().st_mtime_ns, p.name))
        pages = [json.loads(p.read_text(encoding='utf-8')) for p in raw_paths]
        if not pages:
            continue
        info = next((p['thread'] for p in reversed(pages) if p.get('thread')), {})
        thread_id = info.get('id', folder.name)
        title = info.get('title') or thread_id
        limitations = []
        pagination_complete = bool(pages[-1].get('page')) and not pages[-1]['page'].get('hasMore', True)
        # The app API is explicitly a recent-turn reader.  hasMore=false only
        # exhausts this exposed slice; it is not proof of whole-chat coverage.
        complete = False
        if not pagination_complete:
            limitations.append('分页未完成；仍有历史页面未读取。')
        limitations.append('当前 read_thread 为近期对话读取接口；接口通常返回有限的近期片段，即使返回 hasMore=false，仍不能确认会话从创建至今的完整历史。更早内容尚未验证或补齐。')
        turns = merge_turns(pages)
        messages = []
        message_ids = set()
        media_count = 0
        truncated_count = 0
        attachment_refs = []
        attachment_keys = set()
        for page in pages:
            for attachment in page.get('attachments', []):
                key = json_value(attachment)
                if key not in attachment_keys:
                    attachment_keys.add(key)
                    attachment_refs.append(attachment)
        for turn in turns:
            for item in turn.get('items', []):
                kind = item.get('type')
                if kind not in ('userMessage', 'agentMessage'):
                    continue
                if item.get('id') in message_ids:
                    continue
                message_ids.add(item.get('id'))
                role = '用户' if kind == 'userMessage' else '助手'
                chunks = []
                if isinstance(item.get('text'), str):
                    chunks.append(item['text'])
                for part in item.get('content', []):
                    if isinstance(part, str):
                        chunks.append(part)
                    elif part.get('type') == 'text':
                        chunks.append(part.get('text', ''))
                    else:
                        media_count += 1
                        # Reference metadata only; do not inline or download media.
                        safe_part = {k:v for k,v in part.items() if k not in ('data', 'base64', 'bytes')}
                        chunks.append('> 多媒体/附件引用（未下载）：' + json_value(safe_part))
                if not chunks:
                    continue
                if any('image contents were not included' in chunk or 'file contents were not included' in chunk for chunk in chunks):
                    media_count += 1
                truncation = {k:v for k,v in item.items() if ('truncat' in k.lower() and v) or ('original' in k.lower() and isinstance(v, (int,float)))}
                if truncation:
                    truncated_count += 1
                    complete = False
                    chunks.append('> 导出工具返回截断标记；未返回的正文无法从本次导出恢复：' + json_value(truncation))
                messages.append((role, item.get('id'), date(turn.get('startedAt')), '\n\n'.join(chunks)))
        if truncated_count:
            limitations.append(f'{truncated_count} 条消息被工具截断，缺失正文未恢复。')
        if media_count:
            limitations.append(f'{media_count} 个非文本内容块仅保存引用，未下载媒体/附件。')
        if attachment_refs:
            limitations.append(f'{len(attachment_refs)} 个附件只保留工具返回的名称与临时路径，文件未复制到知识库；临时路径可能失效。')
        bodies = '\n'.join(m[3] for m in messages)
        if 'sandbox:' in bodies:
            limitations.append('消息包含原会话 sandbox 文件链接；保留原链接，未下载，链接可能已失效。')
        if ':chatgpt-content-reference' in bodies:
            limitations.append('部分引用为 ChatGPT 内容引用标记；未解析回原引用目标。')
        limitations.append('完整性仅指 read_thread 工具返回的可见消息分页；不证明账号全部历史、其他分支、附件或已删除内容均已覆盖。')
        out_path = Path('对话/ChatGPT') / (thread_id + '.md')
        msg_dicts = [{"role": "user" if r == "用户" else "assistant", "text": b} for r, _, _, b in messages]
        if should_skip_conversation(thread_id, title, msg_dicts, noise_cfg):
            (kb_root / out_path).unlink(missing_ok=True)
            skipped_noise += 1
            continue
        header = [
            '---',
            'title: ' + json_value(title),
            'source: chatgpt',
            'thread_id: ' + json_value(thread_id),
            'created_at: ' + json_value(date(info.get('createdAt'))),
            'updated_at: ' + json_value(date(info.get('updatedAt'))),
            'exported_at: ' + json_value(dt.datetime.now(TZ).isoformat(timespec='seconds')),
            'full_history_verified: false',
            'tool_pagination_complete: ' + str(pagination_complete).lower(),
            '---', '', '# ' + title, '',
            f'- 原对话：[在 ChatGPT 中打开](https://chatgpt.com/c/{thread_id})',
            f'- 对话 ID：`{thread_id}`',
            f'- 创建时间：{date(info.get("createdAt"))}',
            f'- 更新时间：{date(info.get("updatedAt"))}',
            f'- 已导出：{len(turns)} 轮 / {len(messages)} 条可见消息',
            '', '> 这是带角色和时间的原对话存档。助手的建议、推测和回答不自动代表用户已确认的事实。工具调用、系统消息与推理内容不在导出范围。',
            '', '## 导出范围与限制', '',
            *['- ' + limitation for limitation in limitations],
            '',
        ]
        if attachment_refs:
            header.extend(['## 附件引用（未下载）', ''])
            for attachment in attachment_refs:
                header.append('- ' + json_value(attachment))
            header.append('')
        header.extend(['## 对话正文', ''])
        for index, (role, message_id, timestamp, body) in enumerate(messages, 1):
            header.extend([f'### {index}. {role} · {timestamp}', '', f'<!-- message_id: {message_id} -->', '', body, '', '---', ''])
        target = kb_root / out_path
        atomic_write_text(target, redact_secrets('\n'.join(header)))
        manifest.append({
            'id': thread_id, 'title': title, 'source': 'chatgpt',
            'path': str(out_path), 'turn_count': len(turns), 'message_count': len(messages),
            'complete': complete, 'limitations': limitations,
            'tool_pagination_complete': pagination_complete, 'full_history_verified': False,
            'updated_at': date(info.get('updatedAt')), 'created_at': date(info.get('createdAt')),
            'page_count': len(pages), 'raw_snapshot_count': len(pages), 'media_reference_count': media_count,
            'attachment_reference_count': len(attachment_refs),
            'truncated_message_count': truncated_count,
        })
    target = kb_root / '_同步/chatgpt-manifest.json'
    atomic_write_text(target, json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps({'threads':len(manifest), 'skipped_noise': skipped_noise, 'turns':sum(x['turn_count'] for x in manifest), 'messages':sum(x['message_count'] for x in manifest), 'incomplete':[x['id'] for x in manifest if not x['complete']]}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Import read_thread ChatGPT JSON snapshots and merge visible messages by stable ID.')
    parser.add_argument('--output-root', type=Path, default=KB)
    parser.add_argument('--ingest', action='append', default=[], metavar='JSON_FILE', help='Append an immutable snapshot; may be passed repeatedly. Then regenerate Markdown and manifest.')
    args = parser.parse_args()
    out_root = args.output_root.resolve()
    raw_dir = out_root / '_同步/chatgpt-raw'
    for input_path in args.ingest:
        ingest(input_path, data_dir=raw_dir)
    render_all(out_root)
