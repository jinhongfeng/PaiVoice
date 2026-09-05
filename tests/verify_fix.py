#!/usr/bin/env python3
"""验证修复：重启后 agnes/flux 的「基础地址」profile_test 应通过（服务端自动补 /chat/completions）；
再切 agnes 发一轮真实文本轮，确认大脑真的走 agnes 并返回回复；最后还原原 active 配置。"""
import asyncio
import json
import os

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # 仓库根
WS_URL = "ws://localhost:8780/voice/ws"


def load_env() -> dict:
    env = {}
    with open(os.path.join(ROOT, ".env"), "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if "#" in v:
                v = v.split("#", 1)[0].strip()
            env[k.strip()] = v
    return env


def load_profiles() -> dict:
    with open(os.path.join(ROOT, "config", "api_profiles.json"), "r", encoding="utf-8") as f:
        return json.load(f)


async def recv_until(ws, pred, timeout=30):
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if pred(m):
            return m


async def main() -> None:
    env = load_env()
    prof = load_profiles()
    token = env.get("PAIVOICE_TOKEN", "")
    byid = {p["id"]: p for p in prof["profiles"]}
    agnes = byid["pfb0c344e"]
    flux = byid["p-flux"]
    # 故意用「基础地址」（缺 /chat/completions）来验证服务端归一化
    agnes_base = agnes["url"].rsplit("/chat/completions", 1)[0]
    flux_base = flux["url"].rsplit("/chat/completions", 1)[0]

    async with websockets.connect(WS_URL, max_size=None) as ws:
        await ws.send(json.dumps({"type": "start", "token": token}))
        await recv_until(ws, lambda m: m.get("type") == "state")
        await ws.send(json.dumps({"type": "config_get"}))
        snap = await recv_until(ws, lambda m: m.get("type") == "config")
        print("初始快照: url =", snap["gateway_url"], "| model =", snap["llm_model"],
              "| active =", snap["active_profile"])
        orig_active = snap["active_profile"]

        # 1) agnes 基础地址测试连接
        await ws.send(json.dumps({"type": "profile_test", "url": agnes_base,
                                  "key": agnes["key"], "model": agnes["model"]}))
        r = await recv_until(ws, lambda m: m.get("type") == "profile_test_result")
        print("1) agnes 基础地址测试:", "OK" if r.get("ok") else "FAIL",
              "| 回复:", r.get("reply", "")[:30], "|", r.get("latency_ms"), "ms")
        if not r.get("ok"):
            print("   错误:", r.get("error", "")[:120])

        # 2) flux 基础地址测试连接
        await ws.send(json.dumps({"type": "profile_test", "url": flux_base,
                                  "key": flux["key"], "model": flux["model"]}))
        r = await recv_until(ws, lambda m: m.get("type") == "profile_test_result")
        print("2) flux 基础地址测试:", "OK" if r.get("ok") else "FAIL",
              "| 回复:", r.get("reply", "")[:30], "|", r.get("latency_ms"), "ms")
        if not r.get("ok"):
            print("   错误:", r.get("error", "")[:120])

        # 3) 真实通话轮：切 agnes -> 发文本 -> 等回复
        await ws.send(json.dumps({"type": "profile_use", "id": agnes["id"]}))
        snap2 = await recv_until(ws, lambda m: m.get("type") in ("config", "config_error"))
        if snap2.get("type") == "config_error":
            print("3) 切换 agnes 失败:", snap2)
        else:
            print("3) 已切换 agnes -> url:", snap2["gateway_url"])
            await ws.send(json.dumps({"type": "text", "text": "测试：确认大脑走 agnes 接口，用一句话回复即可"}))
            got = None
            for _ in range(30):
                m = json.loads(await asyncio.wait_for(ws.recv(), 30))
                if m.get("type") == "reply_text":
                    got = m["text"]
                    break
                if m.get("type") == "error":
                    print("   轮次错误:", m)
                    break
            print("3) agnes 回复:", repr(got)[:80] if got else "(无回复)")

        # 4) 还原原 active
        await ws.send(json.dumps({"type": "profile_use", "id": orig_active}))
        await recv_until(ws, lambda m: m.get("type") == "config")
        print("4) 已还原 active ->", orig_active)

    print("VERIFY FIX DONE")


if __name__ == "__main__":
    asyncio.run(main())
