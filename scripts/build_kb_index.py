#!/usr/bin/env python3
"""Orchestrate navigation rendering, auto-archival, FTS5/vector indexing, doctor checks, and backup sync."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

CST_TZ = timezone(timedelta(hours=8), 'CST')

from kb_backup import (  # noqa: F401 - re-exported for test/CLI compatibility
    DEFAULT_BUNDLE_BACKUP_PATH,
    DEFAULT_ICLOUD_BUNDLE_PATH,
    KEYCHAIN_ENC_SERVICE,
    atomic_create_and_verify_bundle,
    atomic_create_local_bundle,
    describe_icloud_status,
    ensure_pre_commit_hook,
    get_git_head,
    icloud_needs_catchup,
    load_kb_config as _load_kb_config_base,
    load_pipeline_state,
    maybe_git_gc,
    maybe_notify_issues,
    maybe_push_private_remote,
    save_pipeline_state,
    sync_icloud_mirror,
    sync_lock,
)
from kb_doctor import atomic_write_text, check_sanitization, run_doctor
from kb_search import VEC_PYTHON, build_search_index

try:
    from archive_profile import archive_profile
except ImportError:
    archive_profile = None

try:
    from kb_restore_verify import verify_restore
except ImportError:
    verify_restore = None

try:
    from kb_retrospect import refresh_retrospect
except ImportError:
    refresh_retrospect = None

DEFAULT_CWD_CATEGORIES = [
    ('理财与规划', r'invest|finance|etf|trading|portfolio'),
    ('产品与个人项目', r'side-project|indie|personal-project'),
    ('学习与成长', r'learning|study|personal-kb|agent-memory-kb'),
    ('工程与工作项目', r'/work/|/projects/|frontend|backend|webapp|service'),
]

DEFAULT_TITLE_CATEGORIES = [
    ('理财与规划', r'投资|基金|ETF|资产|理财|定投|回测|预算|财务|finance|portfolio'),
    ('生活与健康', r'健康|健身|饮食|旅行|睡眠|生活|health|fitness'),
    ('学习与成长', r'学习|规划|读书|写作|英语|简历|面试|知识库|复盘|成长|learning'),
    ('产品与个人项目', r'个人项目|副业|独立开发|产品设计|side project|indie'),
    ('工程与工作项目', r'前端|后端|设计文档|接口|组件|React|Vue|TypeScript|Python|Go|Rust|Docker|K8s|SQL|bug|报错|修复|代码|重构|评审|CI|CD|eslint|webpack|vite|git'),
    ('工具与效率', r'Codex|ChatGPT|Claude|Cursor|Antigravity|Skill|MCP|Obsidian|MacBook|Linux|终端|命令|插件|自动化|脚本|快捷键|workflow'),
]

DEFAULT_ACTIVE_REPOS: list[tuple[str, str]] = []


def load_kb_config(root: Path) -> dict:
    return _load_kb_config_base(root, defaults={
        'active_repos': DEFAULT_ACTIVE_REPOS,
        'cwd_categories': DEFAULT_CWD_CATEGORIES,
        'title_categories': DEFAULT_TITLE_CATEGORIES,
    })


def extract_codex_heartbeat_block(report_path: Path) -> str:
    """Preserve Codex automation's incremental check & missing-sources sections across rebuilds."""
    if not report_path.is_file():
        return ''
    text = report_path.read_text(encoding='utf-8', errors='replace')
    marker_m = re.search(r'<!-- CODEX_HEARTBEAT_START -->\s*(.*?)\s*<!-- CODEX_HEARTBEAT_END -->', text, re.S)
    if marker_m and marker_m.group(1).strip():
        return marker_m.group(1).strip()

    start_m = re.search(r'^## 本次增量检查\b', text, re.M)
    if not start_m:
        return ''
    rest = text[start_m.start():]
    end_m = re.search(r'^## (?:尚未完整覆盖|已发现但正文暂不可读取|单条读取限制)\b', rest, re.M)
    block = rest[:end_m.start()] if end_m else rest
    return block.strip()


def clean(value):
    val = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', str(value or ''))
    return re.sub(r'\s+', ' ', val).replace('|', '｜').replace('[', '【').replace(']', '】')


def category(record, cwd_map, cfg=None):
    cwd_rules = (cfg or {}).get('cwd_categories', DEFAULT_CWD_CATEGORIES)
    title_rules = (cfg or {}).get('title_categories', DEFAULT_TITLE_CATEGORIES)
    cwd = record.get('cwd') or cwd_map.get(record.get('id'), '') or ''
    title = record.get('title', '')
    for name, pattern in title_rules:
        if re.search(pattern, title, re.I):
            return name
    for name, pattern in cwd_rules:
        if cwd and re.search(pattern, cwd, re.I):
            return name
    if record.get('source') == 'cursor':
        return '工程与工作项目'
    return '其他与待整理'


def load_codex_cwds():
    db_path = Path.home() / '.codex' / 'state_5.sqlite'
    if not db_path.is_file():
        return {}
    uri = db_path.resolve().as_uri()
    for suffix in ('?mode=ro', '?mode=ro&immutable=1'):
        try:
            conn = sqlite3.connect(uri + suffix, uri=True)
            rows = conn.execute('SELECT id, cwd FROM threads').fetchall()
            conn.close()
            return {r[0]: (r[1] or '') for r in rows}
        except Exception:
            continue
    return {}


def run_sub_exporters(root: Path, include_codex: bool = False) -> list[dict]:
    exporters = ['export_antigravity.py', 'export_cursor.py', 'export_claude.py']
    if include_codex:
        exporters.insert(0, 'export_codex.py')

    errors = []
    for exporter in exporters:
        script_path = root / '_同步' / exporter
        if not script_path.is_file():
            continue
        try:
            res = subprocess.run(
                [sys.executable, str(script_path), '--output-root', str(root)],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180, check=False
            )
            if res.returncode != 0:
                detail = (res.stderr or res.stdout or f'exit code {res.returncode}').strip()
                errors.append({'script': exporter, 'error': detail[:400]})
        except Exception as exc:
            errors.append({'script': exporter, 'error': str(exc)[:400]})
    return errors


def auto_archive_profile_if_needed(root: Path, threshold_lines: int) -> dict | None:
    """Automatically cold-archive older dated sections of 个人档案.md when approaching budget."""
    if archive_profile is None or threshold_lines <= 0:
        return None
    profile_path = root / '个人档案.md'
    if not profile_path.is_file():
        return None
    line_cnt = len(profile_path.read_text(encoding='utf-8', errors='replace').splitlines())
    if line_cnt <= threshold_lines:
        return None
    before_month = datetime.now(CST_TZ).strftime('%Y-%m')
    res = archive_profile(root, before_month)
    return res if res.get('archived_sections', 0) > 0 else None


def sync_vectors_subprocess(root: Path, skip: bool = False) -> dict:
    embed_script = root / '_同步' / 'kb_embed.py'
    if skip or not VEC_PYTHON.is_file() or not embed_script.is_file():
        return {'status': 'skipped'}
    try:
        res = subprocess.run(
            [str(VEC_PYTHON), str(embed_script), '--root', str(root), '--sync'],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120, check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout.strip())
        return {'status': f'rc_{res.returncode}'}
    except Exception as exc:
        return {'status': f'err:{str(exc)[:120]}'}


def probe_active_repos(active_repos: list):
    rows = [
        '## 零、 本地活跃仓库实时 Git 快照（自动探针）',
        '',
        '| 项目仓库 | 当前分支 | 工作区状态 | 最近一次提交 |',
        '| --- | --- | --- | --- |',
    ]
    sig_items = []
    for label, path_str in active_repos:
        path = Path(path_str)
        if not (path / '.git').exists():
            continue
        try:
            branch = subprocess.run(
                ['git', '-C', str(path), 'rev-parse', '--abbrev-ref', 'HEAD'],
                capture_output=True, text=True, timeout=3
            ).stdout.strip() or '未知'
            status_lines = [
                l for l in subprocess.run(
                    ['git', '-C', str(path), 'status', '--porcelain'],
                    capture_output=True, text=True, timeout=3
                ).stdout.splitlines() if l.strip()
            ]
            dirty_text = f'有未提交改动 ({len(status_lines)} 项)' if status_lines else '干净 (Clean)'
            last_commit = subprocess.run(
                ['git', '-C', str(path), 'log', '-1', '--date=format:%Y-%m-%d %H:%M', '--format=%h · %cd · %s'],
                capture_output=True, text=True, timeout=3
            ).stdout.strip() or '无提交记录'
            last_commit_clean = clean(last_commit[:70])
            rows.append(f'| `{label}` | `{branch}` | {dirty_text} | {last_commit_clean} |')
            sig_items.append((label, branch, len(status_lines), last_commit[:40]))
        except Exception:
            continue
    return '\n'.join(rows), sig_items


def update_active_projects_git_table(root: Path, table_md: str):
    proj_file = root / '笔记' / '进行中的项目.md'
    if not proj_file.is_file():
        return
    content = proj_file.read_text(encoding='utf-8')
    pattern = re.compile(r'(<!-- AUTO_GIT_STATUS_START -->).*?(<!-- AUTO_GIT_STATUS_END -->)', re.S)
    if pattern.search(content):
        new_content = pattern.sub(f'\\1\n{table_md}\n\\2', content)
        if new_content != content:
            atomic_write_text(proj_file, new_content)


def _should_run_restore_verify(cfg: dict, state: dict, force: bool, skip: bool) -> bool:
    if skip or verify_restore is None:
        return False
    if force or state.get('restore_verified') is None or state.get('restore_verified') is False:
        return True
    last_epoch = state.get('restore_verified_epoch')
    if not isinstance(last_epoch, (int, float)):
        return True
    interval_sec = float(cfg.get('restore_verify_interval_hours', 24)) * 3600.0
    return (datetime.now(timezone.utc).timestamp() - float(last_epoch)) >= interval_sec


def _write_sync_report(
    root: Path,
    now: str,
    codex: list,
    chatgpt: list,
    cursor: list,
    antigravity: list,
    claude: list,
    exporter_errors: list,
    doctor_res: dict,
    search_stats: dict,
    vector_stats: dict,
    indexed_chunks: int,
    bundle_path: Path,
    icloud_desc: str,
    pipeline_state: dict,
    preserved_heartbeat: str,
    pending: list,
    limits: list,
) -> None:
    report_path = root / '_同步' / '同步报告.md'
    error_section = (
        ['- 所有本地子导出脚本运行正常，无异常报错。']
        if not exporter_errors
        else [f"- **`{err['script']}` 异常**：{clean(err['error'])}" for err in exporter_errors]
    )
    heartbeat_section = (
        ['<!-- CODEX_HEARTBEAT_START -->', preserved_heartbeat, '<!-- CODEX_HEARTBEAT_END -->', '']
        if preserved_heartbeat else ['<!-- CODEX_HEARTBEAT_START -->', '<!-- CODEX_HEARTBEAT_END -->', '']
    )

    rv = pipeline_state.get('restore_verified')
    rv_at = pipeline_state.get('restore_verified_at') or '尚未运行'
    if rv is True:
        restore_desc = f'**已通过**（`{rv_at}`，独立临时目录 `git clone` + `HEAD` + `kb_doctor` 全通过）'
    elif rv is False:
        restore_desc = f"**⚠️ 失败**（`{rv_at}`：{pipeline_state.get('restore_error') or '校验异常'}）"
    else:
        restore_desc = '待首次演练（可通过 `_同步/kb_restore_verify.py` 触发）'

    remote_name = load_kb_config(root).get('private_git_remote')
    if not remote_name:
        private_remote_desc = '未配置（可在 `kb_config.json` 配置私有远端 `private_git_remote`）'
    else:
        pr_ok = pipeline_state.get('private_remote_ok')
        pr_at = pipeline_state.get('private_remote_pushed_at') or '尚未推送'
        if pr_ok is True:
            private_remote_desc = f'**已同步**（私有远端 `{remote_name}`，最近推送 `{pr_at}`）'
        elif pr_ok is False:
            private_remote_desc = f"**⚠️ 失败**（私有远端 `{remote_name}`：{pipeline_state.get('private_remote_note') or '推送异常'}）"
        else:
            private_remote_desc = f'已配置 `{remote_name}`，待首次推送'

    freelist_pages = search_stats.get('freelist_pages', 0)
    db_bytes = search_stats.get('db_bytes')
    db_mb_txt = f"，{db_bytes / (1024 * 1024):.1f} MB，空闲页 {freelist_pages}" if isinstance(db_bytes, int) else ''
    compact_txt = '，已自动整理碎片' if search_stats.get('compacted') else ''

    vec_total = vector_stats.get('total_vectors')
    vec_bytes = vector_stats.get('db_bytes')
    if isinstance(vec_total, int) and vec_total > 0:
        vec_mb = f"，{vec_bytes / (1024 * 1024):.1f} MB" if isinstance(vec_bytes, int) else ''
        vec_desc = f"`{vector_stats.get('mode', 'incremental_vec')}`（共 {vec_total} 条语义向量{vec_mb}）"
    else:
        vec_desc = f"`{vector_stats.get('status', '未启用')}`"

    secret_leaks = doctor_res.get('secret_leaks', [])
    secret_desc = f'⚠️ 发现 {len(secret_leaks)} 处' if secret_leaks else '已通过'
    stale_warns = doctor_res.get('stale_warnings', [])
    stale_desc = '**0 处过期**' if not stale_warns else f"**⚠️ {len(stale_warns)} 处待复核**（{'；'.join(w['file'] + ':' + w['section'] for w in stale_warns[:3])}）"

    if (root / '.git').exists():
        report = [
            '# 同步报告', '', f'报告生成：{now}', '',
            '| 来源 | 已归档对话 | 可见消息 | 覆盖说明 |',
            '| --- | ---: | ---: | --- |',
            f'| Codex 本地 | {len(codex)} | {sum(x["message_count"] for x in codex):,} | 本机可枚举的人机主会话；正文来源与缺失附件见单条记录。 |',
            f'| ChatGPT | {len(chatgpt)} | {sum(x["message_count"] for x in chatgpt):,} | 已发现聊天的工具可见内容；不等于全账号或单条对话全部历史。 |',
            f'| Cursor 本地 | {len(cursor)} | {sum(x["message_count"] for x in cursor):,} | 本机 `Cursor/User/globalStorage/state.vscdb` 可读取的人机主会话文字。 |',
            f'| Antigravity 本地 | {len(antigravity)} | {sum(x["message_count"] for x in antigravity):,} | 本机 `~/.gemini/antigravity/brain` 可读取的人机主会话文字。 |',
            f'| Claude Code 本地 | {len(claude)} | {sum(x["message_count"] for x in claude):,} | 本机 `~/.claude/projects` 可读取的人机主会话文字。 |',
            '',
            '## 导出器执行状态', '',
            *error_section,
            '',
            '## 知识库健康度巡检（KB Doctor & 灾备）', '',
            f"- 脱敏安全门禁（`共享摘录/` 与公开导出目录）：**{'已通过' if not doctor_res['sanitization_violations'] else '存在违规'}**",
            f"- 凭证隔离门禁（`对话/` 与核心档案无原始 Secret/Token）：**{secret_desc}**",
            f"- 内部链接完整性检查（Broken Links）：**{len(doctor_res['broken_links'])} 个死链**",
            f"- 个人档案来源引用检查（Citation Audit）：**{len(doctor_res['missing_citations'])} 处缺失**",
            f"- 活页上下文行数预算监控（Context Budget）：**{'全部在预算内' if not doctor_res['budget_warnings'] else '；'.join(doctor_res['budget_warnings'])}**",
            f"- 活页时效性巡检（Staleness）：{stale_desc}",
            f"- 配置文件与清单校验（Config Integrity）：**{len(doctor_res.get('config_errors', []))} 个异常**",
            f"- 增量 FTS5 索引状态：`{search_stats.get('mode', 'unknown')}`（更新 {search_stats.get('updated_files', 0)} 文件，共 {indexed_chunks} 块{db_mb_txt}{compact_txt}）",
            f"- 语义向量索引状态：{vec_desc}",
            f"- Git `pre-commit` 硬门禁：**{'已激活' if (root / '.git' / 'hooks' / 'pre-commit').is_file() else '未激活'}**",
            f"- 本地原子校验镜像：`{bundle_path}`",
            f"- 私有 Git 远端备份状态：{private_remote_desc}",
            f"- iCloud 异地容灾状态：{icloud_desc}",
            f"- 灾备恢复演练状态：{restore_desc}",
            '',
            *heartbeat_section,
            '## 尚未完整覆盖', '',
            '- ChatGPT 当前读取工具经实测只返回有限的近期片段（本次多为 5 轮），即使请求 10 轮且返回无下一页，也不能据此认定整条聊天完整。',
            '- 列表工具有近期条数限制。网页公开侧栏发现的额外 ID 会加入清单；尚未发现的旧聊天、其他分支或已删除内容无法凭空补齐。',
            '- 非文本附件、语音、图片和历史生成文件目前仅保留可获得的引用与占位，不能据此还原内容。',
            '- 系统与开发者指令、隐藏推理、工具载荷和内部子代理记录不属于本知识库的人机正文范围。',
            '- 本次归档是有时间点的快照，运行期间新增的最后几条消息会由下一次增量同步补入。',
            '- 要补齐 ChatGPT 全量历史正文，可将官方数据导出包放入本知识库的“导入”目录，再让本聊天导入；没有导出包时不标记“全历史完成”。', '',
            f'## 已发现但正文暂不可读取的 ChatGPT 对话（{len(pending)} 条）', '',
            '以下聊天在网页侧栏存在，但应用读取工具返回“找不到会话”。保留标题和原链接作为待补齐清单。', ''
        ]
        for item in pending:
            report.append(f'- [{clean(item["title"])}](https://chatgpt.com/c/{item["id"]}) · `{item["id"]}`')
        report += ['', '## 单条读取限制', '']
        for item in limits:
            report.append(f'- [{clean(item["title"])}](../{item["path"]})：' + '；'.join(item['limitations']))
    else:
        report = [
            '# 同步报告', '', f'报告生成：{now}', '',
            '## 导出器执行状态', '', *error_section, '',
            *heartbeat_section,
        ]
    atomic_write_text(report_path, '\n'.join(report) + '\n')


def run_pipeline(args) -> dict:
    root = args.output_root.resolve()
    cfg = load_kb_config(root)
    pipeline_state = load_pipeline_state(root)

    if (root / '.git').is_dir():
        ensure_pre_commit_hook(root)

    extra_dirs = [] if args.skip_external_checks else None
    pre_violations = check_sanitization(root, extra_public_dirs=extra_dirs)
    if pre_violations:
        raise RuntimeError(f"安全门禁阻断：共享摘录发现未脱敏内容: {json.dumps(pre_violations, ensure_ascii=False)}")

    exporter_errors = run_sub_exporters(root, include_codex=args.include_codex)
    auto_archive_res = auto_archive_profile_if_needed(root, int(cfg.get('profile_auto_archive_lines', 260)))

    sig_path = root / '_同步' / 'index-state-signature.json'
    prev_sig_data = json.loads(sig_path.read_text(encoding='utf-8')) if sig_path.is_file() else {}

    if args.skip_repo_probe:
        repo_sig = prev_sig_data.get('repos_sig', [])
    else:
        git_table_md, repo_sig = probe_active_repos(cfg['active_repos'])
        update_active_projects_git_table(root, git_table_md)

    cwd_map = {} if args.skip_external_checks else load_codex_cwds()
    records = []
    for source in ['codex', 'chatgpt', 'cursor', 'antigravity', 'claude']:
        manifest = root / '_同步' / f'{source}-manifest.json'
        if manifest.exists():
            records.extend(json.loads(manifest.read_text(encoding='utf-8')))
    records.sort(key=lambda x: x.get('updated_at', ''), reverse=True)

    groups = defaultdict(list)
    for record in records:
        groups[category(record, cwd_map, cfg=cfg)].append(record)

    topic_dir = root / '主题'
    topic_dir.mkdir(parents=True, exist_ok=True)

    codex = [x for x in records if x['source'] == 'codex']
    chatgpt = [x for x in records if x['source'] == 'chatgpt']
    cursor = [x for x in records if x['source'] == 'cursor']
    antigravity = [x for x in records if x['source'] == 'antigravity']
    claude = [x for x in records if x['source'] == 'claude']

    discovered_path = root / '_同步' / 'chatgpt-discovered.json'
    discovered = json.loads(discovered_path.read_text(encoding='utf-8')) if discovered_path.exists() else []
    readable_ids = {x['id'] for x in chatgpt}
    raw_chatgpt_dir = root / '_同步' / 'chatgpt-raw'
    pending = [
        x for x in discovered
        if x['id'] not in readable_ids
        and not ((raw_chatgpt_dir / x['id']).is_dir() and any((raw_chatgpt_dir / x['id']).glob('*.json')))
    ]
    limits = [x for x in records if x.get('limitations')]

    search_stats: dict = {}
    indexed_chunks = build_search_index(root, stats_out=search_stats)
    vector_stats = sync_vectors_subprocess(root, skip=(args.skip_external_checks or getattr(args, 'skip_embeddings', False)))
    if refresh_retrospect is not None:
        try:
            refresh_retrospect(root)
        except Exception:
            pass

    notes_hashes = []
    for sub in ['笔记', '笔记/历史档案', '共享摘录']:
        sub_dir = root / sub
        if sub_dir.is_dir():
            for p in sorted(sub_dir.glob('*.md')):
                notes_hashes.append((p.relative_to(root).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest()))
    for top_file in ['个人档案.md', 'INDEX.md', '同步规则.md', '跨设备配置.md', '_同步/kb_config.json']:
        p = root / top_file
        if p.is_file():
            notes_hashes.append((top_file, hashlib.sha256(p.read_bytes()).hexdigest()))

    state_payload = {
        'records': [(r['id'], r['source'], r.get('updated_at'), r.get('message_count'), category(r, cwd_map, cfg=cfg)) for r in records],
        'pending_ids': [x['id'] for x in pending],
        'notes': notes_hashes,
        'repos': repo_sig,
        'exporter_errors': exporter_errors,
    }
    state_sig = hashlib.sha256(json.dumps(state_payload, ensure_ascii=False).encode()).hexdigest()

    report_path = root / '_同步' / '同步报告.md'
    preserved_heartbeat = extract_codex_heartbeat_block(report_path)

    if prev_sig_data.get('signature') == state_sig and (root / 'README.md').is_file() and (root / '对话索引.md').is_file() and report_path.is_file():
        now = prev_sig_data.get('timestamp') or datetime.now(CST_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')
        changed_state = False
    else:
        now = datetime.now(CST_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')
        changed_state = True

    def table(items, prefix=''):
        rows = ['| 日期 | 来源 | 对话 | 可见消息数 |', '| --- | --- | --- | ---: |']
        for item in items:
            title = clean(item['title'])
            rows.append(f"| {item.get('updated_at', '未知')[:10]} | {item['source']} | [{title}]({prefix}{item['path']}) | {item['message_count']} |")
        return rows

    source_summary = f'Codex {len(codex)} 条、ChatGPT {len(chatgpt)} 条、Cursor {len(cursor)} 条、Antigravity {len(antigravity)} 条、Claude Code {len(claude)} 条'

    if changed_state:
        index = [
            '# 对话索引', '', f'索引生成：{now}', '',
            '按会话 ID 去重；点击标题查看来源、正文和读取限制。优先按明确语义关键词分类，通用标题按工作区路径（`cwd`）自动兜底。', '',
            f'共 {len(records)} 条对话，{sum(x["message_count"] for x in records):,} 条可见消息（{source_summary}）。', ''
        ]
        category_order = ['前端与工作项目', '工具与设备', '财富与投资', '健康与生活', '个人成长与关系', '产品与个人项目', '其他与待整理']
        for name in category_order:
            items = groups.get(name, [])
            if not items:
                continue
            index += [f'- [{name}](主题/{name}.md)：{len(items)} 条']
            lines = [f'# {name}', '', '这是按语义关键词与工作区路径自动生成的入口，不将对话中所有内容认定为事实。手写知识整理见[个人档案](../个人档案.md)与[INDEX.md](../INDEX.md)。', ''] + table(items, '../')
            atomic_write_text(topic_dir / f'{name}.md', '\n'.join(lines) + '\n')
        index += ['', '## 全部对话', ''] + table(records)
        atomic_write_text(root / '对话索引.md', '\n'.join(index) + '\n')

        readme = [
            '# Agent Memory KB（AI 原生个人知识库）', '',
            '本地优先（Local-First）Markdown 知识库：自动归档多端 AI 对话，逐步提炼事实、决策、项目断点与避坑铁律。', '',
            f'主目录：`{root}`。', '',
            f'当前归档 **{len(records)} 条对话、{sum(x["message_count"] for x in records):,} 条可见消息**。其中 {source_summary}。', '',
            '## 核心导航（「三层隔离 + 四级漏斗」架构）', '',
            '- **第一入口（Agent 渐进式披露索引）**：[INDEX.md（按场景快速分流）](INDEX.md) · [三层隔离与四级漏斗架构说明](笔记/知识库架构与治理.md)',
            '- **L3 主题状态活页（优先查阅）**：',
            '  - [进行中的项目（含各仓库实时 Git 快照与工程断点）](笔记/进行中的项目.md)',
            '  - [AI 协作错题本（跨端高频防重犯铁律与自动待复盘队列）](笔记/AI协作错题本.md)',
            '- **L2 长期事实基线与变更日志**：[个人档案与已确认约定（含顶部最新有效状态总表）](个人档案.md)',
            '- **L1 原始对话归档与审计**：[全部对话及主题索引](对话索引.md) · [同步报告与健康度巡检](_同步/同步报告.md) · [跨设备配置](跨设备配置.md)', '',
            '## 毫秒级检索与自检工具（CLI & MCP）', '',
            '- **混合语义 + FTS5 检索**：`python3 _同步/kb_search.py -q "关键词或自然语言问题"`（支持 `--mode hybrid|fts|vector`、`--source`、`--role`、`--cwd`、`--since`、`--stats`、`--vacuum`）',
            '- **跨端只读 MCP 服务**：`_同步/kb_mcp.py`（提供 `kb_overview` / `kb_search` / `kb_project_context` / `kb_mistakes_and_guardrails` / `kb_read_note`）',
            '- **健康度巡检**：`python3 _同步/kb_doctor.py`（已绑定 Git `pre-commit` 钩子：死链、引用合规、预算、时效、配置与多维 DLP 脱敏门禁）',
            '- **灾备还原演练**：`python3 _同步/kb_restore_verify.py --check-icloud --json`（临时隔离目录完整解包还原并校验）', '',
            '## 使用方法', '',
            '用任意 Markdown 阅读器或 Obsidian 打开本目录；也可以直接搜索文件内容。原始对话按来源和会话 ID 保存，避免重名覆盖。手写整理请放在“个人档案.md”或新建笔记中，自动索引和对话文件由同步程序维护。', '',
            '## 覆盖边界', '',
            'Codex、Cursor、Antigravity 与 Claude Code 归档覆盖本机可读取的人机主会话。ChatGPT 目前归档的是已发现聊天的工具可见内容，存在历史正文和附件缺口；不能称为全账号全部对话备份。具体范围见同步报告。', '',
            '知识库只保存在本地。本地持久化和定时整理不等同于所有新聊天会自动加载这些文件；需要引用时可指向本目录或本页。', '',
            f'最近索引刷新：{now}', ''
        ]
        atomic_write_text(root / 'README.md', '\n'.join(readme))

    doctor_res = run_doctor(
        root,
        extra_public_dirs=extra_dirs,
        staleness_days=int(cfg.get('staleness_warn_days', 14)),
    )
    if doctor_res['sanitization_violations']:
        raise RuntimeError(f"安全门禁阻断：共享摘录发现未脱敏内容: {json.dumps(doctor_res['sanitization_violations'], ensure_ascii=False)}")

    bundle_verified = bool(args.bundle_path.is_file())
    icloud_synced = False
    icloud_note = None
    gc_ran = False
    private_remote_note = None
    icloud_target = None if args.skip_icloud else args.icloud_bundle_path

    def _commit_and_bundle(force_bundle: bool = False) -> tuple[bool, bool]:
        nonlocal gc_ran
        subprocess.run(['git', '-C', str(root), 'add', '-A'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        diff_res = subprocess.run(['git', '-C', str(root), 'diff', '--cached', '--quiet'], check=False)
        committed = False
        if diff_res.returncode != 0:
            commit_ts = datetime.now(CST_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')
            commit_res = subprocess.run(
                ['git', '-C', str(root), 'commit', '-m', f'chore(kb): auto snapshot ({commit_ts})'],
                capture_output=True, text=True, check=False
            )
            if commit_res.returncode != 0:
                raise RuntimeError(f"git commit 被门禁或冲突阻断: {commit_res.stderr or commit_res.stdout}")
            gc_ran = maybe_git_gc(root, cfg['git_gc_loose_threshold']) or gc_ran
            committed = True
        head_sha = get_git_head(root)
        need_local_bundle = bool(
            committed
            or force_bundle
            or not args.bundle_path.is_file()
            or (head_sha and pipeline_state.get('bundle_head') != head_sha)
        )
        rebuilt_bundle = False
        if need_local_bundle and head_sha:
            atomic_create_local_bundle(root, args.bundle_path)
            pipeline_state['bundle_head'] = head_sha
            pipeline_state['bundle_verified'] = True
            pipeline_state['bundle_mtime'] = args.bundle_path.stat().st_mtime
            rebuilt_bundle = True
        return committed, rebuilt_bundle

    if (root / '.git').exists():
        prev_icloud_alert = prev_sig_data.get('icloud_alert_state')

        def _effective_icloud_status() -> tuple[str, str]:
            if args.skip_icloud and prev_icloud_alert and prev_icloud_alert != 'skipped':
                last_ic = pipeline_state.get('last_icloud_success_at')
                desc = (
                    f'本轮跳过 iCloud 写入（沿用后台状态，最近成功：`{last_ic}`）'
                    if last_ic else '本轮跳过 iCloud 写入（沿用后台状态）'
                )
                return desc, prev_icloud_alert
            return describe_icloud_status(args.bundle_path, icloud_target, cfg['icloud_lag_warn_hours'], prev_state=pipeline_state)

        icloud_desc, icloud_alert_state = _effective_icloud_status()
        restore_state_key = 'ok' if pipeline_state.get('restore_verified') is True else ('fail' if pipeline_state.get('restore_verified') is False else 'pending')
        remote_cfg_key = cfg.get('private_git_remote') or ''
        remote_state_key = f"{remote_cfg_key}:{'ok' if pipeline_state.get('private_remote_ok') is True else ('fail' if pipeline_state.get('private_remote_ok') is False else 'pending')}"
        need_report_refresh = (
            changed_state
            or prev_sig_data.get('doctor_healthy') != doctor_res['healthy']
            or prev_sig_data.get('icloud_alert_state') != icloud_alert_state
            or prev_sig_data.get('restore_state') != restore_state_key
            or prev_sig_data.get('remote_state') != remote_state_key
        )
        if need_report_refresh:
            _write_sync_report(
                root, now, codex, chatgpt, cursor, antigravity, claude,
                exporter_errors, doctor_res, search_stats, vector_stats, indexed_chunks,
                args.bundle_path, icloud_desc, pipeline_state,
                preserved_heartbeat, pending, limits,
            )

        committed_main, rebuilt_local = _commit_and_bundle()
        bundle_verified = bool(pipeline_state.get('bundle_verified'))

        if _should_run_restore_verify(cfg, pipeline_state, args.verify_restore, args.skip_restore_verify):
            rv_res = verify_restore(
                root=root,
                bundle_path=args.bundle_path,
                icloud_bundle_path=icloud_target,
                check_icloud=False,
                skip_external_checks=args.skip_external_checks,
                ignore_budget=True,
            )
            pipeline_state['restore_verified'] = bool(rv_res.get('ok'))
            pipeline_state['restore_verified_at'] = rv_res.get('verified_at')
            pipeline_state['restore_verified_epoch'] = datetime.now(timezone.utc).timestamp()
            pipeline_state['restore_error'] = rv_res.get('error')

        if icloud_target is not None and (rebuilt_local or icloud_needs_catchup(args.bundle_path, icloud_target, pipeline_state)):
            icloud_synced, icloud_note, icloud_encrypted = sync_icloud_mirror(root, args.bundle_path, icloud_target)
            pipeline_state['icloud_synced'] = icloud_synced
            pipeline_state['icloud_note'] = icloud_note
            pipeline_state['icloud_encrypted'] = icloud_encrypted
            if icloud_synced:
                pipeline_state['icloud_consecutive_failures'] = 0
                pipeline_state['last_icloud_success_at'] = datetime.now(CST_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')
            else:
                pipeline_state['icloud_consecutive_failures'] = int(pipeline_state.get('icloud_consecutive_failures', 0)) + 1

        post_icloud_desc, post_icloud_alert_state = _effective_icloud_status()
        post_restore_state_key = 'ok' if pipeline_state.get('restore_verified') is True else ('fail' if pipeline_state.get('restore_verified') is False else 'pending')

        def _sync_private_remote(force_push: bool = False):
            nonlocal private_remote_note
            if getattr(args, 'skip_private_remote', False):
                return
            rname = cfg.get('private_git_remote')
            cur_head = get_git_head(root)
            if rname and (force_push or committed_main or pipeline_state.get('private_remote_head') != cur_head):
                pr_ok, private_remote_note = maybe_push_private_remote(root, rname)
                pipeline_state['private_remote_ok'] = pr_ok
                pipeline_state['private_remote_note'] = private_remote_note
                if pr_ok:
                    pipeline_state['private_remote_head'] = cur_head
                    pipeline_state['private_remote_pushed_at'] = datetime.now(CST_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')

        _sync_private_remote()
        post_remote_state_key = f"{remote_cfg_key}:{'ok' if pipeline_state.get('private_remote_ok') is True else ('fail' if pipeline_state.get('private_remote_ok') is False else 'pending')}"

        if (
            post_icloud_alert_state != (prev_sig_data.get('icloud_alert_state') if not need_report_refresh else icloud_alert_state)
            or post_restore_state_key != (prev_sig_data.get('restore_state') if not need_report_refresh else restore_state_key)
            or post_remote_state_key != (prev_sig_data.get('remote_state') if not need_report_refresh else remote_state_key)
        ):
            _write_sync_report(
                root, now, codex, chatgpt, cursor, antigravity, claude,
                exporter_errors, doctor_res, search_stats, vector_stats, indexed_chunks,
                args.bundle_path, post_icloud_desc, pipeline_state,
                preserved_heartbeat, pending, limits,
            )
            committed_post, rebuilt_post = _commit_and_bundle()
            if committed_post:
                _sync_private_remote(force_push=True)
            if rebuilt_post and icloud_target is not None and icloud_synced:
                icloud_synced, icloud_note, icloud_encrypted = sync_icloud_mirror(root, args.bundle_path, icloud_target)
                pipeline_state['icloud_synced'] = icloud_synced
                pipeline_state['icloud_note'] = icloud_note
            icloud_alert_state = post_icloud_alert_state
            restore_state_key = post_restore_state_key
            remote_state_key = post_remote_state_key
        else:
            icloud_alert_state = post_icloud_alert_state
            restore_state_key = post_restore_state_key
            remote_state_key = post_remote_state_key

        if (
            need_report_refresh
            or changed_state
            or not sig_path.is_file()
            or prev_sig_data.get('icloud_alert_state') != icloud_alert_state
            or prev_sig_data.get('restore_state') != restore_state_key
            or prev_sig_data.get('remote_state') != remote_state_key
        ):
            atomic_write_text(
                sig_path,
                json.dumps({
                    'signature': state_sig,
                    'timestamp': now,
                    'repos_sig': repo_sig,
                    'doctor_healthy': doctor_res['healthy'],
                    'icloud_alert_state': icloud_alert_state,
                    'restore_state': restore_state_key,
                    'remote_state': remote_state_key,
                }, ensure_ascii=False, indent=2) + '\n',
            )
    else:
        icloud_alert_state = 'skipped'
        if changed_state or prev_sig_data.get('doctor_healthy') != doctor_res['healthy']:
            atomic_write_text(
                sig_path,
                json.dumps({'signature': state_sig, 'timestamp': now, 'repos_sig': repo_sig, 'doctor_healthy': doctor_res['healthy']}, ensure_ascii=False, indent=2) + '\n',
            )
            _write_sync_report(
                root, now, codex, chatgpt, cursor, antigravity, claude,
                exporter_errors, doctor_res, search_stats, vector_stats, indexed_chunks,
                args.bundle_path, '无 Git 仓库', pipeline_state,
                preserved_heartbeat, pending, limits,
            )

    pipeline_state['last_run_at'] = datetime.now(CST_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')
    pipeline_state['doctor_healthy'] = doctor_res['healthy']
    maybe_notify_issues(
        cfg=cfg,
        state=pipeline_state,
        icloud_alert_state=icloud_alert_state,
        icloud_note=icloud_note or pipeline_state.get('icloud_note'),
        exporter_errors=exporter_errors,
        doctor_res=doctor_res,
        bundle_verified=bundle_verified,
        skip_notify=(args.skip_external_checks or args.no_notify),
    )
    save_pipeline_state(root, pipeline_state)

    return {
        'conversations': len(records),
        'messages': sum(x['message_count'] for x in records),
        'codex': len(codex),
        'chatgpt': len(chatgpt),
        'cursor': len(cursor),
        'antigravity': len(antigravity),
        'claude': len(claude),
        'indexed_chunks': indexed_chunks,
        'vector_count': vector_stats.get('total_vectors', 0),
        'search_mode': search_stats.get('mode'),
        'search_updated_files': search_stats.get('updated_files', 0),
        'search_compacted': search_stats.get('compacted', False),
        'search_freelist_pages': search_stats.get('freelist_pages', 0),
        'auto_archived': auto_archive_res,
        'exporter_errors': exporter_errors,
        'doctor_healthy': doctor_res['healthy'],
        'stale_warnings': len(doctor_res.get('stale_warnings', [])),
        'git_gc_ran': gc_ran,
        'bundle_verified': bundle_verified,
        'icloud_synced': icloud_synced,
        'icloud_note': icloud_note,
        'icloud_alert_state': icloud_alert_state,
        'restore_verified': pipeline_state.get('restore_verified'),
        'private_remote_note': private_remote_note,
        'changed_state': changed_state,
        'topics': {k: len(v) for k, v in groups.items()},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-root', type=Path, required=True)
    parser.add_argument('--include-codex', action='store_true')
    parser.add_argument('--skip-repo-probe', action='store_true')
    parser.add_argument('--skip-external-checks', action='store_true')
    parser.add_argument('--skip-embeddings', action='store_true')
    parser.add_argument('--skip-icloud', action='store_true')
    parser.add_argument('--skip-private-remote', action='store_true')
    parser.add_argument('--skip-restore-verify', action='store_true')
    parser.add_argument('--verify-restore', action='store_true')
    parser.add_argument('--no-notify', action='store_true')
    parser.add_argument('--bundle-path', type=Path, default=DEFAULT_BUNDLE_BACKUP_PATH)
    parser.add_argument('--icloud-bundle-path', type=Path, default=DEFAULT_ICLOUD_BUNDLE_PATH)
    args = parser.parse_args()

    with sync_lock(args.output_root.resolve()):
        summary = run_pipeline(args)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
