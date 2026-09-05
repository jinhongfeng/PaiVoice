#!/usr/bin/env python3
"""假的 OpenAI 兼容 API（用于验证设置面板自由切换 API 是否真的生效）。
收到请求后打印 body 到 stdout，然后返回固定内容的 SSE 流。"""
import json
from aiohttp import web


async def chat(request: web.Request) -> web.Response:
    body = await request.json()
    # 打印收到的请求，证明 server 确实请求了这个地址
    print("MOCK-GOT model:", body.get("model"), flush=True)
    print("MOCK-GOT messages:", json.dumps(body.get("messages", []), ensure_ascii=False)[:200], flush=True)
    print("MOCK-GOT auth:", request.headers.get("Authorization", "(none)"), flush=True)
    text = "这是来自测试API的回复，证明接口切换成功。"
    # 模拟"聚合类 API 无视 stream 参数、总是返回普通 JSON"的行为
    return web.json_response({
        "id": "mock-1", "object": "chat.completion", "model": body.get("model", "mock"),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
    })


app = web.Application()
app.router.add_post("/v1/chat/completions", chat)

if __name__ == "__main__":
    print("MOCK OpenAI API listening on 127.0.0.1:8799", flush=True)
    web.run_app(app, host="127.0.0.1", port=8799, print=None)
