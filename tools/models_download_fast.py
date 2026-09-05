#!/usr/bin/env python3
"""语音模型下载（快版）：大文件走 dl_chunk 并行分块，小文件单连接。
用法: .venv/Scripts/python models_download_fast.py
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dl_chunk import download, size_of, fetch_range  # noqa: E402

HF = "https://hf-mirror.com"
BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")  # 仓库根/models
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
BIG = 8 * 1024 * 1024  # ≥8MB 走并行

REPOS = [
    ("csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
     ["model.int8.onnx", "tokens.txt"]),
    ("csukuangfj/sherpa-onnx-vits-zh-ll", None),
]
SKIP_EXACT = {".gitattributes", "README.md", "G_multisperaker_latest.json", "test_wavs"}
SKIP_PREFIX = ("test_wavs/", "dict/README.md")


def list_files(repo: str) -> list[str]:
    req = urllib.request.Request(f"{HF}/api/models/{repo}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    return [s["rfilename"] for s in data.get("siblings", [])
            if s["rfilename"] not in SKIP_EXACT
            and not s["rfilename"].startswith(SKIP_PREFIX)]


def fetch_small(url: str, dest: str, retries: int = 5) -> None:
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    for i in range(retries):
        try:
            data = fetch_range(url, 0, size_of(url) - 1, attempts=3)
            with open(dest, "wb") as f:
                f.write(data)
            print(f"  ok  {os.path.basename(dest)} ({len(data)/1e6:.1f} MB)", flush=True)
            return
        except Exception as e:
            print(f"  retry {i+1}: {os.path.basename(dest)} ({e})", flush=True)
    sys.exit(f"small download failed: {dest}")


def main() -> None:
    os.makedirs(BASE, exist_ok=True)
    for repo, allow in REPOS:
        local_dir = os.path.join(BASE, repo.split("/")[-1])
        print(f">>> {repo}", flush=True)
        files = list_files(repo)
        if allow:
            files = [f for f in files if f in allow]
        for f in files:
            dest = os.path.join(local_dir, f)
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                print(f"  skip {os.path.basename(f)}", flush=True)
                continue
            url = f"{HF}/{repo}/resolve/main/{f}"
            try:
                n = size_of(url)
            except Exception as e:
                print(f"  size_of fail {f}: {e}", flush=True)
                continue
            if n >= BIG:
                print(f"  [parallel] {os.path.basename(f)} ({n/1e6:.0f} MB)", flush=True)
                download(url, dest, threads=8)
            else:
                print(f"  [small] {os.path.basename(f)}", flush=True)
                fetch_small(url, dest)
    print("\n模型下载完成。", flush=True)


if __name__ == "__main__":
    main()
