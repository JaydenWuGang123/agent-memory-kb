# Personal Knowledge Base Progressive Disclosure Entry (INDEX.md)

> **Purpose**: First-hop routing index for Coding Agents (Codex / Claude Code / Cursor / Antigravity) and human readers. Load only the relevant living note per task instead of dumping the whole vault into context.

---

## 1. Task Routing Table

| Task / Keywords | Canonical Source (SSOT) | Maintenance Boundary |
| --- | --- | --- |
| **AI collaboration pitfalls & guardrails**<br/>`mistakes`, `rework`, `verification` | [`笔记/AI协作错题本.md`](笔记/AI协作错题本.md)<br/>Pending queue: `_同步/retrospect-queue.json` | Merge recurring root causes with `[复发 xN]`; line budget `220`. |
| **Active project breakpoints & Git snapshots**<br/>`projects`, `branch`, `contracts` | [`笔记/进行中的项目.md`](笔记/进行中的项目.md) | Distinguish user-confirmed rules, mocks, and real integration tests. |
| **Architecture, DLP & disaster recovery**<br/>`backup`, `MCP`, `FTS5`, `ONNX` | [`笔记/知识库架构与治理.md`](笔记/知识库架构与治理.md) · [`跨设备配置.md`](跨设备配置.md) | Keep operational commands synced with scripts. |
| **Long-term facts & raw conversation evidence**<br/>`facts`, `history`, `citations` | [`个人档案.md`](个人档案.md) · [`对话索引.md`](对话索引.md) · [`_同步/同步报告.md`](_同步/同步报告.md) | Automated exporters maintain raw archives without rewriting history. |

---

## 2. Quick CLI & MCP Entry

- **Read-only MCP**: Register `_同步/kb_mcp.py` in Codex / Cursor / Claude Code / Antigravity (`kb_overview`, `kb_search_query`, `kb_project_context`, `kb_mistakes_and_guardrails`, `kb_read_note`).
- **Hybrid Search**: `python3 _同步/kb_search.py -q "your query" --mode hybrid --limit 10`
- **Health & DR Check**: `python3 _同步/kb_doctor.py` / `python3 _同步/kb_restore_verify.py --check-icloud --json`
