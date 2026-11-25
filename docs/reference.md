# Complete Reference: Directory, Scripts, MCP API & Config / 完整参考手册

[← Back to README](../README.md) · [返回中文首页](../README.zh-CN.md)

---

## 1. Generated Knowledge Base Anatomy（初始化后的目录结构）

After running `python3 init_kb.py --root ~/personal-kb`:

```text
~/personal-kb/
├── INDEX.md                       # Hop-1 routing map (~30 lines) for all AI agents
├── 个人档案.md                     # L2 hot fact baseline (dated citations, auto cold-archived)
├── 对话索引.md                     # Auto-generated catalog of 5-source archived chats
├── 跨设备配置.md                   # Obsidian + Remotely Save (OneDrive E2EE) mobile guide
├── 笔记/
│   ├── 进行中的项目.md             # L3 live project board with auto-probed Git status
│   ├── AI协作错题本.md             # L3/L4 top guardrails + [复发 xN] mistake log
│   ├── 知识库架构与治理.md         # Governance rules & maintenance thresholds
│   └── 历史档案/                   # Monthly cold archives from archive_profile.py
├── 对话/                           # L1 read-only archived Markdown by AI tool
│   ├── Codex/
│   ├── Cursor/
│   ├── Antigravity/
│   ├── Claude/
│   └── ChatGPT/
├── 共享摘录/                       # Tier 3 public sanitized zone (enforced by kb_doctor)
└── _同步/                          # All 16 scripts, SQLite index, and sync reports
    ├── kb_config.json             # Single user configuration file
    ├── kb_search.sqlite           # Local FTS5 + vector DB (.gitignored)
    ├── 同步报告.md                 # Transparent incremental sync & gap report
    └── *.py
```

---

## 2. Read-Only MCP Server API Reference（5 个 MCP 原子工具）

`scripts/kb_mcp.py` anchors to `AGENT_MEMORY_KB_ROOT` (default `~/personal-kb`) and exposes 5 strictly read-only tools:

| Tool | Parameters & Defaults | Purpose | Security |
| --- | --- | --- | --- |
| `kb_overview()` | none | Returns compact JSON with `INDEX.md`, health snapshot, and Top guardrails | Read-only |
| `kb_project_context(cwd_or_project, include_recent=True)` | `cwd_or_project: str`, `include_recent: bool = True` | Extracts matching active project checkpoints, live Git probe status, and recent archived turns | Read-only |
| `kb_mistakes_and_guardrails(include_pending=True, full_book=True)` | `include_pending: bool = True`, `full_book: bool = True` | Returns Top guardrails, full mistake book, and pending retrospect queue | Read-only |
| `kb_search_query(query, mode="hybrid", source="all", limit=8)` | `query: str`, `mode: "fts"\|"semantic"\|"hybrid"`, `source: "all"\|"notes"\|...`, `limit: int = 8` | Runs FTS5 / semantic / RRF hybrid search and returns compact trimmed JSON snippets | Read-only |
| `kb_read_note(rel_path, start_line=1, max_lines=160)` | `rel_path: str`, `start_line: int = 1`, `max_lines: int = 160` | Reads bounded plain-text line ranges (no JSON escape bloat) inside the KB | **Strict path-traversal guard** confined to KB root |

Example MCP registration ([`examples/mcp_config.example.json`](../examples/mcp_config.example.json)):

```json
{
  "mcpServers": {
    "personal-kb": {
      "command": "/usr/local/bin/python3",
      "args": ["/Users/YOUR_NAME/personal-kb/_同步/kb_mcp.py"],
      "env": {
        "AGENT_MEMORY_KB_ROOT": "/Users/YOUR_NAME/personal-kb"
      }
    }
  }
}
```

---

## 3. Complete 16-Script Reference（16 个核心脚本速查表）

| Script | Purpose | Dependencies |
| --- | --- | --- |
| `skills/agent-memory-kb/SKILL.md` | Standard Agent Skill (`npx skills add`) with guided first-time bootstrap, 3-Hop read & one-sentence distillation protocols | Any Agent Skills host |
| `install.sh` | Transparent POSIX one-line installer (`--dry-run`, `--profile dev\|general`, never edits global IDE/Agent configs) | `bash`, `git`, Python 3.9+ |
| `init_kb.py` | Bootstraps directory layout, templates, config, Git repo, and DLP `pre-commit` hook | Python 3.9+ stdlib |
| `scripts/export_codex.py` | Read-only incremental exporter for Codex sessions | Python stdlib |
| `scripts/export_cursor.py` | Read-only incremental exporter for Cursor workspace SQLite chats | Python stdlib |
| `scripts/export_antigravity.py` | Read-only incremental exporter for Antigravity transcripts | Python stdlib |
| `scripts/export_claude.py` | Read-only incremental exporter for Claude Code (filters thinking/tool blocks) | Python stdlib |
| `scripts/export_chatgpt.py` | Incremental importer/normalizer for ChatGPT exports | Python stdlib |
| `scripts/build_kb_index.py` | Main orchestrator: `flock`, 180s timeout, dual classification, live Git probe, FTS5 index, idempotent commit | Python stdlib |
| `scripts/kb_search.py` | CLI & library for `fts`, `vector`, and `hybrid` (RRF) search | Stdlib (`fts`); optional ONNX |
| `scripts/kb_embed.py` | Offline INT8 ONNX embedding builder with incremental file-signature tracking | Optional (`requirements.txt`) |
| `scripts/kb_mcp.py` | Standard Read-Only MCP server exposing 5 tools to AI agents | Optional (`mcp`) |
| `scripts/kb_retrospect.py` | Multi-correction detector & mistake-book review queue manager | Python stdlib |
| `scripts/archive_profile.py` | Monthly cold-archiver for `个人档案.md` with automatic link rebasing | Python stdlib |
| `scripts/kb_filters.py` | DLP v2 secret redaction, noise filters, and sanitization rules | Python stdlib |
| `scripts/kb_doctor.py` | 7-dimension health gate outputting structured JSON | Python stdlib |
| `scripts/kb_backup.py` | Atomic `git bundle` + Keychain AES-256 iCloud mirror + private Git remote | Stdlib + `openssl` |
| `scripts/kb_restore_verify.py` | Isolated restore drill verifying 4-way Git `HEAD` parity | Stdlib + `openssl` |
| `scripts/test_kb_pipeline.py` | Automated regression tests for the full pipeline (including recursive `资料/**/*.md` indexing) | Python stdlib |

---

## 4. Full `kb_config.json` Reference（完整配置项说明）

Edit `~/personal-kb/_同步/kb_config.json` to customize your knowledge base without modifying script code:

```json
{
  "active_repos": [
    {"name": "my-frontend-app", "path": "/Users/you/workspace/my-frontend-app"},
    {"name": "my-backend-api", "path": "/Users/you/workspace/my-backend-api"}
  ],
  "cwd_categories": [
    ["工程与工作项目", "/work/|/projects/|frontend|backend"],
    ["产品与个人项目", "side-project|indie|personal-project"],
    ["学习与成长", "learning|study|agent-memory-kb"]
  ],
  "title_categories": [
    ["工程与工作项目", "前端|后端|设计文档|接口|组件|React|Vue|TypeScript|Python|bug|修复|重构|CI"],
    ["工具与效率", "Codex|ChatGPT|Claude|Cursor|Antigravity|Skill|MCP|Obsidian|自动化"]
  ],
  "synonyms": [
    ["个人知识库", "PKM", "知识库"],
    ["容灾", "灾备", "备份"]
  ],
  "sanitization_extra_patterns": [],
  "extra_public_dirs": [],
  "git_gc_loose_threshold": 300,
  "icloud_lag_warn_hours": 4,
  "restore_verify_interval_hours": 24,
  "profile_auto_archive_lines": 260,
  "staleness_warn_days": 14,
  "macos_notify_on_error": true,
  "alert_cooldown_hours": 12,
  "private_git_remote": "private-backup",
  "noise_filter": {
    "drop_zero_message": true,
    "probe_title_regex": "^Reply (?:with )?exactly (?:[A-Z0-9_]+_)?OK\\b",
    "trivial_user_regex": "^(?:你好|您好|hi|hello)[!！?？。.,，\\s]*$",
    "excluded_ids": []
  }
}
```

---

## 5. Daily CLI Cheat Sheet（日常高频命令速查）

```bash
# Hybrid RRF search (JSON output)
python3 ~/personal-kb/_同步/kb_search.py --root ~/personal-kb -q "field mapping" --mode hybrid --json

# Search curated notes only
python3 ~/personal-kb/_同步/kb_search.py --root ~/personal-kb -q "backup" --source notes --limit 5

# Scan for multi-correction high-friction sessions
python3 ~/personal-kb/_同步/kb_retrospect.py --root ~/personal-kb scan

# Mark a retrospect candidate as distilled
python3 ~/personal-kb/_同步/kb_retrospect.py --root ~/personal-kb resolve --path "对话/Codex/xxx.md"

# Run 4-way disaster recovery drill (including iCloud AES-256 decrypt verify)
python3 ~/personal-kb/_同步/kb_restore_verify.py --root ~/personal-kb --check-icloud

# Strict health check including staleness audit
python3 ~/personal-kb/_同步/kb_doctor.py --root ~/personal-kb --strict-staleness

# Run the regression suite
PERSONAL_KB_SKIP_KEYCHAIN=1 python3 -m unittest discover -s scripts -p 'test_*.py' -v
```
