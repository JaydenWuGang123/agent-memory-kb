# Cross-Agent Memory & Mistake-Book Protocol (Template for AGENTS.md / CLAUDE.md / GEMINI.md)

## 1. Progressive Disclosure Entry
- First hop: prefer the read-only `personal-kb` MCP tools (`kb_overview`, `kb_project_context`, `kb_search_query`, `kb_mistakes_and_guardrails`, `kb_read_note`).
- Token discipline: `kb_overview` already includes Top guardrails — do NOT call `kb_mistakes_and_guardrails` again in the same turn unless you explicitly need `full_book=True` or `include_pending=True`.
- Fallback without MCP: read `~/personal-kb/INDEX.md`, check `笔记/进行中的项目.md` for project breakpoints, check `笔记/AI协作错题本.md` for guardrails, or run:
  ```bash
  python3 ~/personal-kb/_同步/kb_search.py -q "your query" --mode hybrid
  ```

## 2. Automatic Mistake-Book Closed Loop
- Before multi-file edits, config changes, or external publishing, self-check against the top guardrails in `笔记/AI协作错题本.md`.
- Whenever the same session requires **>= 2 user corrections or interruptions** before getting a task right, automatically distill the root cause into `笔记/AI协作错题本.md` at wrap-up (merge recurring root causes and increment `[复发 xN]`, and resolve the candidate via `kb_retrospect.py resolve --path "<rel_path>"`).
