#!/usr/bin/env python3
"""验证模型面板「人设提示词」持久化：config_set system_prompt → 回显确认 → config/persona.json 落盘确认 → 还原。
不需要真实通话，走 WS 文本通道（服务端须已启动 :8780，TOKEN 取 .env）。"""
import asyncio
import json
import os
import sys

import websockets

WS_URL = "ws://localhost:8780/voice/ws"
PERSONA_FILE = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "config", "persona.json"))


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

        # 1) 记录当前 system_prompt（保存前先读档，结束时还原）
        await ws.send(json.dumps({"type": "config_get"}))
        snap0 = await recv_until(ws, {"config"})
        sp0 = snap0.get("system_prompt") or ""
        print(f"current system_prompt len={len(sp0)}")

        # 2) config_set 设置一段测试文案（含中文 + 换行，验证整段不截断）
        test_sp = "你是测试用的电话伙伴。\n第一条规则：只说中文。\n第二条规则：每轮回复都要提到「持久化测试」。"
        await ws.send(json.dumps({"type": "config_set", "system_prompt": test_sp}))
        m = await recv_until(ws, {"config", "config_error"})
        if m["type"] == "config_error":
            print("config_set FAILED:", m)
            sys.exit(1)
        if test_sp not in (m.get("system_prompt") or ""):
            print("snapshot does not echo saved system_prompt:\n" + (m.get("system_prompt") or ""))
            sys.exit(1)
        print("snapshot echoes saved system_prompt ✓")

        # 3) 直接读盘确认已落盘（整段保留，不被截断）
        try:
            with open(PERSONA_FILE, "r", encoding="utf-8") as f:
                on_disk = json.load(f)
        except Exception as e:
            print("cannot read", PERSONA_FILE, ":", e)
            sys.exit(1)
        if on_disk.get("system_prompt") != test_sp:
            print("persona.json system_prompt mismatch:\n" + repr(on_disk.get("system_prompt")))
            sys.exit(1)
        print("persona.json persisted system_prompt ✓")

        # 4) 还原原提示词（空串也要显式写回，避免污染用户环境）
        await ws.send(json.dumps({"type": "config_set", "system_prompt": sp0}))
        m = await recv_until(ws, {"config", "config_error"})
        if m["type"] == "config_error":
            print("restore FAILED:", m)
            sys.exit(1)
        print("restore ->", ("(empty)" if not (m.get("system_prompt") or "") else m.get("system_prompt")[:30]))
        print("PERSONA PERSIST API PASS")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
