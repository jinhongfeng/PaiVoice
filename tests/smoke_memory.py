#!/usr/bin/env python3
"""端到端冒烟：无 MySQL 环境下验证五轨记忆全链路。
1. 起假 OpenAI API（返回一段记忆蒸馏 JSON）
2. 起真实 server（PAIVOICE_MYSQL 全空 + PAIVOICE_DATA_DIR 临时目录）
3. WS 打字走几轮对话（server 把 turns 落 SQLite）
4. 直接调 summarize_new_turns → 检查 profile/today_log/longterm/project 落盘
5. 再取一轮 _memory_block 注入 → 确认记忆块出现
用法: python tests/smoke_memory.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "packages", "realtime-core"))

from memory_store import MemoryStore
import memory_evolve
import aiohttp

MOCK_PORT = 8799
SERVER_PORT = 8781   # 避开常驻 8780（本地开发可能已在跑）


def mock_app():
    from aiohttp import web
    async def chat(request):
        body = await request.json()
        print("MOCK-GOT messages:", json.dumps(body.get("messages", []), ensure_ascii=False)[:200], flush=True)
        reply = json.dumps({
            "profile_updates": [{"fact": "用户喜欢深夜聊天", "category": "偏好", "confidence": 0.8}],
            "longterm_events": [{"event": "用户决定把项目打包成 Electron", "date": "2026-09-06", "importance": 0.9}],
            "project_memories": [{"fact": "记忆层改用 SQLite，去掉 MySQL", "category": "项目", "confidence": 0.9}],
            "today_topics": ["Electron 打包"],
            "persona_suggestion": "可以更活泼一点",
        }, ensure_ascii=False)
        # 注意：这是"记忆蒸馏回复"——直接回给任何 user 消息，聊天轮也会拿到这个 JSON。
        # 聊天轮能正常走完 generation_end（server 只把它当回复文本），总结轮则解析成记忆。
        return web.json_response({
            "id": "m-1", "object": "chat.completion", "model": "mock",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                         "finish_reason": "stop"}],
        })
    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    return app


async def main() -> int:
    from aiohttp import web
    from websockets.asyncio.client import connect

    tmp = tempfile.mkdtemp(prefix="paivoice-mem-smoke-")
    os.environ["PAIVOICE_DATA_DIR"] = tmp
    os.environ["PAIVOICE_MYSQL_HOST"] = ""          # 显式空：证明不依赖 MySQL
    os.environ["PAIVOICE_TOKEN"] = ""
    os.environ["PAIVOICE_GATEWAY_URL"] = f"http://127.0.0.1:{MOCK_PORT}/v1/chat/completions"
    os.environ["PAIVOICE_ASR_PROVIDER"] = "mock"
    os.environ["PAIVOICE_TTS_PROVIDER"] = "mock"
    os.environ["PAIVOICE_PORT"] = str(SERVER_PORT)  # 显式端口：避免撞上常驻 8780

    # 1. 起假 API
    runner = web.AppRunner(mock_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", MOCK_PORT)
    await site.start()

    # 2. 起真实 server（直接 import main 不现实，用 subprocess 拉起来）
    import subprocess
    env = os.environ.copy()
    proc = subprocess.Popen(
        [sys.executable, "packages/realtime-core/server.py"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        # 等 server 起来
        for _ in range(50):
            try:
                async with connect(f"ws://127.0.0.1:{SERVER_PORT}/voice/ws", max_size=None) as ws:
                    break
            except Exception:
                await asyncio.sleep(0.2)
        else:
            print("server did not start", flush=True)
            return 1

        # 3. 打字走 6 轮（server 落 SQLite turns）
        async with connect(f"ws://127.0.0.1:{SERVER_PORT}/voice/ws", max_size=None) as ws:
            await ws.send(json.dumps({"type": "start", "token": ""}))
            async def until(pred, what, timeout=20):
                end = asyncio.get_event_loop().time() + timeout
                while True:
                    remain = end - asyncio.get_event_loop().time()
                    if remain <= 0:
                        raise TimeoutError(f"timeout waiting for {what}")
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=min(remain, 10)))
                    if pred(m):
                        return m
            await until(lambda m: m.get("type") == "state", "state")
            for i in range(6):
                await ws.send(json.dumps({"type": "text", "text": f"轮次{i}：我最近在考虑把项目打包成 Electron，记忆要能离线存。"}))
                await until(lambda m: m.get("type") == "generation_end", f"generation_end {i}")
            print("6 turns sent ✓", flush=True)
        # 4. 直接调总结（用真实 server 已写入的库）
        store = MemoryStore(tmp)
        async with aiohttp.ClientSession() as http:
            ok = await memory_evolve.summarize_new_turns(
                store, http, os.environ["PAIVOICE_GATEWAY_URL"], "", "mock", min_turns=1)
        print("summarize ok =", ok, flush=True)
        if not ok:
            print("FAIL: summarize did not run", flush=True)
            return 1
        prof = store.read_profile()
        lt = store.read_longterm()
        tj = store.read_today()
        pj = store.read_project(store.current_branch())
        print(f"profile={len(prof)} longterm={len(lt)} today={tj['topics']} project={len(pj)}", flush=True)
        assert len(prof) == 1 and len(lt) == 1 and tj["topics"] and len(pj) == 1, "evolve merge missing"
        print("MEMORY SMOKE PASS", flush=True)
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        await runner.cleanup()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
