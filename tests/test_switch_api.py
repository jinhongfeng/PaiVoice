#!/usr/bin/env python3
"""端到端验证：设置面板把 LLM API 切到 mock 端点后，通话大脑真的改走 mock。
流程：config_set 切 gateway_url -> 发一轮 text -> 确认 reply 来自 mock -> 还原。"""
import asyncio
import json
import sys

import websockets

WS_URL = "ws://localhost:8780/voice/ws"
TOKEN = "pv-5f2c9e8a1b3d4c6e7f8a9b0c"
MOCK_URL = "http://127.0.0.1:8799/v1/chat/completions"


async def recv_until(ws, pred, timeout=15):
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if pred(m):
            return m


async def main():
    async with websockets.connect(WS_URL, max_size=None) as ws:
        await ws.send(json.dumps({"type": "start", "token": TOKEN}))
        await recv_until(ws, lambda m: m.get("type") == "state")

        # 记录原配置
        await ws.send(json.dumps({"type": "config_get"}))
        orig = await recv_until(ws, lambda m: m.get("type") == "config")

        # 1) 把 API 切到 mock 端点 + 假模型
        await ws.send(json.dumps({
            "type": "config_set",
            "gateway_url": MOCK_URL,
            "gateway_token": "sk-mock-key-123",
            "llm_model": "mock-model-v9",
            "system_prompt": "mock 测试人设",
        }))
        snap = await recv_until(ws, lambda m: m.get("type") in ("config", "config_error"))
        if snap["type"] == "config_error":
            print("切换失败:", snap); sys.exit(1)
        print("已切换 -> url:", snap["gateway_url"], "| model:", snap["llm_model"],
              "| key:", snap["gateway_token"])

        # 2) 发一轮打字对话，等回复
        await ws.send(json.dumps({"type": "text", "text": "测试：现在大脑应该走 mock 接口"}))
        got = None
        for _ in range(20):
            m = json.loads(await asyncio.wait_for(ws.recv(), 30))
            if m.get("type") == "reply_text":
                got = m["text"]
                break
            if m.get("type") == "error":
                print("轮次错误:", m); sys.exit(1)
        if got is None:
            print("没等到回复"); sys.exit(1)
        print("回复内容:", got)
        assert "测试API" in got, "回复不是来自 mock！"
        print(">>> 确认：切换后大脑真的走了 mock 接口 ✅")

        # 3) 还原原配置
        await ws.send(json.dumps({
            "type": "config_set",
            "gateway_url": orig["gateway_url"],
            "gateway_token": orig["gateway_token"],
            "llm_model": orig["llm_model"],
            "system_prompt": orig["system_prompt"],
        }))
        snap2 = await recv_until(ws, lambda m: m.get("type") in ("config", "config_error"))
        print("已还原 -> url:", snap2["gateway_url"], "| model:", snap2["llm_model"])
        print("SWITCH-API TEST PASS")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
