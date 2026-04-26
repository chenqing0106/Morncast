#!/usr/bin/env python3
"""
预生成一个静态 demo（脚本 + 音频 + 句级时间戳）到 frontend/assets/demos/{id}/

用法：
    python scripts/pregen_demo.py scripts/demos/intern.json

输入：scripts/demos/{id}.json（manifest，格式见 *.example.json）
输出：
    frontend/assets/demos/{id}/meta.json   # 给前端 fetch 的元数据
    frontend/assets/demos/{id}/audio.mp3   # 静态音频

注意：必须在本地跑（服务器访问不到 Edge TTS）。生成完用 deploy.sh 同步过去。
"""
import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import edge_tts
from openai import OpenAI


ROOT = Path(__file__).resolve().parent.parent
DEMOS_OUT = ROOT / "frontend" / "assets" / "demos"

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "doubao-pro-32k")
TTS_VOICE = os.environ.get("TTS_VOICE", "zh-CN-XiaoxiaoNeural")
TTS_RATE = os.environ.get("TTS_RATE", "+0%")


PROMPT = """你是 Morncast 的播客脚本生成器。
把以下抖音视频的 AI 摘要整合成一段自然流畅的通勤播客脚本，口语化，约 400-600 字。
本期围绕「{topic}」主题展开，请保持话题聚焦，不要跳到无关领域。

要求：
1. 开头用 "早上好，欢迎收听 Morncast" 起句
2. 各条内容之间用过渡语衔接，不要生硬列举每一条
3. 结尾用一句有温度的话收尾

视频清单（编号 / 标题 / 摘要）：
{items}

输出严格的 JSON，不要有任何 markdown 代码块、不要有注释，格式如下：
{{
  "script": "完整播客脚本文字",
  "chapters": [
    {{"title": "章节名称（≤10字）", "char_start": 0}},
    {{"title": "第二章节名称", "char_start": 120}}
  ]
}}"""


def build_script(manifest: dict) -> dict:
    sources = manifest["sources"]
    if not sources:
        raise ValueError("manifest.sources 不能为空")

    items = "\n\n".join(
        f"【{s.get('id', i + 1)}】{s.get('title', '未命名')}\n{s.get('summary', '')}"
        for i, s in enumerate(sources)
    )

    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    print(f"→ 调 LLM ({LLM_MODEL}) 生成脚本…")
    msg = client.chat.completions.create(
        model=LLM_MODEL,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": PROMPT.format(topic=manifest["topic"], items=items),
        }],
    )
    raw = msg.choices[0].message.content.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError(f"LLM JSON 解析失败: {raw[:200]}")
        data = json.loads(match.group())

    script = data.get("script", "").strip()
    if not script:
        raise ValueError("LLM 没返回 script")

    return {
        "script": script,
        "chapters": data.get("chapters", []),
    }


async def synthesize(script_text: str, output_audio: Path) -> dict:
    print(f"→ Edge TTS 合成音频 → {output_audio.relative_to(ROOT)}")
    output_audio.parent.mkdir(parents=True, exist_ok=True)
    communicate = edge_tts.Communicate(script_text, voice=TTS_VOICE, rate=TTS_RATE)
    transcript_lines: list[dict] = []
    last_end_sec = 0.0
    with open(output_audio, "wb") as f:
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

    return {
        "totalSec": max(round(last_end_sec), 1),
        "transcriptLines": transcript_lines,
    }


def build_card(item: dict, prefix: str, idx: int) -> dict:
    snippet = item.get("snippet")
    if not snippet:
        summary = item.get("summary", "")
        snippet = (summary[:60] + "…") if len(summary) > 60 else summary
    video_file = item.get("videoFile") or ""
    return {
        "id": f"{prefix}{item.get('id', idx + 1)}",
        "title": item.get("title", ""),
        "snippet": snippet,
        "author": item.get("author", "抖音收藏"),
        "thumb": f"t{(idx % 4) + 1}",
        "videoUrl": f"/videos/{video_file}" if video_file else "",
    }


async def main_async(manifest_path: Path) -> None:
    if not LLM_API_KEY:
        print("❌ LLM_API_KEY 未配置，请检查 .env", file=sys.stderr)
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for key in ("id", "topic", "sources"):
        if key not in manifest:
            print(f"❌ manifest 缺少字段：{key}", file=sys.stderr)
            sys.exit(1)

    demo_id = manifest["id"]
    print(f"==> 生成 demo: {demo_id} ({manifest['topic']})")

    out_dir = DEMOS_OUT / demo_id
    out_dir.mkdir(parents=True, exist_ok=True)
    audio_path = out_dir / "audio.mp3"

    script_data = build_script(manifest)
    tts_data = await synthesize(script_data["script"], audio_path)

    total_sec = tts_data["totalSec"]
    script_len = max(len(script_data["script"]), 1)
    chars_per_sec = script_len / max(total_sec, 1)
    chapters = [
        {
            "start": min(round(c.get("char_start", 0) / chars_per_sec), total_sec),
            "title": c.get("title", ""),
        }
        for c in script_data.get("chapters", [])
    ]

    meta = {
        "id": demo_id,
        "topic": manifest["topic"],
        "title": manifest.get("title", manifest["topic"]),
        "status": "ready",
        "audioUrl": f"/assets/demos/{demo_id}/audio.mp3",
        "totalSec": total_sec,
        "chapters": chapters,
        "transcriptLines": tts_data["transcriptLines"],
        "sources": [build_card(s, "s", i) for i, s in enumerate(manifest["sources"])],
        "recommendations": [
            build_card(r, "r", i)
            for i, r in enumerate(manifest.get("recommendations", []))
        ],
    }
    meta_path = out_dir / "meta.json"
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"✓ meta.json  → {meta_path.relative_to(ROOT)}")
    print(f"✓ audio.mp3  → {audio_path.relative_to(ROOT)} ({total_sec}s)")
    print(f"✓ 章节 {len(chapters)} 条 / 逐字稿 {len(tts_data['transcriptLines'])} 句")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="manifest JSON 文件路径")
    args = parser.parse_args()
    if not args.manifest.exists():
        print(f"❌ manifest 不存在：{args.manifest}", file=sys.stderr)
        sys.exit(1)
    asyncio.run(main_async(args.manifest))


if __name__ == "__main__":
    main()
