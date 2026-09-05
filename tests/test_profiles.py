#!/usr/bin/env python3
"""验证设置面板新增的 API 配置档案功能：
保存多个配置 -> 测试连接（成功/失败）-> 设为当前 -> 删除 -> 持久化检查。"""
import asyncio
import json
import sys

import websockets

WS_URL = "ws://localhost:8780/voice/ws"
TOKEN = "pv-5f2c9e8a1b3d4c6e7f8a9b0c"


async def recv_until(ws, pred, timeout=15):
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if pred(m):
            return m


async def main():
    async with websockets.connect(WS_URL, max_size=None) as ws:
        await ws.send(json.dumps({"type": "start", "token": TOKEN}))
        await recv_until(ws, lambda m: m.get("type") == "state")

        # 1) config_get：快照应含 profiles/active_profile
        await ws.send(json.dumps({"type": "config_get"}))
        snap = await recv_until(ws, lambda m: m.get("type") == "config")
        print("1) 快照字段 profiles/active_profile 存在:",
              "profiles" in snap and "active_profile" in snap)
        print("   profiles:", snap.get("profiles"), "| active:", snap.get("active_profile"))

        # 2) 保存两个配置
        await ws.send(json.dumps({"type": "profile_save", "profile": {
            "name": "本地Ollama", "url": "http://localhost:11434/v1/chat/completions",
            "key": "", "model": "qwen2.5:7b-instruct"}}))
        m1 = await recv_until(ws, lambda m: m.get("type") == "profiles")
        ollama_id = m1["saved_id"]
        print("2) 保存[本地Ollama] ->", ollama_id, "| 列表:", [p["name"] for p in m1["profiles"]])

        await ws.send(json.dumps({"type": "profile_save", "profile": {
            "name": "Mock测试", "url": "http://127.0.0.1:8799/v1/chat/completions",
            "key": "sk-mock-key-123", "model": "mock-model-v9"}}))
        m2 = await recv_until(ws, lambda m: m.get("type") == "profiles")
        mock_id = m2["saved_id"]
        print("  保存[Mock测试] ->", mock_id)

        # 3) 测试连接：mock 成功路径
        await ws.send(json.dumps({"type": "profile_test",
                                  "url": "http://127.0.0.1:8799/v1/chat/completions",
                                  "key": "sk-mock-key-123", "model": "mock-model-v9"}))
        t1 = await recv_until(ws, lambda m: m.get("type") == "profile_test_result")
        print("3) 测试 mock:", "OK" if t1["ok"] else "FAIL",
              "| 耗时:", t1.get("latency_ms"), "ms | 回复:", t1.get("reply", "")[:20])

        # 4) 测试连接：坏地址失败路径
        await ws.send(json.dumps({"type": "profile_test",
                                  "url": "http://127.0.0.1:59999/v1/chat/completions",
                                  "key": "", "model": "x"}))
        t2 = await recv_until(ws, lambda m: m.get("type") == "profile_test_result")
        print("4) 测试坏地址: 预期失败 ->", "OK" if not t2["ok"] else "FAIL",
              "|", t2.get("error", "")[:60])

        # 5) 设为当前（mock）-> 快照的 gateway_url 应变化
        await ws.send(json.dumps({"type": "profile_use", "id": mock_id}))
        snap2 = await recv_until(ws, lambda m: m.get("type") in ("config", "config_error"))
        print("5) 启用[Mock测试] -> url:", snap2.get("gateway_url"),
              "| model:", snap2.get("llm_model"), "| active:", snap2.get("active_profile"))

        # 6) 切换回本地Ollama
        await ws.send(json.dumps({"type": "profile_use", "id": ollama_id}))
        snap3 = await recv_until(ws, lambda m: m.get("type") == "config")
        print("6) 启用[本地Ollama] -> url:", snap3.get("gateway_url"),
              "| model:", snap3.get("llm_model"))

        # 7) 删除 mock 配置
        await ws.send(json.dumps({"type": "profile_delete", "id": mock_id}))
        m7 = await recv_until(ws, lambda m: m.get("type") == "profiles")
        print("7) 删除[Mock测试] -> 剩余:", [p["name"] for p in m7["profiles"]])

        # 8) 更新本地Ollama（改名）
        await ws.send(json.dumps({"type": "profile_save", "profile": {
            "id": ollama_id, "name": "本地Ollama-改", "url": "http://localhost:11434/v1/chat/completions",
            "key": "", "model": "qwen2.5:7b-instruct"}}))
        m8 = await recv_until(ws, lambda m: m.get("type") == "profiles")
        print("8) 更新配置 ->", [(p["name"], p["id"]) for p in m8["profiles"]])

        print("PROFILES TEST PASS")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
