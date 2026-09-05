# Codex 宠物包管理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让网页通过官方 `codex-pet-installer` 安装 `%USERPROFILE%\.codex\pets` 中的宠物，并支持安全删除。

**Architecture:** `server.py` 负责解析受限输入、以参数数组启动固定 npm 安装器、校验 Codex 宠物包并安全删除；浏览器只调用安装、列表、文件与删除 API。列表返回解析后的精灵图 URL，使前端兼容多个清单字段。

**Tech Stack:** Python 3.12、asyncio/subprocess、websockets HTTP handler、原生 JavaScript、pytest。

## Global Constraints

- 安装输入仅接受宠物 id、`npx codex-pet-installer add <id>` 或带 `--yes` 的同义命令。
- 用户输入不能直接交给 shell；安装器使用固定参数数组启动。
- 宠物目录使用 `CODEX_HOME/pets`，否则使用当前用户目录 `.codex/pets`。
- 删除目标必须是宠物根目录的直接子目录。
- 完整宠物包必须含有效 `pet.json` 和目录内 `.webp` 或 `.png` 精灵图。

---

### Task 1: 宠物包路径、输入解析与清单校验

**Files:**
- Modify: `packages/realtime-core/server.py`
- Create: `tests/test_pet_package_management.py`

**Interfaces:**
- Produces: `_parse_pet_install_input(value: str) -> str`、`_pets_dir() -> str`、`_pet_asset(d: str) -> tuple[str, str] | None`、`_pet_meta(d: str) -> dict`。

- [ ] **Step 1: Write the failing tests**

测试单独 id 与两种固定 npm 命令可解析，额外参数和 shell 元字符被拒绝；测试 `CODEX_HOME` 与默认主目录；用临时目录构造 `spritesheetPath`、`assets.spritesheet` 和目录穿越清单，并断言只有目录内资源有效。

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests\test_pet_package_management.py -v`

Expected: FAIL，因为解析器和资源解析器尚不存在，旧 `_pets_dir` 仍指向网页目录。

- [ ] **Step 3: Write minimal implementation**

在 `server.py` 中用完整正则解析输入；用 `os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")` 选择根目录；解析清单资源字段后使用 `realpath/commonpath` 限制在宠物目录内，并只允许 `.webp/.png`。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests\test_pet_package_management.py -v`

Expected: PASS。

### Task 2: 官方安装器调用与安全删除

**Files:**
- Modify: `packages/realtime-core/server.py`
- Modify: `tests/test_pet_package_management.py`

**Interfaces:**
- Consumes: `_parse_pet_install_input`、`_pet_asset`、`_pet_dir`。
- Produces: `async _pet_install(value: str) -> tuple[bool, str, str]`、`_pet_remove(slug: str) -> tuple[bool, str]`。

- [ ] **Step 1: Write the failing tests**

注入临时 `CODEX_HOME` 和假的异步进程创建器，断言 Windows 使用 `npx.cmd`、其他平台使用 `npx`，参数严格为 `--yes codex-pet-installer add <id>`，工作目录/环境使安装落入测试 Codex 目录；覆盖非零退出、成功但资源缺失、安全删除、不存在和非法 id。

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests\test_pet_package_management.py -v`

Expected: FAIL，因为旧安装函数仍请求已失效的自定义源站且没有删除函数。

- [ ] **Step 3: Write minimal implementation**

使用 `asyncio.create_subprocess_exec` 固定参数启动安装器，设置 `CODEX_HOME`，捕获并截断输出，超时后终止；退出成功后验证包完整。删除前验证 slug、目标直接父目录和实际路径，然后用 `shutil.rmtree` 删除。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests\test_pet_package_management.py -v`

Expected: PASS。

### Task 3: HTTP 安装、删除与动态资源接口

**Files:**
- Modify: `packages/realtime-core/server.py`
- Modify: `tests/test_pet_package_management.py`

**Interfaces:**
- Consumes: `_pet_install`、`_pet_remove`、`_pet_asset`。
- Produces: `POST /v1/pets/install`、`DELETE /v1/pets/<id>`、列表中的 `spriteUrl`、`GET /v1/pets/<id>/asset`。

- [ ] **Step 1: Write the failing tests**

通过源码级契约测试确认安装端点接收 `input`，删除端点调用 `_pet_remove`，列表元数据包含 `spriteUrl`，静态资源端点不依赖固定文件名。

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests\test_pet_package_management.py -v`

Expected: FAIL，因为删除和动态资源路由不存在。

- [ ] **Step 3: Write minimal implementation**

调整 HTTP handler：安装查询参数改为 `input`（保留 `id` 兼容）；添加删除路由与状态码；列表返回 `spriteUrl`；资源路由通过 `_pet_asset` 读取解析出的文件。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests\test_pet_package_management.py -v`

Expected: PASS。

### Task 4: 前端安装输入与删除交互

**Files:**
- Modify: `packages/web-client/index.html`
- Modify: `tests/test_pet_sprite_rendering.py`

**Interfaces:**
- Consumes: `spriteUrl`、安装和删除 HTTP API。
- Produces: `petInstallSprite(input)`、`petRemoveSprite(slug)`，以及每个 Codex 宠物选择项旁的删除按钮。

- [ ] **Step 1: Write the failing tests**

断言占位符展示完整命令示例，安装请求发送原始输入，使用服务端 `spriteUrl`，存在 `DELETE` 请求与当前宠物删除后回退 `cat` 的逻辑。

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests\test_pet_sprite_rendering.py -v`

Expected: FAIL，因为前端仍只发送 slug 且没有删除按钮。

- [ ] **Step 3: Write minimal implementation**

更新提示和安装函数；让 `petRefreshSprites` 保存 `spriteUrl`；为 Codex 宠物渲染选择按钮和删除按钮；删除成功后刷新，若删当前选择则先切换猫猫。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests\test_pet_sprite_rendering.py -v`

Expected: PASS。

### Task 5: 回归与真实安装验收

**Files:**
- Modify: `README.md`（记录输入格式、目录和删除能力）
- Test: `tests/test_pet_package_management.py`
- Test: `tests/test_pet_sprite_rendering.py`

**Interfaces:**
- Consumes: 完整宠物管理功能。
- Produces: 可重复的验证证据。

- [ ] **Step 1: Run focused tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_pet_package_management.py tests\test_pet_sprite_rendering.py -v`

Expected: PASS。

- [ ] **Step 2: Run full regression suite**

Run: `.venv\Scripts\python.exe -m pytest tests -v`

Expected: PASS；若既有环境型测试需要外部服务，单独记录，不掩盖失败。

- [ ] **Step 3: Verify installer in an isolated Codex home**

在临时目录设置 `CODEX_HOME`，调用后端安装函数安装 `kitagawa-marin`，确认列表含该宠物和可读精灵图，再调用删除函数确认目录消失；测试结束删除临时目录。

- [ ] **Step 4: Document usage**

在 `README.md` 说明输入 `kitagawa-marin` 或完整 `npx codex-pet-installer add kitagawa-marin`，以及界面删除会移除 `%USERPROFILE%\.codex\pets\<id>`。

> 当前工作区不是 Git 仓库，因此本计划不包含不可执行的逐任务 Git 提交步骤。
