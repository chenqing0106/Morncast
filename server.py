import asyncio
import json
import os
import uuid

from dotenv import load_dotenv
load_dotenv()  # 读取 .env 文件

import edge_tts
from openai import OpenAI
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/audio", StaticFiles(directory="audio_cache"), name="audio")

# 支持 Qwen / 任何 OpenAI 兼容接口
# Qwen:   base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", api_key=DASHSCOPE_API_KEY
# Claude: base_url="https://api.anthropic.com/v1",                      api_key=ANTHROPIC_API_KEY (需 openai>=1.0)
# OpenAI: 不填 base_url 即可
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
LLM_API_KEY  = os.environ.get("LLM_API_KEY", "")
LLM_MODEL    = os.environ.get("LLM_MODEL", "doubao-pro-32k")

client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

PROMPT = """你是 Morncast 的播客脚本生成器。
把以下抖音视频 AI 摘要整合成一段自然流畅的通勤播客脚本，口语化，约 300-400 字。

要求：
1. 开头用 "早上好，欢迎收听 Morncast" 起句
2. 各条内容之间用过渡语衔接，不要生硬列举
3. 结尾用一句有温度的话收尾

摘要内容：
{summaries}

输出严格的 JSON，不要有任何 markdown 代码块，格式如下：
{{
  "script": "完整播客脚本文字",
  "chapters": [
    {{"title": "章节名称（10字以内）", "char_start": 0}},
    {{"title": "第二章节名称", "char_start": 120}}
  ],
  "sources": [
    {{"title": "视频标题或主题", "snippet": "一句话摘要"}},
    {{"title": "...", "snippet": "..."}}
  ]
}}"""


class BriefRequest(BaseModel):
    summaries: list[str]


@app.post("/api/generate-brief")
async def generate_brief(body: BriefRequest):
    if not body.summaries:
        raise HTTPException(status_code=400, detail="summaries 不能为空")

    summaries_text = "\n\n".join(f"【{i+1}】{s}" for i, s in enumerate(body.summaries))

    # 1. LLM 生成脚本（OpenAI 兼容格式）
    msg = client.chat.completions.create(
        model=LLM_MODEL,
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": PROMPT.format(summaries=summaries_text),
        }],
    )

    raw = msg.choices[0].message.content.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 兜底：Claude 有时会多输出注释，尝试提取 JSON 块
        import re
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise HTTPException(status_code=500, detail=f"JSON 解析失败: {raw[:200]}")
        data = json.loads(match.group())

    script: str = data.get("script", "")
    chapters: list = data.get("chapters", [])
    sources: list = data.get("sources", [])

    # 2. Edge TTS 生成音频
    audio_id = uuid.uuid4().hex[:8]
    audio_path = f"audio_cache/{audio_id}.mp3"
    communicate = edge_tts.Communicate(script, voice="zh-CN-XiaoxiaoNeural")
    await communicate.save(audio_path)

    # 3. 把 char_start 换算成粗略秒数（约 4.5 字/秒 普通速度）
    chars_per_sec = 4.5
    total_chars = len(script)
    total_sec = round(total_chars / chars_per_sec)

    transcript_lines = _split_to_lines(script, chars_per_sec)

    chapters_out = []
    for c in chapters:
        char_start = c.get("char_start", 0)
        chapters_out.append({
            "start": round(char_start / chars_per_sec),
            "title": c.get("title", ""),
        })

    return {
        "audioUrl": f"http://localhost:8000/audio/{audio_id}.mp3",
        "totalSec": total_sec,
        "chapters": chapters_out,
        "transcriptLines": transcript_lines,
        "sources": [
            {"id": f"s{i}", "title": s.get("title", ""), "snippet": s.get("snippet", ""), "author": "抖音", "thumb": f"t{(i % 4) + 1}"}
            for i, s in enumerate(sources)
        ],
    }


def _split_to_lines(script: str, chars_per_sec: float) -> list[dict]:
    """按句号/感叹号/问号断句，估算每句开始秒数"""
    import re
    sentences = re.split(r"(?<=[。！？…])", script)
    lines = []
    char_pos = 0
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        lines.append({
            "start": round(char_pos / chars_per_sec),
            "text": sent,
        })
        char_pos += len(sent)
    return lines


@app.get("/health")
def health():
    return {"status": "ok"}
