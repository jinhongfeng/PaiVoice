#!/usr/bin/env python3
"""五轨记忆自动进化引擎：后台空闲时用大脑端点把新对话总结进 L1/L3/L4 + 当前分支 L2。

触发时机（main() 里的 memory_loop 每 MEMORY_INTERVAL 检查一次）：
  1. 无活跃通话（server 的 _active_calls == 0）
  2. 距上次总结 >= MEMORY_COOLDOWN
  3. meta.cursor 之后有 >= MEMORY_MIN_TURNS 条新轮次
满足才跑。失败只留日志，绝不影响通话主流程。
"""
from __future__ import annotations

import asyncio
import json
import time

import aiohttp

SUMMARY_PROMPT = """你是一个记忆蒸馏助手。根据下面这段最近对话原文，提炼出关于「用户」的
可靠事实，用于让 AI 伴侣长期更懂这个人。

## 任务
1. 只输出 JSON，不要任何解释、前后缀、markdown 代码块。
2. 只输出【新增】或【修正已有】的事实；与已有档案重复的，一律不要输出。
3. 每条事实必须是能从原文直接或可靠推断的，不要脑补。

## 输出格式（严格 JSON）
输出一个 JSON 对象，字段如下（没有的字段给空数组或空字符串）：
- profile_updates: 数组，元素 {{"fact": 事实一句话, "category": 身份|偏好|工作|关系|雷点|其他, "confidence": 0.0到1.0}}
- longterm_events: 数组，元素 {{"event": 跨日重大事件/重要决定一句话, "date": YYYY-MM-DD或空, "importance": 0.0到1.0}}
- project_memories: 数组，元素 {{"fact": 与项目/代码相关的关键事实一句话, "category": 项目, "confidence": 0.0到1.0}}
- today_topics: 数组，元素是主题字符串
- persona_suggestion: 字符串，若要让伴侣更贴合此人，语气/风格建议一句话；没有则空字符串

## 已有用户档案
{profile}

## 已有长期记忆
{longterm}

## 最近对话原文
{transcript}
"""


def build_summary_prompt(store, rows: list[dict]) -> str:
    """拼总结 prompt：附现有档案/长期记忆，指令"只输出新增/修正"。"""
    profile = store.read_profile(limit=60)
    longterm = store.read_longterm(limit=200)
    profile_txt = "；".join(
        f"{p['fact']}（{p['category']}，conf={p['confidence']:.1f}）" for p in profile) or "（空）"
    longterm_txt = "；".join(
        f"{e['event']}" + (f"（{e['date']}）" if e.get("date") else "") for e in longterm) or "（空）"
    transcript_txt = "\n".join(
        f"[{r['role']}] {r['content']}" for r in rows[-80:])   # 只喂最近 80 条，防超长
    return SUMMARY_PROMPT.format(profile=profile_txt, longterm=longterm_txt, transcript=transcript_txt)


def parse_summary(text: str) -> dict | None:
    """解析模型输出的 JSON。容忍 ```json 围栏与前后杂讯。"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.strip("`")
        t = t.strip()
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(t[start:end + 1])
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def apply_summary(store, data: dict) -> None:
    """合并写盘：L3 档案 upsert、L4 长期只追加、L2 项目按当前分支 upsert、L1 今日日志合并。"""
    branch = store.current_branch()
    today = store.read_today()

    for item in data.get("profile_updates") or []:
        if isinstance(item, dict) and item.get("fact"):
            store.upsert_profile(str(item["fact"]), str(item.get("category") or "其他"),
                                 float(item.get("confidence", 0.5)))

    for item in data.get("longterm_events") or []:
        if isinstance(item, dict) and item.get("event"):
            store.append_longterm(str(item["event"]), str(item.get("date") or ""),
                                  float(item.get("importance", 0.5)))

    for item in data.get("project_memories") or []:
        if isinstance(item, dict) and item.get("fact"):
            store.upsert_project(branch, str(item["fact"]), str(item.get("category") or "项目"),
                                 float(item.get("confidence", 0.5)))

    topics = data.get("today_topics") or []
    if topics or data.get("summary"):
        merged = list(dict.fromkeys(list(today.get("topics") or []) + [str(t) for t in topics]))
        store.upsert_today(today["date"], merged, str(data.get("summary") or today.get("summary") or ""))

    suggestion = data.get("persona_suggestion")
    if isinstance(suggestion, str) and suggestion.strip():
        store.set_meta("suggestion", suggestion.strip())


async def summarize_new_turns(store, http: aiohttp.ClientSession, gateway_url: str,
                              gateway_token: str = "", model: str = "",
                              min_turns: int = 5) -> bool:
    """跑一轮总结。返回 True=成功并推进游标；False=条件不满足/失败（下轮再试）。"""
    if not gateway_url:
        return False
    cursor_s = store.get_meta("cursor", "0")
    try:
        cursor = int(cursor_s)
    except Exception:
        cursor = 0
    rows, new_cursor = store.load_turns_since(cursor, limit=200)
    if len(rows) < min_turns:
        return False

    prompt = build_summary_prompt(store, rows)
    headers = {"content-type": "application/json"}
    if gateway_token:
        headers["authorization"] = f"Bearer {gateway_token}"
    payload = {
        "messages": [{"role": "system", "content": "你是一个记忆蒸馏助手，只输出 JSON。"},
                     {"role": "user", "content": prompt}],
        "stream": False,
        "max_tokens": 1200,
    }
    if model:
        payload["model"] = model
    url = gateway_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"

    try:
        async with http.post(url, json=payload, headers=headers,
                             timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if resp.status != 200:
                print(f"[memory-evolve] summarize http {resp.status}", flush=True)
                return False
            js = await resp.json()
        content = ((js.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        data = parse_summary(content)
        if data is None:
            print("[memory-evolve] summary parse failed, skip this round", flush=True)
            return False
        apply_summary(store, data)
        store.set_meta("cursor", str(new_cursor))
        store.set_meta("last_summary_at", str(time.time()))
        print(f"[memory-evolve] summarized {len(rows)} turns -> cursor={new_cursor}", flush=True)
        return True
    except Exception as e:
        print(f"[memory-evolve] failed: {e}", flush=True)
        return False


async def maybe_summarize(store, http: aiohttp.ClientSession, gateway_url: str,
                          gateway_token: str = "", model: str = "", min_turns: int = 5,
                          cooldown: float = 300.0) -> bool:
    """空闲+冷却+新轮 三条件齐了才跑。返回 True=跑了总结。"""
    last_s = store.get_meta("last_summary_at", "0")
    try:
        last = float(last_s)
    except Exception:
        last = 0.0
    if time.time() - last < cooldown:
        return False
    return await summarize_new_turns(store, http, gateway_url, gateway_token, model, min_turns)


async def memory_loop(store, http: aiohttp.ClientSession, get_cfg, interval: float = 600.0) -> None:
    """main() 后台任务：每 interval 秒检查一次。get_cfg() 返回 dict(active_calls, gateway_url,
    gateway_token, model)，运行时读全局（设置面板热改生效）。"""
    while True:
        try:
            cfg = get_cfg()
            if cfg["active_calls"] <= 0:
                await maybe_summarize(
                    store, http, cfg["gateway_url"], cfg["gateway_token"], cfg["model"],
                    min_turns=cfg["min_turns"], cooldown=cfg["cooldown"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[memory-evolve] loop error: {e}", flush=True)
        await asyncio.sleep(interval)
