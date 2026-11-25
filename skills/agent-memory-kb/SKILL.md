---
name: agent-memory-kb
description: Local-first, Git-backed personal knowledge base and multi-agent memory engine (Codex, Claude Code, Cursor, Antigravity, ChatGPT). Use when the user asks to set up or initialize their personal knowledge base, save or distill confirmed decisions, project breakpoints, or recurring mistakes ("沉淀到知识库", "记到知识库", "更新项目进度", "记一下踩坑"), search past cross-agent conversations, run KB sync/health checks, or connect the read-only personal-kb MCP server.
---

# Agent Memory KB (`agent-memory-kb`)

Manage a local-first, Git-backed, AI-native Personal Knowledge Base (`~/personal-kb` by default, override via `AGENT_MEMORY_KB_ROOT`) across **Codex, Claude Code, Cursor, Antigravity, and ChatGPT** with 3-Hop Progressive Disclosure, offline CJK+ONNX hybrid search, automated mistake-book evolution, and DLP safety gates.

---

## 0. First-Time Guided Bootstrap (When KB Is Not Yet Initialized)

Before reading or writing, check whether `${AGENT_MEMORY_KB_ROOT:-$HOME/personal-kb}/_同步/build_kb_index.py` exists.

If it does **not** exist yet (for example, right after `npx skills add JaydenWuGang123/agent-memory-kb`), guide the user through safe first-time setup:

1. **Clarify only what is missing**:
   - **Profile**: `--profile dev` (software engineers: active Git repo probe + coding mistake book) or `--profile general` (PMs / writers / creators / lifelong learners: goal board + expression-preference guardrails).
   - **Target directory**: default `~/personal-kb`.
2. **Initialize cleanly** (note: `npx skills add` installs only `SKILL.md`, so fetch the engine repo to a temp directory or run `install.sh` — never assume `init_kb.py` sits beside `SKILL.md`):
   ```bash
   # Transparent one-line bootstrap (supports --dry-run; never modifies global IDE/Agent configs):
   curl -fsSL https://raw.githubusercontent.com/JaydenWuGang123/agent-memory-kb/main/install.sh | bash -s -- --root "$HOME/personal-kb" --profile dev
   ```
   Or clone and run `python3 init_kb.py --root "$HOME/personal-kb" --profile dev` manually, then run initial sync and health check:
   ```bash
   python3 "$HOME/personal-kb/_同步/build_kb_index.py" --output-root "$HOME/personal-kb" --skip-icloud
   python3 "$HOME/personal-kb/_同步/kb_doctor.py" --root "$HOME/personal-kb"
   ```
3. **Ask before modifying host configs or schedules**:
   - **MCP registration**: inspect the user's current client config first, show the minimal addition for `personal-kb` (`_同步/kb_mcp.py` with `AGENT_MEMORY_KB_ROOT`), and apply only after confirmation.
   - **Optional extras**: ask before installing `requirements.txt` (MCP + offline ONNX vectors), setting up background sync (`launchd` / `systemd` / Windows Task Scheduler), or configuring encrypted iCloud/OneDrive/private Git backups.

---

## 1. Progressive Disclosure Read Workflow (3-Hop Retrieval)

Never scan or grep the entire `对话/` archive directory blindly. Always follow 3-Hop Progressive Disclosure:

1. **Hop 1 — Routing Map (`~400 tokens`)**:
   - Prefer MCP tool `kb_overview()` (already includes `INDEX.md`, health snapshot, and Top guardrails — do **not** call `kb_mistakes_and_guardrails()` again in the same turn unless `full_book=True` or `include_pending=True` is needed).
   - Without MCP: read `<KB_ROOT>/INDEX.md`.
2. **Hop 2 — Living Dashboards & Guardrails (`~1,100 tokens`)**:
   - Project breakpoints & live Git probe: `kb_project_context(cwd_or_project)` or read `<KB_ROOT>/笔记/进行中的项目.md`.
   - Recurring mistakes & pre-flight guardrails: `kb_mistakes_and_guardrails()` or read `<KB_ROOT>/笔记/AI协作错题本.md`.
   - Confirmed long-term facts baseline: read `<KB_ROOT>/个人档案.md`.
3. **Hop 3 — On-Demand Historical Search (`~1,500 tokens`)**:
   - Prefer MCP `kb_search_query(query, mode="hybrid", limit=8)` and `kb_read_note(rel_path, start_line, max_lines)`.
   - CLI fallback:
     ```bash
     python3 "${AGENT_MEMORY_KB_ROOT:-$HOME/personal-kb}/_同步/kb_search.py" --root "${AGENT_MEMORY_KB_ROOT:-$HOME/personal-kb}" -q "your query" --mode hybrid --limit 8
     ```

---

## 2. One-Sentence Distillation & Commit Workflow (Write Protocol)

When the user asks to save a decision, progress checkpoint, or lesson learned into their knowledge base, follow these 4 steps:

### Step 1: Route by Single Source of Truth (SSOT)
- **Recurring AI collaboration mistakes / multi-correction lessons**: merge into `<KB_ROOT>/笔记/AI协作错题本.md` by root cause and increment `[复发 xN]`. If a matching candidate exists in `_同步/retrospect-queue.json`, mark it resolved:
  ```bash
  python3 "<KB_ROOT>/_同步/kb_retrospect.py" --root "<KB_ROOT>" resolve --path "<rel_path>" --note "distilled"
  ```
- **Active project checkpoints / contracts / verification boundaries**: update `<KB_ROOT>/笔记/进行中的项目.md`.
- **User-confirmed long-term facts, preferences, or decisions**: append a dated entry (`YYYY-MM-DD`) with source anchor to `<KB_ROOT>/个人档案.md`.
- **Sanitized reusable sharing notes**: write only thoroughly redacted generic practices (zero credentials, internal hostnames, private IPs, or personal paths) into `<KB_ROOT>/共享摘录/`.

### Step 2: Anti-Hallucination & Privacy Rules
1. **Always cite date & source**: every durable fact must include `YYYY-MM-DD` and a traceable source (archived conversation link, file, or explicit user confirmation in this turn).
2. **Separate facts from assumptions**: explicitly distinguish *user-confirmed rules*, *static/Mock checks*, and *real end-to-end verification*; mark unverified details as `待确认` (TBD).
3. **Never write secrets**: credentials, tokens, passwords, and private keys stay in OS Keychain or `chmod 600` `.env` files, never inside Markdown notes.
4. **Never modify team/shared standards silently**: keep personal conventions strictly inside personal files unless explicitly authorized.

### Step 3: Refresh Index, Health Gate & Local Git Snapshot
After editing knowledge base Markdown files, run:
```bash
python3 "${AGENT_MEMORY_KB_ROOT:-$HOME/personal-kb}/_同步/build_kb_index.py" --output-root "${AGENT_MEMORY_KB_ROOT:-$HOME/personal-kb}"
```
Check that `"doctor_healthy": true` and `"exporter_errors": []`.

### Step 4: Mistake-Book Auto-Retrospect Discipline
Whenever the same session requires **>= 2 user corrections or interruptions** before completing a task, proactively distill the root cause into `笔记/AI协作错题本.md` at wrap-up without waiting to be asked.

---

## 3. Maintenance & Optional Track 2 Multimodal RAG

- **Health audit**: `python3 "<KB_ROOT>/_同步/kb_doctor.py" --root "<KB_ROOT>"`
- **4-way disaster recovery drill**: `python3 "<KB_ROOT>/_同步/kb_restore_verify.py" --root "<KB_ROOT>" --json`
- **Monthly cold archive**: `python3 "<KB_ROOT>/_同步/archive_profile.py" --root "<KB_ROOT>" --before YYYY-MM`
- **Complex PDF / Office / Scanned Docs (Optional Track 2)**: `kb_search.py` automatically indexes `<KB_ROOT>/资料/**/*.md`. For local zero-API-cost GPU MinerU + Qwen3-Embedding-0.6B + on-demand WeKnora Docker RAG, refer to `docs/optional-multimodal-rag.md` and `extensions/weknora-rag/` in `JaydenWuGang123/agent-memory-kb`.
