#!/usr/bin/env python3
"""本地语音边车：ASR（SenseVoiceSmall）+ TTS（VITS 中文），基于 sherpa-onnx，无 torch。
完全离线推理（CPU），模型放 ./models/ 下，首次启动前先跑 models_download.py。

端点（OpenAI 兼容子集，给 PaiVoice server.py 的 local 分支调用）:
  POST /v1/audio/transcriptions   multipart: file=turn.wav [language=zh]  -> {"text": "..."}
  POST /v1/audio/speech           json: {"input": 文本} 或 {"text": 文本} -> audio/wav 字节
  GET  /v1/tts/models              -> {"current": "sherpa-onnx-vits-zh-ll", "models": [...]}
  POST /v1/tts/model              json: {"model": "sherpa-onnx-vits-zh-ll"} -> 热切换音色

环境变量:
  LOCAL_VOICE_PORT   端口，默认 8792
  LOCAL_VOICE_MODEL_DIR  模型目录，默认 ./models
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import wave

import numpy as np
import sherpa_onnx
from aiohttp import web

BASE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(BASE))  # packages/local-voice → 仓库根
PORT = int(os.getenv("LOCAL_VOICE_PORT", "8792"))
MODEL_DIR = os.getenv("LOCAL_VOICE_MODEL_DIR", os.path.join(REPO_ROOT, "models"))

if os.name == "nt" and not MODEL_DIR.isascii():
    # sherpa-onnx 的 C++ 层按系统 ANSI 代码页（GBK）打开文件，而 pybind 传的是 UTF-8
    # 字节——中文路径（如 语音聊天/）会打不开（err 3）。在 %LOCALAPPDATA% 下建一个
    # ASCII 名 junction 指向真实模型目录，让原生层只见 ASCII 路径。
    _link_root = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "PaiVoice")
    _link = os.path.join(_link_root, "models")
    try:
        os.makedirs(_link_root, exist_ok=True)
        if os.path.isdir(_link) and \
                os.path.normpath(os.path.realpath(_link)) != os.path.normpath(MODEL_DIR):
            os.rmdir(_link)  # 旧 junction 指向别处，删掉重建
        if not os.path.isdir(_link):
            try:
                subprocess.run(["cmd", "/c", "mklink", "/J", _link, MODEL_DIR],
                               check=True, capture_output=True, creationflags=0x08000000)
            except Exception as e:
                # 第一次失败常见原因：残留一个「空目录/坏 junction」挡在目标位置。
                # 只删除目录本身（不递归），然后重试一次。
                print(f"[local-voice] junction 创建失败（{e}），清理后重试", flush=True)
                try:
                    os.rmdir(_link)
                except Exception:
                    pass
                subprocess.run(["cmd", "/c", "mklink", "/J", _link, MODEL_DIR],
                               check=True, capture_output=True, creationflags=0x08000000)
        MODEL_DIR = _link
    except Exception as e:  # junction 不可用时退回原路径（ASCII 目录下本来就不需要）
        print(f"[local-voice] 模型目录 junction 创建失败（{e}），仍用原路径", flush=True)

SENSE_DIR = os.path.join(MODEL_DIR, "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17")
VITS_DIR = os.path.join(MODEL_DIR, "sherpa-onnx-vits-zh-ll")  # 默认音色；启动时若不存在则用第一个扫描到的

recognizer: sherpa_onnx.OfflineRecognizer | None = None
tts: sherpa_onnx.OfflineTts | None = None
tts_name: str = ""  # 当前音色目录名
current_sid: int = 0  # 多说话人模型的说话人编号（单说话人模型恒为 0）


def _find_vits_onnx(vdir: str) -> str:
    """找目录里的 VITS 模型文件：优先 model.onnx，否则任意非 .int8.onnx 的 .onnx。
    （不同仓库命名不同：vits-zh-hf-fanchen-C.onnx / vits-aishell3.onnx …）"""
    standard = os.path.join(vdir, "model.onnx")
    if os.path.exists(standard):
        return standard
    cands = sorted(f for f in os.listdir(vdir)
                   if f.endswith(".onnx") and not f.endswith(".int8.onnx"))
    if not cands:
        raise FileNotFoundError(f"{vdir} 下没有 VITS 模型（.onnx）文件")
    return os.path.join(vdir, cands[0])


def _vits_dirs() -> list[str]:
    """扫描 models/ 下所有 VITS 音色目录（判定：有 tokens.txt 且含非 int8 的 .onnx；
    ASR 的 model.int8.onnx 会被排除）。"""
    if not os.path.isdir(MODEL_DIR):
        return []
    out = []
    for name in sorted(os.listdir(MODEL_DIR)):
        d = os.path.join(MODEL_DIR, name)
        if not os.path.isdir(d) or not os.path.exists(os.path.join(d, "tokens.txt")):
            continue
        try:
            _find_vits_onnx(d)
            out.append(name)
        except FileNotFoundError:
            pass
    return out


def _build_tts(vdir: str) -> tuple[sherpa_onnx.OfflineTts, str]:
    """按目录构造 OfflineTts（lexicon/dict 存在才带上；无词表的 VITS 也能跑）。返回 (tts, 目录名)。"""
    model = _find_vits_onnx(vdir)
    tokens = os.path.join(vdir, "tokens.txt")
    if not os.path.exists(tokens):
        raise FileNotFoundError(f"{vdir} 缺 tokens.txt")
    kw: dict = {"model": model, "tokens": tokens}
    if os.path.exists(os.path.join(vdir, "lexicon.txt")):
        kw["lexicon"] = os.path.join(vdir, "lexicon.txt")
    if os.path.isdir(os.path.join(vdir, "dict")):
        kw["dict_dir"] = os.path.join(vdir, "dict")
    cfg = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(**kw),
            num_threads=2,
        ),
        rule_fsts=",".join(
            os.path.join(vdir, f)
            for f in ("phone.fst", "date.fst", "number.fst", "new_heteronym.fst")
            if os.path.exists(os.path.join(vdir, f))
        ),
        max_num_sentences=1,
    )
    return sherpa_onnx.OfflineTts(cfg), os.path.basename(vdir.rstrip("/\\"))


def init() -> None:
    global recognizer, tts, tts_name
    if not os.path.exists(SENSE_DIR):
        sys.exit(f"[local-voice] ASR 模型目录不存在: {SENSE_DIR}\n先运行 models_download.py 下载模型")
    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=os.path.join(SENSE_DIR, "model.int8.onnx"),
        tokens=os.path.join(SENSE_DIR, "tokens.txt"),
        use_itn=True,
        num_threads=2,
        debug=False,
    )
    print(f"[local-voice] ASR SenseVoiceSmall loaded", flush=True)

    dirs = _vits_dirs()
    if not dirs:
        sys.exit(f"[local-voice] TTS 模型不存在（{MODEL_DIR} 下没有含 model.onnx 的目录）\n先运行 models_download.py 下载模型")
    chosen = os.path.basename(VITS_DIR.rstrip("/\\"))
    if chosen not in dirs:
        chosen = dirs[0]  # 默认目录不在则用第一个扫描到的
    tts, tts_name = _build_tts(os.path.join(MODEL_DIR, chosen))
    print(f"[local-voice] TTS {tts_name} loaded (sid=0, {tts.sample_rate}Hz)", flush=True)


def _wav_to_float32(data: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(data), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return samples, sr


def _float32_to_wav(samples: np.ndarray, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((samples * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


async def handle_asr(request: web.Request) -> web.Response:
    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != "file":
        return web.json_response({"error": "missing file field"}, status=400)
    pcm_wav = await field.read()
    try:
        samples, sr = _wav_to_float32(pcm_wav)
    except Exception as e:
        return web.json_response({"error": f"bad wav: {e}"}, status=400)
    if sr != 16000:
        # 简单线性重采样到 16k（SenseVoice 要求）
        if sr != 0:
            n = int(len(samples) * 16000 / sr)
            x = np.linspace(0, len(samples) - 1, n, dtype=np.float32)
            samples = np.interp(x, np.arange(len(samples)), samples).astype(np.float32)
    stream = recognizer.create_stream()
    stream.accept_waveform(16000, samples)
    recognizer.decode_stream(stream)
    text = stream.result.text.strip()
    return web.json_response({"text": text})


async def handle_tts(request: web.Request) -> web.Response:
    body = await request.json()
    text = body.get("input") or body.get("text") or ""
    if not text:
        return web.json_response({"error": "empty text"}, status=400)
    audio = tts.generate(text, sid=current_sid, speed=1.0)
    samples = np.asarray(audio.samples, dtype=np.float32)  # 该版本 samples 是 list
    if samples.size == 0:
        return web.json_response({"error": "tts produced silence"}, status=500)
    wav_bytes = _float32_to_wav(samples, audio.sample_rate)
    return web.Response(body=wav_bytes, content_type="audio/wav",
                        headers={"Access-Control-Allow-Origin": "*"})


async def handle_tts_models(_: web.Request) -> web.Response:
    """音色列表 + 当前音色 + 各音色说话人数（给拨号页设置面板）。"""
    dirs = _vits_dirs()
    speakers = {}
    for name in dirs:
        try:
            t, _ = _build_tts(os.path.join(MODEL_DIR, name))
            speakers[name] = t.num_speakers
        except Exception:
            speakers[name] = 1
    return web.json_response({"current": tts_name, "current_sid": current_sid,
                              "models": dirs, "speakers": speakers},
                             headers={"Access-Control-Allow-Origin": "*"})


async def handle_tts_sid(request: web.Request) -> web.Response:
    """切换说话人编号（同一模型内秒切，不重载模型）。"""
    global current_sid
    body = await request.json()
    sid = int(body.get("sid", 0))
    n = tts.num_speakers if tts else 1
    if not 0 <= sid < n:
        return web.json_response({"error": f"sid {sid} out of range [0,{n})",
                                  "current_sid": current_sid}, status=400,
                                 headers={"Access-Control-Allow-Origin": "*"})
    current_sid = sid
    print(f"[local-voice] sid -> {sid} ({tts_name})", flush=True)
    return web.json_response({"ok": True, "current_sid": current_sid,
                              "num_speakers": n},
                             headers={"Access-Control-Allow-Origin": "*"})


async def handle_tts_switch(request: web.Request) -> web.Response:
    """热切换音色：swap 语义，加载失败保留旧音色；可带 sid 一起设置。"""
    global tts, tts_name, current_sid
    body = await request.json()
    name = str(body.get("model") or "").strip()
    target = os.path.join(MODEL_DIR, name)
    valid = False
    if name and os.path.isdir(target) and os.path.exists(os.path.join(target, "tokens.txt")):
        try:
            _find_vits_onnx(target)
            valid = True
        except FileNotFoundError:
            valid = False
    if not valid:
        return web.json_response({"error": f"no such model: {name}", "current": tts_name}, status=404,
                                 headers={"Access-Control-Allow-Origin": "*"})
    try:
        new_tts, new_name = _build_tts(target)
    except Exception as e:
        return web.json_response({"error": str(e), "current": tts_name}, status=500,
                                 headers={"Access-Control-Allow-Origin": "*"})
    tts, tts_name = new_tts, new_name  # swap：构造成功才替换
    if "sid" in body:  # 切模型时可选一并设置说话人
        sid = int(body.get("sid", 0))
        if 0 <= sid < tts.num_speakers:
            current_sid = sid
    else:
        current_sid = min(current_sid, tts.num_speakers - 1)  # 新模型说话人少时回退
    print(f"[local-voice] TTS switched -> {tts_name} (sid={current_sid}, {tts.sample_rate}Hz)", flush=True)
    return web.json_response({"ok": True, "current": tts_name, "current_sid": current_sid,
                              "models": _vits_dirs()},
                             headers={"Access-Control-Allow-Origin": "*"})


async def handle_health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "asr": recognizer is not None, "tts": tts is not None})


def main() -> None:
    init()
    app = web.Application()
    app.router.add_post("/v1/audio/transcriptions", handle_asr)
    app.router.add_post("/v1/audio/speech", handle_tts)
    app.router.add_get("/v1/tts/models", handle_tts_models)
    app.router.add_post("/v1/tts/model", handle_tts_switch)
    app.router.add_post("/v1/tts/sid", handle_tts_sid)
    app.router.add_get("/health", handle_health)
    print(f"[local-voice] listening on http://127.0.0.1:{PORT}", flush=True)
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)


if __name__ == "__main__":
    main()
