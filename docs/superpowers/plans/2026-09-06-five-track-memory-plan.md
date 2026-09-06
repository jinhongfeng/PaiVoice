# 五轨记忆 + 自动进化 实施方案 v2（2026-09-06，SQLite 后端）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把记忆架构从 MySQL 迁移到 SQLite（Python 标准库，Electron 打包零依赖），并实现
"五轨记忆 + 自动进化"：通话原文落库（L0=turns 表），后台空闲时模型自动总结出
今日日志（L1）/项目关键记忆（L2，按 git 分支分组）/用户档案（L3）/长期记忆（L4），
每轮通话自动注入记忆块，让她越来越懂他。设计见
`docs/superpowers/specs/2026-09-06-five-track-memory-design.md`。

**Tech Stack:** Python asyncio + `sqlite3`（标准库，无 pip 依赖）+ aiohttp（复用大脑端点非流式调用）+ 单线程写 executor。

## Global Constraints

- 不影响通话主流程：记忆读写/总结失败只留日志，绝不阻塞 answer_turn。
- 删除 pymysql 硬依赖（v2 不再 import）；`PAIVOICE_MYSQL_*` env 兼容保留但置空即停用。
- L2 按当前 git 分支过滤；无 git（打包机器）回退 `default` 桶，功能不丢。
- Lv2 只产出 persona 建议，不自动改。
- 数据目录：env `PAIVOICE_DATA_DIR` 优先，否则 `<repo>/data/`；文件 `paivoice.db`（`.gitignore` 已含 `data/`）。
- 不写前端面板（M3 单独一期）；本次只做服务端 + 测试。

---

## Task 1: SQLite 存储层（替换 MySQL 后端）

**Files:**
- Create: `packages/realtime-core/memory_store.py`
- Modify: `packages/realtime-core/server.py`（替换 `_mem_*` 的 MySQL 实现）

**Interfaces:**
- `MemoryStore` 类（构造传入 data_dir）：
  - `init_schema()`：建五表 + meta（见设计 §2.2）
  - `append_turn(call_id, turn_seq, turn_id, role, text)`（L0）
  - `load_recent(limit)`（旧 `_mem_load_history` 语义：最近 N 条跨通话回放）
  - `load_turns_since(cursor)` → (rows, new_cursor)
  - `read_today() / read_profile() / read_longterm() / read_project(branch)` 读器（[] 兜底）
  - `upsert_profile(fact, category, confidence)` / `append_longterm(event, date, importance)` /
    `upsert_project(branch, fact, category, confidence)` / `upsert_today(date, topics, summary)`
  - `get_meta(key) / set_meta(key, value)`（cursor / last_summary_at / last_branch / suggestion）
  - `current_branch()`：`PAIVOICE_GIT_ROOT` 或向上找 `.git`，`git -C <root> branch --show-current`；失败 → `default`
  - 全部写走单线程 executor（构造时建 `ThreadPoolExecutor(max_workers=1)`）
- server.py：模块级 `_mem_enabled()` 改为"data_dir 可写即启用"（不再依赖 MYSQL_HOST）；
  `_mem_save_turn`/`_mem_load_history` 改调 store；删除 `pymysql` import 与连接池（server.py:196-308）。

- [ ] **Step 1: 写 memory_store.py（纯 sqlite3，单测友好）**
- [ ] **Step 2: server.py 替换 MySQL 后端**（删 pymysql import/池/死锁重试，接 store）
- [ ] **Step 3: 单测** `tests/test_five_track_memory.py`：建表、落库/回放、游标、分支回退、UNIQUE 去重

## Task 2: 自动进化引擎（后台总结任务）

**Files:**
- Modify: `packages/realtime-core/server.py`
- Create: `packages/realtime-core/memory_evolve.py`

**Interfaces:**
- env：`PAIVOICE_MEMORY_INTERVAL`(600s) / `PAIVOICE_MEMORY_COOLDOWN`(300s) /
  `PAIVOICE_MEMORY_MIN_TURNS`(5) / `PAIVOICE_MEMORY_SUMMARY_MODEL`(空=LLM_MODEL)
- `_active_calls` 模块级计数：Call 创建 +1、session finally -1
- `memory_evolve.py::maybe_summarize(store, http, gateway_cfg) -> bool`：
  1. 检查空闲+冷却+新轮次（cursor 后 ≥ MIN_TURNS）
  2. 拼总结 prompt（附现有档案/日志，指令"只输出新增/修正"）→ 非流式调大脑端点 → JSON 解析
  3. 合并写 L1/L3/L4；L2 按 current_branch() 分组 upsert；suggestion 存 meta
  4. 推进 meta.cursor
- main() 里 `asyncio.create_task(memory_loop())`，间隔 sleep，异常吞掉留日志

- [ ] **Step 1: 后台任务骨架 + 空闲/冷却/新轮检查**
- [ ] **Step 2: 总结 prompt + 非流式调用 + JSON 解析**（失败丢弃本轮）
- [ ] **Step 3: 合并写盘 + 游标推进**
- [ ] **Step 4: 单测**：prompt 拼接与合并规则（注入假 http，不真调 API）

## Task 3: 上下文注入（每轮记忆块）

**Files:**
- Modify: `packages/realtime-core/server.py`

**Interfaces:**
- `_memory_block()`：L1/L3/L4 + 当前分支 L2 拼成一条 system（预算：今日200/档案600/长期800/项目600，env 可调），全空返回 ""
- `_call_gateway` 在 persona_sys 与 SYSTEM_PROMPT 之间插入（server.py:573-577）
- 读取失败静默（只读不写）

- [ ] **Step 1: `_memory_block()` + 预算截断**
- [ ] **Step 2: 注入 `_call_gateway`**（日志打印记忆块字数）
- [ ] **Step 3: 单测**：预算截断、分支过滤、全空无注入

## Task 4: env/文档 + 全量回归

**Files:**
- Modify: `.env.example`（`PAIVOICE_DATA_DIR` + 记忆参数；MYSQL 段标注"旧版，置空停用"）
- Modify: `docs/本地搭建.md`（新增"五轨记忆（SQLite）"一节，说明无 MySQL 也能用）
- Modify: `README.md`（魔改清单加一行：记忆改 SQLite + 自动进化）

- [ ] **Step 1: `.env.example` 更新**
- [ ] **Step 2: docs 更新**（目录布局/表结构/备份=拷 db 文件/失忆=删 db）
- [ ] **Step 3: 全量 `python -m pytest tests/ -q` 不新增失败**（基线 25 passed 1 failed，前端 pet 测试遗留）

---

## 交付验证
- `python -m pytest tests/test_five_track_memory.py -q` 全绿
- `python -m pytest tests/ -q`：不新增失败
- 无 MySQL 环境冒烟：跑通话流程 → `data/paivoice.db` 生成、turns 有行 → 手动触发总结 →
  profile/today_log 有内容 → 再拨号日志出现记忆块注入
