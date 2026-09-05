#!/usr/bin/env python3
"""验证设置面板链路：config_get 拿快照 → config_set 换模型/音色/提示词 → 再 get 确认。
不需要真实通话，走 WS 文本通道。"""
import asyncio
import json
import sys

import websockets

WS_URL = "ws://localhost:8780/voice/ws"
TOKEN = "pv-5f2c9e8a1b3d4c6e7f8a9b0c"


async def main():
    async with websockets.connect(WS_URL, max_size=None) as ws:
        # 鉴权 start
        await ws.send(json.dumps({"type": "start", "token": TOKEN}))
        # 等待 state
        while True:
            m = json.loads(await ws.recv())
            if m.get("type") == "state":
                break

        # 1) config_get
        await ws.send(json.dumps({"type": "config_get"}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if m.get("type") == "config":
                snap = m
                break
        print("config_get ->")
        print("  llm_model:", snap["llm_model"])
        print("  llm_models:", snap["llm_models"])
        print("  voice:", snap["voice"], "| voices:", snap["voices"])
        print("  asr:", snap["asr"], "| tts:", snap["tts"], "| gateway:", snap["gateway_url"])

        # 2) config_set：换 API 接口（临时指向同机 Ollama 换个写法）+ Key + 提示词 + 音色
        new_prompt = "你是测试用的人设提示词。"
        await ws.send(json.dumps({
            "type": "config_set",
            "gateway_url": "http://127.0.0.1:11434/v1/chat/completions",
            "gateway_token": "test-key-123",
            "system_prompt": new_prompt,
            "voice": snap["voices"][0] if snap["voices"] else "",
        }))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if m.get("type") in ("config", "config_error"):
                break
        if m["type"] == "config_error":
            print("config_set FAILED:", m)
            sys.exit(1)
        print("config_set ->")
        print("  gateway_url:", m["gateway_url"], "| token:", m["gateway_token"])
        print("  llm_model:", m["llm_model"], "| voice:", m["voice"])
        print("  system_prompt:", m["system_prompt"][:30], "...")

        # 3) 换模型到 qwen3:8b（本地 Ollama 会校验存在）再切回
        await ws.send(json.dumps({"type": "config_set", "llm_model": "qwen3:8b"}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if m.get("type") in ("config", "config_error"):
                break
        print("switch model -> llm_model:", m["llm_model"])
        await ws.send(json.dumps({"type": "config_set", "llm_model": snap["llm_model"]}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if m.get("type") in ("config", "config_error"):
                break
        print("restore model -> llm_model:", m["llm_model"])

        # 4) 不存在的模型应被拒绝
        await ws.send(json.dumps({"type": "config_set", "llm_model": "no-such-model-xyz"}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if m.get("type") in ("config", "config_error"):
                break
        print("bad model rejected:", m.get("error") if m["type"] == "config_error" else "NOT rejected!")
        if m["type"] != "config_error":
            sys.exit(1)

        # 5) 还原 gateway_url/token/prompt
        await ws.send(json.dumps({
            "type": "config_set",
            "gateway_url": snap["gateway_url"],
            "gateway_token": snap["gateway_token"],
            "system_prompt": snap["system_prompt"],
        }))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if m.get("type") in ("config", "config_error"):
                break
        print("restore -> url:", m["gateway_url"], "| prompt:", m["system_prompt"][:20], "...")
        print("CONFIG API PASS")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
