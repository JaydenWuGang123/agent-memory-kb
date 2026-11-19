# agent-memory-kb（AI 原生个人知识库与跨端记忆引擎）

[English README](README.md)

面向多 Coding Agent 开发者（**Codex / Claude Code / Cursor / Antigravity / ChatGPT**）的**本地优先（Local-First）、Git 驱动、自带离线混合检索 MCP + 错题本自进化 + 工业级防漏容灾**个人知识库引擎。

零云端记忆 API 费用，数据完全留在本地 Markdown + Git 中，可选配合 Obsidian + OneDrive（RClone Crypt 端到端加密）零成本同步至手机端。

---

## 核心特性

1. **五端对话零摩擦自动归档**：只读导出 Codex、Cursor、Antigravity、Claude Code、ChatGPT 对话为可追溯 Markdown，按标题语义与工作区路径双层分类。
2. **离线混合检索（零 API 成本）**：SQLite FTS5（中文单字分词 + 长问句软挽救）+ 本地 INT8 ONNX 多语种语义向量 + RRF 混合排序（`scripts/kb_search.py`、`scripts/kb_embed.py`）。
3. **跨端原生只读 MCP Server**：一套 `scripts/kb_mcp.py` 同时接入四大 Coding Agent，提供 `kb_overview`、`kb_search_query`、`kb_project_context`、`kb_mistakes_and_guardrails`、`kb_read_note`，内置严格路径穿越防护。
4. **AI 协作错题本闭环**：`scripts/kb_retrospect.py` 自动扫描历史多轮纠偏会话，归并沉淀为带 `[复发 xN]` 计数的防重犯铁律，避免 AI 反复踩同一个坑。
5. **工业级 DLP 安全门禁与健康巡检**：`scripts/kb_filters.py` + `scripts/kb_doctor.py` 绑定 Git `pre-commit` 硬拦截，自动检查死链、缺来源引用、活页超行预算、项目状态过期、脱敏违规与多维凭证防漏（JWT / Bearer / Cookie / DB DSN / `.env` / 身份证 / 高熵密钥）。
6. **四端容灾与自动恢复演练**：本地 Git + 原子校验 `git bundle` + macOS Keychain AES-256 加密 iCloud 镜像（含解密回环校验与孤儿临时文件清理）+ 私有 Git 远端（防误推含 `public` 远端门禁）+ 独立临时目录克隆恢复演练（`scripts/kb_restore_verify.py`）。

---

## 5 分钟快速上手

### 1. 克隆并一键初始化你的本地知识库

```bash
git clone https://github.com/JaydenWuGang123/agent-memory-kb.git
cd agent-memory-kb

# 在 ~/personal-kb（或通过 --root 指定目录）生成你的独立本地知识库
python3 init_kb.py --root ~/personal-kb
```

### 2. 首次运行同步与体检（核心功能仅依赖 Python 3.9+ 标准库）

```bash
python3 ~/personal-kb/_同步/build_kb_index.py --output-root ~/personal-kb --skip-icloud
python3 ~/personal-kb/_同步/kb_doctor.py --root ~/personal-kb
python3 ~/personal-kb/_同步/kb_search.py --root ~/personal-kb -q "关键词或问题" --mode fts
```

### 3. 开启只读 MCP 与本地语义向量（可选）

```bash
pip install -r requirements.txt
```

参考 [`examples/mcp_config.example.json`](examples/mcp_config.example.json) 将 `~/personal-kb/_同步/kb_mcp.py` 注册进 Codex / Cursor / Claude Code / Antigravity，并参考 [`examples/agent-rules.md`](examples/agent-rules.md) 配置全局提示词。

### 4. 配置后台每 30 分钟自动同步与 iCloud 加密容灾（macOS）

1. 将 [`examples/personal-kb-sync.sh.example`](examples/personal-kb-sync.sh.example) 复制到 `~/.local/bin/personal-kb-sync.sh` 并赋予执行权限。
2. 在 macOS 钥匙串写入加密口令（不落盘明文）：
   ```bash
   security add-generic-password -U -a "$USER" -s "agent-memory-kb.icloud-bundle" -w
   ```
3. 按 [`examples/launchd.personal-kb-sync.plist.example`](examples/launchd.personal-kb-sync.plist.example) 配置 `launchd` 定时任务；如需后台写入 iCloud Drive，在系统「隐私与安全性 → 完全磁盘访问权限」授权 `/bin/zsh`。

### 5. 免费跨端加密同步至手机（Obsidian + OneDrive）

详见初始化后生成的 [`跨设备配置.md`](templates/跨设备配置.md)，使用 Obsidian + 社区插件 `Remotely Save`（OneDrive App Folder + RClone Crypt 端到端加密）即可零成本同步至 iPhone / Android / Windows。

---

## 运行单元测试

```bash
python3 -m unittest discover -s scripts -p 'test_*.py' -v
```

---

## 开源协议

[MIT](LICENSE)
