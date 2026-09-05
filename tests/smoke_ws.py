"""端到端冒烟：模拟拨号页连接 PaiVoice，走一轮文字通话。
用法: .venv/Scripts/python smoke_ws.py [ws_url] [token]
默认: ws://localhost:8780/voice/ws  +  .env 里的 PAIVOICE_TOKEN
"""
import asyncio, json, os, sys, urllib.parse
from websockets.asyncio.client import connect


async def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:8780/voice/ws"
    token = sys.argv[2] if len(sys.argv) > 2 else ""
    # 读 .env 拿 token（如果没在命令行给）
    if not token:
        envf = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, ".env")
        if os.path.exists(envf):
            for line in open(envf, encoding="utf-8"):
                line = line.strip()
                if line.startswith("PAIVOICE_TOKEN="):
                    val = line.split("=", 1)[1].strip()
                    token = val.split("#")[0].strip()   # 剥离行内注释
    if token:
        sep = "&" if "?" in url else "?"
        url += sep + "token=" + urllib.parse.quote(token)

    async def until(ws, pred, what, timeout=15):
        end = asyncio.get_event_loop().time() + timeout
        while True:
            remain = end - asyncio.get_event_loop().time()
            if remain <= 0:
                raise TimeoutError(f"timeout waiting for {what}")
            msg = json.loads(await asyncio.wait_for(ws.recv(), remain))
            print("  <<", msg.get("type"), json.dumps(msg, ensure_ascii=False)[:140])
            if pred(msg):
                return msg

    async with connect(url, max_size=None) as ws:
        print("[1] 连接成功, 发送 start ...")
        await ws.send(json.dumps({"type": "start", "token": token}))
        await until(ws, lambda m: m.get("type") == "state", "state(listening)")
        print("[2] 拨号页就绪，发送一轮文字 ...")
        await ws.send(json.dumps({"type": "text", "text": "你好，这是冒烟测试"}))
        await until(ws, lambda m: m.get("type") == "generation_end", "generation_end")
        print("[3] 一轮通话闭环 OK")
        await ws.send(json.dumps({"type": "hangup"}))
        await asyncio.sleep(0.5)
        print("SMOKE PASS OK")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
