# Optional Track 2 Extension: Local MinerU + Qwen3-Embedding + WeKnora RAG

> **Core `agent-memory-kb` stays 100% pure Python with zero Docker/GPU required.**
> Install this optional Track 2 extension only when you need high-precision layout parsing for complex PDFs (multi-column tables, formulas, scanned slides, Office files) and cross-document deep RAG on a 16GB laptop with **\$0 cloud API cost**.

Full bilingual architecture, setup & pitfall guide: [`../../docs/optional-multimodal-rag.md`](../../docs/optional-multimodal-rag.md).

## Files Included

- [`scripts/weknora-ctl.sh`](scripts/weknora-ctl.sh) — One-command `start / stop / status / parse / sync` controller for 16GB laptops
- [`scripts/local_embed_server.py`](scripts/local_embed_server.py) — Local OpenAI-compatible `/v1/embeddings` API (`Qwen/Qwen3-Embedding-0.6B`, 1024d / 32K context on MPS/CUDA/CPU, Bearer auth, \$0 cost)
- [`scripts/kb_parse_mineru.py`](scripts/kb_parse_mineru.py) — Local MinerU 4.2+ parser outputting structured Markdown into `<kb_root>/资料/<category>/` + refreshing Track 1 SQLite/ONNX indexes
- [`scripts/kb_sync_weknora.py`](scripts/kb_sync_weknora.py) — Incremental DLP-redacted sync from `agent-memory-kb` to WeKnora (reuses `kb_filters`, polls async delete `404` to avoid `409 Duplicate`, defaults `summary_enabled: false`)
- [`scripts/weknora_mcp_bridge.py`](scripts/weknora_mcp_bridge.py) — Zero-dependency Stdio-to-Streamable-HTTP MCP bridge for Codex / Claude Code / Cursor / Antigravity (reads `chmod 600` `.mcp.env`, bypasses local proxy, graceful offline fallback)
- [`docker-compose.override.yml`](docker-compose.override.yml) — Pins `docreader:latest` alongside `WEKNORA_VERSION=main` for fast offline-safe restarts
- [`.sync.env.example`](.sync.env.example) & [`.mcp.env.example`](.mcp.env.example) — Sanitized credential templates
