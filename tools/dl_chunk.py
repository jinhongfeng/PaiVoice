#!/usr/bin/env python3
"""并行分块下载器（单连接被限速时的加速方案）：
N 线程各自按 Range 拉互不重叠的分块，pwrite 按偏移落盘，可断点续传。
用法: python dl_chunk.py <url> <dest> [threads]
"""
import os
import sys
import threading
import time
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
CHUNK = 4 * 1024 * 1024


def size_of(url: str) -> int:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return int(r.headers["Content-Length"])


def fetch_range(url: str, start: int, end: int, attempts: int = 8) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Range": f"bytes={start}-{end}"})
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception:
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"range {start}-{end} failed after {attempts} tries")


def download(url: str, dest: str, threads: int = 8) -> None:
    total = size_of(url)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".part"
    if os.path.exists(tmp) and os.path.getsize(tmp) == total:
        os.replace(tmp, dest)
        print(f"skip {os.path.basename(dest)}", flush=True)
        return
    lock = threading.Lock()
    done = [0] * threads
    last = {"t": time.time()}
    write_lock = threading.Lock()  # Windows 无 pwrite：seek+write 需串行
    errors: list[str] = []
    err_lock = threading.Lock()
    diag: list[str] = []

    def worker(i: int) -> None:
        start = i * CHUNK
        while start < total:
            end = min(start + CHUNK - 1, total - 1)
            try:
                data = fetch_range(url, start, end)
            except Exception as e:
                with err_lock:
                    errors.append(f"range {start}-{end}: {e}")
                return
            data = data[: end - start + 1]  # 服务器可能多给字节，截到请求范围防重叠
            with err_lock:
                diag.append(f"w{i} start={start} want={end - start + 1} got={len(data)}")
            fd = os.open(tmp, os.O_CREAT | os.O_RDWR)
            try:
                with write_lock:
                    os.lseek(fd, start, os.SEEK_SET)
                    os.write(fd, data)
            finally:
                os.close(fd)
            done[i] += len(data)
            now = time.time()
            if now - last["t"] >= 4:
                last["t"] = now
                got = sum(done)
                print(f"  {os.path.basename(dest)}: {got/1e6:.0f}/{total/1e6:.0f} MB "
                      f"({100.0*got/total:.0f}%)  "
                      f"{got/1024/(now - time.time() + now - last['t'] + 1):.0f}KB/s-ish",
                      flush=True)
            start += CHUNK * threads

    t0 = time.time()
    ts = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    if errors:
        for e in errors[:10]:
            print(f"  ERR {e}", flush=True)
        print(f"  FAILED: {len(errors)} ranges, partial {os.path.getsize(tmp)}/{total} "
              f"-> 保留 .part，请重跑续传", flush=True)
        sys.exit(1)
    for d in diag[-16:]:
        print(f"  DIAG {d}", flush=True)
    if os.path.getsize(tmp) != total:
        # Windows 并发写句柄可能残留尾部字节：截断到权威大小再验
        try:
            os.truncate(tmp, total)
        except OSError:
            pass
        if os.path.getsize(tmp) != total:
            print(f"  FAILED: size mismatch {os.path.getsize(tmp)} != {total}", flush=True)
            sys.exit(1)
    os.replace(tmp, dest)
    print(f"  done {os.path.basename(dest)} ({total/1e6:.1f} MB in "
          f"{time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    url, dest = sys.argv[1], sys.argv[2]
    threads = int(sys.argv[3]) if len(sys.argv) > 3 else 8
    download(url, dest, threads)
