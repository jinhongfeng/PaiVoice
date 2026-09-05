#!/usr/bin/env python3
"""30 秒速度对照：单连接 vs 8 线程分块（下载前 64MB 就停）。"""
import os
import sys
import threading
import time
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
URL = ("https://hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/"
       "resolve/main/model.int8.onnx")
LIMIT = 64 * 1024 * 1024
DURATION = 30


def single() -> float:
    req = urllib.request.Request(URL, headers={"User-Agent": UA, "Range": "bytes=0-67108863"})
    t0 = time.time()
    got = 0
    with urllib.request.urlopen(req, timeout=90) as r:
        while got < LIMIT and time.time() - t0 < DURATION:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            got += len(chunk)
    return got / (time.time() - t0)


def parallel() -> float:
    stop = {"t": time.time() + DURATION}
    got = [0] * 8
    fail = [0]

    def worker(i: int) -> None:
        start = i * (4 * 1024 * 1024)
        while start < LIMIT and time.time() < stop["t"]:
            end = min(start + 4 * 1024 * 1024 - 1, LIMIT - 1)
            req = urllib.request.Request(
                URL, headers={"User-Agent": UA, "Range": f"bytes={start}-{end}"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                got[i] += len(data)
            except Exception:
                fail[0] += 1
                break
            start += 8 * 4 * 1024 * 1024

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    t0 = time.time()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return sum(got) / (time.time() - t0)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"
    if mode in ("single", "both"):
        print(f"single: {single() / 1024:.0f} KB/s", flush=True)
    if mode in ("parallel", "both"):
        print(f"parallel: {parallel() / 1024:.0f} KB/s", flush=True)
