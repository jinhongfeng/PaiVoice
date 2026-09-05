# cleanse.py — TTS 清洗层（生产实现，测试直接 import 本模块，杜绝复制漂移）
# 职责：按 K 的输出协议切分整段回复 → (tts_text, caption_text)。
#   - 引号内 = 朗读段：中间协议语气标记转译成当前厂商方言后进 TTS
#   - 引号外 = 字幕段：只做剥标记清理，进字幕不进 TTS
#   - 无引号时兜底整段进 TTS（措辞异常时宁可多读不错过）
# 中间协议：[laughs] [sighs] [whispers] / (pause) (laughs) (sighs)——措辞只学这一套，
# 各家方言映射在 _TTS_DIALECT，换厂商措辞零改动。

import os
import re

TTS_PROVIDER = os.getenv("PAIVOICE_TTS_PROVIDER", "mock")

# 语气中间协议 → 各家 TTS 方言映射（比武时逐家实测校准）
_TTS_DIALECT = {
    "elevenlabs": {
        # ElevenLabs v3 原生支持 [laughs] [sighs] [whispers] 等方括号 audio tags → 直通；
        # (pause) 无原生停顿标签 → 省略号近似（v3 不支持 SSML break，官方推荐省略号）
        "[laughs]": "[laughs]", "[sighs]": "[sighs]", "[whispers]": "[whispers]",
        "(pause)": "...", "(sighs)": "[sighs]", "(laughs)": "[laughs]",
    },
    "minimax": {
        # MiniMax t2a_v2 原生停顿标记 <#秒#>（0.01~3s）；笑声类暂转文本，比武时校准
        "(pause)": "<#0.6#>", "[laughs]": "哈哈", "(laughs)": "哈哈",
        "[sighs]": "唉", "(sighs)": "唉", "[whispers]": "",
    },
    "local": {
        # sherpa-onnx VITS：无语气标签，全部转成文本/标点
        "(pause)": "。", "[laughs]": "哈哈", "(laughs)": "哈哈",
        "[sighs]": "唉", "(sighs)": "唉", "[whispers]": "",
    },
    "mock": {},
}

# 全角语气括号 → 拟声词（qwen 等模型爱写（笑）（哈哈），VITS 念括号会出怪音）
_CN_MOOD = {
    "（笑）": "哈哈", "（哈哈）": "哈哈", "（呵呵）": "哈哈",
    "（嘿嘿）": "嘿嘿", "（叹气）": "唉", "（哎）": "唉",
    "（微笑）": "嘿嘿", "（停顿）": "。", "（认真）": "",
    "（温柔）": "", "（轻声）": "", "（害羞）": "哈哈",
}


def split_for_tts(text: str, provider: str | None = None) -> tuple[str, str]:
    """按协议切分整段回复 → (tts_text, caption_text)。
    顺序铁律：markdown 清理必须在方言转换之前（否则会吃掉 MiniMax 的 <#0.6#> 原生标记）。"""
    provider = provider or TTS_PROVIDER
    dialect = _TTS_DIALECT.get(provider, {})

    quoted = re.findall(r'"([^"]*)"', text)
    spoken = " ".join(q.strip() for q in quoted if q.strip()) if quoted else text

    spoken = re.sub(r"[*_`#>]+", "", spoken)
    for src, dst in dialect.items():
        spoken = spoken.replace(src, dst)
    for k, v in _CN_MOOD.items():  # 全角语气括号转拟声（源头是 LLM 自发输出）
        spoken = spoken.replace(k, v)
    if provider != "elevenlabs":  # 非直通厂商：剥掉残余的方括号标签，防被念出来
        spoken = re.sub(r"\[[^\]\n]{1,20}\]", "", spoken)
    # 其余全角括号：去掉括号只留内容（防念出"（""）"符号的怪音）
    spoken = re.sub(r"（([^）]{1,8})）", r"\1", spoken)
    spoken = spoken.strip()

    caption = re.sub(r"\[[^\]\n]{1,20}\]", "", text)
    caption = re.sub(r"\([^)\n]{1,16}\)", "", caption)
    caption = re.sub(r"[*_`#>]+", "", caption)
    return spoken, caption


class LineSegmenter:
    """V2 流式 TTS 的增量切句器（COVE §12）：网关 SSE 增量喂进来，切出"可说单元"立刻回调。
    K 的输出协议天然按行分句（一行 = "English line." 中文翻译），所以行就是切分单位；
    无换行的多句长段按句末标点切（min_sentence_chars 内不切，防"好。"单字成段），
    240 字强切兜底（措辞异常时仍能流水出声，不至于憋到全量）。段间顺序由消费方保证（串行下发）。"""

    _SENT_END = "。！？!?…"
    _CLOSERS = "\"'“”」』）)》"   # 句末标点后跟的收尾引号/括号归入本段，防下一段以裸引号开头破坏引号配对

    def __init__(self, max_chars: int = 240, min_sentence_chars: int = 6):
        self.buf = ""
        self.max_chars = max_chars
        self.min_sentence_chars = min_sentence_chars

    def _sentence_cut(self) -> int:
        """buf 里第一个可切位置（句末标点+连续标点+收尾引号之后）；不满足最短句长返回 -1。"""
        for i, ch in enumerate(self.buf):
            if ch in self._SENT_END and i + 1 >= self.min_sentence_chars:
                j = i
                while j + 1 < len(self.buf) and (self.buf[j + 1] in self._SENT_END
                                                 or self.buf[j + 1] in self._CLOSERS):
                    j += 1
                return j + 1
        return -1

    def feed(self, delta: str) -> list[str]:
        self.buf += delta
        segs: list[str] = []
        while True:
            if "\n" in self.buf:
                line, self.buf = self.buf.split("\n", 1)
                if line.strip():
                    segs.append(line.strip())
                continue
            cut = self._sentence_cut()
            if cut > 0:
                seg, self.buf = self.buf[:cut], self.buf[cut:]
                if seg.strip():
                    segs.append(seg.strip())
                continue
            break
        if len(self.buf) >= self.max_chars:      # 措辞异常兜底：无标点长段强切
            if self.buf.strip():
                segs.append(self.buf.strip())
            self.buf = ""
        return segs

    def flush(self) -> str | None:
        out = self.buf.strip()
        self.buf = ""
        return out or None
