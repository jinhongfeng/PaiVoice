# 五轨记忆 + 自动进化 设计 v2（2026-09-06，Electron 就绪版）

> 目标：让 AI 伴侣（她/K）自动进化——没人打电话的时候，后台用模型把最近对话总结成
> 对人的理解（今日日志/项目关键记忆/用户档案/长期记忆），下一轮通话自动注入上下文，
> 让她越来越懂他。
>
> **v2 核心变更：去掉 MySQL，全部落到 SQLite（Python 标准库 `sqlite3`，零 pip 依赖）。**
> 这是为 Electron 打包设计的：目标机器不需要 MySQL 服务器、不需要 Python 环境、
> 断网可离线存储。数据文件放操作系统应用数据目录，与程序本体分离。

## 0. 为什么 v1（MySQL）不能用于 Electron

| 问题 | v1（MySQL） | v2（SQLite） |
|---|---|---|
| 目标机器要有 MySQL | 每台都要装/连服务器 | 无需任何服务器 |
| Python 依赖 | 需 `pip install pymysql`（server.py:197 模块级 import，缺了就 ImportError） | `sqlite3` 是标准库，PyInstaller 直接打包 |
| 离线 | 不行 | 可以 |
| 单文件备份/迁移 | 导出麻烦 | 拷一个 `.db` 文件即可 |
| 并发 | 要防 1213 死锁（现代码专门写了重试） | WAL + 单写线程，无死锁 |

现有的 Supabase 云端落盘（TurnWriter，server.py:794）是另一条链路（可选云端同步），
与本地 SQLite 并存，不受影响。

## 1. 五轨定义

| 轨 | 名字 | 内容 | 生命周期 |
|---|---|---|---|
| L0 | 对话原文 | 每轮 user/assistant 原文（就是 `turns` 表本身） | 只增不删 |
| L1 | 今日日志 | 今天聊了什么主题、摘要 | 按天覆盖 |
| L2 | 项目关键记忆 | 与代码/项目相关的事实，**按 git 分支过滤** | 每分支一组，upsert |
| L3 | 用户档案 | 结构化事实：称呼、职业、喜好、雷点、关系 | 增改，有置信度 |
| L4 | 长期记忆 | 跨日重大事件、他的重要决定、关系里程碑 | 只增不改，可折叠 |

L0（`turns` 表）是唯一事实源，L1–L4 都是从中蒸馏的"可注入摘要"。
原有"最近 N 轮回放"（现 `_mem_load_history` 语义）继续存在，读的也是 L0。

## 2. 存储：单文件 SQLite

### 2.1 位置（Electron 就绪）

```
<data_dir>/paivoice.db        # 一个文件装全部
```

`data_dir` 解析顺序（env `PAIVOICE_DATA_DIR` 优先）：
- Electron 打包运行时：Electron 主进程启动后端时设 `PAIVOICE_DATA_DIR=<app.getPath('userData')>`
  → Windows `%APPDATA%\PaiVoice` / macOS `~/Library/Application Support/PaiVoice` /
  Linux `~/.local/share/paivoice`
- 本地开发：`<repo>/data/`（`.gitignore` 已含 `data/`）

### 2.2 Schema（一个库五张表 + meta）

```sql
PRAGMA journal_mode=WAL;   -- 读写不互斥

-- L0 原文（取代 MySQL voice_call_turns）
CREATE TABLE IF NOT EXISTS turns (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  call_session_id TEXT NOT NULL,
  turn_seq      INTEGER NOT NULL,
  turn_id       TEXT NOT NULL,
  role          TEXT NOT NULL CHECK(role IN ('user','assistant')),
  content       TEXT NOT NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(call_session_id, turn_seq, role)
);
CREATE INDEX IF NOT EXISTS idx_turns_id ON turns(id);

-- L3 用户档案：fact 唯一 → 天然去重（模型合并 + 库约束双保险）
CREATE TABLE IF NOT EXISTS profile (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  fact     TEXT NOT NULL UNIQUE,
  category TEXT NOT NULL DEFAULT '其他',
  confidence REAL NOT NULL DEFAULT 0.5,
  since    TEXT,
  last_seen TEXT NOT NULL DEFAULT (datetime('now'))
);

-- L4 长期记忆：只追加
CREATE TABLE IF NOT EXISTS longterm (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  event      TEXT NOT NULL,
  date       TEXT,
  importance REAL NOT NULL DEFAULT 0.5
);

-- L2 项目关键记忆：按 git 分支分组
CREATE TABLE IF NOT EXISTS project_memory (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  branch     TEXT NOT NULL,
  fact       TEXT NOT NULL,
  category   TEXT NOT NULL DEFAULT '项目',
  confidence REAL NOT NULL DEFAULT 0.5,
  UNIQUE(branch, fact)
);

-- L1 今日日志：一行一天
CREATE TABLE IF NOT EXISTS today_log (
  date    TEXT PRIMARY KEY,
  topics  TEXT NOT NULL DEFAULT '[]',   -- JSON 数组
  summary TEXT NOT NULL DEFAULT ''
);

-- meta 键值：cursor / last_summary_at / last_branch / suggestion
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
```

### 2.3 写入纪律（与 v1 同哲学）

- 所有 SQLite 写收拢到单线程 executor（沿用现 `_mem_exec` 单线程池模式，server.py:207）；
  读可走同线程，简单一致。
- 失败只留日志，绝不影响通话主流程（延续 server.py:270-277 的"记忆锦上添花"）。
- `cursor` = 后台总结已处理的 `turns.id` 最大值，存 `meta`；重启不丢、不重总结。
- 备份 = 拷 `paivoice.db`；让她彻底失忆 = 删库文件或 `DELETE` 各表。

## 3. 自动进化引擎（核心，与 v1 相同思路，读库不读 jsonl）

### 3.1 触发时机（只在空闲）
- `main()` 里起后台 asyncio 任务，每 `PAIVOICE_MEMORY_INTERVAL`（默认 600s）检查：
  1. 无活跃通话（模块级 `_active_calls` 计数为 0）；
  2. 距上次总结 ≥ `PAIVOICE_MEMORY_COOLDOWN`（默认 300s）；
  3. `meta.cursor` 之后有 ≥ `PAIVOICE_MEMORY_MIN_TURNS`（默认 5）条新轮次。
- 满足才跑。挂断后是天然空闲窗口，通常挂断几分钟后就自动总结一次。

### 3.2 总结调用
- 复用大脑端点：`GATEWAY_URL` + `GATEWAY_TOKEN` + `LLM_MODEL`（可 `PAIVOICE_MEMORY_SUMMARY_MODEL` 覆盖），非流式，`max_tokens≈1200`。
- Prompt 要点：
  - 输入：cursor 之后的新轮次原文（截最近 N 条）；
  - 附**现有**档案/长期/日志/项目记忆，指令"只输出新增或修正，重复的不许再输出"——模型合并去重，不做向量库（单人场景）；
  - 输出结构化 JSON：`{profile_updates[], longterm_events[], project_memories[], today_topics[]}`；
  - 每条带 `confidence`(0-1) 与 `category`（身份/偏好/工作/关系/雷点/其他）。
- 解析失败/格式不合法：丢弃本轮，下轮再试（不阻塞）。

### 3.3 合并写盘
- L3：`INSERT ... ON CONFLICT(fact) DO UPDATE SET confidence, last_seen`（同文案覆盖，新文案追加；上限 60 条，超限把 confidence 最低+最旧的一条折叠进 L4 或丢弃）。
- L4：只 INSERT；上限 200 条，超限让模型压缩合并同类。
- L2：按 `current_branch()` 分组 upsert（`UNIQUE(branch, fact)`）。
- L1：`ON CONFLICT(date) DO UPDATE`，主题合并。

### 3.4 分支检测（Electron 注意）
- `current_branch()`：`PAIVOICE_GIT_ROOT` 指定项目根时用 `git -C <root> branch --show-current`；
  开发模式（仓库内跑）自动向上找 `.git`；
  **打包后的 Electron 机器上没有 git 仓库 → 回退 `default` 桶**，L2 照常工作（只是不分支）。
- 优雅降级：无 git = 单一 `default` 分组，功能不丢。

### 3.5 "优化自己"两档（不变）
- **Lv1（默认开）**：用户画像自动进上下文 → 她自然贴合他的话题/口吻偏好。
- **Lv2（默认关）**：不自动改写 `persona.json` 的 tone/style（人格突变不可预期）；
  改为总结 prompt 额外产出一条"语气风格建议"存 `meta.suggestion`，人设面板显示建议供他确认。

## 4. 上下文注入（每轮怎么进）

`_call_gateway`（server.py:559-668）组装 messages 时，`persona_sys` 与 `SYSTEM_PROMPT` 之间插一条 system 记忆块：

```
【今日日志】…（≤200字）
【用户档案】…（≤600字）
【长期记忆】…（≤800字）
【项目关键记忆·当前分支】…（≤600字）
```

- 各段有字数预算（env 可调），超限从旧截断——闲聊 max_tokens=384，记忆块必须小。
- L2 读取时 `WHERE branch = current_branch()`，只注入当前分支（用户硬要求）。
- 读取失败静默（只读不写，不影响通话）。

## 5. 迁移（可选）：已有 MySQL 数据怎么办

提供一次性脚本 `tools/migrate_mysql_to_sqlite.py`（可选做）：
读旧 `voice_call_turns` 逐行 INSERT 进 `turns`，然后正常启用 SQLite 后端。
不做也不影响新安装。

## 6. Electron 打包清单（记忆相关）

| 项 | 处理 |
|---|---|
| Python 后端 | PyInstaller 打成单 exe，Electron 主进程 spawn 启动（子进程方式）；`sqlite3` 随标准库自动打包 |
| 数据目录 | 主进程 `app.getPath('userData')` → 子进程 env `PAIVOICE_DATA_DIR` |
| 前端 | Electron 加载打包内的 index.html 或外挂 dev server，与记忆无耦合 |
| pymysql | 删除硬依赖（v2 不再 import），打包体积变小 |
| 卸载 | 数据在 userData，卸载程序不清（符合"数据属于用户"惯例）；彻底清除 = 删目录 |

## 7. 关键权衡（不做的事）
- 不做向量检索/embedding：单人场景，模型合并去重够用，零额外依赖。
- 不保留 MySQL 为默认后端；仅当确有"多设备同步"需求时才值得再评估（且那时 Supabase 已是现成云路，MySQL 冗余）。
- 不改 Supabase TurnWriter（云端可选同步，与本地 SQLite 并存）。
- 不做自动改写人格预设（Lv2 只产出建议）。

## 8. 落地阶段
- M0：SQLite 存储层 + 替换 `_mem_*` 后端（turns 落库/回放），删 pymysql 硬依赖。
- M1：后台总结任务 + L1/L3/L4 写盘。
- M2：L2 项目记忆（分支检测 + 按分支读写注入）。
- M3：前端记忆面板 + 手动总结 + Lv2 建议确认。

## 9. 验证
- `python tests/test_five_track_memory.py`：schema 建表、turns 落库/回放、游标推进、
  原子写、分支过滤、去重合并（UNIQUE 约束 + 模型指令双保险）、预算截断。
- 冒烟：无 MySQL 环境下跑通通话流程 → `data/paivoice.db` 生成 → 手动触发总结 →
  profile/today_log 有内容 → 再拨号日志里出现记忆块注入。
- 全量 `python -m pytest tests/ -q` 不新增失败（既有 1 个前端 pet 测试失败为改版遗留，不在本范围）。
