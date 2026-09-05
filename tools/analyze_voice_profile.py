#!/usr/bin/env python3
"""sherpa-onnx-vits-zh-hf-fanchen-C 音色画像：对 试听/ 下全部 sid 的 wav 做实测分析。

对每个文件：
  - 时长、RMS 能量、浊音占比
  - 基频 F0（FFT 自相关法，帧长 25ms / 帧移 10ms，搜索 50~500Hz，中值平滑去八度跳变）
  - 估算语速 = 固定文本音节数(15) / 浊音时长

输出：控制台汇总 + CSV（voice-call/试听/fanchen-C-音色画像.csv）
类别为按基频划分的倾向性标签，仅供参考，不代表性别判定。
"""
import csv
import glob
import io
import os
import wave

import numpy as np

SR = 16000
FRAME_LEN = int(0.025 * SR)      # 400
HOP = int(0.010 * SR)            # 160
F0_MIN, F0_MAX = 50.0, 500.0
LAG_MAX = int(SR / F0_MIN)       # 320
LAG_MIN = max(1, int(SR / F0_MAX))  # 32
VOICED_TH = 0.35
SAMPLE_TEXT_SYLLABLES = 15  # 你好今天过得怎么样今天天气真好 = 15 个音节

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "试听")
CSV_PATH = os.path.join(OUT_DIR, "fanchen-C-音色画像.csv")


def read_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
        sr = w.getframerate()
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return data, sr


def pitch_track(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """返回 (voiced_f0_hz, voiced_mask)，帧级 F0 估计。"""
    if x.size < FRAME_LEN:
        return np.array([]), np.array([])
    n_frames = (x.size - FRAME_LEN) // HOP + 1
    f0s = np.zeros(n_frames)
    voiced = np.zeros(n_frames, dtype=bool)
    for i in range(n_frames):
        seg = x[i * HOP: i * HOP + FRAME_LEN]
        seg = seg - seg.mean()
        e = float(seg @ seg)
        if e < 1e-6:
            continue
        seg = seg / np.sqrt(e + 1e-12)
        # FFT 自相关（零填充到 2 的幂）
        n = 1
        while n < 2 * FRAME_LEN:
            n <<= 1
        spec = np.fft.rfft(seg, n)
        acf = np.fft.irfft(spec * np.conj(spec), n)[:LAG_MAX + 1].real
        acf_norm = acf / (acf[0] + 1e-12)
        band = acf_norm[LAG_MIN:LAG_MAX + 1]
        k = int(np.argmax(band))
        peak = float(band[k])
        if peak >= VOICED_TH:
            lag = LAG_MIN + k
            # 抛物线插值细化
            if 0 < k < len(band) - 1:
                a, b, c = band[k - 1], band[k], band[k + 1]
                denom = a - 2 * b + c
                if abs(denom) > 1e-9:
                    lag += 0.5 * (a - c) / denom
            f0s[i] = SR / lag
            voiced[i] = True
    # 中值平滑（窗口 5）去八度跳变
    if voiced.sum() > 0:
        idx = np.where(voiced)[0]
        vals = f0s[idx]
        med = np.empty_like(vals)
        for j in range(vals.size):
            lo = max(0, j - 2)
            hi = min(vals.size, j + 3)
            med[j] = np.median(vals[lo:hi])
        f0s[idx] = med
    return f0s, voiced


def classify(f0_med: float) -> str:
    if f0_med < 140:
        return "低音·男声倾向"
    if f0_med < 170:
        return "中低音·偏男声"
    if f0_med < 210:
        return "中音·中性"
    if f0_med < 260:
        return "中高音·偏女声"
    return "高音·女声倾向"


def analyze(path: str) -> dict | None:
    data, sr = read_wav(path)
    if data.size == 0:
        return None
    if sr != SR:
        # 重采样到 16k（线性插值，足够用于统计）
        t_new = np.arange(0, data.size / sr, 1.0 / SR)
        t_old = np.arange(data.size) / sr
        data = np.interp(t_new, t_old, data)
    dur = data.size / SR
    f0s, voiced = pitch_track(data)
    voiced_f0 = f0s[voiced]
    vd = float(voiced.sum()) * HOP / SR  # 浊音时长
    if voiced_f0.size < 10:
        return {"sid": None, "dur": dur, "voiced_ratio": float(voiced.mean()),
                "f0_mean": float("nan"), "f0_med": float("nan"),
                "f0_p5": float("nan"), "f0_p95": float("nan"),
                "rate": float("nan"), "cls": "无有效浊音", "rms": 0.0}
    rms = float(np.sqrt(np.mean(data ** 2)))
    p5, p95 = np.percentile(voiced_f0, [5, 95])
    f0_mean = float(np.mean(voiced_f0))
    f0_med = float(np.median(voiced_f0))
    rate = SAMPLE_TEXT_SYLLABLES / vd if vd > 0.5 else float("nan")
    return {"dur": dur, "voiced_ratio": float(voiced.mean()), "f0_mean": f0_mean,
            "f0_med": f0_med, "f0_p5": float(p5), "f0_p95": float(p95),
            "rate": rate, "cls": classify(f0_med), "rms": rms}


def main() -> None:
    files = sorted(glob.glob(os.path.join(OUT_DIR, "sherpa-onnx-vits-zh-hf-fanchen-C-sid*.wav")))
    if not files:
        print("no files found"); return
    rows = []
    for fp in files:
        sid = int(os.path.basename(fp).split("-sid")[1].split(".wav")[0])
        r = analyze(fp)
        if r is None:
            continue
        r["sid"] = sid
        rows.append(r)
        print(f"sid={sid:03d} f0med={r['f0_med']:.0f}Hz [{r['f0_p5']:.0f}-{r['f0_p95']:.0f}] "
              f"cls={r['cls']} dur={r['dur']:.2f}s rate={r['rate']:.1f}/s", flush=True)
    rows.sort(key=lambda r: r["sid"])
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["sid", "时长s", "浊音占比", "F0均值Hz", "F0中位数Hz", "F0-P5", "F0-P95",
                    "F0范围Hz", "估算语速(音节/s)", "RMS", "类别"])
        for r in rows:
            w.writerow([r["sid"], f"{r['dur']:.2f}", f"{r['voiced_ratio']:.2f}",
                        f"{r['f0_mean']:.0f}", f"{r['f0_med']:.0f}",
                        f"{r['f0_p5']:.0f}", f"{r['f0_p95']:.0f}",
                        f"{r['f0_p95'] - r['f0_p5']:.0f}",
                        f"{r['rate']:.1f}" if r['rate'] == r['rate'] else "NaN",
                        f"{r['rms']:.3f}", r["cls"]])
    print(f"\nDONE: {len(rows)} voices -> {CSV_PATH}")
    from collections import Counter
    cnt = Counter(r["cls"] for r in rows)
    print("类别统计:", dict(cnt))
    if rows:
        by_med = sorted(rows, key=lambda r: r["f0_med"])
        print(f"最低音: sid={by_med[0]['sid']:03d} {by_med[0]['f0_med']:.0f}Hz | "
              f"最高音: sid={by_med[-1]['sid']:03d} {by_med[-1]['f0_med']:.0f}Hz")


if __name__ == "__main__":
    main()
