#!/usr/bin/env python3
"""诊断：抓 qwen2.5 真实回复原文 → cleanse 切分 → 检查朗读段/字幕段里有什么怪内容。"""
import json
import os
import sys
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "packages", "realtime-core"))
from cleanse import split_for_tts  # noqa: E402

sys_prompt = ""
for line in open(os.path.join(_ROOT, ".env"), encoding="utf-8"):
    line = line.strip()
    if line.startswith("PAIVOICE_SYSTEM_PROMPT="):
        sys_prompt = line.split("=", 1)[1].split("#")[0].strip()

questions = ["你好呀，今天过得怎么样？", "给我讲个笑话吧"]
for q in questions:
    body = json.dumps({
        "model": "qwen2.5:7b-instruct",
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": q},
        ],
        "stream": False,
        "max_tokens": 96,
    }).encode("utf-8")
    req = urllib.request.Request("http://localhost:11434/v1/chat/completions",
                                 data=body, headers={"Content-Type": "application/json"})
    r = json.loads(urllib.request.urlopen(req, timeout=120).read())
    reply = r["choices"][0]["message"]["content"]
    print("=" * 60)
    print("问题:", q)
    print("回复原文 ascii():", ascii(reply))
    print("回复原文 utf8 文件里是这样:")
    with open(os.path.join(_ROOT, "logs", "_diag.txt"), "a", encoding="utf-8") as f:
        f.write(f"Q: {q}\nA: {reply}\n---\n")
    spoken, caption = split_for_tts(reply, "local")
    print("朗读段 ascii():", ascii(spoken))
    print("字幕段 ascii():", ascii(caption))
    # 统计怪内容
    import re
    en = re.findall(r"[A-Za-z]+", spoken)
    emoji = re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", reply)
    symbols = re.findall(r"[^\u4e00-\u9fff\uff00-\uffef0-9A-Za-z，。！？、；：" r"'（）\s\-—…]", reply)
    print("朗读段含英文:", en)
    print("回复含 emoji:", emoji)
    print("回复含特殊符号:", symbols)
