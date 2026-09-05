#!/usr/bin/env python3
"""下载本地语音模型（hf-mirror 直链 + 断点续传，纯标准库，无第三方依赖）。
用法: .venv/Scripts/python models_download.py
"""
import json
import os
import sys
import time
import urllib.request

HF = "https://hf-mirror.com"
BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")  # 仓库根/models

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

REPOS = [
    # (repo, 文件白名单；None=全要)
    ("csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
     ["model.int8.onnx", "tokens.txt"]),        # ASR 耳朵（int8，~230MB）
    ("csukuangfj/sherpa-onnx-vits-zh-ll", None),  # TTS 嘴（model.onnx + 词典 + fst）
]

SKIP_EXACT = {".gitattributes", "README.md", "G_multisperaker_latest.json",
              "export-onnx.py", "test_wavs"}
# dict 目录下全是小词典文件，需要；但跳过其中的 README
SKIP_PREFIX = ("test_wavs/", "dict/README.md")


def list_files(repo: str) -> list[str]:
    req = urllib.request.Request(f"{HF}/api/models/{repo}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    out = []
    for s in data.get("siblings", []):
        f = s["rfilename"]
        if f in SKIP_EXACT or f.startswith(SKIP_PREFIX):
            continue
        out.append(f)
    return out


def download(url: str, dest: str, retries: int = 5) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    for attempt in range(retries):
        try:
            exist = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            headers = {"Range": f"bytes={exist}-"} if exist else {}
            headers["User-Agent"] = UA
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "ab") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
            os.replace(tmp, dest)
            print(f"  ok  {os.path.basename(dest)}  {os.path.getsize(dest) / 1e6:.1f} MB",
                  flush=True)
            return
        except Exception as e:
            print(f"  retry {attempt + 1}/{retries}: {dest.split(chr(92))[-1]} ({e})",
                  flush=True)
            time.sleep(2)
    sys.exit(f"download failed: {url}")


def main() -> None:
    os.makedirs(BASE, exist_ok=True)
    for repo, allow in REPOS:
        local_dir = os.path.join(BASE, repo.split("/")[-1])
        print(f">>> {repo}", flush=True)
        files = list_files(repo)
        if allow:
            files = [f for f in files if f in allow]
        print(f"    {len(files)} files: {', '.join(os.path.basename(f) for f in files)}",
              flush=True)
        for f in files:
            dest = os.path.join(local_dir, f)
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                print(f"  skip {os.path.basename(f)} (exists)", flush=True)
                continue
            download(f"{HF}/{repo}/resolve/main/{f}", dest)
    print("\n模型下载完成。启动: python packages/local-voice/local_voice.py（或双击 start.bat）", flush=True)


if __name__ == "__main__":
    main()
