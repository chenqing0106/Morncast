# Morncast 后端跑通计划（无感版）

> 目标：用户打开页面 → 直接听到由 `video/` 目录素材生成的播客，**零输入、零等待**。
>
> 当前分支：`main`。

---

## 1. 数据源盘点（`video/` 已就绪）

```
video/
├── title.txt        # 8 条视频标题（编号 1–8）
├── summary.txt      # 8 条视频 AI 摘要（编号 1–8，空行分隔）
├── 强烈推荐6个自用skills.mp4
├── OpenCode详细攻略.mp4
├── 推荐三个超级实用的skills.mp4
├── 让vibecoding 产品变好看的四个工具.mp4
├── Gemini 难用到抓狂？这个开源神器直接掀桌子.mp4
├── github开源，10w+ AI提示词写法.mp4
├── AI编程中控 Skills.mp4
└── 装完claudecode去哪里.mp4
```

`title.txt` 和 `summary.txt` 都用「编号、」做锚点，能正则切成 8 条结构化记录。每条记录可以匹配到一个 mp4 文件（按编号顺序对应 `title.txt` 的行序）。

**结论：摘要文本 + 视频文件齐全，不需要任何额外用户输入。**

---

## 2. 两层流水线

整个后端就是两层独立的转换，各自一个函数、各自一份缓存，可以单独跑、单独重算。

```
                ┌────────────────────────────────────────┐
Layer 1         │ build_script(manifest) → script.json   │
（内容层 / LLM）│ video/summary.txt + title.txt          │
                │   → 解析成 8 条结构化记录              │
                │   → LLM 整合成播客脚本                 │
                │ 产物：节目标题、正文、章节、推荐       │
                │ 缓存：data/script.json                 │
                └────────────────────────────────────────┘
                                  │
                                  ▼
                ┌────────────────────────────────────────┐
Layer 2         │ synthesize(script) → audio + timing    │
（语音层 / TTS）│ Edge TTS stream() 合成 MP3 +           │
                │   采集 WordBoundary 真实时间戳         │
                │ 产物：mp3 文件、totalSec、句级时间戳   │
                │ 缓存：audio_cache/morncast.mp3 +       │
                │        data/timing.json                │
                └────────────────────────────────────────┘
                                  │
                                  ▼
                ┌────────────────────────────────────────┐
对外            │ GET /api/brief                          │
                │ = script.json + timing.json + sources   │
                │   （sources 直接来自 manifest）         │
                └────────────────────────────────────────┘
```

**为什么分层**：
- 改 LLM Prompt → 只需删 `data/script.json`，TTS 不重跑（省 Edge TTS 调用）
- 换音色/调语速 → 只需删 `audio_cache/morncast.mp3` + `data/timing.json`，LLM 不重跑（省钱）
- 调试时可以直接打开 `data/script.json` 看脚本对不对，甚至手工改完再触发 Layer 2

**前端动线不变**：

```
前端打开 /  →  GET /api/brief  →  hydrate 四个 Tab
sources 卡片点击  →  /videos/<filename>.mp4  →  新标签页播放原视频
```

---

## 3. 后端要改/加的东西

### Layer 0 · 公共：manifest 解析

新增 `load_manifest()`（写在 `server.py` 顶部即可）：解析 `video/title.txt` + `video/summary.txt` → 返回 8 条结构化记录：

```python
[
  {"id": 1, "title": "强烈推荐6个自用skills", "summary": "...", "videoFile": "强烈推荐6个自用skills.mp4"},
  {"id": 2, "title": "OpenCode详细攻略",      "summary": "...", "videoFile": "OpenCode详细攻略.mp4"},
  ...
]
```

匹配 `videoFile`：按 title 模糊匹配 `video/*.mp4`（去空格、去标点比对），匹配失败留空。

### Layer 1 · 内容层

**函数**：`build_script(manifest) -> dict`

输入 8 条 manifest，输出：

```jsonc
{
  "title": "今日通勤｜AI 编程工具与 Skill 清单",
  "script": "早上好，欢迎收听 Morncast……",      // 完整正文
  "chapters": [{"char_start": 0, "title": "开场"}, ...],
  "recommendations": [{"id": "r1", "title": "...", "author": "...", "snippet": "...", "thumb": "t2"}, ...]
}
```

**缓存**：`data/script.json`，命中直接返回，未命中调 LLM 写入。

**注意**：sources **不在这里生成**，避免 LLM 幻觉视频名 —— 让 manifest 直接当 sources。

### Layer 2 · 语音层

**函数**：`synthesize(script_text) -> dict`

输入正文字符串，输出：

```jsonc
{
  "audioFile": "audio_cache/morncast.mp3",
  "totalSec": 240,
  "transcriptLines": [{"start": 0, "text": "早上好..."}, ...]
}
```

**缓存**：`audio_cache/morncast.mp3` + `data/timing.json`（句级时间戳）。两者都存在才算命中，缺一就重跑。

**实现要点（WordBoundary）**：

```python
async def synthesize(script_text: str) -> dict:
    communicate = edge_tts.Communicate(script_text, voice="zh-CN-XiaoxiaoNeural")
    boundaries = []  # [(offset_sec, text_at_that_moment), ...]
    with open("audio_cache/morncast.mp3", "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                boundaries.append((chunk["offset"] / 1e7, chunk["text"]))
                # offset 单位是 100ns（HNS），除以 1e7 得到秒
    # 按句号/感叹号/问号断句，每句 start = 该句首字 boundary 的 offset
    transcript_lines = _split_to_lines_by_boundary(script_text, boundaries)
    total_sec = boundaries[-1][0] + 1 if boundaries else 0
    return {"audioFile": "...", "totalSec": total_sec, "transcriptLines": transcript_lines}
```

不再用 `chars_per_sec` 估算。`_split_to_lines_by_boundary` 用 `script_text` 里 `[。！？…]` 的字符位置去 boundaries 列表里找对应的真实秒数。

### 对外接口

**`GET /api/brief`**（替代旧的 POST `/api/generate-brief`）：

```python
def get_brief():
    manifest = load_manifest()
    script  = build_script(manifest)        # Layer 1，自带 cache
    audio   = synthesize(script["script"])  # Layer 2，自带 cache
    return {
        **script,                            # title, chapters, recommendations
        "audioUrl":         "/audio/morncast.mp3",
        "totalSec":         audio["totalSec"],
        "transcriptLines":  audio["transcriptLines"],
        "sources": [
            {"id": f"s{m['id']}", "title": m["title"], "snippet": m["summary"][:60],
             "author": "抖音收藏", "thumb": f"t{((m['id']-1) % 4) + 1}",
             "videoUrl": f"/videos/{m['videoFile']}"}
            for m in manifest
        ],
    }
```

### 静态目录

```python
app.mount("/videos", StaticFiles(directory="video"), name="videos")
```

让前端 sources 卡片可以点击播放原视频。

### 前端最小改动

- 删除 `frontend/index.html:1174` 起的写死 `transcriptLines / SOURCES_INITIAL / recommendations / chapters`
- 启动时 `fetch('/api/brief')`，把响应灌进现有渲染函数
- sources 卡片点击 → `window.open(videoUrl)`
- 加一个简单 loading 态（首次冷启动 LLM+TTS 大约 5–15 秒；二次访问秒开）

---

## 4. 接口契约

```http
GET /api/brief
```

```jsonc
{
  "title": "今日通勤｜AI 编程工具与 Skill 清单",
  "audioUrl": "/audio/morncast.mp3",
  "totalSec": 240,
  "chapters":        [{"start": 0,  "title": "开场"}],
  "transcriptLines": [{"start": 0,  "text":  "早上好..."}],
  "sources": [
    {"id": "s1", "title": "强烈推荐6个自用skills", "snippet": "...",
     "author": "抖音收藏", "thumb": "t1",
     "videoUrl": "/videos/强烈推荐6个自用skills.mp4"}
  ],
  "recommendations": [
    {"id": "r1", "title": "...", "author": "...", "snippet": "...", "thumb": "t2"}
  ]
}
```

---

## 5. 文件结构（改动后）

```
agents-a3fcc025a1/
├── server.py                # load_manifest + build_script + synthesize + GET /api/brief
├── video/                   # 已有，内置数据源
│   ├── title.txt
│   ├── summary.txt
│   └── *.mp4
├── data/                    # 新增（不入 git）
│   ├── script.json          # Layer 1 缓存：脚本+章节+推荐
│   └── timing.json          # Layer 2 缓存：句级时间戳
├── audio_cache/
│   └── morncast.mp3         # Layer 2 缓存：音频，文件名固定
└── frontend/index.html      # 删 demo 数据，改 fetch /api/brief
```

**手工触发重算**：

| 改了什么 | 删什么 |
|---|---|
| LLM Prompt / 摘要内容 | `data/script.json`（会顺带让 timing 也失效） |
| 音色 / 语速 | `audio_cache/morncast.mp3` + `data/timing.json` |
| 全部 | 上面三个全删 |

---

## 6. 开发顺序（一个下午搞定）

1. **Layer 0 + Layer 1**（25 分钟）
   - `load_manifest()` 解析 title.txt + summary.txt + 匹配 mp4
   - `build_script()` + `data/script.json` 缓存
   - 命令行单测一次：删缓存、跑 build_script、看 JSON 内容是否合理
2. **Layer 2**（25 分钟）
   - `synthesize()` 用 `Communicate.stream()` 收 WordBoundary
   - `audio_cache/morncast.mp3` + `data/timing.json` 缓存
   - 命令行单测：跑一次，确认 mp3 能播放、timing.json 里的秒数与音频对得上
3. **接口拼装**（10 分钟）
   - `GET /api/brief` 拼三层结果
   - 挂载 `/videos/*` 静态目录
4. **前端接通**（30 分钟）
   - 删写死数据，加 fetch + loading
   - sources 卡片加点击跳转
5. **联调跑通**（10 分钟）
   - 删全部 cache 冷启动一次
   - 验证音频、文字稿高亮、章节、来源卡片都正常

---

## 7. 暂不做（明确砍掉）

- ❌ 抖音链接 → 字幕提取
- ❌ 视频缩略图截帧（继续用 t1–t4 色块）
- ❌ 取消收藏的持久化
- ❌ 错误重试 UI

---

请审核：这个方向（无感、cache-first、video 目录直读）对吗？没问题我就动手。
