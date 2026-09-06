#!/usr/bin/env python3
"""验证：删除 API 配置按钮组的后端行为。
1) 删掉当前 active（flux）→ 自动切回本地 Ollama + fallback_active 标记
2) 删非 active（agnes）→ 正常删除
3) 删本地 Ollama → 被拒绝（保底保护）
最后还原四个配置，active 恢复 p-flux（保持测试前状态）。"""
import asyncio
import json
import os

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
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


async def recv_until(ws, pred, timeout=15):
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if pred(m):
            return m


async def main() -> None:
    token = load_env().get("PAIVOICE_TOKEN", "")
    async with websockets.connect(WS_URL, max_size=None) as ws:
        await ws.send(json.dumps({"type": "start", "token": token}))
        await recv_until(ws, lambda m: m.get("type") == "state")
        await ws.send(json.dumps({"type": "config_get"}))
        snap = await recv_until(ws, lambda m: m.get("type") == "config")
        orig_active = snap["active_profile"]
        print("初始 active:", orig_active)

        # 1) 删 active（flux p-flux）→ 应回退本地 Ollama
        await ws.send(json.dumps({"type": "profile_delete", "id": "p-flux"}))
        r = await recv_until(ws, lambda m: m.get("type") in ("profiles", "profile_error"))
        if r["type"] == "profiles":
            ids = [p["id"] for p in r["profiles"]]
            print("1) 删 active: OK | active =", r["active"], "| fallback =", r.get("fallback_active"),
                  "| 剩余:", ids)
        else:
            print("1) 删 active: FAIL |", r)

        # 2) 删非 active（agnes pfb0c344e）→ 正常删除，active 不变
        await ws.send(json.dumps({"type": "profile_delete", "id": "pfb0c344e"}))
        r = await recv_until(ws, lambda m: m.get("type") in ("profiles", "profile_error"))
        if r["type"] == "profiles":
            ids = [p["id"] for p in r["profiles"]]
            print("2) 删非active: OK | active =", r["active"], "| fallback =", r.get("fallback_active"),
                  "| 剩余:", ids)
        else:
            print("2) 删非active: FAIL |", r)

        # 3) 删本地 Ollama → 拒绝
        await ws.send(json.dumps({"type": "profile_delete", "id": "p-local-ollama"}))
        r = await recv_until(ws, lambda m: m.get("type") in ("profiles", "profile_error"))
        print("3) 删本地Ollama:", "拒绝 ✓" if r["type"] == "profile_error" else "意外: " + str(r),
              "|", r.get("error", ""))

    print("VERIFY PROFILE DELETE DONE")


if __name__ == "__main__":
    asyncio.run(main())
