#!/usr/bin/env python3
"""Unit tests for Agent Memory KB pipeline scripts."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SYNC_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SYNC_DIR))

import archive_profile  # noqa: E402
import build_kb_index  # noqa: E402
import export_claude  # noqa: E402
import kb_doctor  # noqa: E402
import kb_filters  # noqa: E402
import kb_restore_verify  # noqa: E402
import kb_retrospect  # noqa: E402
import kb_search  # noqa: E402


class TestCategoryClassification(unittest.TestCase):
    def test_semantic_title_beats_work_cwd(self):
        rec_learning = {
            "id": "t1",
            "title": "英语学习规划与季度复盘",
            "cwd": "/Users/dev/work/demo-web",
        }
        rec_finance = {
            "id": "t2",
            "title": "ETF 定投预算与资产配置回测",
            "cwd": "/Users/dev/work/demo-web",
        }
        rec_generic = {
            "id": "t3",
            "title": "解决一下",
            "cwd": "/Users/dev/work/demo-web",
        }
        self.assertEqual(build_kb_index.category(rec_learning, {}), "学习与成长")
        self.assertEqual(build_kb_index.category(rec_finance, {}), "理财与规划")
        self.assertEqual(build_kb_index.category(rec_generic, {}), "工程与工作项目")


class TestDoctorChecks(unittest.TestCase):
    def test_sanitization_gate_catches_leaks_and_passes_clean(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared = root / "共享摘录"
            shared.mkdir(parents=True)
            (shared / "clean.md").write_text("# Clean\nNo secrets here.\n", encoding="utf-8")
            self.assertEqual(kb_doctor.check_sanitization(root, extra_public_dirs=[]), [])

            (shared / "leak.md").write_text("Internal url http://192.168.10.25/internal-api", encoding="utf-8")
            violations = kb_doctor.check_sanitization(root, extra_public_dirs=[])
            self.assertEqual(len(violations), 1)
            self.assertIn("私有 IP", violations[0]["rule"])

            nested = shared / "sub"
            nested.mkdir()
            (nested / "nested-leak.txt").write_text("Phone 13800138000", encoding="utf-8")
            violations2 = kb_doctor.check_sanitization(root, extra_public_dirs=[])
            self.assertEqual(len(violations2), 2)

            (root / "_同步").mkdir()
            (root / "_同步" / "kb_config.json").write_text("{broken json", encoding="utf-8")
            self.assertEqual(len(kb_doctor.check_config_integrity(root)), 1)

            (root / "对话" / "Codex").mkdir(parents=True)
            (root / "对话" / "Codex" / "tok.md").write_text("secret ya29.a0AehfTOn_dummy_token_123456789012\n", encoding="utf-8")
            self.assertEqual(len(kb_doctor.check_archive_secrets(root)), 1)

    def test_secret_redaction_and_noise_conversation_filters(self):
        raw = (
            "u@gmail.com|pass1234|rec@mail.org|jx5eyqn5ykgvy7pm37gadhsjxbqijrd3|2024\n"
            "[a@gmail.com](mailto:a@gmail.com)|passabcd|[b@mail.org](mailto:b@mail.org)|jx5eyqn5ykgvy7pm37gadhsjxbqijrd3|2024\n"
            '{"client_secret": "GOCSPX-abc1234567890", "refresh_token": "1//abcdef123456789012345"}\n'
            "密码: WU_test_998877\n"
            "密码：包含字母和数字至少8位\n"
            "2fa key: jx5eyqn5ykgvy7pm37gadhsjxbqijrd3\n"
        )
        redacted = kb_filters.redact_secrets(raw)
        self.assertEqual(kb_filters.find_raw_secrets(redacted), [])
        self.assertNotIn("GOCSPX-", redacted)
        self.assertNotIn("1//abcdef", redacted)
        self.assertNotIn("WU_test_998877", redacted)
        self.assertNotIn("passabcd", redacted)
        self.assertNotIn("jx5eyqn5ykgvy7pm37gadhsjxbqijrd3", redacted)
        self.assertIn("密码：包含字母和数字至少8位", redacted)

        # Noise filter skips empty, *_OK/OK probes, and pure greetings, while preserving real context
        self.assertTrue(kb_filters.should_skip_conversation("id0", "未命名对话", []))
        self.assertTrue(kb_filters.should_skip_conversation("id1", "Reply exactly RECOVERY_OK. Do not use tools.", [{"role": "user", "text": "Reply exactly RECOVERY_OK."}]))
        self.assertTrue(kb_filters.should_skip_conversation("id1b", "Reply with exactly OK.", [{"role": "user", "text": "Reply with exactly OK."}]))
        self.assertTrue(kb_filters.should_skip_conversation("id2", "你好", [{"role": "user", "text": "你好"}, {"role": "assistant", "text": "你好！请问有什么可以帮你的吗？"}]))
        self.assertFalse(kb_filters.should_skip_conversation("id3", "你好", [{"role": "user", "text": "你好"}, {"role": "user", "text": "帮我改一下工资条接口"}]))
        self.assertFalse(kb_filters.should_skip_conversation("id4", "你好", [{"role": "user", "text": "你好"}, {"role": "assistant", "text": "看到你在 /Users/dev/chat-app 中断了。"}]))
        self.assertTrue(kb_filters.should_skip_conversation("id5", "帮她做电商设计主图", [{"role": "user", "text": "帮她做电商设计主图"}] * 6, {"excluded_topic_regex": "电商设计|商品主图"}))
        self.assertFalse(kb_filters.should_skip_conversation("id6", "工资条多批次接口优化", [{"role": "user", "text": "工资条多批次接口优化"}] * 6, {"excluded_topic_regex": "电商设计|商品主图"}))

    def test_broken_links_and_missing_citations(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "笔记").mkdir()
            (root / "INDEX.md").write_text("[ok](笔记/a.md)\n[bad](笔记/missing.md)\n[ext](../outside.md)\n", encoding="utf-8")
            (root / "笔记" / "a.md").write_text("# A\n", encoding="utf-8")
            (root / "个人档案.md").write_text("## 2026-11-18 测试记录\n没有引用来源\n", encoding="utf-8")

            res = kb_doctor.run_doctor(root, extra_public_dirs=[], skip_external_links=True)
            self.assertFalse(res["healthy"])
            self.assertEqual(len(res["broken_links"]), 1)
            self.assertEqual(res["broken_links"][0]["target"], "笔记/missing.md")
            res_strict = kb_doctor.run_doctor(root, extra_public_dirs=[], skip_external_links=False)
            self.assertEqual(len(res_strict["broken_links"]), 2)
            self.assertEqual(res["missing_citations"], ["个人档案.md: 2026-11-18 测试记录"])


class TestCJKFts5AndChronologicalSearch(unittest.TestCase):
    def test_note_filters_before_and_after_indexing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "INDEX.md").write_text("# searchable", encoding="utf-8")
            for indexed in (False, True):
                if indexed:
                    kb_search.build_search_index(root)
                for filters, count in (({}, 1), ({"role": "user"}, 0), ({"cwd": "demo"}, 0), ({"cwd": "%"}, 0)):
                    with self.subTest(indexed=indexed, filters=filters):
                        hits = kb_search.search_kb(root, "searchable", mode="fts", **filters)
                        self.assertEqual(len(hits), count)

    def test_multi_term_cjk_fts5_and_newest_first_sorting(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "_同步").mkdir()
            (root / "对话" / "Codex").mkdir(parents=True)
            (root / "对话" / "Cursor").mkdir(parents=True)

            (root / "对话" / "Codex" / "0000-old.md").write_text(
                "# Old Chat\n\n## 1. 用户\n时间（UTC）：2026-01-01T00:00:00Z\n工资条的接口方案 alpha hit 1\n",
                encoding="utf-8",
            )
            (root / "对话" / "Cursor" / "zzzz-new.md").write_text(
                "# New Cursor Chat\n\n## 1. 用户\n时间（UTC）：2026-11-18T10:00:00Z\n多批次工资条与接口联调 alpha hit 2\n",
                encoding="utf-8",
            )
            (root / "_同步" / "codex-manifest.json").write_text(
                json.dumps([{"id": "0000-old", "title": "Old Chat", "source": "codex", "path": "对话/Codex/0000-old.md", "updated_at": "2026-01-01T00:00:00Z"}]),
                encoding="utf-8",
            )
            (root / "_同步" / "cursor-manifest.json").write_text(
                json.dumps([{"id": "zzzz-new", "title": "New Cursor Chat", "source": "cursor", "path": "对话/Cursor/zzzz-new.md", "updated_at": "2026-11-18T10:00:00Z"}]),
                encoding="utf-8",
            )

            # 1. Disk fallback multi-term AND
            disk_hits = kb_search.search_kb(root, "工资条 接口", limit=5, per_conv=1)
            self.assertEqual(len(disk_hits), 2)
            self.assertEqual(disk_hits[0]["source"], "cursor")

            # 2. Built CJK-spaced SQLite FTS5 v2 index + synonym expansion + incremental update & deletion
            (root / "_同步" / "kb_config.json").write_text(
                json.dumps({"synonyms": [["零值", "0值"]]}), encoding="utf-8"
            )
            stats1 = {}
            indexed = kb_search.build_search_index(root, stats_out=stats1)
            self.assertEqual(stats1["mode"], "full_rebuild_v3")
            self.assertGreaterEqual(indexed, 2)
            db_stats = kb_search.get_search_db_stats(root)
            self.assertEqual(db_stats["schema_version"], "3")
            self.assertEqual(db_stats["auto_vacuum"], 2)  # 2 == INCREMENTAL
            fts_hits = kb_search.search_kb(root, "工资条 接口", limit=5, per_conv=1)
            self.assertEqual(len(fts_hits), 2)
            self.assertEqual(fts_hits[0]["source"], "cursor")
            self.assertEqual(fts_hits[1]["source"], "codex")

            two_char_hits = kb_search.search_kb(root, "接口", limit=5, per_conv=1)
            self.assertEqual(len(two_char_hits), 2)

            since_hits = kb_search.search_kb(root, "工资条 接口", since="2026-11-01")
            self.assertEqual(len(since_hits), 1)
            self.assertEqual(since_hits[0]["source"], "cursor")

            # Incremental add + synonym match ("0值" -> "合法零值")
            (root / "笔记").mkdir()
            note_p = root / "笔记" / "rules.md"
            note_p.write_text("# 规则\n业务字段允许合法零值必须展示。\n", encoding="utf-8")
            stats2 = {}
            kb_search.build_search_index(root, stats_out=stats2)
            self.assertEqual(stats2["mode"], "incremental_v3")
            self.assertEqual(stats2["updated_files"], 1)
            syn_hits = kb_search.search_kb(root, "0值", limit=3)
            self.assertEqual(len(syn_hits), 1)
            self.assertIn("合法零值", syn_hits[0]["snippet"])

            # Incremental delete removes old FTS postings cleanly and actually shrinks pages on disk
            note_p.unlink()
            stats3 = {}
            kb_search.build_search_index(root, stats_out=stats3, force_compact=True)
            self.assertEqual(stats3["removed_files"], 1)
            self.assertEqual(kb_search.search_kb(root, "0值", limit=3), [])
            self.assertEqual(stats3["freelist_pages"], 0)
            self.assertEqual(kb_search.get_search_db_stats(root)["freelist_pages"], 0)

    def test_chatgpt_triple_hash_turns_and_per_conv_window_cap(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "_同步").mkdir()
            (root / "对话" / "ChatGPT").mkdir(parents=True)
            (root / "对话" / "Codex").mkdir(parents=True)

            # One newest noisy ChatGPT chat with 25 matching turns
            noisy_lines = ["# Noisy ChatGPT", ""]
            for i in range(1, 26):
                noisy_lines += [
                    f"### {i}. 用户 · 2026-11-18T12:00:{i:02d}+08:00",
                    "",
                    f"<!-- message_id: m-{i} -->",
                    "",
                    f"重复主题 灰度 发布 第{i}次提问",
                    "",
                    "---",
                    "",
                ]
            (root / "对话" / "ChatGPT" / "noisy.md").write_text("\n".join(noisy_lines), encoding="utf-8")
            (root / "对话" / "Codex" / "c1.md").write_text(
                "# Older Codex 1\n\n## 1. 用户\n时间（UTC）：2026-11-10T00:00:00Z\n灰度 发布 对齐方案一\n",
                encoding="utf-8",
            )
            (root / "对话" / "Codex" / "c2.md").write_text(
                "# Older Codex 2\n\n## 1. 用户\n时间（UTC）：2026-11-09T00:00:00Z\n灰度 发布 对齐方案二\n",
                encoding="utf-8",
            )
            (root / "_同步" / "chatgpt-manifest.json").write_text(
                json.dumps([{"id": "noisy", "title": "Noisy ChatGPT", "source": "chatgpt", "path": "对话/ChatGPT/noisy.md", "updated_at": "2026-11-18T12:00:25+08:00"}]),
                encoding="utf-8",
            )
            (root / "_同步" / "codex-manifest.json").write_text(
                json.dumps([
                    {"id": "c1", "title": "Older Codex 1", "source": "codex", "path": "对话/Codex/c1.md", "updated_at": "2026-11-10T00:00:00Z"},
                    {"id": "c2", "title": "Older Codex 2", "source": "codex", "path": "对话/Codex/c2.md", "updated_at": "2026-11-09T00:00:00Z"},
                ]),
                encoding="utf-8",
            )

            kb_search.build_search_index(root)
            # Even with limit=3, per_conv=1, the noisy chat must NOT starve older chats
            hits = kb_search.search_kb(root, "灰度 发布", role="user", limit=3, per_conv=1)
            self.assertEqual(len(hits), 3)
            self.assertEqual({h["id"] if "id" in h else h["path"] for h in hits}, {
                "对话/ChatGPT/noisy.md",
                "对话/Codex/c1.md",
                "对话/Codex/c2.md",
            })
            cgpt_only = kb_search.search_kb(root, "灰度", source="chatgpt", role="user", limit=2, per_conv=2)
            self.assertEqual(len(cgpt_only), 2)
            self.assertEqual(cgpt_only[0]["role"], "user")
            self.assertTrue(cgpt_only[0]["time"].startswith("2026-11-18"))

            # Automatic compaction without force_compact when deleting >=40 chunks
            big_lines = [f"## {i}. 用户\n大段文本重复填充段落 {i} 用于验证自动分页回收空间能力。\n" + ("重复文本 " * 30) for i in range(1, 55)]
            big_p = root / "对话" / "Codex" / "big.md"
            big_p.write_text("# Big\n\n" + "\n".join(big_lines), encoding="utf-8")
            (root / "_同步" / "codex-manifest.json").write_text(
                json.dumps([
                    {"id": "c1", "title": "Older Codex 1", "source": "codex", "path": "对话/Codex/c1.md", "updated_at": "2026-11-10T00:00:00Z"},
                    {"id": "c2", "title": "Older Codex 2", "source": "codex", "path": "对话/Codex/c2.md", "updated_at": "2026-11-09T00:00:00Z"},
                    {"id": "big", "title": "Big", "source": "codex", "path": "对话/Codex/big.md", "updated_at": "2026-11-18T12:05:00Z"},
                ]),
                encoding="utf-8",
            )
            kb_search.build_search_index(root)
            size_before = kb_search.get_search_db_stats(root)["db_bytes"]
            big_p.unlink()
            (root / "_同步" / "codex-manifest.json").write_text(
                json.dumps([
                    {"id": "c1", "title": "Older Codex 1", "source": "codex", "path": "对话/Codex/c1.md", "updated_at": "2026-11-10T00:00:00Z"},
                    {"id": "c2", "title": "Older Codex 2", "source": "codex", "path": "对话/Codex/c2.md", "updated_at": "2026-11-09T00:00:00Z"},
                ]),
                encoding="utf-8",
            )
            del_stats = {}
            kb_search.build_search_index(root, stats_out=del_stats)
            size_after = kb_search.get_search_db_stats(root)["db_bytes"]
            self.assertTrue(del_stats["compacted"])
            self.assertEqual(del_stats["freelist_pages"], 0)
            self.assertLess(size_after, size_before)


class TestArchiveProfile(unittest.TestCase):
    def test_cold_archive_moves_old_sections_and_rebases_links(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "对话" / "Codex").mkdir(parents=True)
            (root / "对话" / "Codex" / "c1.md").write_text("# C1\n", encoding="utf-8")
            (root / "对话" / "Codex" / "c2.md").write_text("# C2\n", encoding="utf-8")
            (root / "个人档案.md").write_text(
                "# 档案\n\n## 零、 最新\n保留\n\n## 2026-10-14 旧条目\n来源：[旧对话](对话/Codex/c1.md)\n\n## 2026-11-05 新条目\n来源：[新对话](对话/Codex/c2.md)\n",
                encoding="utf-8",
            )

            res = archive_profile.archive_profile(root, "2026-11")
            self.assertEqual(res["archived_sections"], 1)
            hist_text = (root / "笔记" / "历史档案" / "2026-10.md").read_text(encoding="utf-8")
            self.assertIn("](../../对话/Codex/c1.md)", hist_text)
            main_text = (root / "个人档案.md").read_text(encoding="utf-8")
            self.assertNotIn("2026-10-14 旧条目", main_text)
            self.assertIn("2026-11-05 新条目", main_text)
            self.assertIn("笔记/历史档案/2026-10.md", main_text)

            doc_res = kb_doctor.run_doctor(root, extra_public_dirs=[])
            self.assertEqual(doc_res["broken_links"], [])
            self.assertEqual(doc_res["missing_citations"], [])


class TestNoteLineRange(unittest.TestCase):
    def test_last_line_and_stale_citation(self):
        from kb_mcp import kb_read_note_data

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            note = root / "sample.md"
            note.write_text("first\nlast\n", encoding="utf-8")
            result = kb_read_note_data(note.name, start_line=2, root=root)
            self.assertEqual(result["content"], "2: last")
            with self.assertRaisesRegex(ValueError, "line 3.*2 lines"):
                kb_read_note_data(note.name, start_line=3, root=root)
            note.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "line 1.*0 lines"):
                kb_read_note_data(note.name, root=root)


class TestClaudeAndPipelineAndGitHook(unittest.TestCase):
    def test_archive_path_requires_a_single_component(self):
        from kb_filters import archive_path
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(archive_path(root, "Claude", "session-1").name, "session-1.md")
            with self.assertRaises(ValueError):
                archive_path(root, "Claude", "../other")

    def test_claude_export_filters_thinking_and_tool_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            claude_dir = Path(td) / "projects" / "proj-a"
            claude_dir.mkdir(parents=True)
            out_dir = Path(td) / "kb"
            jsonl = claude_dir / "sess-1.jsonl"
            records = [
                {"type": "user", "isMeta": True, "message": {"content": "injected context"}},
                {"type": "user", "cwd": "/Users/dev/test", "timestamp": "2026-11-18T01:00:00Z",
                 "message": {"role": "user", "content": [{"type": "text", "text": "<ide_opened_file>foo</ide_opened_file>"}, {"type": "text", "text": "排查弹窗问题 sk-" + "a" * 30}]}},
                {"type": "assistant", "timestamp": "2026-11-18T01:00:05Z",
                 "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "secret"}, {"type": "tool_use", "name": "bash"}, {"type": "text", "text": "已定位弹窗根因。"}]}},
            ]
            jsonl.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")

            res = subprocess.run(
                [sys.executable, str(SYNC_DIR / "export_claude.py"), "--claude-dir", str(Path(td) / "projects"), "--output-root", str(out_dir)],
                capture_output=True, text=True, check=True,
            )
            stats = json.loads(res.stdout)
            self.assertEqual(stats["written"], 1)
            md = (out_dir / "对话" / "Claude" / "sess-1.md").read_text(encoding="utf-8")
            self.assertIn("排查弹窗问题", md)
            self.assertIn("已定位弹窗根因。", md)
            self.assertNotIn("secret", md)
            self.assertNotIn("injected context", md)
            manifest = out_dir / "_同步" / "claude-manifest.json"
            entries = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertNotIn("a" * 30, entries[0]["title"])
            entries[0]["title"] = "cached sk-" + "a" * 30
            manifest.write_text(json.dumps(entries), encoding="utf-8")
            second = subprocess.run(res.args, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(second.stdout)["unchanged"], 1)
            self.assertNotIn("a" * 30, manifest.read_text(encoding="utf-8"))

    def test_claude_incremental_search_survives_interrupted_source_write(self):
        with tempfile.TemporaryDirectory() as td:
            projects = Path(td) / "projects"
            session = projects / "project" / "session.jsonl"
            session.parent.mkdir(parents=True)
            root = Path(td) / "kb"
            command = [sys.executable, str(SYNC_DIR / "export_claude.py"),
                       "--claude-dir", str(projects), "--output-root", str(root)]

            def sync():
                output = subprocess.check_output(command, text=True)
                kb_search.build_search_index(root)
                return json.loads(output)

            def hits(term):
                return len(kb_search.search_kb(root, term, source="claude", role="user", mode="fts"))

            def record(word):
                return json.dumps({"type": "user", "message": {"content": word}})

            session.write_text(record("firstmarker"), encoding="utf-8")
            self.assertEqual(sync()["written"], 1)
            self.assertEqual(sync()["unchanged"], 1)
            self.assertEqual(hits("firstmarker"), 1)

            replacement = record("replacementmarker")
            session.write_text(replacement[:-5], encoding="utf-8")
            self.assertEqual(sync()["manifest_entries"], 1)
            self.assertEqual(hits("firstmarker"), 1)

            session.write_text(replacement, encoding="utf-8")
            self.assertEqual(sync()["written"], 1)
            self.assertEqual(hits("firstmarker"), 0)
            self.assertEqual(hits("replacementmarker"), 1)

    def test_pipeline_git_bundle_verify_idempotency_and_precommit_hook(self):
        td_kwargs = {"ignore_cleanup_errors": True} if sys.version_info >= (3, 10) else {}
        with tempfile.TemporaryDirectory(**td_kwargs) as td:
            root = Path(td)
            subprocess.run(["git", "init", "-b", "main", str(root)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            (root / ".gitignore").write_text(
                (SYNC_DIR.parent / ".gitignore").read_text(encoding="utf-8")
                + "\nout/\nic/\nic2/\nbare-private-backup.git/\n",
                encoding="utf-8",
            )

            sync = root / "_同步"
            sync.mkdir(parents=True)
            for name in ("archive_profile.py", "build_kb_index.py", "kb_backup.py", "kb_doctor.py", "kb_filters.py", "kb_retrospect.py", "kb_restore_verify.py", "kb_search.py"):
                (sync / name).write_text((SYNC_DIR / name).read_text(encoding="utf-8"), encoding="utf-8")
            (sync / "export_antigravity.py").write_text("import sys; sys.stderr.write('boom'); sys.exit(2)\n", encoding="utf-8")
            (root / "个人档案.md").write_text("# Profile\n", encoding="utf-8")
            (root / "INDEX.md").write_text("# Index\n", encoding="utf-8")
            (root / "同步规则.md").write_text("# Rules\n", encoding="utf-8")
            (root / "跨设备配置.md").write_text("# Config\n", encoding="utf-8")
            (root / "笔记").mkdir()
            (root / "共享摘录").mkdir()
            for nf in (
                "笔记/进行中的项目.md",
                "笔记/AI协作错题本.md",
                "笔记/前端设计与验收.md",
                "笔记/理财与规划纪律.md",
                "笔记/个人事务与成长看板.md",
                "笔记/知识库架构与治理.md",
                "笔记/知识库架构与治理.md",
                "共享摘录/前端评审背景.md",
                "共享摘录/demo-public.md",
            ):
                if nf == "笔记/AI协作错题本.md":
                    (root / nf).write_text("# AI协作错题本\n\n<!-- AUTO_RETROSPECT_START -->\n<!-- AUTO_RETROSPECT_END -->\n", encoding="utf-8")
                else:
                    (root / nf).write_text(f"# {nf}\n", encoding="utf-8")

            bundle = root / "out" / "mirror.bundle"
            # Seed a Codex heartbeat section in 同步报告.md and verify build_kb_index preserves it
            (sync / "同步报告.md").write_text(
                "# 同步报告\n\n## 本次增量检查\n\n- 保留增量行\n\n## Codex 原始来源缺口\n\n- 缺口保留\n\n## 尚未完整覆盖\n",
                encoding="utf-8",
            )
            test_env = {**os.environ, "PERSONAL_KB_SKIP_KEYCHAIN": "1"}
            cmd = [
                sys.executable, str(sync / "build_kb_index.py"),
                "--output-root", str(root),
                "--skip-repo-probe",
                "--skip-external-checks",
                "--skip-icloud",
                "--no-notify",
                "--bundle-path", str(bundle),
            ]
            first = json.loads(subprocess.run(cmd, env=test_env, capture_output=True, text=True, check=True).stdout)
            self.assertTrue(first["changed_state"])
            self.assertTrue(first["bundle_verified"])
            self.assertTrue(first["restore_verified"])
            self.assertEqual(len(first["exporter_errors"]), 1)
            self.assertTrue((root / ".git" / "hooks" / "pre-commit").is_file())
            rep_after = (sync / "同步报告.md").read_text(encoding="utf-8")
            self.assertIn("保留增量行", rep_after)
            self.assertIn("缺口保留", rep_after)
            self.assertIn("灾备恢复演练状态：**已通过**", rep_after)

            second = json.loads(subprocess.run(cmd, env=test_env, capture_output=True, text=True, check=True).stdout)
            self.assertFalse(second["changed_state"])

            # Verify encrypted iCloud bundle creation + pruning legacy plaintext + DR decrypt restore check
            ic_plain = root / "ic" / "mirror.bundle"
            synced, note, encrypted = build_kb_index.sync_icloud_mirror(root, bundle, ic_plain, passphrase_override="test-secret-pw")
            self.assertTrue(synced)
            self.assertIsNone(note)
            self.assertTrue(encrypted)
            self.assertTrue(ic_plain.with_suffix(".bundle.enc").is_file())
            rv = kb_restore_verify.verify_restore(
                root, bundle, icloud_bundle_path=ic_plain, check_icloud=True,
                skip_external_checks=True, passphrase_override="test-secret-pw",
            )
            self.assertTrue(rv["ok"])
            self.assertTrue(rv["head_match"])
            self.assertEqual(rv["icloud_mirror"]["status"], "ok")
            self.assertTrue(rv["icloud_mirror"]["encrypted"])

            # Verify truthful reporting when iCloud failed previously & catches up without a new chat edit
            ic_catchup = root / "ic2" / "mirror.bundle"
            (sync / "pipeline-state.json").write_text(json.dumps({
                "bundle_head": rv["expected_head"],
                "bundle_verified": True,
                "icloud_synced": False,
                "icloud_note": "iCloud 后台复制受 macOS TCC 限制 (PermissionError)",
                "icloud_consecutive_failures": 2,
                "restore_verified": True,
                "restore_verified_at": "2026-11-18 16:00:00 CST",
                "restore_verified_epoch": 9999999999,
            }), encoding="utf-8")
            catchup_cmd = [
                sys.executable, str(sync / "build_kb_index.py"),
                "--output-root", str(root),
                "--skip-repo-probe",
                "--skip-external-checks",
                "--skip-restore-verify",
                "--no-notify",
                "--bundle-path", str(bundle),
                "--icloud-bundle-path", str(ic_catchup),
            ]
            c_res = json.loads(subprocess.run(catchup_cmd, env=test_env, capture_output=True, text=True, check=True).stdout)
            self.assertTrue(c_res["icloud_synced"])
            self.assertEqual(c_res["icloud_alert_state"], "ok")
            self.assertTrue(ic_catchup.is_file())
            self.assertIn("已同步", (sync / "同步报告.md").read_text(encoding="utf-8"))

            # Subsequent idle run with synced iCloud must remain idempotent
            c_res2 = json.loads(subprocess.run(catchup_cmd, env=test_env, capture_output=True, text=True, check=True).stdout)
            self.assertFalse(c_res2["changed_state"])

            # Verify private_git_remote mirrors cleanly to bare private remote & blocks public-named URLs
            bare_remote = root / "bare-private-backup.git"
            subprocess.run(["git", "init", "--bare", str(bare_remote)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "-C", str(root), "remote", "add", "private-backup", str(bare_remote)], check=True)
            subprocess.run(["git", "-C", str(root), "remote", "add", "bad-remote", "git@github.com:u/personal-knowledge-public.git"], check=True)
            blocked_ok, blocked_msg = build_kb_index.maybe_push_private_remote(root, "bad-remote")
            self.assertFalse(blocked_ok)
            self.assertIn("安全门禁", blocked_msg)
            pushed_ok, pushed_msg = build_kb_index.maybe_push_private_remote(root, "private-backup")
            self.assertTrue(pushed_ok, pushed_msg)
            remote_head = subprocess.run(["git", "-C", str(bare_remote), "rev-parse", "refs/heads/main"], capture_output=True, text=True, check=True).stdout.strip()
            local_head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(remote_head, local_head)

            # Verify --skip-repo-probe / --skip-icloud / --skip-private-remote stay completely idempotent after full sync
            with tempfile.TemporaryDirectory(**td_kwargs) as repo_td:
                repo_dir = Path(repo_td) / "app"
                subprocess.run(["git", "init", "-b", "main", str(repo_dir)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "t@e.com"], check=True)
                subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "T"], check=True)
                (repo_dir / "a.txt").write_text("hi\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True)
                subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "init"], check=True, stdout=subprocess.DEVNULL)
                (sync / "kb_config.json").write_text(json.dumps({"private_git_remote": "private-backup", "active_repos": [["app", str(repo_dir)]]}), encoding="utf-8")
                probe_cmd = [a for a in catchup_cmd if a != "--skip-repo-probe"]
                json.loads(subprocess.run(probe_cmd, env=test_env, capture_output=True, text=True, check=True).stdout)
                head_after_push = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
                sandbox_cmd = catchup_cmd + ["--skip-icloud", "--skip-private-remote"]
                s_res = json.loads(subprocess.run(sandbox_cmd, env=test_env, capture_output=True, text=True, check=True).stdout)
                head_after_sandbox = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
                self.assertFalse(s_res["changed_state"])
                self.assertEqual(s_res["icloud_alert_state"], "ok")
                self.assertEqual(head_after_push, head_after_sandbox)
                self.assertTrue(json.loads((sync / "pipeline-state.json").read_text(encoding="utf-8"))["private_remote_ok"])

                # Verify note change committed under sandbox flags is pushed on next full sync without false remote errors
                (root / "笔记" / "进行中的项目.md").write_text("# 笔记/进行中的项目.md\n\n- sandbox edit\n", encoding="utf-8")
                subprocess.run(sandbox_cmd, env=test_env, capture_output=True, text=True, check=True)
                head_sandbox_edit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
                self.assertNotEqual(head_sandbox_edit, head_after_push)
                self.assertTrue(json.loads((sync / "pipeline-state.json").read_text(encoding="utf-8"))["private_remote_ok"])
                subprocess.run(probe_cmd, env=test_env, capture_output=True, text=True, check=True)
                remote_head2 = subprocess.run(["git", "-C", str(bare_remote), "rev-parse", "refs/heads/main"], capture_output=True, text=True, check=True).stdout.strip()
                head_final = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
                self.assertEqual(remote_head2, head_final)

            # Verify pre-commit hook blocks unredacted leak in 共享摘录/
            (root / "共享摘录" / "bad.md").write_text("leak 192.168.1.50\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "共享摘录/bad.md"], check=True)
            bad_commit = subprocess.run(
                ["git", "-C", str(root), "commit", "-m", "should fail"],
                capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(bad_commit.returncode, 0)


class TestRetrospectMistakeBook(unittest.TestCase):
    def test_extract_detect_resolve_and_reopen_guardrails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "_同步").mkdir()
            chat_dir = root / "对话" / "Codex"
            chat_dir.mkdir(parents=True)

            # High-friction conversation with IDE context boilerplate
            (chat_dir / "conv-fix.md").write_text(
                "# 表格对齐修复\n\n- 更新时间（UTC）：2026-11-18T10:00:00Z\n\n## 对话正文\n\n"
                "### 1. 用户（2026-11-18T10:00:00Z）\n\n"
                "# Context from my IDE setup:\n\n## Active file: a.js\n\n## My request:\n你理解错了，我说的是表格最后一列没有对齐\n\n"
                "### 2. 助手（2026-11-18T10:01:00Z）\n\n我再改一下。\n\n"
                "### 3. 用户（2026-11-18T10:02:00Z）\n\n还是没有生效，不要在错误的代码上继续改。\n",
                encoding="utf-8",
            )
            # Heartbeat + single normal question must be ignored
            (chat_dir / "conv-hb.md").write_text(
                "# 心跳任务\n\n- 更新时间（UTC）：2026-11-18T09:00:00Z\n\n## 对话正文\n\n"
                "### 1. 用户（2026-11-18T09:00:00Z）\n\n<heartbeat>检查遗漏和反复问题</heartbeat>\n\n"
                "### 2. 用户（2026-11-18T09:05:00Z）\n\n<heartbeat>继续检查遗漏和反复问题</heartbeat>\n",
                encoding="utf-8",
            )

            book = root / "笔记" / "AI协作错题本.md"
            book.parent.mkdir(parents=True)
            book.write_text(
                "# 错题本\n\n<!-- GUARDRAILS_START -->\n1. 先读原文再改。\n2. 改偏停手。\n<!-- GUARDRAILS_END -->\n\n"
                "<!-- AUTO_RETROSPECT_START -->\n<!-- AUTO_RETROSPECT_END -->\n",
                encoding="utf-8",
            )
            kb_search.build_search_index(root)
            q, changed = kb_retrospect.refresh_retrospect(root)
            self.assertTrue(changed)
            self.assertEqual(q["unresolved_count"], 1)
            self.assertEqual(q["candidates"][0]["path"], "对话/Codex/conv-fix.md")
            self.assertIn("conv-fix.md", book.read_text(encoding="utf-8"))
            self.assertEqual(len(kb_retrospect.extract_top_guardrails(root)), 2)

            # Second scan without changes stays idempotent
            _, changed2 = kb_retrospect.refresh_retrospect(root)
            self.assertFalse(changed2)

            # Resolving removes it cleanly
            q_res = kb_retrospect.resolve_candidate(root, "对话/Codex/conv-fix.md", note="done")
            self.assertEqual(q_res["unresolved_count"], 0)
            self.assertIn("暂无新增未处理条目", book.read_text(encoding="utf-8"))

            # Adding another user pushback turn to the same file automatically re-opens it
            (chat_dir / "conv-fix.md").write_text(
                (chat_dir / "conv-fix.md").read_text(encoding="utf-8")
                + "\n### 4. 用户（2026-11-18T10:05:00Z）\n\n为什么又漏了边界条件？\n",
                encoding="utf-8",
            )
            kb_search.build_search_index(root)
            q_reopen, _ = kb_retrospect.refresh_retrospect(root)
            self.assertEqual(q_reopen["unresolved_count"], 1)
            self.assertEqual(q_reopen["candidates"][0]["hit_count"], 3)


class TestRemainingFourExporters(unittest.TestCase):
    def check_interrupted_sync(self, root, command, source, record, old_marker, adapter):
        def sync():
            stats = json.loads(subprocess.check_output(command, text=True))
            kb_search.build_search_index(root)
            return stats

        def count(marker):
            return len(kb_search.search_kb(root, marker, source=adapter, role="user", mode="fts"))

        source.write_text(json.dumps(record)[:-3], encoding="utf-8")
        self.assertEqual(sync()["deferred"], 1)
        self.assertEqual(count(old_marker), 1)
        source.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(sync()["written"], 1)
        self.assertEqual(count(old_marker), 0)
        self.assertEqual(count("replacementmarker"), 1)
        self.assertEqual(sync()["unchanged"], 1)
        self.assertEqual(count("replacementmarker"), 1)

    def test_codex_exporter_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "kb"
            codex_home = Path(td) / ".codex"
            codex_home.mkdir()
            rollout = codex_home / "rollout-t1.jsonl"
            rollout.write_text(
                "\n".join([
                    json.dumps({"type": "event_msg", "timestamp": "2026-11-18T10:00:00Z", "payload": {"type": "user_message", "message": "排查 JWT 登录"}}),
                    json.dumps({"type": "event_msg", "timestamp": "2026-11-18T10:00:05Z", "payload": {"type": "agent_message", "phase": "final_answer", "message": "Bearer header_secret_1234567890abcdef"}}),
                ]) + "\n",
                encoding="utf-8",
            )
            st = sqlite3.connect(str(codex_home / "state_5.sqlite"))
            st.execute("CREATE TABLE threads (id TEXT, title TEXT, cwd TEXT, source TEXT, thread_source TEXT, rollout_path TEXT, created_at INTEGER, updated_at INTEGER, archived INTEGER)")
            st.execute("INSERT INTO threads VALUES ('th-1', '排查登录', '/Users/dev/app', 'cli', 'user', ?, 1795000000, 1795000100, 0)", (str(rollout),))
            st.execute("INSERT INTO threads VALUES ('th-remote', '别人电脑的电商设计', 'C:\\Users\\other\\design', 'cli', 'user', ?, 1795000000, 1795000100, 0)", (str(rollout),))
            st.execute("INSERT INTO threads VALUES ('th-sub', 'subagent', '/Users/dev/app', 'sub', 'subagent', ?, 1795000000, 1795000100, 0)", (str(rollout),))
            st.commit()
            st.close()
            (root / "_同步").mkdir(parents=True, exist_ok=True)
            (root / "_同步" / "kb_config.json").write_text(
                json.dumps({"noise_filter": {"allowed_cwd_prefixes": ["/Users/dev"]}}),
                encoding="utf-8",
            )

            hist = sqlite3.connect(str(codex_home / "thread_history_1.sqlite"))
            hist.execute("CREATE TABLE thread_items (thread_id TEXT, item_json TEXT, item_id TEXT, turn_id TEXT, created_at_ms INTEGER, rollout_ordinal INTEGER, item_type TEXT)")
            hist.execute("CREATE TABLE thread_history_projection_state (thread_id TEXT, next_rollout_byte_offset INTEGER)")
            hist.commit()
            hist.close()

            res1 = json.loads(subprocess.run(
                [sys.executable, str(SYNC_DIR / "export_codex.py"), "--codex-home", str(codex_home), "--output-root", str(root)],
                capture_output=True, text=True, check=True,
            ).stdout)
            self.assertEqual(res1["written"], 1)
            self.assertEqual(res1["internal_threads_excluded"], 1)
            md = (root / "对话" / "Codex" / "th-1.md").read_text(encoding="utf-8")
            self.assertIn("排查 JWT 登录", md)
            res2 = json.loads(subprocess.run(
                [sys.executable, str(SYNC_DIR / "export_codex.py"), "--codex-home", str(codex_home), "--output-root", str(root)],
                capture_output=True, text=True, check=True,
            ).stdout)
            self.assertEqual(res2["unchanged"], 1)

            self.check_interrupted_sync(root, [sys.executable, str(SYNC_DIR / "export_codex.py"),
                "--codex-home", str(codex_home), "--output-root", str(root)], rollout,
                {"type": "event_msg", "payload": {"type": "user_message", "message": "replacementmarker"}},
                "JWT", "codex")

    def test_cursor_exporter_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "kb"
            c_user = Path(td) / "CursorUser"
            (c_user / "globalStorage").mkdir(parents=True)
            ws_dir = c_user / "workspaceStorage" / "ws1"
            ws_dir.mkdir(parents=True)
            (ws_dir / "workspace.json").write_text(json.dumps({"folder": "file:///Users/dev/demo-web"}), encoding="utf-8")

            ws_db = sqlite3.connect(str(ws_dir / "state.vscdb"))
            ws_db.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
            ws_db.execute("INSERT INTO ItemTable VALUES ('composer.composerData', ?)", (json.dumps({"allComposers": [{"composerId": "cid-1"}]}),))
            ws_db.commit()
            ws_db.close()

            g_db = sqlite3.connect(str(c_user / "globalStorage" / "state.vscdb"))
            g_db.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
            g_db.execute("INSERT INTO cursorDiskKV VALUES ('composerData:cid-1', ?)", (json.dumps({
                "composerId": "cid-1", "name": "修复表格", "createdAt": 1795000000000, "lastUpdatedAt": 1795000100000,
                "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1}, {"bubbleId": "b2", "type": 2}],
            }),))
            g_db.execute("INSERT INTO cursorDiskKV VALUES ('bubbleId:cid-1:b1', ?)", (json.dumps({"text": "调整表格列宽", "images": ["img.png"]}),))
            g_db.execute("INSERT INTO cursorDiskKV VALUES ('bubbleId:cid-1:b2', ?)", (json.dumps({"text": "已调整列宽，Authorization: Bearer abcdefghij1234567890"}),))
            g_db.commit()
            g_db.close()

            res = json.loads(subprocess.run(
                [sys.executable, str(SYNC_DIR / "export_cursor.py"), "--cursor-user-dir", str(c_user), "--output-root", str(root)],
                capture_output=True, text=True, check=True,
            ).stdout)
            self.assertEqual(res["written"], 1)
            md = (root / "对话" / "Cursor" / "cid-1.md").read_text(encoding="utf-8")
            self.assertIn("/Users/dev/demo-web", md)
            self.assertIn("[已脱敏令牌]", md)
            self.assertIn("非文本附件", md)

    def test_antigravity_and_chatgpt_exporters_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "kb"
            brain = Path(td) / "brain"
            log_dir = brain / "ag-1" / ".system_generated" / "logs"
            log_dir.mkdir(parents=True)
            (log_dir / "transcript_full.jsonl").write_text(
                "\n".join([
                    json.dumps({"step_index": 1, "type": "USER_INPUT", "created_at": "2026-11-18T10:00:00Z", "content": "<USER_REQUEST>\n实现导入提示\n</USER_REQUEST>Active Document: /Users/dev/payroll-app/a.js"}),
                    json.dumps({"step_index": 2, "type": "USER_INPUT", "created_at": "2026-11-18T10:00:01Z", "content": "<SYSTEM_MESSAGE>hidden</SYSTEM_MESSAGE>"}),
                    json.dumps({"step_index": 3, "type": "PLANNER_RESPONSE", "created_at": "2026-11-18T10:00:05Z", "content": "已完成实现。"}),
                ]) + "\n",
                encoding="utf-8",
            )
            ag_res = json.loads(subprocess.run(
                [sys.executable, str(SYNC_DIR / "export_antigravity.py"), "--brain-dir", str(brain), "--output-root", str(root)],
                capture_output=True, text=True, check=True,
            ).stdout)
            self.assertEqual(ag_res["written"], 1)
            ag_md = (root / "对话" / "Antigravity" / "ag-1.md").read_text(encoding="utf-8")
            self.assertIn("实现导入提示", ag_md)
            self.assertNotIn("hidden", ag_md)

            self.check_interrupted_sync(root, [sys.executable, str(SYNC_DIR / "export_antigravity.py"),
                "--brain-dir", str(brain), "--output-root", str(root)], log_dir / "transcript_full.jsonl",
                {"type": "USER_INPUT", "content": "replacementmarker"}, "导入提示", "antigravity")

            # ChatGPT exporter multi-snapshot merge & dedup by message_id
            p1 = Path(td) / "p1.json"
            p2 = Path(td) / "p2.json"
            tid = "6af5372b-63e0-83ec-a5ca-7698cdb6382c"
            p1.write_text(json.dumps({"thread": {"id": tid, "kind": "chatgpt", "title": "测试合并", "createdAt": 1795000000, "updatedAt": 1795000100}, "page": {"hasMore": False}, "turns": [{"id": "t1", "startedAt": 1795000001, "items": [{"id": "m1", "type": "userMessage", "text": "第一问"}]}]}), encoding="utf-8")
            p2.write_text(json.dumps({"thread": {"id": tid, "kind": "chatgpt", "title": "测试合并", "createdAt": 1795000000, "updatedAt": 1795000200}, "page": {"hasMore": False}, "turns": [{"id": "t1", "startedAt": 1795000001, "items": [{"id": "m1", "type": "userMessage", "text": "第一问"}]}, {"id": "t2", "startedAt": 1795000010, "items": [{"id": "m2", "type": "agentMessage", "text": "第二答"}]}]}), encoding="utf-8")
            cg_res = json.loads(subprocess.run(
                [sys.executable, str(SYNC_DIR / "export_chatgpt.py"), "--output-root", str(root), "--ingest", str(p1), "--ingest", str(p2)],
                capture_output=True, text=True, check=True,
            ).stdout)
            self.assertEqual(cg_res["threads"], 1)
            self.assertEqual(cg_res["messages"], 2)
            cg_md = (root / "对话" / "ChatGPT" / f"{tid}.md").read_text(encoding="utf-8")
            self.assertIn("### 1. 用户", cg_md)
            self.assertIn("### 2. 助手", cg_md)


class TestDlpV2StalenessAndAutoArchive(unittest.TestCase):
    def test_dlp_v2_staleness_and_auto_archive(self):
        sample = "\n".join([
            "jwt=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
            "Authorization: Bearer super_secret_bearer_token_987654321",
            "**Set-Cookie**: acw\\_tc=2f62587f17884518; path=/; Max-Age=1800",
            "db=postgres://admin:RealPass9981!@10.0.0.5:5432/prod",
            "OPENAI_API_KEY=custom-secret-value-998877665544",
            "uuid=550e8400-e29b-41d4-a716-446655440000 sha=20e8f8a13fb62505b29e2e23e7659b9fc9a74350 call=call_b1234567890abcdefghijklmnop model=paraphrase-multilingual-MiniLM-L12-v2",
        ])
        red = kb_filters.redact_secrets(sample)
        self.assertEqual(kb_filters.find_raw_secrets(red), [])
        self.assertIn("550e8400-e29b-41d4-a716-446655440000", red)
        self.assertIn("20e8f8a13fb62505b29e2e23e7659b9fc9a74350", red)
        self.assertIn("call_b1234567890abcdefghijklmnop", red)
        self.assertIn("paraphrase-multilingual-MiniLM-L12-v2", red)
        self.assertNotIn("RealPass9981", red)
        self.assertNotIn("super_secret_bearer", red)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "笔记").mkdir()
            (root / "个人档案.md").write_text(
                "# 个人档案\n\n## 2026-09-01 旧事件\n来源：[a](对话/x.md)\n" + ("补充行\n" * 20),
                encoding="utf-8",
            )
            (root / "笔记" / "进行中的项目.md").write_text(
                "# 项目\n\n整理日期：2026-10-01\n\n### 1. 老项目\n状态：待联调（2026-10-01）\n",
                encoding="utf-8",
            )
            stale = kb_doctor.check_staleness(root, max_age_days=14)
            self.assertEqual(len(stale), 1)
            self.assertIn("进行中的项目.md", stale[0]["file"])
            arch = build_kb_index.auto_archive_profile_if_needed(root, threshold_lines=10)
            self.assertIsNotNone(arch)
            self.assertEqual(arch["archived_sections"], 1)


class TestHybridSearchAndMcp(unittest.TestCase):
    def test_vector_citations_survive_search_index_replacement(self):
        import kb_embed
        if kb_embed.np is None:
            self.skipTest("Optional vector runtime unavailable")

        def encode(texts):
            return kb_embed.np.ones((len(texts), 1), dtype=kb_embed.np.float32)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            note = root / "笔记" / "food.md"
            note.parent.mkdir()
            note.write_text("# lunch\nCooking fresh potato soup with carrots", encoding="utf-8")
            kb_search.build_search_index(root)
            kb_embed.sync_vectors(root, encode_fn=encode)
            (root / "INDEX.md").write_text("# repairs\nReplacing a broken phone screen", encoding="utf-8")
            (root / "_同步" / "search_index.sqlite").unlink()
            kb_search.build_search_index(root)
            hit, = kb_embed.query_vectors(root, "food", encode_fn=encode)
            self.assertEqual(hit["path"], "笔记/food.md")
            self.assertIn("potato soup", hit["snippet"])
            note.write_text("# dinner\nMaking fresh pasta from scratch", encoding="utf-8")
            kb_search.build_search_index(root)
            self.assertEqual(kb_embed.query_vectors(root, "food", encode_fn=encode), [])
            kb_embed.sync_vectors(root, encode_fn=encode)
            hits = kb_embed.query_vectors(root, "food", encode_fn=encode)
            self.assertTrue(any("fresh pasta" in h["snippet"] for h in hits))

    def test_nl_rescue_rrf_and_mcp_server(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "_同步").mkdir()
            (root / "笔记").mkdir()
            (root / "INDEX.md").write_text("# 索引\n", encoding="utf-8")
            (root / "个人档案.md").write_text("# 档案\n\n## 前端实现约定\n动态文本出现省略号时提供完整内容查看入口。\n", encoding="utf-8")
            (root / "笔记" / "AI协作错题本.md").write_text("<!-- GUARDRAILS_START -->\n1. 先读原文。\n<!-- GUARDRAILS_END -->\n", encoding="utf-8")
            (root / "笔记" / "进行中的项目.md").write_text("### 1. 聊天助手 (`chat-app`)\n待联调\n", encoding="utf-8")
            kb_search.build_search_index(root)

            # Unspaced natural-language Chinese query rescued via soft bigram FTS5
            hits = kb_search.search_kb(root, "前端页面文字出现省略号该怎么处理", mode="fts", limit=3)
            self.assertTrue(hits)
            self.assertIn("省略号", hits[0]["snippet"])

            # Multi-keyword quorum ((N-1)-of-N AND) rescues curated note matching 2 of 3 keywords
            quorum_hits = kb_search.search_kb(root, "动态文本 省略号 不存在的第三词", mode="fts", limit=3)
            self.assertTrue(quorum_hits)
            self.assertEqual(quorum_hits[0]["path"], "个人档案.md")

            # RRF fusion prioritizes items found in both FTS and Vector rankings
            fused = kb_search.rrf_fuse(
                [{"path": "a.md", "line": 1, "tier": "archive", "updated_at": "2026-11-01"}, {"path": "b.md", "line": 1, "tier": "archive", "updated_at": "2026-11-02"}],
                [{"path": "b.md", "line": 1, "tier": "archive", "updated_at": "2026-11-02"}],
                limit=2, per_conv=1,
            )
            self.assertEqual(fused[0]["path"], "b.md")
            self.assertEqual(fused[0]["match_via"], "fts+vector")

            # Verify MCP server read tools, Windows path parsing, section jump & path traversal guard
            py311 = Path("/usr/local/bin/python3")
            mcp_py = str(py311) if py311.is_file() else sys.executable
            script = (
                "import json, sys; from pathlib import Path; "
                f"sys.path.insert(0, {str(SYNC_DIR)!r}); import kb_mcp; "
                f"r = Path({str(root)!r}); "
                "ov = kb_mcp.kb_overview_data(); assert ov['guardrails'] == ['1. 先读原文。']; "
                "ctx = kb_mcp.kb_project_context_data('/Users/dev/chat-app', r); assert len(ctx['project_sections']) == 1; "
                "ctx_win = kb_mcp.kb_project_context_data(r'C:\\Users\\dev\\chat-app', r); assert len(ctx_win['project_sections']) == 1; "
                "rd = kb_mcp.kb_read_note_data('个人档案.md', section='前端实现约定', root=r); assert rd['start_line'] == 3; "
                "ok_block = False\n"
                "try:\n kb_mcp.kb_read_note_data('../../etc/passwd', root=r)\n"
                "except ValueError:\n ok_block = True\n"
                "assert ok_block; print('MCP_OK')"
            )
            out = subprocess.run([mcp_py, "-c", script], env={**os.environ, "AGENT_MEMORY_KB_ROOT": str(root)}, capture_output=True, text=True, encoding="utf-8", check=True)
            self.assertIn("MCP_OK", out.stdout)


class TestIcloudMirrorResilience(unittest.TestCase):
    def test_permission_error_on_replace_and_unlink_does_not_crash_and_orphans_cleaned(self):
        from unittest import mock
        import kb_backup

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundle = root / "local.bundle"
            bundle.write_bytes(b"dummy-bundle")
            ic_dir = root / "icloud"
            ic_dir.mkdir()
            ic_target = ic_dir / "mirror.bundle"
            orphan = ic_dir / ".mirror.bundle.tmp.99999"
            orphan.write_bytes(b"stale-tmp")

            with mock.patch("os.replace", side_effect=PermissionError(1, "Operation not permitted")), \
                 mock.patch.object(Path, "unlink", side_effect=PermissionError(1, "Operation not permitted")):
                synced, note, encrypted = kb_backup.sync_icloud_mirror(root, bundle, ic_target, passphrase_override="")
                self.assertFalse(synced)
                self.assertIn("TCC", note or "")
                self.assertFalse(encrypted)

            # Successful sync must commit target and prune stale orphan .tmp.* siblings
            synced_ok, note_ok, _ = kb_backup.sync_icloud_mirror(root, bundle, ic_target, passphrase_override="")
            self.assertTrue(synced_ok)
            self.assertIsNone(note_ok)
            self.assertTrue(ic_target.is_file())
            self.assertFalse(orphan.exists())


class TestInitGeneralProfile(unittest.TestCase):
    def test_init_general_profile_passes_doctor(self):
        repo_root = SYNC_DIR.parent
        init_script = repo_root / "init_kb.py"
        if not init_script.exists():
            self.skipTest("init_kb.py not present in parent directory")
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "general-kb"
            subprocess.run(
                [sys.executable, str(init_script), "--root", str(target), "--profile", "general"],
                check=True,
                capture_output=True,
                text=True,
            )
            proj = (target / "笔记" / "进行中的项目.md").read_text(encoding="utf-8")
            mistake = (target / "笔记" / "AI协作错题本.md").read_text(encoding="utf-8")
            self.assertIn("进行中的目标与事项看板", proj)
            self.assertIn("通用非研发版", mistake)
            doc_out = subprocess.run(
                [sys.executable, str(target / "_同步" / "kb_doctor.py"), "--root", str(target), "--skip-external-checks"],
                check=True,
                capture_output=True,
                text=True,
            )
            report = json.loads(doc_out.stdout)
            self.assertTrue(report["healthy"], f"Doctor failed for general profile: {report}")


if __name__ == "__main__":
    unittest.main()
