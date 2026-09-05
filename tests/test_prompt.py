#!/usr/bin/env python3
"""验证：Python UTF-8 读取 .env 的 system prompt 传给 Ollama 后回复是否正常。"""
import json
import os
import time
import urllib.request

sys_prompt = ""
_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, ".env")
for line in open(_env, encoding="utf-8"):
    line = line.strip()
    if line.startswith("PAIVOICE_SYSTEM_PROMPT="):
        sys_prompt = line.split("=", 1)[1].split("#")[0].strip()

print("system prompt:", sys_prompt[:60], "...")
body = json.dumps({
    "model": "qwen2.5:7b-instruct",
    "messages": [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": "你好呀，今天过得怎么样？"},
    ],
    "stream": False,
    "max_tokens": 96,
}).encode("utf-8")
t0 = time.time()
req = urllib.request.Request(
    "http://localhost:11434/v1/chat/completions", data=body,
    headers={"Content-Type": "application/json"})
r = json.loads(urllib.request.urlopen(req, timeout=120).read())
print(f"耗时 {time.time()-t0:.1f}s, {r['usage']['completion_tokens']} tokens")
print("回复:", r["choices"][0]["message"]["content"])
