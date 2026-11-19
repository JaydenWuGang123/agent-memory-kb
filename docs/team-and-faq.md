# Team Adoption, Enterprise Compliance & FAQ / 团队落地与常见问题

[← Back to README](../README.md) · [返回中文首页](../README.zh-CN.md)

---

## 1. Team Adoption & Enterprise Compliance（团队落地与企业内网合规）

`agent-memory-kb` is designed for both individual engineers and enterprise engineering teams:

1. **100% Intranet-Ready（零公网依赖）**: Point `private_git_remote` to your company's internal GitLab instance. Zero third-party cloud SaaS lock-in, zero recurring memory API fees.
2. **SSOT Separation（个人规范与团队规范严格隔离）**:
   - **Priority 1 (Read-Only)**: Shared team coding standards and API contracts always win and are never mutated by personal agents.
   - **Priority 2 (Personal Sandbox)**: New learnings and traps incubate safely in your local mistake book.
   - **Promotion Flywheel**: High-recurrence (`[复发 xN]`) findings validated across multiple projects are promoted into shared team standards or a team read-only MCP via normal MR review.
3. **3-Phase Low-Risk Rollout**:
   - **Week 1 (Individual Pilot)**: Voluntary 30-minute setup for active project checkpoints & personal mistake book.
   - **Month 1 (Team Mistake Book)**: Distill 3–5 recurring frontend/backend pitfalls per sprint into a shared team guardrail file.
   - **Quarter 1 (Team Read-Only MCP)**: Expose internal standards, component traps, and release playbooks via read-only MCP so new hires onboard in a single day.

---

## 2. Frequently Asked Questions（常见问题）

### Q1. Does `agent-memory-kb` upload my code or private notes to any third-party cloud?
**No.** All Markdown files, Git history, SQLite FTS5 indexes, and local ONNX embeddings stay 100% on your machine. Optional backups go strictly to targets *you* configure (your private GitLab/GitHub repo, AES-256 encrypted iCloud bundle, or client-side E2EE OneDrive App Folder).

### Q2. Can I use this inside a company with internal GitLab?
**Yes.** Simply point your local KB's `private_git_remote` to an internal private GitLab repo. Nothing requires external internet access once initialized.

### Q3. Do I need a GPU or paid embedding API for semantic search?
**No.** `kb_embed.py` runs a lightweight local INT8 ONNX multilingual model on CPU with content-hash caching. If you prefer zero extra dependencies, `--mode fts` runs on pure Python 3.9+ stdlib in milliseconds.

### Q4. Will the exporters modify or lock my Codex / Cursor / Claude databases?
**No.** Every exporter opens source files strictly read-only and has a 180-second timeout circuit breaker so one source never blocks or mutates another.

### Q5. How does it avoid bloating or slowing down after a year of heavy use?
SQLite FTS5 handles tens of thousands of messages in milliseconds; `kb_doctor.py` enforces line budgets on `INDEX.md` and hot notes; `archive_profile.py` automatically cold-archives older monthly sections into `笔记/历史档案/` and rebases relative Markdown links.

### Q6. Does it work on Linux or Windows?
All core Python scripts (init, exporters, FTS5/hybrid search, doctor, MCP server, tests, Git snapshots) are cross-platform across macOS, Linux, and Windows. Only macOS Keychain and `launchd` are macOS-specific — on Linux/Windows you can use `systemd` or Task Scheduler with environment passphrases.

### Q7. Why separate "verified facts" from "AI suggestions" and "Mock passes"?
If an AI logs a Mock-only pass or an unverified assumption as "backend integration complete", the next session builds on a false premise and compounds hallucinations. Explicit dated citations and verification boundaries keep long-term memory trustworthy.

### Q8. What happens if I accidentally paste an API key or secret into a note?
`.git/hooks/pre-commit` (powered by `kb_filters.py` + `kb_doctor.py`) immediately blocks the commit and reports the exact file and matched DLP rule.
