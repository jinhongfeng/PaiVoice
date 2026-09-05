"""临时验证：1) 讲笑话要真讲完 2) 挂断重拨后跨通话记得刚才的内容。用法同 smoke_ws.py。"""
import asyncio, json, os, sys, urllib.parse
from websockets.asyncio.client import connect

URL = "ws://localhost:8780/voice/ws"


def load_token() -> str:
    envf = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, ".env")
    for line in open(envf, encoding="utf-8"):
        line = line.strip()
        if line.startswith("PAIVOICE_TOKEN="):
            return line.split("=", 1)[1].strip().split("#")[0].strip()
    return ""


async def one_call(ws_url: str, text: str) -> str:
    """拨一次号发一句话，聚合本轮字幕文本返回。"""
    async with connect(ws_url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "start", "token": TOKEN}))
        end = asyncio.get_event_loop().time() + 120
        # 等接通（state listening）再开口
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if msg.get("type") == "state" and msg.get("mode") == "listening":
                break
        await ws.send(json.dumps({"type": "text", "text": text}))
        parts: list[str] = []
        while True:
            remain = end - asyncio.get_event_loop().time()
            if remain <= 0:
                raise TimeoutError("generation_end 超时")
            msg = json.loads(await asyncio.wait_for(ws.recv(), remain))
            t = msg.get("type")
            if t == "reply_text":
                parts.append(msg.get("text", ""))
            elif t == "generation_end":
                break
            elif t == "error":
                raise RuntimeError(msg.get("error"))
        await ws.send(json.dumps({"type": "hangup"}))
        await asyncio.sleep(0.3)
        return "".join(parts)


async def main() -> int:
    print("[1] 第一次拨号：讲个笑话")
    r1 = await one_call(URL, "讲个笑话")
    print("    他答：", r1)
    ok1 = len(r1) > 30  # 一句话敷衍的回复通常很短；真讲了笑话必然是一大段
    print("    判定：", "PASS（内容讲完整了）" if ok1 else "FAIL（疑似只预告没讲）")

    print("[2] 挂断，重新拨号（新 call_session），问还记得吗")
    r2 = await one_call(URL, "我刚才让你做什么了？你还记得吗")
    print("    他答：", r2)
    ok2 = ("笑话" in r2)
    print("    判定：", "PASS（跨通话记得）" if ok2 else "FAIL（没记得）")

    print("RESULT:", "PASS" if (ok1 and ok2) else "FAIL")
    return 0 if (ok1 and ok2) else 1


TOKEN = load_token()
sys.exit(asyncio.run(main()))
