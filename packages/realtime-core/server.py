#!/usr/bin/env python3
"""PaiVoice realtime core — Jester 魔改版（电话项目，2026-09-01）。

原版：model-neutral 通话核心（PCM16 → ASR → Adapter → TTS → SSE/WS 回传）。
魔改三处：
  1. 耳朵：ASR 新增 siliconflow（SenseVoiceSmall，中文主赛道）
  2. 大脑：Adapter 新增 gateway 模式——把转录组装成 OpenAI 格式 POST 给
     网关语音快车道（/v1/chat/completions + call_session_id + pai-voice UA），
     消费其 SSE 流聚合为整段回复。人格/记忆/通话历史全部由网关侧负责，
     本进程不存对话历史（单一事实源在网关通话缓存）。
  3. 嘴：TTS 保留 elevenlabs（主赛道），minimax 留桩（M1 后接）。
新增：挂断时把通话全文 POST 到网关 /v1/voice/archive 归档（K 自己写摘要）。
隐私不变：真 Key 只在服务端环境变量，浏览器不持有任何供应商密钥。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import shutil
import struct
from enum import Enum

from cleanse import split_for_tts, LineSegmenter
import time
import uuid
import wave
from dataclasses import dataclass, field

import aiohttp
from websockets.asyncio.server import serve

SAMPLE_RATE = 16_000
HOST = os.getenv("PAIVOICE_HOST", "127.0.0.1")
PORT = int(os.getenv("PAIVOICE_PORT", "8780"))
TOKEN = os.getenv("PAIVOICE_TOKEN", "")
ASR_PROVIDER = os.getenv("PAIVOICE_ASR_PROVIDER", "mock")          # mock | groq | siliconflow | local
TTS_PROVIDER = os.getenv("PAIVOICE_TTS_PROVIDER", "mock")          # mock | elevenlabs | minimax(桩) | local
ASR_KEY = os.getenv("PAIVOICE_ASR_API_KEY") or os.getenv("GROQ_API_KEY", "")
TTS_KEY = os.getenv("PAIVOICE_TTS_API_KEY") or os.getenv("ELEVENLABS_API_KEY", "")
GROQ_MODEL = os.getenv("PAIVOICE_GROQ_ASR_MODEL", "whisper-large-v3-turbo")
# 本地语音边车（local_voice.py）：OpenAI 兼容子集端点
LOCAL_ASR_URL = os.getenv("PAIVOICE_LOCAL_ASR_URL", "http://127.0.0.1:8792/v1/audio/transcriptions")
LOCAL_TTS_URL = os.getenv("PAIVOICE_LOCAL_TTS_URL", "http://127.0.0.1:8792/v1/audio/speech")
ELEVEN_VOICE = os.getenv("PAIVOICE_ELEVEN_VOICE_ID", "")
ELEVEN_MODEL = os.getenv("PAIVOICE_ELEVEN_MODEL", "eleven_multilingual_v2")  # v3 填 eleven_v3
# v3 专属：stability 三档 Creative(0.0 最有表现力)/Natural(0.5 均衡)/Robust(1.0 最稳)。要 audio tags 表现力选前两档
ELEVEN_STABILITY = os.getenv("PAIVOICE_ELEVEN_STABILITY", "")

# 大脑：网关语音快车道（OpenAI 兼容 + SSE）。UA 必须带 pai-voice，网关靠它分流。
GATEWAY_URL = os.getenv("PAIVOICE_GATEWAY_URL", "")      # 可在设置面板运行时更换（config_set）
GATEWAY_TOKEN = os.getenv("PAIVOICE_GATEWAY_TOKEN", "")  # 云端端点（DeepSeek/OpenAI 等）填 API Key
# 可选系统提示词（人格/润色要求）。原网关模式不设 system（人格在网关侧注入）；
# 这里只给"直连普通 OpenAI 兼容 LLM（如本地 Ollama）"的搭建场景用。
SYSTEM_PROMPT = os.getenv("PAIVOICE_SYSTEM_PROMPT", "")
# 人设定制（称呼/语气/风格）：_persona_state 在下方人设函数区初始化（读盘），设置面板热改
# 直连 OpenAI 兼容端点时显式指定模型名（Ollama 必需）；网关模式可留空由网关路由
LLM_MODEL = os.getenv("PAIVOICE_LLM_MODEL", "")   # 可在设置面板运行时更换（config_set）
# 语音边车（local_voice.py）根地址：由 LOCAL_TTS_URL 推导
SIDECAR_BASE = LOCAL_TTS_URL.rsplit("/v1/", 1)[0] if LOCAL_TTS_URL else "http://127.0.0.1:8792"

# --- API 配置档案（设置面板多配置管理；持久化到 config/api_profiles.json，重启保留）---
_prof_root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "config"))
PROFILE_FILE = os.getenv("PAIVOICE_PROFILE_FILE", os.path.join(_prof_root, "api_profiles.json"))


def _load_profiles() -> dict:
    try:
        with open(PROFILE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"profiles": [], "active": ""}


def _save_profiles(profiles: dict) -> None:
    tmp = PROFILE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PROFILE_FILE)

# --- 人设定制（聊天语气 / 聊天风格 / 称呼）---
# 数据形态：
#   pet_name   她的昵称——他每轮都要这样叫她（"叫xxx"）
#   his_name   他的名字——让她有名字可喊（"我叫他xxx"）
#   tone       聊天语气预设键（TONE_PRESETS）；"custom" 时用 tone_free
#   style      聊天风格预设键（STYLE_PRESETS）；"custom" 时用 style_free
# 自由文本与预设可叠加。持久化到 config/persona.json，设置面板热改，重启保留。
PERSONA_FILE = os.getenv("PAIVOICE_PERSONA_FILE", os.path.join(_prof_root, "persona.json"))
TONE_PRESETS = {
    "default": "",
    "gentle":  "语气温柔体贴，轻声细语，像哄人入睡一样柔和。",
    "tender":  "语气黏人撒娇，偶尔拖长尾音，像女朋友打电话那样亲昵。",
    "lively":  "语气活泼元气，语速轻快，充满活力，爱开玩笑。",
    "calm":    "语气平静沉稳，从容淡定，给人可靠的安全感。",
    "humorous": "语气幽默搞笑，爱玩梗抖包袱，正经不过三秒。",
    "cold":    "语气高冷傲娇，嘴上嫌弃心里在乎，话少但句句戳心。",
    "custom":  "",
}
STYLE_PRESETS = {
    "default": "",
    "intimate": "像热恋期的情侣煲电话粥，聊日常琐事也带着甜蜜。",
    "playful":  "像青梅竹马斗嘴打闹，互怼互损但气氛轻松愉快。",
    "caring":   "像贴心家人嘘寒问暖，关心吃饭睡觉天气和心情。",
    "story":    "爱用讲故事和画面感的说法，把日常说成小剧场。",
    "concise":  "惜字如金，每句都短促有力，绝不啰嗦。",
    "custom":   "",
}
_TONE_LABEL = {k: ("自定义" if k == "custom" else (v.strip("。")[:10] or "默认")) for k, v in TONE_PRESETS.items()}
_STYLE_LABEL = {k: ("自定义" if k == "custom" else (v.strip("。")[:10] or "默认")) for k, v in STYLE_PRESETS.items()}


def _load_persona() -> dict:
    p = {"pet_name": "", "his_name": "", "tone": "default", "style": "default",
         "tone_free": "", "style_free": ""}
    try:
        with open(PERSONA_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            for k in p:
                if k in ("tone", "style"):
                    v = str(saved.get(k, p[k]) or p[k])
                    p[k] = v if v in (TONE_PRESETS if k == "tone" else STYLE_PRESETS) else "default"
                else:
                    p[k] = str(saved.get(k, "") or "")[:50]
    except Exception:
        pass
    return p


def _save_persona(p: dict) -> None:
    tmp = PERSONA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PERSONA_FILE)


def _persona_directive(p: dict) -> str:
    """人设 → system 指令：称呼规则（硬要求：每轮都要叫）+ 语气/风格描述。
    选预设用预设文案，选 custom 用自填文本；无任何定制时返回空串，不打扰原 SYSTEM_PROMPT。"""
    rules: list[str] = []
    pet = (p.get("pet_name") or "").strip()
    his = (p.get("his_name") or "").strip()
    if pet:
        rules.append(f"用户的昵称是「{pet}」。每次回复都必须用「{pet}」称呼用户，"
                     f"自然地融进句子里，不要每句都生硬地喊名字，但每一轮回复都不能漏掉。")
    if his:
        rules.append(f"你自己的名字是「{his}」，用户会用「{his}」叫你。")
    if p.get("tone") == "custom":
        tone = (p.get("tone_free") or "").strip()
    else:
        tone = (TONE_PRESETS.get(p.get("tone")) or "").strip()
    if tone:
        rules.append(f"聊天语气：{tone}")
    if p.get("style") == "custom":
        style = (p.get("style_free") or "").strip()
    else:
        style = (STYLE_PRESETS.get(p.get("style")) or "").strip()
    if style:
        rules.append(f"聊天风格：{style}")
    if not rules:
        return ""
    return _PERSONA_HEADER + " ".join(rules)


_PERSONA_HEADER = "【聊天语气与风格定制】"


# 启动读盘，此后内存为准（config_set 即写回）
_persona_state = _load_persona()

CLIENT_UA = "pai-voice/0.1 (jester-build)"
# 兼容模式（Gateway 未配置时的原 Adapter 路线）；GATEWAY_URL 优先
ADAPTER_URL = os.getenv("PAIVOICE_ADAPTER_URL", "")
ADAPTER_TOKEN = os.getenv("PAIVOICE_ADAPTER_TOKEN", "")

# 归档：挂断后通话全文回传网关，K 自己写摘要进记忆（C3 拍板）
ARCHIVE_URL = os.getenv("PAIVOICE_ARCHIVE_URL", "")
# 逐轮实时落盘（Supabase voice_call_turns）：断线/崩溃零丢失——每轮转写与回复即写
SB_URL = os.getenv("PAIVOICE_SB_URL", "")
SB_KEY = os.getenv("PAIVOICE_SB_KEY", "")
MAX_TURN_SECONDS = int(os.getenv("PAIVOICE_MAX_TURN_SECONDS", "60"))

# --- 通话记忆（MySQL，hongfeng 连接）---
# 直连普通 OpenAI 兼容 LLM（本地 Ollama 等）没有网关侧记忆，模型每轮天然失忆。
# 把每轮对话落 MySQL、请求时回放最近历史作上下文——记忆跨通话、跨重启。
# PAIVOICE_MYSQL_HOST 留空 = 不启用（零依赖，原行为不变）。
from concurrent.futures import ThreadPoolExecutor
import pymysql

MYSQL_HOST = os.getenv("PAIVOICE_MYSQL_HOST", "")
MYSQL_PORT = int(os.getenv("PAIVOICE_MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("PAIVOICE_MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("PAIVOICE_MYSQL_PASSWORD", "")
MYSQL_DB = os.getenv("PAIVOICE_MYSQL_DB", "paivoice")
MEMORY_TURNS = int(os.getenv("PAIVOICE_MEMORY_TURNS", "30"))  # 回放最近 N 条（user+assistant 合计）
# 全部 MySQL 操作收拢到单线程：连接池无锁，user/assistant 两落库天然串行
# （并发 INSERT 带 MAX 子查询会在 InnoDB 上互锁，1213 死锁实测抓过）
_mem_exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="paivoice-mem")
_mem_pool: list = []  # 极简连接池（只在 _mem_exec 线程里取用还）


def _mem_enabled() -> bool:
    return bool(MYSQL_HOST and MYSQL_DB)


def _mem_conn():
    """池里取一条连接（ping 复活断链）；池空则新建。只在 _mem_exec 线程跑。"""
    while _mem_pool:
        conn = _mem_pool.pop()
        try:
            conn.ping(reconnect=True)
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
    return pymysql.connect(
        host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER, password=MYSQL_PASSWORD,
        database=MYSQL_DB, charset="utf8mb4", autocommit=True,
        connect_timeout=3, read_timeout=5, write_timeout=5,
    )


def _mem_release(conn) -> None:
    """用完归还池（上限 4）；拿不出手的直接丢弃。只在 _mem_exec 线程跑。"""
    if conn is None:
        return
    if len(_mem_pool) < 4:
        _mem_pool.append(conn)
    else:
        try:
            conn.close()
        except Exception:
            pass


def _mem_write_turn(call_id: str, turn_id: str, role: str, text: str) -> None:
    """借连接 → 落一行 → 还连接，整个过程独占（死锁免疫）。
    1213 可重试：INSERT 带 MAX 子查询在 InnoDB 上偶发互锁（外部客户端持锁也会触发），
    服务端报错原文就写着 try restarting transaction——重试一次几乎必成。"""
    for attempt in (1, 2):
        conn = _mem_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO voice_call_turns (call_session_id, turn_seq, turn_id, role, content) "
                    "VALUES (%s, (SELECT COALESCE(MAX(t.turn_seq), 0) + 1 FROM "
                    "(SELECT turn_seq FROM voice_call_turns WHERE call_session_id=%s) t), %s, %s, %s)",
                    (call_id, call_id, turn_id, role, text[:4000]))
            return
        except pymysql.err.OperationalError as e:
            if e.args and e.args[0] == 1213 and attempt == 1:
                continue          # 死锁：回连接池重抽一条再来（连接已在 finally 归还）
            raise
        finally:
            _mem_release(conn)


async def _mem_save_turn(call_id: str, turn_id: str, role: str, text: str) -> None:
    """单轮落库。记忆是锦上添花：失败只留日志，绝不影响通话主流程。"""
    if not _mem_enabled() or not text:
        return
    try:
        await asyncio.get_event_loop().run_in_executor(
            _mem_exec, _mem_write_turn, call_id, turn_id, role, text)
    except Exception as e:
        print(f"[memory] save failed: {e}", flush=True)


def _mem_load_history(call_id: str) -> list[dict]:
    """回放最近 MEMORY_TURNS 条（id 升序）拼成 messages 历史。
    跨通话回放（不按 call_session_id 过滤）：每次拨号都是新 session，按 session 过滤
    等于一挂断就清零——长久记忆要的就是"她上次说过的事这次还记得"。call_id 参数保留
    是为了日志可读；当前通话的早前轮次也已实时落库，会自然出现在回放里。
    同步毫秒级查询，仅新轮开始时调一次（经 _mem_exec 线程）。"""
    if not _mem_enabled():
        return []
    conn = None
    try:
        conn = _mem_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT role, content FROM ("
                "  SELECT id, role, content FROM voice_call_turns"
                "  ORDER BY id DESC LIMIT %s"
                ") recent ORDER BY id ASC", (MEMORY_TURNS,))
            return [{"role": r, "content": c} for r, c in cur.fetchall()]
    except pymysql.err.OperationalError as e:
        if e.args and e.args[0] == 1213:   # 死锁：记忆回放是锦上添花，本轮少带历史不影响通
            print("[memory] load deadlock, skip this turn's history", flush=True)
            return []
        print(f"[memory] load failed: {e}", flush=True)
        return []
    except Exception as e:
        print(f"[memory] load failed: {e}", flush=True)
        return []
    finally:
        _mem_release(conn)

# 自然挂断（COVE §16 / M2）：告别词命中 → 正常生成告别回复 → 前端播完（playback_idle）+
# 宽限期她没再开口 → 请前端挂断收线；全程硬截止，超时强制关连接（归档统一走 finally）。
HANGUP_GRACE_MS = int(os.getenv("PAIVOICE_HANGUP_GRACE_MS", "3500"))
HANGUP_DEADLINE_S = float(os.getenv("PAIVOICE_HANGUP_DEADLINE_S", "30"))
FAREWELL_RE = re.compile(
    r"(先挂了|挂了哈|挂了吧|挂断了|那我挂|拜拜|再见|晚安|先睡了|睡了哈|先这样|去忙了|先去忙|上班去了|干活去了)")
# 反例保护："别挂/不许挂"含"挂"字绝不能当告别。
# 注意"不聊了/不说了"故意不收——话题转换也这么说，误挂比漏挂（她手动挂）事故得多
FAREWELL_NEG_RE = re.compile(r"(别挂|不许挂|不准挂|不要挂|不能挂|谁挂|还没挂|没挂)")

# V2 流式 TTS 总开关（B6 三保险精神）：SSE 增量→切句→逐段合成下发，首句不再等全量。
# 出问题 env 置 0 秒回整段老路（代码保留原路径为回落）。
STREAM_TTS = os.getenv("PAIVOICE_STREAM_TTS", "1") == "1"

_adapter_sem = asyncio.Semaphore(1)  # 同一时刻只投递一轮，避免两句转录并发进网关

# ASR 幻听过滤（2026-09-03）：静音/呼吸/摩擦声被 SenseVoice 脑补成单字碎片
# （"嗯""句号"之类）。命中即整轮丢弃——她随口一声"嗯"本就不该让他接话，
# 与闻序三级打断里"短促附和不打断"同理。只作用于语音路径，打字内容不过滤。
ASR_HALLUCINATION_MULTI = {
    "嗯嗯", "嗯嗯嗯", "啊啊", "句号", "逗号", "问号", "感叹号", "省略号",
    "谢谢观看", "谢谢收看", "谢谢大家", "请不吝点赞", "订阅", "关注我们",
    # 英文幻听词（匹配前已去空格去标点并转小写，故 here 无空格）
    "um", "uh", "hm", "mm", "hmm", "mhm", "huh", "bye", "you",
    "thankyou", "thanksforwatching",
}
MIN_SPEECH_RMS = 150  # int16 满量程 32767；低于此当环境音丢弃（比前端 VAD 门限还低，双保险）


def _pcm_rms(pcm: bytes) -> float:
    """整段 PCM16 的均方根音量（抽样步长 8，60s 音频也在毫秒级算完）。"""
    import array
    samples = array.array("h")
    samples.frombytes(pcm[: (len(pcm) // 2) * 2])
    if not samples:
        return 0.0
    picked = samples[::8]
    return (sum(s * s for s in picked) / len(picked)) ** 0.5


def _is_hallucination(text: str) -> bool:
    """去标点后空串或单字碎片 → 幻听；多字短语再对黑名单核对一遍。"""
    t = re.sub(r"[，。！？、,.!?~～…\s]", "", text or "").lower()
    if not t or len(t) <= 1:
        return True
    return t in ASR_HALLUCINATION_MULTI


def wav(pcm: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(pcm)
    return buffer.getvalue()


async def transcribe(http: aiohttp.ClientSession, pcm: bytes) -> str:
    """Return text only. Provider errors are intentionally safe to show."""
    if ASR_PROVIDER == "mock":
        return ""
    if ASR_PROVIDER != "local" and not ASR_KEY:  # local 边车不需要云端 key
        raise RuntimeError("ASR provider is not configured")

    form = aiohttp.FormData()
    form.add_field("file", wav(pcm), filename="turn.wav", content_type="audio/wav")
    form.add_field("language", "zh")

    if ASR_PROVIDER == "siliconflow":
        form.add_field("model", os.getenv("PAIVOICE_SILICONFLOW_ASR_MODEL",
                                          "FunAudioLLM/SenseVoiceSmall"))
        url = "https://api.siliconflow.cn/v1/audio/transcriptions"
    elif ASR_PROVIDER == "groq":
        form.add_field("model", GROQ_MODEL)
        url = "https://api.groq.com/openai/v1/audio/transcriptions"
    elif ASR_PROVIDER == "local":
        url = LOCAL_ASR_URL  # 本地语音边车（local_voice.py，sherpa-onnx SenseVoiceSmall）
    else:
        raise RuntimeError("ASR provider is not configured")

    async with http.post(url, data=form, headers={"Authorization": f"Bearer {ASR_KEY}"}) as response:
        if response.status != 200:
            raise RuntimeError(f"ASR request failed ({response.status})")
        return str((await response.json()).get("text", "")).strip()


async def _ollama_models(http: aiohttp.ClientSession) -> list[str]:
    """从本地 Ollama 拉模型列表（设置面板用）；失败返回空。"""
    try:
        async with http.get("http://localhost:11434/api/tags",
                            timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status == 200:
                js = await r.json()
                return [m.get("name", "") for m in js.get("models", []) if m.get("name")]
    except Exception:
        pass
    return []


async def _sidecar_voices(http: aiohttp.ClientSession) -> dict | None:
    """查询语音边车音色列表；不可达返回 None。"""
    try:
        async with http.get(SIDECAR_BASE + "/v1/tts/models",
                            timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status == 200:
                return await r.json()
    except Exception:
        pass
    return None


async def _sidecar_set_voice(http: aiohttp.ClientSession, name: str, sid: int | None = None) -> tuple[int, dict]:
    """切换音色（边车热重载）。返回 (status, json)。"""
    payload: dict = {"model": name}
    if sid is not None:
        payload["sid"] = sid
    try:
        async with http.post(SIDECAR_BASE + "/v1/tts/model", json=payload,
                             timeout=aiohttp.ClientTimeout(total=30)) as r:
            return r.status, await r.json()
    except Exception as e:
        return 0, {"error": str(e)}


async def _sidecar_set_sid(http: aiohttp.ClientSession, sid: int) -> tuple[int, dict]:
    """切换说话人编号（边车秒切，不重载模型）。"""
    try:
        async with http.post(SIDECAR_BASE + "/v1/tts/sid", json={"sid": sid},
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
            return r.status, await r.json()
    except Exception as e:
        return 0, {"error": str(e)}


async def _test_endpoint(http: aiohttp.ClientSession, url: str, key: str, model: str) -> dict:
    """测试 OpenAI 兼容端点连通性（非流式小请求，验证地址/Key/模型）。"""
    url = _normalize_gateway_url(url)  # 基础地址自动补 /chat/completions，与通话请求一致
    t0 = time.time()
    headers = {"content-type": "application/json"}
    if key:
        headers["authorization"] = f"Bearer {key}"
    payload = {
        "model": model or "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": "用两个字回复：好"}],
        "stream": False,
        "max_tokens": 16,
    }
    try:
        async with http.post(url, json=payload, headers=headers,
                             timeout=aiohttp.ClientTimeout(total=12)) as r:
            lat = int((time.time() - t0) * 1000)
            if r.status != 200:
                body = (await r.text())[:200]
                return {"ok": False, "error": f"HTTP {r.status}: {body}", "latency_ms": lat}
            try:
                js = await r.json()
            except Exception:
                return {"ok": False, "error": "响应不是合法 JSON", "latency_ms": lat}
            choices = js.get("choices") or []
            reply = ""
            if choices:
                msg = choices[0].get("message") or {}
                reply = str(msg.get("content") or "").strip()
            if not choices:
                return {"ok": False,
                        "error": "响应里没有 choices（URL 可能少了 /chat/completions，或该端点不是 OpenAI 兼容格式）",
                        "latency_ms": lat}
            return {"ok": True, "reply": reply[:60],
                    "model": js.get("model") or model, "latency_ms": lat}
    except Exception as e:
        lat = int((time.time() - t0) * 1000)
        return {"ok": False, "error": str(e)[:150], "latency_ms": lat}


def _persona_snapshot() -> dict:
    """人设当前值 + 面板可选项（预设键与文案），config 快照/回执统一走这里。"""
    return {
        "pet_name": _persona_state["pet_name"],
        "his_name": _persona_state["his_name"],
        "tone": _persona_state["tone"],
        "style": _persona_state["style"],
        "tone_free": _persona_state["tone_free"],
        "style_free": _persona_state["style_free"],
        "tone_options": [{"key": k, "text": v} for k, v in TONE_PRESETS.items()],
        "style_options": [{"key": k, "text": v} for k, v in STYLE_PRESETS.items()],
    }


def _apply_persona_patch(patch: dict) -> tuple[bool, str]:
    """校验并应用人设补丁（部分字段可选）。返回 (ok, err)。"""
    if not isinstance(patch, dict):
        return False, "persona 必须是对象"
    for key in ("pet_name", "his_name", "tone_free", "style_free"):
        if key in patch:
            _persona_state[key] = str(patch.get(key) or "").strip()[:50]
    for key, presets in (("tone", TONE_PRESETS), ("style", STYLE_PRESETS)):
        if key in patch:
            v = str(patch.get(key) or "default").strip()
            if v not in presets:
                return False, f"不支持的{ '语气' if key == 'tone' else '风格' }预设: {v}"
            _persona_state[key] = v
    _save_persona(_persona_state)
    return True, ""


async def _config_snapshot(http: aiohttp.ClientSession) -> dict:
    """当前配置快照（设置面板）：大脑端点/模型/Key + 音色/说话人 + 提示词 + 人设 + 组件类型 + 配置档案。"""
    voices = await _sidecar_voices(http)
    prof = _load_profiles()
    # system_prompt 回显带人设指令的"最终生效版"：面板改完人设立刻能核对措辞
    persona_sys = _persona_directive(_persona_state)
    final_prompt = (persona_sys + ("\n" if persona_sys and SYSTEM_PROMPT else "") + SYSTEM_PROMPT).strip()
    return {
        "type": "config",
        "gateway_url": GATEWAY_URL,
        "gateway_token": GATEWAY_TOKEN,
        "llm_model": LLM_MODEL,
        "llm_models": await _ollama_models(http) if _is_local_ollama(GATEWAY_URL) else [],
        "voice": voices.get("current", "") if voices else "",
        "voice_sid": voices.get("current_sid", 0) if voices else 0,
        "voices": voices.get("models", []) if voices else [],
        "speakers": voices.get("speakers", {}) if voices else {},
        "system_prompt": final_prompt,
        "persona": _persona_snapshot(),
        "asr": ASR_PROVIDER,
        "tts": TTS_PROVIDER,
        "profiles": prof.get("profiles", []),
        "active_profile": prof.get("active", ""),
    }


def _is_local_ollama(url: str) -> bool:
    """判断大脑端点是否为本地 Ollama（模型可枚举校验；云端端点无法枚举）。"""
    return url.startswith("http://localhost:11434") or url.startswith("http://127.0.0.1:11434")


def _normalize_gateway_url(url: str) -> str:
    """OpenAI 兼容聊天端点归一化：基础地址（如 …/v1）自动补 /chat/completions。
    否则原样 POST 到 /v1 根路径会被上游 404 拒绝（Invalid URL (POST /v1)）。"""
    url = (url or "").strip()
    if not url:
        return url
    url = url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    return url


async def _call_gateway(http: aiohttp.ClientSession, turn: dict, metrics: dict | None = None,
                        on_segment=None) -> str:
    """大脑：把转录 POST 给网关语音快车道，消费 OpenAI SSE 聚合为整段回复。
    网关侧负责：人格注入 / 通话缓存 / 意图分流 / 记忆检索。本函数只当传声筒。
    metrics 非 None 时记录 gateway_first_at / gateway_done_at（分阶段指标，M1.5）。
    on_segment 非 None（V2 流式）：SSE 增量喂切句器，每切出一段就 await 回调——
    首句不等全量（COVE §12）；返回值仍是完整整段（归档/落盘语义不变）。"""
    headers = {
        "authorization": f"Bearer {GATEWAY_TOKEN}",
        "content-type": "application/json",
        "user-agent": CLIENT_UA,
    }
    messages: list[dict] = []
    # 人设指令（称呼/语气/风格）在最前，用户自己的 SYSTEM_PROMPT 随后——两者都算 system 层
    persona_sys = _persona_directive(_persona_state)
    if persona_sys:
        messages.append({"role": "system", "content": persona_sys})
    if SYSTEM_PROMPT:
        messages.append({"role": "system", "content": SYSTEM_PROMPT})
    # 通话记忆：直连 LLM（Ollama 等）时网关不帮忙记历史，从 MySQL 回放最近几轮，
    # 否则模型每轮都失忆。只对网关模式生效；保存发生在 reply 落定后（见下）。
    history = []
    if _mem_enabled():
        loop = asyncio.get_event_loop()
        # 记忆读写同走 _mem_exec 单线程：回放若用默认池，会和落库并发共用
        # 同一条池化连接，pymysql 协议错位直接崩掉整个 answer_turn 任务
        history = await loop.run_in_executor(_mem_exec, _mem_load_history, turn["call_session_id"])
        messages.extend(history)
        print(f"[memory] replay {len(history)} turns for call={turn['call_session_id']}", flush=True)
    messages.append({"role": "user", "content": turn["transcript"]})
    payload = {
        "call_session_id": turn["call_session_id"],
        "turn_id": turn["turn_id"],
        "messages": messages,
        "stream": True,
        "max_tokens": 384,  # 闲聊短回复用不满；讲笑话/段子等内容需要上百字。
                            # 流式 TTS 边生成边播，长回复不拖首句出声（原 96 会把笑话截成预告）
    }
    if LLM_MODEL:  # 直连 OpenAI 兼容端点（如本地 Ollama）时显式指定模型
        payload["model"] = LLM_MODEL
    parts: list[str] = []
    first_at = None
    segmenter = LineSegmenter() if on_segment is not None else None

    async def _emit(line: str) -> None:
        if on_segment is not None:
            await on_segment(line)

    async with http.post(_normalize_gateway_url(GATEWAY_URL), json=payload, headers=headers,
                         timeout=aiohttp.ClientTimeout(total=120, sock_read=90)) as response:
        if response.status != 200:
            body = (await response.text())[:300]
            raise RuntimeError(f"Gateway request failed ({response.status}): {body}")
        ctype = (response.headers.get("content-type") or "").lower()
        if "text/event-stream" in ctype:
            async for raw in response.content:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    piece = json.loads(data)["choices"][0].get("delta", {}).get("content") or ""
                except Exception:
                    continue
                if piece:
                    if first_at is None:
                        first_at = time.time()
                        if metrics is not None:
                            metrics["gateway_first_at"] = first_at
                    parts.append(piece)
                    if segmenter is not None:
                        for seg in segmenter.feed(piece):
                            await _emit(seg)
        else:
            # 聚合类端点可能无视 stream=True 返回普通 JSON：按非流式解析
            try:
                js = await response.json()
            except Exception:
                raise RuntimeError(f"Gateway returned non-JSON (content-type: {ctype})")
            piece = ((js.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            if piece:
                first_at = time.time()
                if metrics is not None:
                    metrics["gateway_first_at"] = first_at
                parts.append(piece)
                if segmenter is not None:
                    for seg in segmenter.feed(piece):
                        await _emit(seg)
        if segmenter is not None:
            tail = segmenter.flush()
            if tail:
                await _emit(tail)
    if not parts:
        raise RuntimeError(f"Gateway returned empty content (status 200, type={ctype})")
    done_at = time.time()
    if metrics is not None:
        metrics["gateway_done_at"] = done_at
    reply = "".join(parts).strip()
    # 防呆：剥掉模型思考块（qwen3 等默认 thinking 模型的 ＜think＞…＜/think＞），
    # 否则思考内容会被 TTS 念出来。全角/半角尖括号都处理。
    reply = re.sub(r"[<＜]think[>＞].*?[<＜]/think[>＞]", "", reply, flags=re.S).strip()
    # 记忆写入：她这一问 + 他这一答 成对落 MySQL（后台任务，不阻塞返回）
    if _mem_enabled():
        asyncio.get_event_loop().create_task(_mem_save_turn(
            turn["call_session_id"], turn["turn_id"], "user", turn["transcript"]))
        asyncio.get_event_loop().create_task(_mem_save_turn(
            turn["call_session_id"], turn["turn_id"], "assistant", reply))
    return reply


async def request_reply(http: aiohttp.ClientSession, turn: dict, metrics: dict | None = None,
                        on_segment=None) -> str:
    """路由：配置了 GATEWAY_URL 走网关快车道；否则兼容原 Adapter 协议。
    on_segment 仅网关路径支持（V2 流式）；Adapter/兜底路径忽略之。"""
    if GATEWAY_URL:
        async with _adapter_sem:
            return await _call_gateway(http, turn, metrics, on_segment=on_segment)

    if not ADAPTER_URL:
        return f"我听见了：{turn['transcript']}" if turn["transcript"] else "我没有听清楚。"
    headers = {"content-type": "application/json"}
    if ADAPTER_TOKEN:
        headers["authorization"] = f"Bearer {ADAPTER_TOKEN}"
    async with http.post(ADAPTER_URL.rstrip("/") + "/turn", json=turn, headers=headers,
                         timeout=aiohttp.ClientTimeout(total=120)) as response:
        if response.status != 200:
            raise RuntimeError(f"Adapter request failed ({response.status})")
        result = await response.json()
    return str(result.get("reply", "")).strip()


# 语气中间协议 → 各家 TTS 方言映射（K 在措辞里只用中间协议，方言由清洗层转译；
# 换 TTS 厂商只改这里，措辞零改动。语法依据各官方文档，比武时逐家实测校准。）
async def synthesize(http: aiohttp.ClientSession, text: str, metrics: dict | None = None) -> bytes | None:
    """TTS：主赛道 elevenlabs；minimax 为 M1 后桩位。
    没有 TTS provider 时仍回传文本（字幕先行）。
    metrics 非 None 时记录 tts_request_at / tts_first_byte_at / tts_done_at（M1.5）。"""
    if TTS_PROVIDER == "mock" or not text:
        return None
    if metrics is not None:
        metrics["tts_request_at"] = time.time()
    if TTS_PROVIDER == "elevenlabs":
        if not TTS_KEY or not ELEVEN_VOICE:
            raise RuntimeError("TTS provider is not configured")
        headers = {"xi-api-key": TTS_KEY, "accept": "audio/mpeg", "content-type": "application/json"}
        body = {"text": text, "model_id": ELEVEN_MODEL}
        if ELEVEN_MODEL == "eleven_v3" and ELEVEN_STABILITY:
            # v3 只收 stability（不支持 v2 的 similarity/style 等设置）
            body["voice_settings"] = {"stability": float(ELEVEN_STABILITY)}
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}/stream"
        async with http.post(url, headers=headers, json=body) as response:
            if response.status != 200:
                raise RuntimeError(f"TTS request failed ({response.status})")
            first = True
            chunks: list[bytes] = []
            async for chunk in response.content.iter_chunked(16384):
                if first and metrics is not None:
                    metrics["tts_first_byte_at"] = time.time()
                    first = False
                chunks.append(chunk)
            done = time.time()
            if metrics is not None:
                metrics["tts_done_at"] = done
            audio = b"".join(chunks)
            return audio if audio else None
    if TTS_PROVIDER == "local":
        # 本地语音边车（local_voice.py，sherpa-onnx VITS 中文）
        url = LOCAL_TTS_URL
        async with http.post(url, json={"text": text}) as response:
            if response.status != 200:
                raise RuntimeError(f"TTS request failed ({response.status})")
            audio = await response.read()
            return audio if audio else None
    if TTS_PROVIDER == "minimax":
        raise RuntimeError("minimax TTS 桩位：M1 后接（t2a_v2，需 GROUP_ID）")
    raise RuntimeError("TTS provider is not configured")


async def archive_call(http: aiohttp.ClientSession, call_id: str, turns: list, duration_ms: int) -> bool:
    """挂断归档：通话全文回传网关 /v1/voice/archive（C3：K 自己写摘要进记忆）。
    返回 True=归档成功（或未配置归档端点）；False=失败——失败时调用方不得清空 turns。"""
    if not ARCHIVE_URL:
        return True  # 未配置归档视为已处理（本地开发/mock）
    transcript = "\n".join(f"[{r}] {c}" for r, c in turns)
    try:
        async with http.post(ARCHIVE_URL, json={
            "call_session_id": call_id,
            "transcript": transcript,
            "duration_ms": duration_ms,
        }, headers={
            "authorization": f"Bearer {GATEWAY_TOKEN}",
            "content-type": "application/json",
        }, timeout=aiohttp.ClientTimeout(total=30)) as response:
            if response.status != 200:
                print(f"[archive] failed ({response.status})", flush=True)
                return False
            return True
    except Exception as e:
        print(f"[archive] error: {e}", flush=True)
        return False


@dataclass
class _TurnWrite:
    turn_seq: int
    turn_id: str
    role: str
    text: str
    attempts: int = 0


async def _write_turn_row(http: aiohttp.ClientSession, call_id: str, item: _TurnWrite) -> bool:
    """单条落盘（Supabase upsert）。返回 True=成功（或未配置落盘）。"""
    if not (SB_URL and SB_KEY):
        return True  # 未配置落盘视为已处理（本地开发/mock）
    try:
        async with http.post(SB_URL.rstrip("/") + "/rest/v1/voice_call_turns", json={
            "call_session_id": call_id, "turn_id": item.turn_id, "turn_seq": item.turn_seq,
            "role": item.role, "content": item.text[:4000],
        }, headers={
            "apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
            # merge-duplicates=upsert，依赖 voice_call_turns_idem_uidx（call_session_id,turn_id,role）
            "Prefer": "return=minimal,resolution=merge-duplicates",
        }, timeout=aiohttp.ClientTimeout(total=15)) as response:
            if response.status >= 300:
                print(f"[turn-queue] write failed ({response.status}) seq={item.turn_seq}", flush=True)
                return False
            return True
    except Exception as e:
        print(f"[turn-queue] error seq={item.turn_seq}: {e}", flush=True)
        return False


class TurnWriter:
    """M1.5-2 顺序持久化队列：每 Call 一个串行写 worker。
    - 提交方 put_nowait 立即返回——DB 抖动/宕机绝不阻塞网关调用与 TTS 主流程
    - worker 按 turn_seq 入队顺序逐条写；单条失败指数退避重试（1/2/4s，最多 4 次）后
      放弃并留日志，后续轮次继续（读侧按 turn_seq 排序，乱序到达不破序）
    - 幂等：DB 唯一索引 (call_session_id, turn_id, role) + merge-duplicates，重试不产生重复行
    - close()：哨兵+限时等待排空；超时取消——不活过 ClientSession（0 号热修哲学延续）"""
    MAX_ATTEMPTS = 4
    RETRY_BASE_S = 1.0

    def __init__(self, http: aiohttp.ClientSession, call_id: str):
        self.http = http
        self.call_id = call_id
        self.q: asyncio.Queue = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.stopped = False

    def submit(self, turn_seq: int, turn_id: str, role: str, text: str) -> None:
        if self.stopped or not text:
            return
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._worker())
        self.q.put_nowait(_TurnWrite(turn_seq, turn_id, role, text))

    async def close(self, timeout: float = 8.0) -> None:
        self.stopped = True
        if self.task is None:
            return
        self.q.put_nowait(None)  # 哨兵：worker 排空队列后自然退出
        try:
            await asyncio.wait_for(asyncio.shield(self.task), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self.task.cancel()
            try:
                await self.task
            except BaseException:
                pass

    async def _worker(self) -> None:
        while True:
            item: _TurnWrite | None = await self.q.get()
            if item is None:
                return
            while item.attempts < self.MAX_ATTEMPTS:
                item.attempts += 1
                if await _write_turn_row(self.http, self.call_id, item):
                    break
                if item.attempts < self.MAX_ATTEMPTS:
                    await asyncio.sleep(self.RETRY_BASE_S * 2 ** (item.attempts - 1))
            else:
                print(f"[turn-queue] give up: call={self.call_id} "
                      f"seq={item.turn_seq} role={item.role}", flush=True)


async def log_metrics(call: "Call", turn_seq: int, turn_id: str, generation_id: int, metrics: dict) -> None:
    """M1.5 第一步：分阶段延迟指标（纯观测，fire-and-forget）。写 Supabase voice_call_metrics + JSON 日志。"""
    try:
        import datetime
        row = {
            "call_session_id": call.id, "turn_seq": turn_seq, "turn_id": turn_id,
            "generation_id": generation_id,
        }
        # 只对 *_at 时间戳键做 epoch→ISO 转换；其余原样（此前无差别转换把 generation_id 转成了 1970 怪串）
        row.update({
            k: (datetime.datetime.fromtimestamp(v, datetime.timezone.utc).isoformat()
                if v else None) for k, v in metrics.items() if k.endswith("_at")
        })
        print("[metrics] " + json.dumps(row, ensure_ascii=False), flush=True)
        if SB_URL and SB_KEY:
            async with aiohttp.ClientSession() as http:
                await http.post(SB_URL.rstrip("/") + "/rest/v1/voice_call_metrics", json=row, headers={
                    "apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}", "Prefer": "return=minimal",
                }, timeout=aiohttp.ClientTimeout(total=15))
    except Exception as e:
        print(f"[metrics] error: {e}", flush=True)


class CallState(str, Enum):
    """M1.5-3：会话显式状态机（闻序蓝图八态的 M1.5 子集；RECONNECTING 等归 M2 断线续接）。
    状态只描述"此刻谁占着话筒"，接收循环的响应速度与状态无关（生成任务已后台化）。"""
    LISTENING = "listening"            # 通道开放，等她开口
    USER_SPEAKING = "user_speaking"    # 她正在说（收音中）
    K_THINKING = "k_thinking"          # 生成任务在途（ASR/网关/TTS）
    K_SPEAKING = "k_speaking"          # 音频已下发（实际播完由前端播放队列自理）
    ENDING = "ending"                  # 挂断/断线，收尾中


@dataclass
class Call:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    audio: bytearray = field(default_factory=bytearray)
    active: bool = False
    generation: int = 0
    turns: list = field(default_factory=list)          # [(role, text)] 归档用
    turn_seq: int = 0                                  # M1.5：接收本轮时生成（与 generation_id 分开）
    writer: "TurnWriter | None" = None                 # M1.5-2：顺序持久化队列（session() 里创建）
    pending_generation: object = None                  # M1.5-3：在途生成任务（新轮确认有话时才顶替它）
    state: CallState = CallState.LISTENING             # M1.5-3：显式状态机
    started_at: float = field(default_factory=time.time)
    # 自然挂断（COVE §16 / M2）：告别轮标记 + 收线任务 + 前端播放排空时刻
    farewell: bool = False
    hangup_task: "asyncio.Task | None" = None
    playback_idle_at: float = 0.0

    def set_state(self, s: CallState) -> None:
        if s is not self.state:
            print(f"[state] {self.state.value} -> {s.value}", flush=True)
            self.state = s

    def begin_turn(self, preroll: bytes = b"") -> None:
        """开始收音。preroll：客户端预滚缓冲（开口前 ~900ms 音频，M1.5-4 防吞句首），
        截断到最近 1 秒防异常大包——ASR 对头部静音不敏感，多补无害。"""
        self.active = True
        self.audio = bytearray(preroll[-SAMPLE_RATE * 2:])

    def end_turn(self) -> bytes:
        self.active = False
        max_bytes = SAMPLE_RATE * 2 * MAX_TURN_SECONDS
        return bytes(self.audio[-max_bytes:])


async def send(ws, message: dict) -> None:
    await ws.send(json.dumps(message, ensure_ascii=False))


async def _finish_turn(ws, call: Call) -> None:
    """轮次收口：状态回 LISTENING 并通知前端——所有出口统一走，防界面卡在'正在传达'。"""
    call.set_state(CallState.LISTENING)
    try:
        await send(ws, {"type": "state", "mode": "listening"})
    except Exception:
        pass  # ws 已断：状态照常收敛，通知尽力而为


async def answer_turn(ws, call: Call, http: aiohttp.ClientSession, pcm: bytes,
                      supplied_text: str = "", prev_generation: asyncio.Task | None = None) -> None:
    if not pcm and not supplied_text:
        await send(ws, {"type": "nothing_heard"})
        return
    turn_id = uuid.uuid4().hex
    call.turn_seq += 1                     # M1.5：接收本轮时生成（与 generation_id 分开，不混用）
    turn_seq = call.turn_seq
    vad_end_at = time.time()               # 自适应停句触发点（闻序分阶段指标）
    metrics: dict = {"vad_end_at": vad_end_at}
    call.set_state(CallState.K_THINKING)
    await send(ws, {"type": "state", "mode": "thinking"})
    try:
        transcript = supplied_text or await transcribe(http, pcm)
        metrics["asr_done_at"] = time.time()
        if not transcript:
            await send(ws, {"type": "nothing_heard"})
            await _finish_turn(ws, call)
            return
        if not supplied_text:  # 打字内容不过滤；只防语音路径的幻听碎片
            if _pcm_rms(pcm) < MIN_SPEECH_RMS or _is_hallucination(transcript):
                print(f"[vad-filter] dropped as hallucination: {transcript!r}", flush=True)
                await send(ws, {"type": "nothing_heard"})
                await _finish_turn(ws, call)
                return
        # 顶替时机：新轮内容确认在手（ASR 非幻听/打字）才掐旧轮——
        # 边缘音频轮、幻听轮从此没有杀人资格（她的实战教训：打字轮两度被陪葬）。
        # prev_generation 由 session 显式传入（创建时刻的旧任务），不会误伤自己。
        if prev_generation is not None and not prev_generation.done():
            prev_generation.cancel()
        # 自然挂断第一步（COVE §16）：这一轮是不是告别？反例保护优先（"别挂"含"挂"字）。
        call.farewell = bool(FAREWELL_RE.search(transcript)) and not FAREWELL_NEG_RE.search(transcript)
        # 先存事实（内存归档缓冲 + 顺序队列落盘），再通知可能已离线的前端（闻序遗漏二）
        call.turns.append(("她", transcript))
        call.writer.submit(turn_seq, turn_id, "user", transcript)
        await send(ws, {"type": "transcript", "call_session_id": call.id, "turn_id": turn_id, "text": transcript})
        call.generation += 1
        generation = call.generation
        metrics["generation_id"] = generation

        streamed = bool(GATEWAY_URL) and STREAM_TTS
        if streamed:
            # V2 流式（COVE §12）：SSE 增量 → 按行切句 → 每段立刻清洗/合成/下发。
            # 首句 4~14 字（K 措辞协议）最先出声，不再等网关全量+整段 TTS。
            # 代价：assistant 轮的存档从"先存后发"变为"流完再存"——SSE 期间被 cancel
            # （打断/挂断）则该轮 reply 不入档，与 M1.5-3"被顶轮生成即止"语义一致。
            seg_parts: list[str] = []
            seg_first_done = False

            async def _emit_segment(line: str) -> None:
                nonlocal seg_first_done
                seg_parts.append(line)
                if generation != call.generation:
                    return  # 已被顶替/打断：字幕音频都不再出（文本照攒，方便日志排查）
                spoken, caption = split_for_tts(line)
                if caption:
                    await send(ws, {"type": "reply_text", "generation_id": generation,
                                    "turn_id": turn_id, "text": caption})
                if not spoken:
                    return
                seg_metrics = None
                if not seg_first_done:   # 延迟指标只看首段（后续段不覆盖 *_at）
                    seg_first_done = True
                    metrics["first_segment_at"] = time.time()
                    seg_metrics = metrics
                try:
                    audio = await synthesize(http, spoken, seg_metrics)
                except Exception as e:
                    print(f"[tts-seg] failed: {e}", flush=True)  # 单段失败跳过，后续段继续
                    return
                if audio and generation == call.generation:
                    call.set_state(CallState.K_SPEAKING)
                    await send(ws, {"type": "audio", "generation_id": generation,
                                    "data": base64.b64encode(audio).decode("ascii")})
                    await send(ws, {"type": "audio_sentence_end", "generation_id": generation})

            reply = await request_reply(http, {"call_session_id": call.id, "turn_id": turn_id,
                                               "transcript": transcript}, metrics,
                                        on_segment=_emit_segment)
        else:
            reply = await request_reply(http, {"call_session_id": call.id, "turn_id": turn_id,
                                               "transcript": transcript}, metrics)
        if not reply:
            await _finish_turn(ws, call)
            return
        call.turns.append(("他", reply))
        call.writer.submit(turn_seq, turn_id, "assistant", reply)  # 闻序遗漏一：同 turn_id 不同 role，与 user 轮成对
        if not streamed:
            spoken, caption = split_for_tts(reply)   # 引号内朗读段转译方言；字幕用清洗后文本（无协议标记）
            await send(ws, {"type": "reply_text", "generation_id": generation, "turn_id": turn_id,
                            "text": caption})  # 闻序热修：空 caption 不回退原始 reply（防协议标签漏进字幕）
            audio = await synthesize(http, spoken, metrics) if spoken else None
            if audio and generation == call.generation:
                call.set_state(CallState.K_SPEAKING)   # 音频已下发（实际播完由前端自理）
                await send(ws, {"type": "audio", "generation_id": generation, "data": base64.b64encode(audio).decode("ascii")})
                await send(ws, {"type": "audio_sentence_end", "generation_id": generation})
        if streamed:
            metrics["tts_stream_first_ok"] = seg_first_done  # 首段是否出过声（纯观测）
        await send(ws, {"type": "generation_end", "generation_id": generation})
        if call.farewell:
            # 告别回复已下发：告诉前端"道别中"，播完排空后由 graceful_hangup 收线
            metrics["farewell"] = True
            await send(ws, {"type": "hangup_soon", "grace_ms": HANGUP_GRACE_MS})
        await _finish_turn(ws, call)
        await log_metrics(call, turn_seq, turn_id, generation, metrics)  # 纯观测写入，失败不影响通话
    except Exception as error:  # do not serialize credentials or provider bodies
        await _finish_turn(ws, call)  # 收口先行（内含 ws 断保护），再尽量通知错误
        try:
            await send(ws, {"type": "error", "error": str(error)})
        except Exception:
            pass


async def graceful_hangup(ws, call: Call) -> None:
    """自然挂断（COVE §16 / M2）：告别回复已生成下发，这里等三件事再收线——
    ① 前端播放真正排空（playback_idle，即 TTS 队列听不见了的地面信号）
    ② 宽限期内她没再开口（她再开口 = speech_start 分支取消本任务，告别不算数）
    ③ 全程硬截止 HANGUP_DEADLINE_S，超时不等了直接关。
    到点后请前端自行挂断（do_hangup），归档照旧统一走 session 的 finally——一套出口。"""
    print(f"[farewell] graceful hangup armed (grace={HANGUP_GRACE_MS}ms, "
          f"deadline={HANGUP_DEADLINE_S}s)", flush=True)
    try:
        deadline = time.time() + HANGUP_DEADLINE_S
        while not call.playback_idle_at and time.time() < deadline:
            await asyncio.sleep(0.2)
        grace_end = time.time() + HANGUP_GRACE_MS / 1000
        while time.time() < grace_end and time.time() < deadline:
            await asyncio.sleep(0.2)
        call.set_state(CallState.ENDING)
        try:
            await send(ws, {"type": "do_hangup"})
        except Exception:
            pass
        await asyncio.sleep(1.5)   # 给前端留出发送 hangup/本地清理的余裕
        try:
            await ws.close()       # 前端没响应也强制收线；async for 退出 → finally 归档
        except Exception:
            pass
    except asyncio.CancelledError:
        raise  # 她宽限期内又开口了：告别作废，通话继续（session 里已同步复位 call.farewell）


async def session(ws) -> None:
    call = Call()
    # 鉴权 token 兼容两种传递：start 消息内 token 字段，或拨号 URL 的 ?token=xxx
    url_token = ""
    try:
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(ws.request.path).query)
        url_token = (q.get("token") or [""])[0]
    except Exception:
        pass
    token_ok = (not TOKEN) or (url_token == TOKEN)
    end_reason = ""  # ""=异常断开 | "hangup"=正常挂断——所有出口统一走 finally

    async with aiohttp.ClientSession() as http:
        call.writer = TurnWriter(http, call.id)
        pending_generation: asyncio.Task | None = None  # M1.5-3：在途生成任务（至多一个，新顶旧/打断/挂断均即时取消）
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    if call.active:
                        call.audio.extend(raw)
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind = event.get("type")
                # 鉴权门：未通过 start 鉴权的连接只接受 start——防止匿名连接直接发
                # text/speech_end 白烧 ASR/LLM/TTS 额度（闻序审查 P0）
                if TOKEN and kind != "start" and not token_ok:
                    continue
                if kind == "start":
                    if TOKEN and not token_ok and event.get("token") != TOKEN:
                        await send(ws, {"type": "error", "error": "Unauthorized"})
                        return
                    token_ok = True
                    await send(ws, {"type": "state", "call_session_id": call.id, "mode": "listening"})
                elif kind == "speech_start":
                    # 她又开口了：在途的自然挂断一律作废（说拜拜之后想起还有事说，太正常）
                    if call.hangup_task and not call.hangup_task.done():
                        call.hangup_task.cancel()
                    call.hangup_task = None
                    call.farewell = False
                    preroll = b""
                    p64 = event.get("preroll")   # M1.5-4：客户端预滚缓冲（开口前 ~900ms），防 VAD 确认延迟吞句首
                    if p64:
                        try:
                            preroll = base64.b64decode(p64)
                        except Exception:
                            preroll = b""
                    call.begin_turn(preroll)
                    call.set_state(CallState.USER_SPEAKING)
                elif kind == "speech_end" or kind == "text":
                    if kind == "text":
                        # 打字没有 speech_start 前奏（VAD 不触发）：宽限期内她改打字说事，同样算反悔
                        if call.hangup_task and not call.hangup_task.done():
                            call.hangup_task.cancel()
                        call.hangup_task = None
                        call.farewell = False
                        pcm = b""
                    else:
                        pcm = call.end_turn()
                        # VAD 误触发防御：过短/过低音量的"轮"不创建任务、不惊动任何人
                        if len(pcm) < SAMPLE_RATE * 2 * 0.3 or _pcm_rms(pcm) < MIN_SPEECH_RMS:
                            print(f"[vad-filter] junk turn dropped "
                                  f"({len(pcm) // 3200}0ms, rms={_pcm_rms(pcm):.0f})", flush=True)
                            call.set_state(CallState.LISTENING)
                            await send(ws, {"type": "state", "mode": "listening"})
                            continue
                    # M1.5-3 核心：生成任务后台化，接收循环不再被 ASR/网关/TTS 阻塞。
                    # 顶替不在这里做——此刻还不知道新轮有没有真话，answer_turn 在内容确认后才掐旧轮。
                    prev_task = call.pending_generation
                    pending_generation = asyncio.create_task(
                        answer_turn(ws, call, http, pcm, str(event.get("text", "")) if kind == "text" else "",
                                    prev_generation=prev_task if isinstance(prev_task, asyncio.Task) else None))
                    call.pending_generation = pending_generation  # 同步赋值，先于新任务首次调度

                    def _arm_farewell(task: asyncio.Task) -> None:
                        """告别轮正常落幕后才武装收线（answer_turn 吞异常，cancelled 除外）。
                        她中途再开口会在 speech_start 复位 farewell，回调到时自然哑火。"""
                        if call.farewell and not task.cancelled() and (call.hangup_task is None or call.hangup_task.done()):
                            call.playback_idle_at = 0.0  # 旧轮的排空信号作废——必须等告别回复自己播完
                            call.hangup_task = asyncio.create_task(graceful_hangup(ws, call))
                    pending_generation.add_done_callback(_arm_farewell)
                elif kind == "playback_idle":
                    call.playback_idle_at = time.time()   # 前端播放排空：自然挂断等的就是这个地面信号
                elif kind == "config_get":
                    await send(ws, await _config_snapshot(http))
                elif kind == "config_set":
                    # 设置面板：运行时更换 API 接口 / 大脑模型 / 音色 / 人设提示词（无需重启）
                    global LLM_MODEL, SYSTEM_PROMPT, GATEWAY_URL, GATEWAY_TOKEN
                    err = None
                    if "gateway_url" in event:
                        GATEWAY_URL = str(event.get("gateway_url") or "").strip()
                    if "gateway_token" in event:
                        GATEWAY_TOKEN = str(event.get("gateway_token") or "").strip()
                    if "llm_model" in event:
                        name = str(event.get("llm_model") or "").strip()
                        # 仅在直连本地 Ollama 时校验模型存在；云端端点无法枚举，直接接受
                        if name and _is_local_ollama(GATEWAY_URL):
                            models = await _ollama_models(http)
                            if models and name not in models:
                                err = f"模型 {name} 不在 Ollama 列表里"
                        if not err:
                            LLM_MODEL = name
                    if "system_prompt" in event:
                        sp = str(event.get("system_prompt") or "")
                        # 面板回显的是"人设指令 + 用户提示词"合成版；剥离人设段再存，
                        # 防止用户在模型面板保存时把人设指令重复固化进 SYSTEM_PROMPT
                        if sp.startswith(_PERSONA_HEADER):
                            sp = sp.split("\n", 1)[1].strip() if "\n" in sp else ""
                        SYSTEM_PROMPT = sp
                    if "persona" in event:
                        ok_p, perr = _apply_persona_patch(event.get("persona") or {})
                        if not ok_p:
                            err = perr
                    if "voice" in event:
                        name = str(event.get("voice") or "").strip()
                        want_sid = event.get("voice_sid")
                        st, js = await _sidecar_set_voice(
                            http, name,
                            int(want_sid) if want_sid is not None else None) if name else (404, {"error": "empty voice"})
                        if st != 200:
                            err = js.get("error", f"切换音色失败 ({st})")
                    elif "voice_sid" in event:
                        sid = int(event.get("voice_sid", 0))
                        st, js = await _sidecar_set_sid(http, sid)
                        if st != 200:
                            err = js.get("error", f"切换说话人失败 ({st})")
                    if err:
                        await send(ws, {"type": "config_error", "error": err})
                    await send(ws, await _config_snapshot(http))
                elif kind == "profile_test":
                    st_ = await _test_endpoint(http, str(event.get("url") or "").strip(),
                                               str(event.get("key") or "").strip(),
                                               str(event.get("model") or "").strip())
                    await send(ws, {"type": "profile_test_result", **st_})
                elif kind == "profile_save":
                    p = event.get("profile") or {}
                    name = str(p.get("name") or "").strip()
                    url = str(p.get("url") or "").strip()
                    key = str(p.get("key") or "").strip()
                    model = str(p.get("model") or "").strip()
                    if not name or not url:
                        await send(ws, {"type": "profile_error", "error": "配置名称和 API 地址必填"})
                    else:
                        prof = _load_profiles()
                        pid = str(p.get("id") or "")
                        existing = next((x for x in prof["profiles"] if x.get("id") == pid), None)
                        if existing:
                            existing.update(name=name, url=url, key=key, model=model)
                        else:
                            pid = pid or ("p" + uuid.uuid4().hex[:8])
                            prof["profiles"].append({"id": pid, "name": name,
                                                     "url": url, "key": key, "model": model})
                        _save_profiles(prof)
                        await send(ws, {"type": "profiles", "profiles": prof["profiles"],
                                        "active": prof["active"], "saved_id": pid})
                elif kind == "profile_delete":
                    pid = str(event.get("id") or "")
                    prof = _load_profiles()
                    prof["profiles"] = [x for x in prof["profiles"] if x.get("id") != pid]
                    if prof["active"] == pid:
                        prof["active"] = ""
                    _save_profiles(prof)
                    await send(ws, {"type": "profiles", "profiles": prof["profiles"],
                                    "active": prof["active"], "saved_id": ""})
                elif kind == "profile_use":
                    pid = str(event.get("id") or "")
                    prof = _load_profiles()
                    target = next((x for x in prof["profiles"] if x.get("id") == pid), None)
                    if not target:
                        await send(ws, {"type": "profile_error", "error": "配置不存在"})
                    else:
                        # LLM_MODEL/GATEWAY_URL/GATEWAY_TOKEN 的 global 声明在 config_set 分支
                        GATEWAY_URL = target.get("url", "")
                        GATEWAY_TOKEN = target.get("key", "")
                        LLM_MODEL = target.get("model", "")
                        prof["active"] = pid
                        _save_profiles(prof)
                        print(f"[profile] use {target.get('name')} -> {GATEWAY_URL} model={LLM_MODEL}", flush=True)
                        await send(ws, await _config_snapshot(http))
                elif kind == "interrupt":
                    if call.hangup_task and not call.hangup_task.done():
                        call.hangup_task.cancel()      # 打断告别轮 = 告别作废
                    call.hangup_task = None
                    call.farewell = False
                    call.generation += 1                     # 在途音频作废（与前端 _stopPlayback 双保险）
                    if pending_generation and not pending_generation.done():
                        pending_generation.cancel()          # 掐断网关/TTS 链路（闻序三级打断的服务端半边）
                    call.set_state(CallState.LISTENING)
                    await send(ws, {"type": "interrupted"})
                elif kind == "hangup":
                    end_reason = "hangup"  # 清理统一走 finally，不再复制一套时序
                    return
        finally:
            # 统一出口（在 ClientSession 关闭前）：收割生成任务 → 排空顺序队列 → 归档。
            # 覆盖三种离开方式：正常 hangup / keepalive 异常断开 / 处理异常——一套时序不再漂移
            call.set_state(CallState.ENDING)
            if pending_generation:
                pending_generation.cancel()  # 任务可能正拿着 http——必须先收割，不允许活过 ClientSession
                try:
                    await asyncio.wait({pending_generation}, timeout=3)
                except Exception:
                    pass
            if call.hangup_task and not call.hangup_task.done():
                call.hangup_task.cancel()    # 自然挂断任务拿着 ws，同理必须收割在 ClientSession 之前
                try:
                    await asyncio.wait({call.hangup_task}, timeout=1)
                except Exception:
                    pass
            await call.writer.close(timeout=8)
            if call.turns:
                try:
                    ok = await archive_call(http, call.id, call.turns,
                                            int((time.time() - call.started_at) * 1000))
                    if ok:
                        call.turns.clear()  # 归档成功才清；失败不清空——当前仅保留到会话销毁（归档重试另行实现）
                except Exception as e:
                    print(f"[archive] error: {e}", flush=True)


def _wallpaper_bytes() -> tuple[bytes | None, str]:
    """读取 Windows 桌面当前壁纸，返回 (图片字节, content_type)；拿不到返回 (None, "")。
    浏览器没有读系统壁纸的 API，由本机服务端代读：SystemParametersInfoW 拿路径，
    读不到文件再退回 Windows 转码缓存 TranscodedWallpaper，按魔数判格式。"""
    path = ""
    if os.name == "nt":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(520)
            if ctypes.windll.user32.SystemParametersInfoW(0x0073, 520, buf, 0) and buf.value:
                path = buf.value   # SPI_GETDESKWALLPAPER
        except Exception:
            pass
        if not path:
            path = os.path.join(os.getenv("APPDATA", ""), "Microsoft", "Windows", "Themes", "TranscodedWallpaper")
    if not path or not os.path.isfile(path):
        return None, ""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception:
        return None, ""
    head = data[:16]
    if head.startswith(b"\x89PNG"): ctype = "image/png"
    elif head.startswith(b"\xff\xd8"): ctype = "image/jpeg"
    elif head.startswith(b"GIF8"): ctype = "image/gif"
    elif head.startswith(b"BM"): ctype = "image/bmp"
    elif head.startswith(b"RIFF") and head[8:12] == b"WEBP": ctype = "image/webp"
    else: ctype = "image/jpeg"   # TranscodedWallpaper 无扩展名，默认按 JPEG 猜
    return data, ctype


# Wallpaper Engine 的默认安装位置：Steam 库 under steamapps/common/wallpaper_engine
_WE_INSTALL_CANDIDATES = (
    "C:/Program Files (x86)/Steam/steamapps/common/wallpaper_engine",
    "D:/Steam/steamapps/common/wallpaper_engine",
    "D:/steam/steamapps/common/wallpaper_engine",
    "E:/Steam/steamapps/common/wallpaper_engine",
    "C:/Steam/steamapps/common/wallpaper_engine",
)
# WE 配置里 selectedwallpapers 的 file 字段按 workshop content 根目录（…/workshop/content/431960）
_WE_WORKSHOP_ID = "431960"


def _we_install_dir() -> str:
    """定位 Wallpaper Engine 安装目录：注册表 installPath 最准，退回常见盘符探测。"""
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\WallpaperEngine") as k:
                v, _ = winreg.QueryValueEx(k, "installPath")
                v = str(v).replace("/", "\\")
                if v.lower().endswith("wallpaper64.exe"):
                    return v[:-len("wallpaper64.exe")].rstrip("\\/")
                if v.lower().endswith(("wallpaper32.exe", "launcher.exe")):
                    return v[:-len("wallpaper32.exe")].rstrip("\\/")
        except Exception:
            pass
        # 注册表没有就顺着 Steam libraryfolders.vdf 找
        try:
            vdf = r"C:\Program Files (x86)\Steam\steamapps\libraryfolders.vdf"
            if os.path.isfile(vdf):
                txt = open(vdf, encoding="utf-8", errors="ignore").read()
                for m in re.finditer(r'"path"\s+"([^"]+)"', txt):
                    cand = m.group(1).replace("\\\\", "\\").rstrip("\\/") + "/steamapps/common/wallpaper_engine"
                    if os.path.isdir(cand):
                        return cand
        except Exception:
            pass
    for cand in _WE_INSTALL_CANDIDATES:
        if os.path.isdir(cand):
            return cand
    return ""


def _we_wallpaper_file() -> str:
    """从 WE config.json 读当前生效壁纸的素材文件路径（selectedwallpapers.file）。
    每次 IO 现读——WE 换壁纸只改这个文件，无须重启本服务。"""
    root = _we_install_dir()
    if not root:
        return ""
    cfg = os.path.join(root, "config.json")
    if not os.path.isfile(cfg):
        return ""
    try:
        data = json.load(open(cfg, encoding="utf-8"))
        for user_obj in data.values():   # 顶层键是 Windows 用户名（如 "Admin"）
            if not isinstance(user_obj, dict):
                continue
            sel = (user_obj.get("general", {}).get("wallpaperconfig", {})
                   .get("selectedwallpapers", {}))
            # 多显示器取第一个（本项目通话页是单背景，Monitor0 优先）
            for monitor in sorted(sel):
                f = (sel[monitor] or {}).get("file", "")
                if f and os.path.isfile(f):
                    return f
    except Exception:
        pass
    return ""


def _we_content_root() -> str:
    """WE 的 Steam 创意工坊内容根目录（…/workshop/content/431960）。
    位置不固定（Steam 库可装任意盘），从已装工程的 file 路径反推最稳。"""
    f = _we_wallpaper_file()
    if f:
        parts = f.replace("\\", "/").split("/")
        if _WE_WORKSHOP_ID in parts:
            i = len(parts) - 1 - parts[::-1].index(_WE_WORKSHOP_ID)
            return "/".join(parts[: i + 1])
    root = _we_install_dir()   # 兜底：安装目录同盘的 steamapps
    if root:
        drive = os.path.splitdrive(root)[0]
        cand = f"{drive}/steamapps/workshop/content/{_WE_WORKSHOP_ID}"
        if os.path.isdir(cand):
            return cand
    return ""


def _we_item_dir(item_id: str) -> str:
    """按创意工坊 id 拼工程目录；不存在的 id 返回空串。"""
    if not re.fullmatch(r"\d{1,12}", item_id or ""):
        return ""
    root = _we_content_root()
    d = os.path.join(root, item_id) if root else ""
    return d if d and os.path.isdir(d) else ""


def _we_title(d: str) -> str:
    """读工程 project.json 的标题；读不到退回目录名。"""
    try:
        pj = json.load(open(os.path.join(d, "project.json"), encoding="utf-8"))
        return str(pj.get("title") or os.path.basename(d))
    except Exception:
        return os.path.basename(d)


def _we_preview_path(d: str) -> str:
    """工程目录里的预览图路径：常规是 preview.jpg/png/gif；缺了再按 project.json 里的
    preview/file 字段找（视频型工程常用 preview.gif）。没有预览图的工程返回空串。"""
    for name in ("preview.jpg", "preview.png", "preview.gif"):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    try:
        pj = json.load(open(os.path.join(d, "project.json"), encoding="utf-8"))
        for key in ("preview", "file"):
            rel = str(pj.get(key, "") or "")
            if rel:
                p = os.path.join(d, rel.replace("/", os.sep))
                if os.path.isfile(p) and p.lower().endswith((".jpg", ".jpeg", ".png", ".gif")):
                    return p
    except Exception:
        pass
    return ""


def _we_list() -> list[dict]:
    """已安装的 WE 壁纸清单：id + 标题 + 有无预览图 + 类型。列表面板用它画缩略图。"""
    root = _we_content_root()
    out = []
    if root and os.path.isdir(root):
        for item_id in sorted(os.listdir(root), key=lambda s: (len(s), s)):
            d = os.path.join(root, item_id)
            if os.path.isdir(d) and re.fullmatch(r"\d{1,12}", item_id) \
                    and os.path.isfile(os.path.join(d, "project.json")):
                out.append({"id": item_id,
                            "title": _we_title(d),
                            "preview": bool(_we_preview_path(d)),
                            "video": _we_video_file(d) != ""})
    return out


def _we_video_file(d: str) -> str:
    """type=video 工程的素材视频路径（浏览器能直接播的 mp4/webm/m4v）。
    scene/pkg/web 型工程没有独立视频文件，返回空串——那些只能上预览图。"""
    try:
        pj = json.load(open(os.path.join(d, "project.json"), encoding="utf-8"))
        if str(pj.get("type", "")).lower() != "video":
            return ""
        rel = str(pj.get("file", "") or "")
        p = os.path.join(d, rel.replace("/", os.sep))
        return p if rel and os.path.isfile(p) \
            and p.lower().endswith((".mp4", ".webm", ".m4v")) else ""
    except Exception:
        return ""


def _we_resolve(item_id: str = "") -> str:
    """把 ?id= 解析成工程目录：缺省 = 当前生效壁纸所在目录；无效返回空串。"""
    if item_id:
        return _we_item_dir(item_id)
    f = _we_wallpaper_file()
    return os.path.dirname(f) if f else ""


def _we_wallpaper_bytes(item_id: str = "") -> tuple[bytes | None, str]:
    """读 WE 壁纸的背景图，返回 (字节, content_type)。
    item_id 为空 = 当前生效的壁纸（跟 WE 里选的一致）。
    背景用高清源：scene.pkg 里内嵌的原图（常见 1080p~4K）；没有 pkg 再退预览图
    （preview.jpg 只有 800~1024px，直接当背景会明显发虚）。"""
    d = _we_resolve(item_id)
    if not d:
        return None, ""
    pkg = os.path.join(d, "scene.pkg")
    if os.path.isfile(pkg):
        data = _pkg_best_image(pkg)
        if data:
            ctype = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
            return data, ctype
    p = _we_preview_path(d)
    if p and os.path.isfile(p):
        try:
            with open(p, "rb") as fh:
                data = fh.read()
            lower = p.lower()
            ctype = ("image/png" if lower.endswith(".png")
                     else "image/gif" if lower.endswith(".gif") else "image/jpeg")
            return data, ctype
        except Exception:
            pass
    return None, ""


def _pkg_best_image(pkg_path: str) -> bytes | None:
    """从 WE scene.pkg（PKGV0023 容器）里抽最大的一张内嵌图（JPEG/PNG）。
    工程的原始壁纸图（作者常打包 1080p~4K 原图）就在 pkg 数据区里，魔数扫描即可定位；
    按"数据段最长"取优，并要求长度与像素数成比例（≥0.02B/px），防止截到缩略图碎块。"""
    try:
        with open(pkg_path, "rb") as fh:
            d = fh.read()
    except Exception:
        return None

    def _jpg_dim(buf: bytes, i: int) -> tuple[int, int] | tuple[None, None]:
        j = i + 2
        while j < len(buf) - 9:
            if buf[j] != 0xFF:
                j += 1
                continue
            m = buf[j + 1]
            if m in (0xC0, 0xC1, 0xC2, 0xC3):
                h, w = struct.unpack_from(">HH", buf, j + 5)
                return w, h
            if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                j += 2
                continue
            if j + 4 > len(buf):
                break
            ln, = struct.unpack_from(">H", buf, j + 2)
            j += 2 + ln
        return None, None

    best_end = 0
    best_len = 0
    for m in re.finditer(b"\xff\xd8\xff[\xdb\xe0\xee\xe1\xed]", d):
        o = m.start()
        w, h = _jpg_dim(d, o)
        if not w or w > 12000 or h > 12000:
            continue
        e = d.find(b"\xff\xd9", o + 2)          # 缩略图在前时它会先命中，靠长度择优兜底
        if e == -1:
            continue
        if e + 2 - o < w * h * 0.02:
            continue
        if e + 2 - o > best_len:
            best_len, best_end = e + 2 - o, e + 2
            best_start = o
    for m in re.finditer(b"\x89PNG\r\n\x1a\n", d):
        o = m.start()
        if o + 33 > len(d):
            continue
        w, h = struct.unpack_from(">II", d, o + 16)
        if not w or not h or w > 12000 or h > 12000:
            continue
        e = d.find(b"IEND", o)
        if e == -1:
            continue
        if e + 8 - o > best_len:
            best_len, best_end, best_start = e + 8 - o, e + 8, o
    if best_len:
        return d[best_start:best_end]
    return None


def _we_current_id() -> str:
    """当前生效壁纸的 workshop id（从素材路径截取）；本地工程没有 id 返回空串。"""
    f = _we_wallpaper_file()
    if not f:
        return ""
    parts = f.replace("\\", "/").split("/")
    if _WE_WORKSHOP_ID in parts:
        i = len(parts) - 1 - parts[::-1].index(_WE_WORKSHOP_ID)
        if i + 1 < len(parts):
            return parts[i + 1]
    return ""


def _we_video_range(request_path: str, range_header: str):
    """解析视频壁纸请求：返回 (文件路径, 起始, 结束, 状态码)；不可播返回 None。
    Range 头（浏览器拖动/流量节省会发 bytes=xxx-yyy）按 RFC 切段，超界收敛到文件尾。"""
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(request_path).query)
    item_id = (qs.get("id") or [""])[0]
    d = _we_resolve(item_id)
    vf = _we_video_file(d) if d else ""
    if not vf:
        return None
    size = os.path.getsize(vf)
    start, end, status = 0, size - 1, 200
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", (range_header or "").strip())
    if m:
        first, last = m.group(1), m.group(2)
        if first == "" and last == "":      # bytes=- 无效
            return None
        if first == "":                     # 后缀段：bytes=-N 取末尾 N 字节
            start = max(0, size - int(last))
        else:
            start = int(first)
            end = min(size - 1, int(last)) if last else size - 1
        if start >= size:                   # 越界段无内容可给
            return None
        status = 206
    return vf, start, end, status


def ctype_of(path: str) -> str:
    """视频/图片扩展名 → content-type。"""
    p = path.lower()
    if p.endswith(".webm"): return "video/webm"
    if p.endswith(".mp4") or p.endswith(".m4v"): return "video/mp4"
    if p.endswith(".png"): return "image/png"
    return "image/jpeg"


# ======================= Codex 宠物（codex-pet.org 精灵图桌宠）=======================
# 资源格式：每只宠物 = pet.json（清单）+ spritesheet.webp（8 列 × 9 行帧图）。
# 行号固定：0 待机 1 向右跑 2 向左跑 3 挥手 4 跳跃 5 失败 6 等待 7 奔跑 8 审阅；
# 每行帧数：6/8/8/4/5/7/6/6/6。安装 = 从 codex-pet.org 拉两个文件落到
# packages/web-client/pets/<slug>/，浏览器直接按行播放帧。
PET_FRAME_COLS = 8
PET_FRAME_ROWS = 9
PET_ROW_FRAMES = (6, 8, 8, 4, 5, 8, 6, 6, 6)   # 每行动作帧数（与官网渲染逻辑一致）
PET_STATES = ("idle", "run-right", "run-left", "waving", "jumping",
              "failed", "waiting", "running", "review")


def _pets_dir() -> str:
    """返回 Codex 官方宠物目录。"""
    codex_home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    d = os.path.abspath(os.path.join(codex_home, "pets"))
    os.makedirs(d, exist_ok=True)
    return d


def _parse_pet_install_input(value: str) -> str:
    value = str(value or "").strip().lower()
    match = re.fullmatch(
        r"(?:npx\s+(?:--yes\s+)?codex-pet-installer\s+add\s+)?([a-z0-9]+(?:-[a-z0-9]+)*)",
        value,
    )
    return match.group(1) if match else ""


def _pet_dir(slug: str) -> str:
    slug = _parse_pet_install_input(slug)
    return os.path.join(_pets_dir(), slug) if slug else ""


def _read_pet_manifest(d: str) -> dict:
    try:
        with open(os.path.join(d, "pet.json"), encoding="utf-8") as f:
            meta = json.load(f)
        return meta if isinstance(meta, dict) else {}
    except Exception:
        return {}


def _pet_asset(d: str) -> tuple[str, str] | None:
    meta = _read_pet_manifest(d)
    rel = meta.get("spritesheetPath")
    if not rel and isinstance(meta.get("assets"), dict):
        rel = meta["assets"].get("spritesheet")
    rel = rel or "spritesheet.webp"
    if not isinstance(rel, str) or os.path.splitext(rel)[1].lower() not in (".webp", ".png"):
        return None
    root = os.path.realpath(d)
    path = os.path.realpath(os.path.join(root, rel))
    try:
        if os.path.commonpath((root, path)) != root or os.path.dirname(path) == root and not os.path.basename(path):
            return None
    except ValueError:
        return None
    if not os.path.isfile(path):
        return None
    return path, "image/png" if path.lower().endswith(".png") else "image/webp"


def _pet_meta(d: str) -> dict:
    """读一只已安装宠物的元信息（pet.json + 安装时间 + 网格补全）。"""
    meta_path = os.path.join(d, "pet.json")
    meta = _read_pet_manifest(d)
    slug = os.path.basename(d)
    meta.setdefault("id", slug)
    meta.setdefault("slug", slug)
    meta.setdefault("displayName", slug)
    meta.setdefault("description", "")
    meta.setdefault("creator", "Community")
    meta.setdefault("spritesheetPath", "spritesheet.webp")
    meta["columns"] = PET_FRAME_COLS
    meta["rows"] = PET_FRAME_ROWS
    meta["rowFrames"] = list(PET_ROW_FRAMES)
    meta["states"] = list(PET_STATES)
    meta["spriteUrl"] = f"/v1/pets/{slug}/asset"
    try:
        meta["installedAt"] = os.path.getmtime(meta_path)
    except OSError:
        meta["installedAt"] = 0
    return meta


def _pets_installed() -> list[dict]:
    root = _pets_dir()
    out = []
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            d = os.path.join(root, name)
            if os.path.isdir(d) and _pet_asset(d):
                out.append(_pet_meta(d))
    return out


async def _pet_install(value: str) -> tuple[bool, str, str]:
    """使用官方 npm 安装器安装宠物，并验证落盘结果。"""
    slug = _parse_pet_install_input(value)
    if not slug:
        return False, "请输入宠物 id 或 npx codex-pet-installer add <id>", ""
    executable = "npx.cmd" if os.name == "nt" else "npx"
    env = os.environ.copy()
    env["CODEX_HOME"] = os.path.dirname(_pets_dir())
    try:
        proc = await asyncio.create_subprocess_exec(
            executable, "--yes", "codex-pet-installer", "add", slug,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env, shell=False,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return False, "安装超时，请检查 npm 网络连接", slug
        output = (stderr or stdout or b"").decode("utf-8", "replace").strip()[-1200:]
        if proc.returncode != 0:
            return False, f"安装器失败：{output or f'退出码 {proc.returncode}'}", slug
    except FileNotFoundError:
        return False, "无法启动 npx，请先安装 Node.js 和 npm", slug
    except Exception as e:
        return False, f"安装失败：{e}", slug
    d = _pet_dir(slug)
    if not os.path.isfile(os.path.join(d, "pet.json")) or not _pet_asset(d):
        return False, "安装器已结束，但宠物包缺少有效 pet.json 或精灵图", slug
    return True, _pet_meta(d).get("displayName", slug), slug


def _pet_remove(slug: str) -> tuple[bool, str]:
    clean = _parse_pet_install_input(slug)
    if not clean or clean != str(slug).strip().lower():
        return False, "无效的宠物 id"
    root = os.path.realpath(_pets_dir())
    target = os.path.realpath(os.path.join(root, clean))
    if os.path.dirname(target) != root:
        return False, "无效的宠物目录"
    if not os.path.isdir(target):
        return False, "宠物不存在或已删除"
    try:
        shutil.rmtree(target)
    except OSError as e:
        return False, f"删除失败：{e}"
    return True, clean


def _pet_file(slug: str, fname: str) -> tuple[bytes | None, str]:
    """读取已安装宠物的清单或实际精灵图。"""
    d = _pet_dir(slug)
    if not d or fname not in ("pet.json", "asset"):
        return None, ""
    if fname == "asset":
        asset = _pet_asset(d)
        if not asset:
            return None, ""
        p, ctype = asset
    else:
        p, ctype = os.path.join(d, "pet.json"), "application/json; charset=utf-8"
    if not os.path.isfile(p):
        return None, ""
    try:
        with open(p, "rb") as f:
            return f.read(), ctype
    except Exception:
        return None, ""


async def main() -> None:
    from pathlib import Path

    index_paths = [Path(__file__).parent / "index.html",          # 容器：/app/index.html（Dockerfile COPY）
                   Path(__file__).parent.parent / "web-client" / "index.html"]  # 本地开发
    index_text = next((p.read_text(encoding="utf-8") for p in index_paths if p.exists()),
                      "<h1>voice-call page missing</h1>")
    # 无登录页：把令牌注入前端占位符，页面打开即自动连接（地址由前端按 location 推导）
    index_text = index_text.replace("__PAIVOICE_TOKEN__", TOKEN)
    vc_paths = [Path(__file__).parent / "voice-call.js",
                Path(__file__).parent.parent / "web-client" / "voice-call.js"]
    vc_text = next((p.read_text(encoding="utf-8") for p in vc_paths if p.exists()),
                   "export default {}")
    pet_controller_paths = [Path(__file__).parent / "pet-controller.js",
                            Path(__file__).parent.parent / "web-client" / "pet-controller.js"]
    pet_controller_text = next((p.read_text(encoding="utf-8") for p in pet_controller_paths if p.exists()),
                               "export class PetController {}")
    icon_dir = next((p for p in [Path(__file__).parent / "assets" / "icons",               # 容器：/app/assets/icons（Dockerfile COPY）
                                 Path(__file__).parent.parent.parent / "assets" / "icons"]  # 本地开发：项目根 assets/icons
                     if p.is_dir()), None)
    icon_files: dict[str, tuple[str, bytes]] = {}   # 图标路由 → (类型, 字节)；启动时读进内存，文件缺失就跳过
    for route, fname, ctype in [("/favicon.ico", "favicon.ico", "image/x-icon"),
                                ("/icons/paivoice-icon-16.png", "paivoice-icon-16.png", "image/png"),
                                ("/icons/paivoice-icon-32.png", "paivoice-icon-32.png", "image/png"),
                                ("/icons/paivoice-icon-48.png", "paivoice-icon-48.png", "image/png"),
                                ("/icons/paivoice-icon-180.png", "paivoice-icon-180.png", "image/png"),
                                ("/icons/paivoice-icon-192.png", "paivoice-icon-192.png", "image/png"),
                                ("/icons/paivoice-icon-256.png", "paivoice-icon-256.png", "image/png"),
                                ("/icons/paivoice-icon-512.png", "paivoice-icon-512.png", "image/png"),
                                ("/icons/paivoice-icon-1024.png", "paivoice-icon-1024.png", "image/png")]:
        if icon_dir and (icon_dir / fname).is_file():
            icon_files[route] = (ctype, (icon_dir / fname).read_bytes())
    if "/icons/paivoice-icon-180.png" in icon_files:   # iOS Safari 默认请求根路径的 apple-touch-icon
        icon_files["/apple-touch-icon.png"] = icon_files["/icons/paivoice-icon-180.png"]
    manifest_icons = [{"src": route, "sizes": f"{size}x{size}", "type": "image/png"}
                      for route, size in [("/icons/paivoice-icon-192.png", 192),
                                          ("/icons/paivoice-icon-512.png", 512)]
                      if route in icon_files]
    if manifest_icons:   # PWA 安装图标（添加到主屏幕用）
        icon_files["/manifest.webmanifest"] = (
            "application/manifest+json",
            json.dumps({"name": "PaiVoice", "short_name": "PaiVoice", "display": "standalone",
                        "start_url": "/", "background_color": "#171008", "theme_color": "#171008",
                        "icons": manifest_icons}, ensure_ascii=False).encode("utf-8"))
    static_files = {
        "/": ("text/html; charset=utf-8", index_text),
        "/index.html": ("text/html; charset=utf-8", index_text),
        "/voice-call.js": ("application/javascript; charset=utf-8", vc_text),
        "/pet-controller.js": ("application/javascript; charset=utf-8", pet_controller_text),
    }

    async def process_request(connection, request):  # 静态托管通话页 + voice-call.js；其余路径走 WS 升级
        # （async：/v1/pets/install 要现场去 codex-pet.org 下载宠物包；websockets 17 支持协程返回值）
        from websockets.datastructures import Headers
        from websockets.http11 import Response
        try:
            path = request.path.split("?")[0]
            if path in static_files:
                ctype, body_text = static_files[path]
                body = body_text.encode("utf-8")
                return Response(200, "OK", Headers([
                    ("content-type", ctype),
                    ("content-length", str(len(body))),
                    ("access-control-allow-origin", "*"),
                ]), body)
            if path in icon_files:   # 应用图标 / PWA 清单（启动时从 assets/icons 读进内存）
                ctype, data = icon_files[path]
                return Response(200, "OK", Headers([
                    ("content-type", ctype),
                    ("content-length", str(len(data))),
                    ("cache-control", "max-age=3600"),
                    ("access-control-allow-origin", "*"),
                ]), data)
            if path == "/health":
                return Response(200, "OK", Headers([
                    ("content-type", "text/plain"),
                ]), b"ok")
            if path == "/v1/wallpaper":   # 电脑桌面壁纸 → 前端背景（背景设置面板「电脑壁纸」模式）
                data, ctype = _wallpaper_bytes()
                if data is None:
                    body = "无法读取电脑壁纸（仅支持 Windows 本机运行服务端）".encode("utf-8")
                    return Response(404, "Not Found", Headers([
                        ("content-type", "text/plain; charset=utf-8"),
                        ("content-length", str(len(body))),
                        ("access-control-allow-origin", "*"),
                    ]), body)
                return Response(200, "OK", Headers([
                    ("content-type", ctype),
                    ("content-length", str(len(data))),
                    ("cache-control", "no-store"),
                    ("access-control-allow-origin", "*"),
                ]), data)
            if path == "/v1/wallpaper-engine":   # Wallpaper Engine 壁纸图 → 前端背景 / 网格缩略图
                # ?id=<workshop id> 指定某张；缺省 = WE 当前生效的那张
                # ?thumb=1 = 只要小预览图（挑选网格用）；否则返回 pkg 内嵌高清原图当背景
                from urllib.parse import parse_qs, urlparse
                qs = parse_qs(urlparse(request.path).query)
                item_id = (qs.get("id") or [""])[0]
                if (qs.get("thumb") or [""])[0] and item_id:
                    d = _we_item_dir(item_id)
                    p = _we_preview_path(d) if d else ""
                    if p and os.path.isfile(p):
                        with open(p, "rb") as fh:
                            data = fh.read()
                        ctype = ("image/png" if p.lower().endswith(".png")
                                 else "image/gif" if p.lower().endswith(".gif") else "image/jpeg")
                    else:
                        data, ctype = None, ""
                else:
                    data, ctype = _we_wallpaper_bytes(item_id)
                if data is None:
                    body = ("未找到该 Wallpaper 壁纸的预览图" if item_id
                            else "无法读取 Wallpaper Engine 当前壁纸（未安装 / 未运行 / 配置缺失）").encode("utf-8")
                    return Response(404, "Not Found", Headers([
                        ("content-type", "text/plain; charset=utf-8"),
                        ("content-length", str(len(body))),
                        ("access-control-allow-origin", "*"),
                    ]), body)
                return Response(200, "OK", Headers([
                    ("content-type", ctype),
                    ("content-length", str(len(data))),
                    ("cache-control", "no-store"),
                    ("access-control-allow-origin", "*"),
                ]), data)
            if path == "/v1/wallpaper-engine/list":   # 已安装的 WE 壁纸清单（id+标题+有无预览）
                body = json.dumps({"items": _we_list(),
                                   "current": _we_current_id()},
                                  ensure_ascii=False).encode("utf-8")
                return Response(200, "OK", Headers([
                    ("content-type", "application/json; charset=utf-8"),
                    ("content-length", str(len(body))),
                    ("cache-control", "no-store"),
                    ("access-control-allow-origin", "*"),
                ]), body)
            if path == "/v1/wallpaper-engine/video":   # WE 视频壁纸流式代理（支持 Range）
                rr = _we_video_range(request.path, request.headers.get("Range", ""))
                if rr is None:
                    body = "该 Wallpaper 壁纸没有可播放的视频（scene/web 型工程只有预览图）".encode("utf-8")
                    return Response(404, "Not Found", Headers([
                        ("content-type", "text/plain; charset=utf-8"),
                        ("content-length", str(len(body))),
                        ("access-control-allow-origin", "*"),
                    ]), body)
                vf, start, end, status = rr
                length = end - start + 1
                try:
                    with open(vf, "rb") as fh:   # 壁纸视频多在几十~几百 MB，一次读进内存
                        fh.seek(start)
                        data = fh.read(length)
                except Exception:
                    data = b""
                hdrs = Headers([
                    ("content-type", ctype_of(vf)),
                    ("content-length", str(len(data))),
                    ("accept-ranges", "bytes"),
                    ("cache-control", "no-store"),
                    ("access-control-allow-origin", "*"),
                ] + ([("content-range", f"bytes {start}-{end}/{os.path.getsize(vf)}")] if status == 206 else []))
                return Response(status, "OK", hdrs, data)
            if path == "/v1/pets/install" and request.method == "POST":   # 安装一只 Codex 宠物
                from urllib.parse import parse_qs, urlparse
                qs = parse_qs(urlparse(request.path).query)
                value = (qs.get("input") or qs.get("id") or [""])[0]
                ok, msg, slug = await _pet_install(value)
                body = json.dumps({"ok": ok, "message": msg, "slug": slug}, ensure_ascii=False).encode("utf-8")
                return Response(200 if ok else 400, "OK", Headers([
                    ("content-type", "application/json; charset=utf-8"),
                    ("content-length", str(len(body))),
                    ("cache-control", "no-store"),
                    ("access-control-allow-origin", "*"),
                ]), body)
            m = re.fullmatch(r"/v1/pets/([a-z0-9-]+)", path)
            if m and request.method == "DELETE":
                ok, msg = _pet_remove(m.group(1))
                body = json.dumps({"ok": ok, "message": msg}, ensure_ascii=False).encode("utf-8")
                return Response(200 if ok else 404, "OK", Headers([
                    ("content-type", "application/json; charset=utf-8"),
                    ("content-length", str(len(body))),
                    ("cache-control", "no-store"),
                    ("access-control-allow-origin", "*"),
                ]), body)
            if path == "/v1/pets":   # 已安装的 Codex 宠物清单
                body = json.dumps({"pets": _pets_installed()},
                                  ensure_ascii=False).encode("utf-8")
                return Response(200, "OK", Headers([
                    ("content-type", "application/json; charset=utf-8"),
                    ("content-length", str(len(body))),
                    ("cache-control", "no-store"),
                    ("access-control-allow-origin", "*"),
                ]), body)
            m = re.fullmatch(r"/v1/pets/([a-z0-9-]+)/(pet\.json|asset)", path)
            if m:   # 宠物静态文件（清单 / 精灵图）
                data, ctype = _pet_file(m.group(1), m.group(2))
                if data is None:
                    return Response(404, "Not Found", Headers([
                        ("content-type", "text/plain; charset=utf-8"),
                        ("access-control-allow-origin", "*"),
                    ]), b"pet file not found")
                return Response(200, "OK", Headers([
                    ("content-type", ctype),
                    ("content-length", str(len(data))),
                    ("cache-control", "no-store"),
                    ("access-control-allow-origin", "*"),
                ]), data)
            return None
        except Exception:
            import traceback
            return Response(500, "ERR", Headers([
                ("content-type", "text/plain; charset=utf-8"),
            ]), traceback.format_exc().encode())

    async with serve(session, HOST, PORT, max_size=None, process_request=process_request,
                     ping_interval=25, ping_timeout=120):  # 手机+VPN 链路抖动大，放宽保活判定
        print(f"PaiVoice listening on ws://{HOST}:{PORT}", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
