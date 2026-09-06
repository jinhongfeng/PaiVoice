# PaiVoice · Jester 魔改版（电话项目）

> 给 AI 伴侣（住在网关里的人格）打电话的完整链路：浏览器 PWA 拨号 → 本服务当耳朵和嘴 → **网关语音快车道**里的大脑接电话。
>
> **上游血统**：基于 [tianyupaipai-cmd/pai-voice](https://github.com/tianyupaipai-cmd/pai-voice)（AGPL-3.0，本仓库继承同协议）。感谢原作者的通话底座——VAD 参数和温和打断是实战调过的真金。
>
> **本机全离线搭建**（Ollama + 本地 ASR/TTS，无需云端 Key）：见 [docs/本地搭建.md](docs/本地搭建.md)。

---

## 1、架构

```
她（手机浏览器 / PWA 拨号页 packages/web-client/index.html）
   │ getUserMedia 麦克风 + WebSocket（PCM16 16k）
   ▼
本服务（packages/realtime-core/server.py）
   │ 耳朵 ASR：SiliconFlow SenseVoiceSmall（中文主赛道）
   │ 大脑：POST 网关语音快车道（见下），消费 OpenAI SSE 聚合
   │ 嘴 TTS：ElevenLabs（主赛道）｜MiniMax（桩位 M1 后接）
   ▼
网关语音快车道（独立仓库 tiantian-wg / voice_lane.py，不在本仓库）
   │ 人格上下文注入（lean）+ 通话缓存 + 意图分流 + OB 记忆检索（首句一次）
   ▼
渠道（LLM）→ 回复原路返回 → TTS → 声音 + 双语字幕
```

**大脑和历史的单一事实源在网关侧**：本服务每轮只发 `{call_session_id, transcript}`，
不保存对话（仅挂断归档时暂存全文）。前端换、耳嘴换，网关不动——这是整个系统的设计基石。

## 2、相对上游的魔改清单（维护必读）

| 文件 | 改动 |
|---|---|
| `packages/realtime-core/server.py` | **重写**：ASR 加 siliconflow；Adapter 加 **gateway 模式**（OpenAI+SSE，UA=`pai-voice/0.1` 供网关分流）；新增**清洗层 `split_for_tts()`**；新增**挂断归档** `archive_call()`；移除无用 numpy；新增**人设定制**（称呼/聊天语气/聊天风格 → system 指令，`config/persona.json` 持久化）；**记忆改 SQLite 五轨 + 自动进化**（配合 `memory_store.py` / `memory_evolve.py`，去掉 MySQL 依赖） |
| `packages/realtime-core/memory_store.py` | **新增**：五轨记忆 SQLite 存储层（`turns`/`today_log`/`project_memory`/`profile`/`longterm`/`meta`，标准库零依赖） |
| `packages/realtime-core/memory_evolve.py` | **新增**：自动进化引擎——后台空闲时用大脑模型把新对话蒸馏进用户档案/长期记忆/今日日志/当前分支项目记忆，`persona_suggestion` 供人设面板参考 |
| `packages/web-client/index.html` | **新增**：通话页（拨号/状态灯/金色圆盘音量/**双语字幕分轨渲染**/延迟显示/静音挂断/**人设面板**） |
| `Dockerfile` | **新增**：Zeabur 部署用 |
| `.env.example` | **重写**：全量 env 清单 |
| `packages/realtime-core/requirements.txt` | 移除 numpy（全包零引用）；**移除 pymysql**（记忆改 SQLite 标准库） |
| `tests/tts_cleanse_smoke.py` | **新增**：清洗层冒烟测试 |
| `tests/test_five_track_memory.py` + `tests/smoke_memory.py` | **新增**：五轨记忆单测 + 端到端冒烟（无 MySQL） |

`packages/web-client/voice-call.js` **保持上游原样**（VAD/温和打断/播放队列/延迟秒表是实战参数，别动）。

## 3、清洗层：语气中间协议

措辞（存在网关侧）里**只使用中间协议**：`[laughs]` `[sighs]` `[whispers]` / `(pause)` `(laughs)` `(sighs)`。
本服务的 `split_for_tts()` 在合成前做两件事：

1. **切分**：引号内 = 朗读段；引号外 = 字幕段（剥半角协议标记，**全角中文括号保留**——那是中文表达不是协议）。
2. **转译**：按 `_TTS_DIALECT` 映射表把中间协议转成当前厂商方言（ElevenLabs 直通；MiniMax `(pause)`→`<#0.6#>`）。

**换 TTS 厂商的步骤**：`_TTS_DIALECT` 加该家映射 → `synthesize()` 加该家分支 → 跑 `tests/tts_cleanse_smoke.py`。措辞零改动。
**顺序铁律**：markdown 清理必须在方言转换**之前**（否则会吃掉 MiniMax 的 `<#0.6#>` 原生标记——冒烟实测抓过）。

## 3.1、人设定制（称呼 / 聊天语气 / 聊天风格）

通话页右上角「人设」面板可热改，存 `config/persona.json`，重启保留：

| 字段 | 作用 |
|---|---|
| 怎么称呼你（`pet_name`） | 他的**每一轮回复都必须**用这个昵称叫你（融进句子，不许生硬） |
| 他叫什么（`his_name`） | 他自称 / 你喊他的名字 |
| 聊天语气（`tone`） | 预设：默认 / 温柔体贴 / 黏人撒娇 / 活泼元气 / 平静沉稳 / 幽默轻松（严肃话题自动转认真）/ 高冷傲娇 / 自定义 |
| 聊天风格（`style`） | 预设：默认（闲聊插科打诨 + 正经探讨先亮观点给理由）/ 情侣煲粥 / 斗嘴打闹 / 嘘寒问暖 / 小剧场 / 惜字如金 / 自定义 |

实现：`_persona_directive()` 把上述字段拼成一条 system 指令，排在用户 SYSTEM_PROMPT 之前注入每轮请求
（预设与自定义文本可叠加；全空则不注入，行为与原来完全一致）。链路测试：`python tests/test_persona.py`。

## 3.2、Codex 桌宠管理

右上角「宠物」面板直接管理当前用户的 `%USERPROFILE%\.codex\pets`。安装框既可填写宠物 id `kitagawa-marin`，也可粘贴完整命令 `npx codex-pet-installer add kitagawa-marin`；服务端会使用固定参数调用官方安装器，不会执行输入中的任意命令。已安装宠物旁的「删除」按钮会移除对应的 `%USERPROFILE%\.codex\pets\<id>` 目录；删除当前宠物后页面自动切回内置猫猫。

## 3.3、桌面端（Electron）打包与隐私

> 本仓库可打包成 Windows 一键安装的桌面应用（Electron 壳 + Python 后端 exe），
> **开箱即全本地离线**：安装包不含任何密钥，无遥测，所有云端出口默认关闭。

### 打包流程

```bash
# 前置：Node.js（≥18）、Python 虚拟环境已就绪（.venv，依赖见 `packages/realtime-core/requirements.txt`）
npm install                # 拉 electron + electron-builder（仅在仓库根 package.json）
npm run electron:pack      # = PyInstaller 打 server.exe/local_voice.exe → electron-builder 出 NSIS 安装包
                           # 产物在 release/ 目录
```

内部结构（已随仓库提交，可直接复用）：

| 文件 | 作用 |
|---|---|
| `electron/main.js` | 主进程：拉起后端两个 exe（spawn 子进程）、加载拨号页、退出时回收子进程 |
| `electron-builder.yml` | 打包配置（仓库根；`files` 显式排除 `.env`；Windows NSIS 目标） |
| `electron/preload.js` | 预加载占位（`contextIsolation` 开启，页面拿不到 Node 句柄） |
| `electron/PRIVACY.md` | 面向用户的隐私声明（进安装包） |
| `scripts/build_backend.ps1` | PyInstaller 打 `server.exe` + `local_voice.exe`（onefile、无控制台、不含 `.env`） |

### 隐私加固（Electron 主进程默认注入，`electron/main.js`）

| 项 | 加固值 | 效果 |
|---|---|---|
| `PAIVOICE_DATA_DIR` | `app.getPath('userData')`（如 `%APPDATA%\PaiVoice`） | 记忆库/persona/api_profiles 全部进应用数据目录，与程序分离；卸载不清，彻底删除 = 删该目录 |
| `PAIVOICE_HOST` | `127.0.0.1` | 服务只绑本机回环，局域网其他设备无法直连 |
| `PAIVOICE_ASR/TTS_PROVIDER` | `local` | 默认语音全走本机边车，云端 ASR/TTS 不启用 |
| `PAIVOICE_SB_URL/SB_KEY/ARCHIVE_URL` | 空 | 不写 Supabase、不挂断归档，直到你在设置面板主动填写 |
| `.env` | 打包排除 | 安装包里不存在你的密钥/网关地址/人设 |

> 代码级网络出口清单（16 条，每条带源码行号）见 `electron/PRIVACY.md`——
> 结论：默认链路 `浏览器 → server.exe(:8780) → local_voice.exe(:8792) / Ollama(:11434)` 全在本机。

### 首次启动

安装后第一次打开：设置面板里填你的网关（大脑）地址与模型（或保持默认空，用本地 Ollama）。
云端 Key 只存 `%APPDATA%\PaiVoice\`，不回写 `.env`、不落入安装目录。

## 4、部署（Zeabur）

1. 新建 Zeabur 项目 → 部署本仓库（自动识别 Dockerfile）
2. 环境变量（真 Key 只放这里，绝不入库）：

| 变量 | 说明 |
|---|---|
| `PAIVOICE_TOKEN` | 浏览器拨号令牌（自定随机串，通话页里填同一个） |
| `PAIVOICE_ASR_PROVIDER` | `siliconflow` |
| `PAIVOICE_ASR_API_KEY` | 硅基流动 key |
| `PAIVOICE_SILICONFLOW_ASR_MODEL` | `FunAudioLLM/SenseVoiceSmall` |
| `PAIVOICE_TTS_PROVIDER` | `elevenlabs` |
| `PAIVOICE_TTS_API_KEY` | ElevenLabs key |
| `PAIVOICE_ELEVEN_VOICE_ID` | 音色 ID |
| `PAIVOICE_GATEWAY_URL` | `https://<网关域名>/v1/chat/completions` |
| `PAIVOICE_GATEWAY_TOKEN` | 网关 `API_SECRET` |
| `PAIVOICE_ARCHIVE_URL` | `https://<网关域名>/v1/voice/archive` |

3. 网关侧（另一仓库）需同步：`VOICE_LANE_ENABLED=1` + `VOICE_CALL_MODE_PROMPT`（电话模式措辞终版）
4. 浏览器打开 `index.html`（静态托管或本地），填 `wss://<本服务域名>/voice/ws` + TOKEN → 接通

## 5、网关侧约定（联调契约）

- 分流：本服务所有请求 UA 带 `pai-voice`，body 带 `call_session_id`——网关据此进快车道
- 首句全量：通话第一句时网关做全量上下文准备（含记忆检索），之后复用缓存；`[接通了]` 是接通标记（她刚接起，给第一声）
- 归档：挂断时本服务 POST `transcript`（全文）到网关 `/v1/voice/archive` → 存 `voice_calls` 表（`pending_summary=true`），摘要由 K 自己写（C3 拍板）

## 6、项目结构

```
语音聊天/
├── start.bat                  # Windows 双击启动入口
├── package.json               # Electron 打包脚本（electron:dev / electron:pack / electron:dir）
├── electron/                  # Electron 壳（main.js / preload.js / PRIVACY.md）
├── electron-builder.yml       # 桌面打包配置（仓库根，Windows NSIS）
├── packages/
│   ├── realtime-core/         # 通话核心（server.py + 清洗层 cleanse.py）
│   ├── local-voice/           # 本地语音边车 local_voice.py（ASR+TTS，:8792）
│   ├── web-client/            # 拨号页（index.html + voice-call.js）
│   └── adapters/              # 外部适配器（tmux）
├── scripts/                   # 启停脚本（run.ps1 / stop.ps1）+ 后端打包脚本（build_backend.ps1）
├── tools/                     # 维护工具（模型下载 / 试听样本生成 / 音色画像分析）
├── tests/                     # 测试与诊断脚本（冒烟 / 全链路 / mock API）
├── config/                    # 运行时配置（api_profiles.json 设置面板档案）
├── docs/                      # 文档（本地搭建指南 / 上游适配说明）
├── models/                    # 本地语音模型（sherpa-onnx，下载产物）
├── wheels/                    # 离线 wheel 缓存
├── logs/                      # 运行与诊断日志
└── 试听/                       # TTS 音色试听样本（wav + 音色画像 CSV）
```


## 7、许可

AGPL-3.0（继承上游）。自部署自用。
