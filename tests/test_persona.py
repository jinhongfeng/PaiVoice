#!/usr/bin/env python3
"""验证人设面板链路：config_get 拿 persona → config_set 改称呼/语气/风格 → get 确认 → 指令内容核对 → 还原。
不需要真实通话，走 WS 文本通道（服务端须已启动 :8780，TOKEN 取 .env）。"""
import asyncio
import json
import os
import sys

import websockets

WS_URL = "ws://localhost:8780/voice/ws"


def _token():
    env = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, ".env")
    try:
        for line in open(env, encoding="utf-8"):
            line = line.strip()
            if line.startswith("PAIVOICE_TOKEN="):
                return line.split("=", 1)[1].split("#")[0].strip()
    except Exception:
        pass
    return ""


async def recv_until(ws, types, timeout=15):
    """收包直到命中目标类型。config_set 无论成败都会补发一条 config 快照，
    error 分支后要把尾随的 config 也收掉，否则它会被下一步误当回执。"""
    got_error = None
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        t = m.get("type")
        if t == "config_error":
            got_error = m
            continue
        if t in types:
            return m


async def main():
    async with websockets.connect(WS_URL, max_size=None) as ws:
        await ws.send(json.dumps({"type": "start", "token": _token()}))
        await recv_until(ws, {"state"})

        # 1) config_get：快照应带 persona
        await ws.send(json.dumps({"type": "config_get"}))
        snap = await recv_until(ws, {"config"})
        p0full = snap.get("persona") or {}
        assert "pet_name" in p0full and "tone" in p0full and "style" in p0full and "tone_free" in p0full and "style_free" in p0full, p0full
        p0 = {k: p0full[k] for k in ("pet_name", "his_name", "tone", "style", "tone_free", "style_free")}
        print(f"config_get persona -> {p0}")

        # 2) config_set：改称呼 + 语气/风格选自定义并填自由文本（自由文本只在 custom 下生效）
        await ws.send(json.dumps({"type": "config_set",
                                  "persona": {"pet_name": "测试昵称XYZ", "tone": "custom",
                                              "style": "custom", "tone_free": "说话像小猫",
                                              "style_free": "爱用叠词"}}))
        m = await recv_until(ws, {"config", "config_error"})
        if m["type"] == "config_error":
            print("config_set FAILED:", m)
            sys.exit(1)
        p1 = m["persona"]
        if not (p1["pet_name"] == "测试昵称XYZ" and p1["tone"] == "custom"
                and p1["style"] == "custom" and p1["tone_free"] == "说话像小猫"
                and p1["style_free"] == "爱用叠词"):
            print("config_set mismatch:", {k: p1[k] for k in ("pet_name", "tone", "style", "tone_free", "style_free")})
            sys.exit(1)
        print("config_set persona ->", {k: p1[k] for k in ("pet_name", "tone", "style", "tone_free", "style_free")})

        # 3) 指令内容核对：system 提示词应包含称呼与自由定制文本
        await ws.send(json.dumps({"type": "config_get"}))
        snap2 = await recv_until(ws, {"config"})
        sp = snap2.get("system_prompt") or ""
        for needle in ("测试昵称XYZ", "说话像小猫", "爱用叠词", "聊天语气", "聊天风格"):
            if needle not in sp:
                print(f"system prompt missing {needle!r}:\n{sp}")
                sys.exit(1)
        print("system prompt contains persona directives ✓")

        # 4) 非法语气值应被拒
        await ws.send(json.dumps({"type": "config_set",
                                  "persona": {"tone": "hacker", "style": "nope"}}))
        m = await recv_until(ws, {"config", "config_error"})
        rejected = m["type"] == "config_error"
        kept = m["type"] == "config" and m["persona"]["tone"] == "custom"
        if not (rejected or kept):
            print("invalid tone was silently accepted:", m.get("persona"))
            sys.exit(1)
        print("invalid tone/style rejected ✓")

        # 5) 还原（p0 已是纯 6 字段）
        await ws.send(json.dumps({"type": "config_set", "persona": p0}))
        m = await recv_until(ws, {"config", "config_error"})
        got = {k: m["persona"][k] for k in p0} if m["type"] == "config" else {}
        if m["type"] == "config_error" or got != p0:
            print("restore FAILED:", m if m["type"] == "config_error" else got)
            sys.exit(1)
        print("restore ->", got)
        print("PERSONA API PASS")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
