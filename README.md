# Morncast

把你收藏的抖音 AI 摘要，变成一段能在通勤路上听完的播客。

## 功能

- 粘贴抖音视频 AI 摘要 → 自动生成播客脚本
- TTS 合成音频，支持逐字稿高亮跟读
- 章节大纲 + 进度跳转
- 收藏来源管理 + 延伸推荐
- 移动端竖屏优先设计

## 技术栈

| 层 | 技术 |
|---|---|
| 前端 | 原生 HTML / CSS / JS（单文件） |
| 后端 | Python · FastAPI |
| LLM | 火山引擎豆包（任何 OpenAI 兼容接口均可） |
| TTS | Edge TTS（微软晓晓音色） |

## 快速开始

**1. 安装依赖**

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p audio_cache
```

**2. 启动后端**

```bash
LLM_API_KEY=你的火山方舟APIKey \
.venv/bin/uvicorn server:app --reload
```

**3. 打开前端**

直接用浏览器打开 `index.html`，或用任意静态服务器托管。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_API_KEY` | 必填 | 大模型 API Key |
| `LLM_BASE_URL` | `https://ark.cn-beijing.volces.com/api/v3` | 兼容 OpenAI 格式的接口地址 |
| `LLM_MODEL` | `doubao-pro-32k` | 模型名称 |

切换到其他模型示例：

```bash
# Qwen
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1 \
LLM_MODEL=qwen-plus \
LLM_API_KEY=你的Key \
.venv/bin/uvicorn server:app --reload

# DeepSeek
LLM_BASE_URL=https://api.deepseek.com/v1 \
LLM_MODEL=deepseek-chat \
LLM_API_KEY=你的Key \
.venv/bin/uvicorn server:app --reload
```

## API

### `POST /api/generate-brief`

**请求体**

```json
{
  "summaries": [
    "第一条抖音 AI 摘要文字",
    "第二条抖音 AI 摘要文字"
  ]
}
```

**响应**

```json
{
  "audioUrl": "http://localhost:8000/audio/abc123.mp3",
  "totalSec": 82,
  "chapters": [{ "start": 0, "title": "开场" }],
  "transcriptLines": [{ "start": 0, "text": "早上好，欢迎收听 Morncast..." }],
  "sources": [{ "id": "s1", "title": "视频标题", "snippet": "摘要", "author": "抖音", "thumb": "t1" }]
}
```

## 目录结构

```
├── index.html        # 前端（单文件）
├── server.py         # 后端 API
├── requirements.txt  # Python 依赖
├── audio_cache/      # TTS 生成的音频文件（运行时生成）
└── assets/
    └── hero-daily-brief.png
```
