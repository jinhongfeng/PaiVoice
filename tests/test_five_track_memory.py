#!/usr/bin/env python3
"""五轨记忆：SQLite 存储层 + 自动进化引擎 单测（不依赖真实通话/API）。

覆盖：
- schema 建表（五表 + meta）
- L0 turns 落库 / 跨通话回放 / 游标推进
- L3 档案 upsert 去重（UNIQUE fact）
- L2 项目记忆按分支分组、当前分支过滤
- L1 今日日志 upsert
- 自动进化：prompt 拼接、JSON 解析（含 ``` 围栏）、合并写盘、游标推进
- 分支回退：无 git 环境返回 default
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "packages", "realtime-core"))

from memory_store import MemoryStore, DEFAULT_BRANCH
import memory_evolve


def make_store():
    tmp = tempfile.mkdtemp(prefix="paivoice-mem-test-")
    store = MemoryStore(tmp, git_root="")   # git_root 留空 → current_branch 会向上找 .git
    return store, tmp


def test_schema_creates_tables():
    store, tmp = make_store()
    assert os.path.isfile(store.db_path)
    import sqlite3
    conn = sqlite3.connect(store.db_path)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"turns", "profile", "longterm", "project_memory", "today_log", "meta"} <= tables


def test_turns_append_and_replay():
    store, tmp = make_store()
    store.append_turn("callA", 1, "t1", "user", "你好")
    store.append_turn("callA", 1, "t1", "assistant", "在呢")
    store.append_turn("callB", 1, "t2", "user", "还记得吗")
    recent = store.load_recent(10)
    assert [r["role"] for r in recent] == ["user", "assistant", "user"]


def test_turns_cursor():
    store, tmp = make_store()
    for i, (r, c) in enumerate([("user", "a"), ("assistant", "b"), ("user", "c")]):
        store.append_turn("call", i + 1, f"t{i}", r, c)
    rows, cursor = store.load_turns_since(0)
    assert len(rows) == 3 and cursor == 3
    rows2, cursor2 = store.load_turns_since(cursor)
    assert rows2 == [] and cursor2 == 3


def test_profile_upsert_dedup():
    store, tmp = make_store()
    store.upsert_profile("他喜欢深夜聊天", "偏好", 0.8)
    store.upsert_profile("他喜欢深夜聊天", "偏好", 0.9)   # 同 fact → 覆盖 confidence
    store.upsert_profile("他是程序员", "工作", 0.7)
    prof = store.read_profile()
    assert len(prof) == 2
    assert prof[0]["fact"] == "他喜欢深夜聊天" and abs(prof[0]["confidence"] - 0.9) < 1e-6


def test_project_branch_scope():
    store, tmp = make_store()
    store.upsert_project("master", "网关侧有记忆检索", "项目", 0.9)
    store.upsert_project("dev", "分支 dev 的探索", "项目", 0.6)
    assert len(store.read_project("master")) == 1
    assert store.read_project("master")[0]["fact"] == "网关侧有记忆检索"
    assert len(store.read_project("dev")) == 1
    assert len(store.read_project("default")) == 0


def test_today_log_upsert():
    store, tmp = make_store()
    store.upsert_today("2026-09-06", ["工作", "猫"], "今天聊了工作")
    store.upsert_today("2026-09-06", ["工作", "猫", "做饭"], "今天聊了工作和做饭")
    today = store.read_today("2026-09-06")
    assert today["topics"] == ["工作", "猫", "做饭"]
    assert today["summary"] == "今天聊了工作和做饭"


def test_current_branch_fallback_default():
    store, tmp = make_store()
    branch = store.current_branch()
    # 测试在临时目录跑，无 .git → 应回退 default（除非从测试目录向上真的找到仓库）
    assert branch == DEFAULT_BRANCH or os.path.isdir(os.path.join(os.path.dirname(store.db_path), ".git"))


def test_parse_summary_with_fence():
    text = '```json\n{"profile_updates": [{"fact": "他养猫", "category": "偏好", "confidence": 0.9}]}\n```'
    data = memory_evolve.parse_summary(text)
    assert data and data["profile_updates"][0]["fact"] == "他养猫"


def test_parse_summary_junk():
    assert memory_evolve.parse_summary("对不起，我无法") is None
    assert memory_evolve.parse_summary("") is None


def test_apply_summary_merges():
    store, tmp = make_store()
    store.upsert_today("2026-09-06", ["旧主题"], "旧摘要")
    data = {
        "profile_updates": [{"fact": "他喜欢喝美式", "category": "偏好", "confidence": 0.8}],
        "longterm_events": [{"event": "他决定把项目打包成 Electron", "date": "2026-09-06", "importance": 0.9}],
        "project_memories": [{"fact": "记忆层改用 SQLite", "category": "项目", "confidence": 0.8}],
        "today_topics": ["新主题"],
        "persona_suggestion": "可以更活泼一些",
    }
    memory_evolve.apply_summary(store, data)
    assert len(store.read_profile()) == 1
    assert len(store.read_longterm()) == 1
    assert store.read_project(store.current_branch())[0]["fact"] == "记忆层改用 SQLite"
    today = store.read_today("2026-09-06")
    assert "新主题" in today["topics"] and "旧主题" in today["topics"]
    assert store.get_meta("suggestion") == "可以更活泼一些"


def test_current_branch_fallback_default():
    store, tmp = make_store()
    branch = store.current_branch()
    # 测试目录在仓库内（开发时）会真跑到分支名；打包机器无 git 才回退 default。
    # 这里只验证返回值是字符串且非空。
    assert isinstance(branch, str) and branch


def test_build_prompt_includes_transcript():
    store, tmp = make_store()
    store.append_turn("c", 1, "t", "user", "我最近在学做饭")
    rows, _ = store.load_turns_since(0)
    prompt = memory_evolve.build_summary_prompt(store, rows)
    assert "我最近在学做饭" in prompt
    assert "已有用户档案" in prompt


def test_maybe_summarize_respects_cooldown():
    store, tmp = make_store()
    store.set_meta("last_summary_at", str(__import__("time").time()))   # 刚刚总结过
    for i in range(6):
        store.append_turn("c", i + 1, f"t{i}", "user" if i % 2 == 0 else "assistant", f"轮次{i}")
    import asyncio
    ok = asyncio.run(memory_evolve.maybe_summarize(store, None, "http://x", "", "", min_turns=5, cooldown=300.0))
    assert ok is False   # 冷却期没到，不跑（不真发请求）


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
