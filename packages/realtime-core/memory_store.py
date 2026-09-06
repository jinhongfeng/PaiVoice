#!/usr/bin/env python3
"""五轨记忆存储层：单文件 SQLite（标准库 sqlite3，零 pip 依赖，Electron 打包就绪）。

五轨全部装在一张库里（见 docs/superpowers/specs/2026-09-06-five-track-memory-design.md §2.2）：
  turns          L0 对话原文（唯一事实源；取代旧 MySQL voice_call_turns）
  today_log      L1 今日日志（一行一天）
  project_memory L2 项目关键记忆（按 git 分支分组）
  profile        L3 用户档案（fact 唯一 → 天然去重）
  longterm       L4 长期记忆（只追加）
  meta           游标 / 上次总结时间 / 当前分支 / persona 建议

写入纪律：所有写收拢到单线程 executor（与旧 _mem_exec 同款哲学，死锁免疫）；
失败只留日志，绝不影响通话主流程。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  call_session_id TEXT NOT NULL,
  turn_seq        INTEGER NOT NULL,
  turn_id         TEXT NOT NULL,
  role            TEXT NOT NULL CHECK(role IN ('user','assistant')),
  content         TEXT NOT NULL,
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(call_session_id, turn_seq, role)
);
CREATE INDEX IF NOT EXISTS idx_turns_id ON turns(id);

CREATE TABLE IF NOT EXISTS profile (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  fact      TEXT NOT NULL UNIQUE,
  category  TEXT NOT NULL DEFAULT '其他',
  confidence REAL NOT NULL DEFAULT 0.5,
  since     TEXT,
  last_seen TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS longterm (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  event      TEXT NOT NULL,
  date       TEXT,
  importance REAL NOT NULL DEFAULT 0.5
);

CREATE TABLE IF NOT EXISTS project_memory (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  branch     TEXT NOT NULL,
  fact       TEXT NOT NULL,
  category   TEXT NOT NULL DEFAULT '项目',
  confidence REAL NOT NULL DEFAULT 0.5,
  UNIQUE(branch, fact)
);

CREATE TABLE IF NOT EXISTS today_log (
  date    TEXT PRIMARY KEY,
  topics  TEXT NOT NULL DEFAULT '[]',
  summary TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""

DEFAULT_BRANCH = "default"


class MemoryStore:
    """单文件 SQLite 记忆库。进程内单实例；写经单线程 executor 串行化。"""

    def __init__(self, data_dir: str, git_root: str = ""):
        os.makedirs(data_dir, exist_ok=True)
        self.db_path = os.path.join(data_dir, "paivoice.db")
        self.git_root = git_root or ""
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="paivoice-mem")
        self._lock = threading.Lock()   # 双保险：读也经 executor，锁只护 get_meta/set_meta 之外的并发
        self._init_schema()

    # ---------- 连接 ----------
    def _connect(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self):
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    # ---------- L0 原文 ----------
    def append_turn(self, call_session_id: str, turn_seq: int, turn_id: str, role: str, text: str) -> None:
        text = (text or "")[:4000]
        if not text:
            return
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO turns (call_session_id, turn_seq, turn_id, role, content) "
                    "VALUES (?,?,?,?,?)",
                    (call_session_id, turn_seq, turn_id, role, text))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            print(f"[memory] append_turn failed: {e}", flush=True)

    def load_recent(self, limit: int) -> list[dict]:
        """旧 _mem_load_history 语义：最近 limit 条跨通话回放（id 升序）。"""
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT role, content FROM (SELECT id, role, content FROM turns "
                    "ORDER BY id DESC LIMIT ?) recent ORDER BY id ASC", (limit,)).fetchall()
                return [{"role": r, "content": c} for r, c in rows]
            finally:
                conn.close()
        except Exception as e:
            print(f"[memory] load_recent failed: {e}", flush=True)
            return []

    def load_turns_since(self, cursor: int, limit: int = 200) -> tuple[list[dict], int]:
        """后台总结用：cursor 之后的原文行（id 升序）。返回 (rows, 新 cursor=最后一行 id)。"""
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id, role, content FROM turns WHERE id > ? ORDER BY id ASC LIMIT ?",
                    (cursor, limit)).fetchall()
                new_cursor = cursor
                out = []
                for rid, role, content in rows:
                    out.append({"id": rid, "role": role, "content": content})
                    new_cursor = rid
                return out, new_cursor
            finally:
                conn.close()
        except Exception as e:
            print(f"[memory] load_turns_since failed: {e}", flush=True)
            return [], cursor

    # ---------- L3 用户档案 ----------
    def read_profile(self, limit: int = 60) -> list[dict]:
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT fact, category, confidence, since, last_seen FROM profile "
                    "ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()
                return [{"fact": r[0], "category": r[1], "confidence": r[2],
                         "since": r[3], "last_seen": r[4]} for r in rows]
            finally:
                conn.close()
        except Exception as e:
            print(f"[memory] read_profile failed: {e}", flush=True)
            return []

    def upsert_profile(self, fact: str, category: str = "其他", confidence: float = 0.5) -> None:
        fact = (fact or "").strip()
        if not fact:
            return
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO profile (fact, category, confidence, since, last_seen) VALUES (?,?,?,datetime('now'),datetime('now')) "
                    "ON CONFLICT(fact) DO UPDATE SET category=excluded.category, "
                    "confidence=excluded.confidence, last_seen=datetime('now')",
                    (fact, category or "其他", float(confidence)))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            print(f"[memory] upsert_profile failed: {e}", flush=True)

    # ---------- L4 长期记忆 ----------
    def read_longterm(self, limit: int = 200) -> list[dict]:
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT event, date, importance FROM longterm "
                    "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
                return [{"event": r[0], "date": r[1], "importance": r[2]} for r in rows]
            finally:
                conn.close()
        except Exception as e:
            print(f"[memory] read_longterm failed: {e}", flush=True)
            return []

    def append_longterm(self, event: str, date: str = "", importance: float = 0.5) -> None:
        event = (event or "").strip()
        if not event:
            return
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO longterm (event, date, importance) VALUES (?,?,?)",
                    (event, date or "", float(importance)))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            print(f"[memory] append_longterm failed: {e}", flush=True)

    # ---------- L2 项目关键记忆 ----------
    def read_project(self, branch: str = "") -> list[dict]:
        branch = branch or self.current_branch()
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT branch, fact, category, confidence FROM project_memory "
                    "WHERE branch=? ORDER BY id DESC LIMIT 60", (branch,)).fetchall()
                return [{"branch": r[0], "fact": r[1], "category": r[2], "confidence": r[3]} for r in rows]
            finally:
                conn.close()
        except Exception as e:
            print(f"[memory] read_project failed: {e}", flush=True)
            return []

    def upsert_project(self, branch: str, fact: str, category: str = "项目", confidence: float = 0.5) -> None:
        branch = branch or self.current_branch()
        fact = (fact or "").strip()
        if not fact:
            return
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO project_memory (branch, fact, category, confidence) VALUES (?,?,?,?) "
                    "ON CONFLICT(branch, fact) DO UPDATE SET category=excluded.category, "
                    "confidence=excluded.confidence",
                    (branch, fact, category or "项目", float(confidence)))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            print(f"[memory] upsert_project failed: {e}", flush=True)

    # ---------- L1 今日日志 ----------
    def read_today(self, date: str = "") -> dict:
        date = date or time.strftime("%Y-%m-%d")
        try:
            conn = self._connect()
            try:
                row = conn.execute("SELECT date, topics, summary FROM today_log WHERE date=?", (date,)).fetchone()
                if not row:
                    return {"date": date, "topics": [], "summary": ""}
                try:
                    topics = json.loads(row[1])
                except Exception:
                    topics = []
                return {"date": row[0], "topics": topics if isinstance(topics, list) else [], "summary": row[2] or ""}
            finally:
                conn.close()
        except Exception as e:
            print(f"[memory] read_today failed: {e}", flush=True)
            return {"date": date, "topics": [], "summary": ""}

    def upsert_today(self, date: str, topics: list, summary: str) -> None:
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO today_log (date, topics, summary) VALUES (?,?,?) "
                    "ON CONFLICT(date) DO UPDATE SET topics=excluded.topics, summary=excluded.summary",
                    (date, json.dumps(topics or [], ensure_ascii=False), (summary or "")[:4000]))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            print(f"[memory] upsert_today failed: {e}", flush=True)

    # ---------- meta 键值 ----------
    def get_meta(self, key: str, default: str = "") -> str:
        try:
            conn = self._connect()
            try:
                row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
                return row[0] if row else default
            finally:
                conn.close()
        except Exception as e:
            print(f"[memory] get_meta failed: {e}", flush=True)
            return default

    def set_meta(self, key: str, value: str) -> None:
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            print(f"[memory] set_meta failed: {e}", flush=True)

    # ---------- 分支检测（Electron 注意：无 git 回退 default）----------
    def current_branch(self) -> str:
        root = self.git_root
        if root:
            try:
                out = subprocess.run(
                    ["git", "-C", root, "branch", "--show-current"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                name = (out.stdout or "").strip()
                if name:
                    return name
            except Exception:
                pass
        # 开发模式：从 data_dir 向上找仓库根（.git 目录）；找不到 = 打包机器 → default
        try:
            d = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # packages/realtime-core
            while True:
                if os.path.isdir(os.path.join(d, ".git")):
                    out = subprocess.run(
                        ["git", "-C", d, "branch", "--show-current"],
                        capture_output=True, text=True, timeout=5,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    name = (out.stdout or "").strip()
                    return name if name else DEFAULT_BRANCH
                parent = os.path.dirname(d)
                if parent == d:
                    break
                d = parent
        except Exception:
            pass
        return DEFAULT_BRANCH

    def set_git_root(self, root: str) -> None:
        self.git_root = root or ""

    def close(self) -> None:
        try:
            self._exec.shutdown(wait=False)
        except Exception:
            pass
