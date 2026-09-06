# PaiVoice 桌面端 · 网络出口清单（代码级核实）

> 用途：回答「这个应用会连哪些地方」。以下每条都对应源码位置，便于随时复核。
> 结论先行：**无遥测/分析/埋点**；开箱默认全本地离线；云端出口全部由你在设置面板填了才启用。

## 后端（packages/realtime-core/server.py）

| # | 目的地（由 .env/设置面板决定） | 触发时机 | 发送内容 | 代码位置 |
|---|---|---|---|---|
| 1 | `PAIVOICE_GATEWAY_URL`（默认空） | 每轮大脑请求 | 转录 + 记忆回放 + 人设指令 + system prompt | `_call_gateway` server.py:551 |
| 2 | `PAIVOICE_ADAPTER_URL`（默认空，旧路） | GATEWAY 未配置时 | 转录全文 | server.py:629 |
| 3 | `LOCAL_ASR_URL`（默认 127.0.0.1:8792） | 每轮语音转写 | 你的 PCM 音频 | server.py:328 |
| 4 | `LOCAL_TTS_URL`（默认 127.0.0.1:8792） | 每轮语音合成 | 回复文本 | server.py:673 |
| 5 | SiliconFlow ASR（ASR_PROVIDER=siliconflow） | 每轮语音转写 | 你的 PCM 音频 | server.py:319 |
| 6 | Groq ASR（ASR_PROVIDER=groq） | 每轮语音转写 | 你的 PCM 音频 | server.py:322 |
| 7 | ElevenLabs TTS（TTS_PROVIDER=elevenlabs） | 每轮语音合成 | 回复文本 | server.py:658 |
| 8 | Ollama 模型枚举（GATEWAY_URL=localhost:11434） | 设置面板打开 | 无敏感数据 | server.py:337 |
| 9 | `PAIVOICE_ARCHIVE_URL`（默认空） | 挂断时 | 通话全文 | server.py:691 |
| 10 | `PAIVOICE_SB_URL`（默认空） | 每轮落盘 | 通话原文（≤4000 字符） | server.py:722 |
| 11 | `PAIVOICE_SB_URL`（默认空） | 每轮 | 延迟指标 | server.py:809 |

## 后端（packages/realtime-core/memory_evolve.py）

| # | 目的地 | 触发时机 | 发送内容 | 代码位置 |
|---|---|---|---|---|
| 12 | `PAIVOICE_GATEWAY_URL`（默认空） | 后台记忆蒸馏 | 最近对话（≤200 条） | memory_evolve.py:139 |

## 前端（packages/web-client/）

| # | 目的地 | 触发时机 | 发送内容 | 代码位置 |
|---|---|---|---|---|
| 13 | 本服务 `ws://127.0.0.1:8780/voice/ws` | 通话中 | 音频 + 文本 + 控制消息 | index.html:881 |
| 14 | 本服务 `http://127.0.0.1:8780/v1/config` | 设置面板 | 无敏感数据（响应含 gateway_token，仅本机回环） | index.html:2008 |
| 15 | Ollama `http://localhost:11434` | 设置面板模型枚举 | 无敏感数据 | index.html:745 |
| 16 | 静态文档链接 codex-pet.org（仅链接，不自动访问） | 用户点击 | 无 | index.html:652 |

> 注：14 的响应会包含 `gateway_token`，但它走的是本机回环（`127.0.0.1`），
> 不会经过任何外部网络；Electron 版默认 `PAIVOICE_HOST=127.0.0.1` 时该端点同样只在本机可达。

## 结论

- 全部 16 条中，只有 **1、5、6、7、9、10、11、12** 是"云端出口"，且**默认全部为空/关闭**（Electron 主进程还把 ASR/TTS 强制为 `local`）。
- 默认情况下数据流完全在本机：`浏览器 → server.py(:8780) → local_voice.py(:8792) / Ollama(:11434) → 浏览器`。
- 你说话的音频只发给本机边车（#3/#4）或你显式配置的云端 ASR（#5/#6）。
- 安装包不含 `.env`，所有云端配置由你首次启动后在设置面板填写（存 `%APPDATA%\PaiVoice`）。
