#!/usr/bin/env python3
"""合成多说话人模型的全部说话人试听样本（每句固定文本），供人工挑选音色。
用法: python make_voice_samples.py <音色目录名> [起始sid] [结束sid]
输出: 试听/<音色名>-sid<N>.wav
"""
import io
import os
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "packages", "local-voice"))
import local_voice

TEXT = os.getenv("SAMPLE_TEXT", "你好，今天过得怎么样？今天天气真好。")


def main() -> None:
    voice = sys.argv[1] if len(sys.argv) > 1 else "sherpa-onnx-vits-zh-hf-fanchen-C"
    start = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    end = int(sys.argv[3]) if len(sys.argv) > 3 else 10**9
    vdir = os.path.join(local_voice.MODEL_DIR, voice)
    tts, name = local_voice._build_tts(vdir)
    total = tts.num_speakers
    print(f"model {name}: {total} speakers, {tts.sample_rate}Hz", flush=True)
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "试听")
    os.makedirs(out_dir, exist_ok=True)
    stop = min(end, total)
    ok = 0
    for sid in range(start, stop):
        try:
            audio = tts.generate(TEXT, sid=sid, speed=1.0)
            s = np.asarray(audio.samples, dtype=np.float32)
            if s.size == 0:
                print(f"sid={sid}: SILENCE", flush=True)
                continue
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(audio.sample_rate)
                w.writeframes((s * 32767).astype(np.int16).tobytes())
            fn = os.path.join(out_dir, f"{name}-sid{sid:03d}.wav")
            with open(fn, "wb") as f:
                f.write(buf.getvalue())
            ok += 1
            if sid % 20 == 0 or sid == stop - 1:
                print(f"  ... sid={sid} done ({ok} files)", flush=True)
        except Exception as e:
            print(f"sid={sid}: FAIL {type(e).__name__}: {str(e)[:100]}", flush=True)
    print(f"DONE: {ok} samples -> {out_dir}", flush=True)


if __name__ == "__main__":
    main()
