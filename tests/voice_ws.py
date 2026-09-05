#!/usr/bin/env python3
"""语音全链路冒烟：本地 TTS 合成一句中文 → 当麦克风 PCM 播给 PaiVoice
→ 服务端走 ASR(本地 SenseVoice) → LLM(Ollama) → TTS(本地 VITS) → 音频回传。
用法: .venv/Scripts/python voice_ws.py [ws_url] [token]
"""
import asyncio
import io
import json
import os
import sys
import urllib.parse
import urllib.request
import wave

from websockets.asyncio.client import connect

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根（.env 所在）
WS_URL = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:8780/voice/ws"
TTS_URL = os.getenv("LOCAL_TTS_URL", "http://127.0.0.1:8792/v1/audio/speech")
TEST_SENTENCE = "你好，今天过得怎么样？"


def load_env_token() -> str:
    envf = os.path.join(BASE, ".env")
    if os.path.exists(envf):
        for line in open(envf, encoding="utf-8"):
            line = line.strip()
            if line.startswith("PAIVOICE_TOKEN="):
                return line.split("=", 1)[1].split("#")[0].strip()
    return ""


def synth_wav_pcm(text: str) -> bytes:
    req = urllib.request.Request(
        TTS_URL, data=json.dumps({"text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        wav_bytes = r.read()
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
    if sr != 16000:
        print(f"  [!] TTS 采样率 {sr} != 16k，测试可能不准", flush=True)
    print(f"  [tts] '{text}' -> {n / sr:.1f}s wav ({sr}Hz, {len(raw)} bytes)", flush=True)
    return raw


async def main() -> int:
    token = load_env_token()
    url = WS_URL
    if token:
        sep = "&" if "?" in url else "?"
        url += sep + "token=" + urllib.parse.quote(token)

    pcm = synth_wav_pcm(TEST_SENTENCE)
    if len(pcm) < 16000 * 2 * 0.3:
        print("[!] 测试音频太短，跳过", flush=True)
        return 2

    async def until(ws, pred, what, timeout=90):
        end = asyncio.get_event_loop().time() + timeout
        while True:
            remain = end - asyncio.get_event_loop().time()
            if remain <= 0:
                raise TimeoutError(f"timeout waiting {what}")
            msg = json.loads(await asyncio.wait_for(ws.recv(), remain))
            t = msg.get("type")
            if t in ("transcript", "reply_text"):
                print(f"  << {t}: {msg.get('text', '')[:80]}", flush=True)
            elif t == "audio":
                print(f"  << audio: {len(msg.get('data', ''))} b64 chars", flush=True)
            elif t == "error":
                print(f"  << error: {msg}", flush=True)
            else:
                print(f"  << {t}", flush=True)
            if pred(msg):
                return msg

    async with connect(url, max_size=None) as ws:
        print("[1] 连接，发送 start ...", flush=True)
        await ws.send(json.dumps({"type": "start", "token": token}))
        await until(ws, lambda m: m.get("type") == "state", "listening")

        print(f"[2] 播报语音轮（{TEST_SENTENCE}）...", flush=True)
        await ws.send(json.dumps({"type": "speech_start", "preroll": ""}))
        # 分块发 PCM（模拟实时流）
        for i in range(0, len(pcm), 16000 * 2):
            await ws.send(pcm[i:i + 16000 * 2])
        await ws.send(json.dumps({"type": "speech_end"}))

        await until(ws, lambda m: m.get("type") == "transcript", "transcript")
        await until(ws, lambda m: m.get("type") == "generation_end", "generation_end")
        print("[3] 语音轮闭环 OK（ASR→LLM→TTS 全链路）", flush=True)
        await ws.send(json.dumps({"type": "hangup"}))
        await asyncio.sleep(0.5)
        print("VOICE SMOKE PASS", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
