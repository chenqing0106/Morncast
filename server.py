import json
import os
import re
import unicodedata
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import edge_tts
from openai import OpenAI
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


VIDEO_DIR = Path("video")
DATA_DIR = Path("data")
AUDIO_DIR = Path("audio_cache")
DATA_DIR.mkdir(exist_ok=True)
AUDIO_DIR.mkdir(exist_ok=True)

SCRIPT_CACHE = DATA_DIR / "script.json"
TIMING_CACHE = DATA_DIR / "timing.json"
AUDIO_FILE = AUDIO_DIR / "morncast.mp3"

TTS_VOICE = os.environ.get("TTS_VOICE", "zh-CN-XiaoxiaoNeural")
TTS_RATE = os.environ.get("TTS_RATE", "+0%")

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "doubao-pro-32k")

client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/audio", StaticFiles(directory="audio_cache"), name="audio")
app.mount("/assets", StaticFiles(directory="frontend/assets"), name="assets")
app.mount("/videos", StaticFiles(directory="video"), name="videos")


@app.get("/")
def index():
    return FileResponse("frontend/index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------- Layer 0: manifest ----------

def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"[\s\W_]+", "", s).lower()


def _parse_numbered_blocks(text: str) -> list[tuple[int, str, str]]:
    """切分以行首 '编号、' 开头的多段文本。返回 [(序号, 标题, 正文)]。
    summary.txt 的结构是：编号、标题 + 空行 + 正文段落 + 双空行 + 下一编号..."""
    chunks = re.split(r"\n(?=\s*\d+\s*[、.．])", text.strip())
    out = []
    for chunk in chunks:
        m = re.match(r"^\s*(\d+)\s*[、.．]\s*([^\n]+)\n(.*)$", chunk, flags=re.DOTALL)
        if m:
            idx = int(m.group(1))
            title = m.group(2).strip()
            body = m.group(3).strip()
            out.append((idx, title, body))
        else:
            m2 = re.match(r"^\s*(\d+)\s*[、.．]\s*(.+?)$", chunk, flags=re.DOTALL)
            if m2:
                out.append((int(m2.group(1)), m2.group(2).strip(), ""))
    return out


def load_manifest() -> list[dict]:
    title_text = (VIDEO_DIR / "title.txt").read_text(encoding="utf-8")
    summary_text = (VIDEO_DIR / "summary.txt").read_text(encoding="utf-8")

    titles = {}
    for line in title_text.splitlines():
        m = re.match(r"^\s*(\d+)\s*[、.．]\s*(.+)$", line)
        if m:
            titles[int(m.group(1))] = m.group(2).strip()

    summaries = {}
    for idx, _t, body in _parse_numbered_blocks(summary_text):
        summaries[idx] = body

    mp4_files = [p.name for p in VIDEO_DIR.glob("*.mp4")]
    norm_mp4 = {_normalize(name.rsplit(".", 1)[0]): name for name in mp4_files}

    manifest = []
    for idx in sorted(titles.keys()):
        title = titles[idx]
        norm_title = _normalize(title)
        video_file = ""
        for nk, fname in norm_mp4.items():
            if norm_title in nk or nk in norm_title:
                video_file = fname
                break
        manifest.append({
            "id": idx,
            "title": title,
            "summary": summaries.get(idx, ""),
            "videoFile": video_file,
        })
    return manifest


# ---------- Layer 1: build_script ----------

PROMPT = """你是 Morncast 的播客脚本生成器。
把以下抖音视频的 AI 摘要整合成一段自然流畅的通勤播客脚本，口语化，约 400-600 字。

要求：
1. 开头用 "早上好，欢迎收听 Morncast" 起句
2. 各条内容之间用过渡语衔接，不要生硬列举每一条
3. 结尾用一句有温度的话收尾

视频清单（编号 / 标题 / 摘要）：
{items}

输出严格的 JSON，不要有任何 markdown 代码块、不要有注释，格式如下：
{{
  "title": "本期节目总标题（≤14字，概括全期主题）",
  "script": "完整播客脚本文字",
  "chapters": [
    {{"title": "章节名称（≤10字）", "char_start": 0}},
    {{"title": "第二章节名称", "char_start": 120}}
  ]
}}"""


def build_script(manifest: list[dict]) -> dict:
    if SCRIPT_CACHE.exists():
        return json.loads(SCRIPT_CACHE.read_text(encoding="utf-8"))

    items = "\n\n".join(
        f"【{m['id']}】{m['title']}\n{m['summary']}" for m in manifest
    )

    msg = client.chat.completions.create(
        model=LLM_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": PROMPT.format(items=items)}],
    )
    raw = msg.choices[0].message.content.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise HTTPException(status_code=500, detail=f"LLM JSON 解析失败: {raw[:200]}")
        data = json.loads(match.group())

    script = data.get("script", "").strip()
    if not script:
        raise HTTPException(status_code=500, detail="LLM 没返回 script")

    out = {
        "title": data.get("title", "今日通勤播客"),
        "script": script,
        "chapters": data.get("chapters", []),
    }
    SCRIPT_CACHE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ---------- Layer 2: synthesize ----------

async def synthesize(script_text: str) -> dict:
    if AUDIO_FILE.exists() and TIMING_CACHE.exists():
        return json.loads(TIMING_CACHE.read_text(encoding="utf-8"))

    communicate = edge_tts.Communicate(script_text, voice=TTS_VOICE, rate=TTS_RATE)
    transcript_lines: list[dict] = []
    last_end_sec = 0.0
    with open(AUDIO_FILE, "wb") as f:
        async for chunk in communicate.stream():
            t = chunk.get("type")
            if t == "audio":
                f.write(chunk["data"])
            elif t == "SentenceBoundary":
                offset_sec = chunk["offset"] / 1e7
                duration_sec = chunk.get("duration", 0) / 1e7
                transcript_lines.append({
                    "start": round(offset_sec, 2),
                    "text": chunk["text"].strip(),
                })
                last_end_sec = max(last_end_sec, offset_sec + duration_sec)

    if not transcript_lines:
        transcript_lines = [{"start": 0, "text": script_text.strip()}]
        last_end_sec = max(len(script_text) / 4.5, 1)

    total_sec = max(round(last_end_sec), 1)
    out = {
        "totalSec": total_sec,
        "transcriptLines": transcript_lines,
    }
    TIMING_CACHE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ---------- 对外接口 ----------

def _video_card(m: dict, prefix: str, idx: int) -> dict:
    return {
        "id": f"{prefix}{m['id']}",
        "title": m["title"],
        "snippet": (m["summary"][:60] + "…") if len(m["summary"]) > 60 else m["summary"],
        "author": "抖音收藏",
        "thumb": f"t{(idx % 4) + 1}",
        "videoUrl": f"/videos/{m['videoFile']}" if m["videoFile"] else "",
    }


@app.get("/api/brief")
async def get_brief():
    if not LLM_API_KEY:
        raise HTTPException(status_code=500, detail="LLM_API_KEY 未配置，请检查 .env")

    manifest = load_manifest()
    if not manifest:
        raise HTTPException(status_code=500, detail="manifest 为空，检查 video/title.txt 和 summary.txt")

    # 前 4 条 = 收藏来源（参与脚本生成），后 4 条 = 猜你想听（仅展示，不进 Layer 1）
    source_items = manifest[:4]
    rec_items = manifest[4:]

    script = build_script(source_items)
    audio = await synthesize(script["script"])

    chars_per_sec = max(len(script["script"]), 1) / max(audio["totalSec"], 1)
    chapters_out = [
        {
            "start": round(c.get("char_start", 0) / chars_per_sec),
            "title": c.get("title", ""),
        }
        for c in script.get("chapters", [])
    ]

    return {
        "title": script["title"],
        "audioUrl": "/audio/morncast.mp3",
        "totalSec": audio["totalSec"],
        "chapters": chapters_out,
        "transcriptLines": audio["transcriptLines"],
        "sources": [_video_card(m, "s", i) for i, m in enumerate(source_items)],
        "recommendations": [_video_card(m, "r", i) for i, m in enumerate(rec_items)],
    }
