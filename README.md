# Qwen3-Omni Realtime Demo

这是一个基于 FastAPI + WebSocket 的 Qwen3-Omni 实时语音/文字测试项目。项目本身不部署大模型，只提供浏览器页面和后端转发层，用来连接已有的 Qwen3-Omni OpenAI-compatible 服务。

核心能力：

- 浏览器采集麦克风音频，按 PCM Float32 分块发送到后端。
- 后端把录音合成为 WAV，请求 `QWEN_API_BASE/v1/chat/completions`。
- 支持文本问答、语音问答、流式文本输出、可选语音播报。
- `/demo` 支持 Silero VAD 自动判断用户说完，并支持播报期间打断。
- `/chatbox` 支持 runtime skill 选择、意图识别、按意图切换特定 skill。
- `/chatbox` 的 system prompt 只来自勾选的 runtime skill，不再使用页面默认 prompt 或手写 system prompt。
- 支持本地 mock 组件调用，例如 `call_10901558` 开户网点查询。

## 项目定位

本项目是“网页后端 + 前端测试页”，不是模型服务。

你需要先启动或准备一个 Qwen3-Omni 兼容 OpenAI Chat Completions 的服务，例如：

```bash
curl http://127.0.0.1:5440/v1/models
```

默认模型服务地址：

```text
http://127.0.0.1:5440/v1
```

默认模型名：

```text
Qwen3-Omni-30B-A3B-Instruct
```

## 快速启动

安装依赖：

```bash
uv sync
```

前台启动：

```bash
bash ./run_demo.sh
```

后台启动：

```bash
bash ./run_demo_background.sh
tail -f qwen3_omni_demo.log
```

停止后台服务：

```bash
bash ./stop_demo.sh
```

默认监听：

```text
http://0.0.0.0:55785
```

本机浏览器访问：

```text
http://127.0.0.1:55785/chatbox
```

如果要换端口：

```bash
PORT=7861 bash ./run_demo.sh
```

也可以直接用 uvicorn：

```bash
uv run uvicorn app:app --host 0.0.0.0 --port 55785
```

如果服务器文件系统不支持 uv cache 锁，可以把 uv cache 放到临时目录：

```bash
export UV_CACHE_DIR="${TMPDIR:-/tmp}/uv-cache-${USER}"
uv sync
```

浏览器麦克风权限通常要求 `localhost` 或 HTTPS。远程服务器建议开 SSH 隧道：

```bash
ssh -L 55785:127.0.0.1:55785 <user>@<server> -p <port>
```

然后在本地浏览器打开：

```text
http://127.0.0.1:55785/chatbox
```

## 页面入口

### `/chatbox`

主要业务测试页面。适合验证 runtime skill、意图识别、语音输入、文字输入和语音播报。

页面能力：

- 底部输入框发送文字。
- 麦克风按钮发送单轮语音。
- 实时通话按钮开启 VAD 自动监听。
- 右侧选择 runtime skill。
- 右侧开启或关闭语音输出。
- 意图识别后自动切换到对应 skill。
- 多轮历史保存在当前浏览器页面内。

`/chatbox` 当前规则：

- system prompt 只等于当前勾选的 skill 内容。
- 页面没有手写 System prompt 输入框。
- 前端发送 `prompt: ""`。
- 后端在 `routeContext=chatbox` 时会忽略传入 prompt，只用 skill 生成 system prompt。
- 初始意图识别使用当前勾选的 skill。
- 意图识别选中某个具体 skill 后，后端只用这个 skill 重新请求模型。
- 选中具体 skill 后，前端会清空 history，并只勾选这个 skill，后续对话也只使用这个 skill。

### `/demo`

简洁语音演示页。适合测试“开始对话 -> 自动 VAD -> 模型回答 -> 语音播报 -> 插话打断”的完整链路。

特点：

- 后端 Silero VAD 判断语音开始和结束。
- 用户说完后自动停止录音并推理。
- 模型输出文字会流式显示。
- 开启语音输出时，后端把输出文本按句子分段合成语音，前端边生成边播放。
- 播报期间会额外打开 `/ws/vad` 做打断检测。

### `/chat`

普通文字聊天页。

特点：

- 类似 ChatGPT 的文本问答。
- 支持多个本地 session。
- 会话和消息保存在浏览器 `localStorage`。
- 服务端不持久化聊天记录。

### `/`

开发调试页，适合查看基础录音、chunk、prefill、最终推理等底层状态。

### `/realtime`

兼容旧入口，会重定向到：

```text
/chatbox
```

## Chatbox Skill 工作流

`/chatbox` 的 skill 是给模型使用的业务运行时上下文，不是 Codex 本机 skill。

推荐目录：

```text
runtime_skills/
└── company-realtime/
    └── SKILL.md
```

`SKILL.md` 示例：

```markdown
---
name: company-realtime
description: 平安银行客服通话场景规则。
---

你是平安银行的专业通话顾问。

输出必须是合法 JSON。
字段、层级、字段名、枚举值和必填项全部以本 Skill 定义为准。
不要在 JSON 外输出解释、Markdown 或代码块。
```

刷新 `/chatbox` 后，右侧 `Runtime skills` 会显示可用 skill。

默认勾选 skill：

```bash
REALTIME_DEFAULT_SKILLS=company-realtime bash ./run_demo.sh
```

调整 skill 注入上限：

```bash
REALTIME_SKILL_MAX_CHARS=30000 bash ./run_demo.sh
```

当前 `skill_loader` 行为：

- 读取 `runtime_skills/<skill-name>/SKILL.md`。
- frontmatter 中的 `name` 作为 skill 名称。
- frontmatter 中的 `description` 只给前端列表展示。
- 真正发给模型的 system prompt 是 skill 正文内容。
- 不再额外包 `<runtime_skill>` 标签。
- 不再拼接“以下是本轮实时语音对话必须遵循...”等额外说明。

## 意图识别和自动切换 Skill

`/chatbox` 的文字和语音请求会先使用当前勾选的 skill 进行当前轮处理。

如果模型输出能解析为意图 JSON，后端会：

1. 尝试解析模型输出中的 JSON。
2. 如果 JSON 不完整或格式异常，调用 `repair_intent_json` 进行修复。
3. 使用 `complete_intent_target` 补全意图 target。
4. 使用 `select_skill_for_intent` 在可用 runtime skill 中选择最匹配的 skill。
5. 如果选中 skill，则只用这个 skill 作为新的 system prompt，再请求模型生成业务回复。
6. 前端收到 `new_session=true` 后清空 history，并只勾选该 skill。

因此，确定具体 skill 之后，相当于开始一个新的业务会话：

- history 清空。
- system prompt 只保留选中的 skill。
- 后续每轮请求只使用这个 skill。

## History 规则

前端维护当前页面内的多轮 history，默认最多保留：

```text
MAX_HISTORY_TURNS=10
```

后端 `normalize_history` 会把最近 history 放在当前用户输入之前。

当前规则：

- 文本 history 只保存文本，不重复携带历史音频。
- 如果 assistant 输出是 JSON，history 保存完整 JSON 字符串。
- 不再只保存 JSON 里的 `content` 字段。
- 如果 history item 的 `content` 本身是对象或数组，会序列化成 JSON 字符串。
- 如果 history item 有除 `role` 外的额外字段，也会序列化为完整 JSON 字符串。

语音播报和 history 是分开的：

- `history_text` 保存完整内容，JSON 会完整保存。
- `speech_text` 用于语音播报。
- JSON 输出时，播报字段优先取 `content`，没有则取 `soundsName`。
- 不会使用 `playsentence` 字段播报。

## 语音链路

浏览器端：

1. `static/realtime-audio-client.js` 请求麦克风权限。
2. `static/recorder-worklet.js` 把麦克风音频转成单声道 Float32 PCM。
3. 前端按 `PREFILL_INTERVAL_MS` 分块发送 PCM 到 `/ws/audio`。
4. 停止录音时发送 `{"type":"stop"}`。

后端：

1. `/ws/audio` 接收 PCM chunk。
2. 如果开启 prefill probe，则把 chunk 放入 prefill 队列。
3. 停止录音后把全部 PCM 合成 WAV。
4. 调用 `QWEN_API_BASE/v1/chat/completions`。
5. 如果 `STREAM_FINAL_OUTPUT=1`，用 SSE 解析 text/audio delta。
6. 把最终文本、播报音频、history 信息发回前端。

生成 WAV 时会重采样到：

```text
TARGET_SAMPLE_RATE=16000
```

## Chunk Prefill 说明

Qwen3-Omni 论文里的 chunked prefill 是模型服务内部能力：音频/视觉编码器按时间维输出 chunk，Thinker 和 Talker 异步预填充并复用 cache。

当前 OpenAI-compatible `/v1/chat/completions` 是无状态 HTTP 接口，没有暴露可复用 KV/cache 的 prefill 句柄。

所以本项目实现的是 API 层 prefill 探测：

- `PREFILL_MODE=cumulative_probe`：每次对“截至当前 chunk 的累计音频前缀”发轻量请求。
- `PREFILL_MODE=off`：只在停止录音后做最终推理。

关闭 prefill probe：

```bash
PREFILL_MODE=off bash ./run_demo.sh
```

如果后续模型服务开放原生 cacheful prefill 接口，主要替换：

```text
realtime_audio_demo/routes/audio.py:run_prefill_probe
```

## Silero VAD

Silero VAD 用于检测用户开始说话、说完和播报打断。

默认配置：

```text
SILERO_VAD_ENABLED=1
SILERO_VAD_PRELOAD=1
SILERO_VAD_THRESHOLD=0.5
SILERO_VAD_MIN_SPEECH_MS=180
SILERO_VAD_MIN_SILENCE_MS=450
SILERO_VAD_MAX_SPEECH_MS=30000
SILERO_VAD_SPEECH_PAD_MS=30
```

关闭 VAD：

```bash
SILERO_VAD_ENABLED=0 bash ./run_demo.sh
```

Silero 默认在 CPU 上运行。FastAPI 启动时如果 `SILERO_VAD_PRELOAD=1`，会预加载模型，减少第一次使用时的延迟。

## 语音输出

`/chatbox` 默认只显示文字。打开右侧“语音输出”后：

- 后端会根据模型输出生成可播报文本。
- JSON 输出时优先播报 `content`。
- 如果没有 `content`，播报 `soundsName`。
- 如果两者都没有，则不播报。
- history 仍然保存完整 JSON。

`/demo` 会把输出文本按句子分段合成语音，边生成边播放。

## 组件调用

项目内置了一个本地 mock 组件：

```text
call_10901558
```

用途：

- 模拟开户网点查询。
- 如果模型输出中包含 `call_10901558`，后端会调用 `realtime_audio_demo/services/component_tools.py`。
- 当前返回固定 mock 数据，例如 `dm_vnoName`、`dm_vniAddr` 等字段。

组件相关接口：

```text
POST /api/chatbox/components/call
```

## HTTP 和 WebSocket 接口

页面接口：

```text
GET  /
GET  /demo
GET  /chat
GET  /chatbox
GET  /realtime
GET  /health
GET  /api/chatbox/skills
```

文字接口：

```text
POST /api/chat/text
POST /api/chatbox/text
POST /api/realtime/text
POST /api/chatbox/speech
POST /api/chatbox/components/call
```

WebSocket 接口：

```text
WS /ws/audio
WS /ws/vad
```

`/health` 会返回当前后端配置，例如模型名、API base、provider、VAD 状态、默认 skill、history 上限等。

## 关键环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | 后端监听地址，脚本中使用 |
| `PORT` | `55785` | 后端监听端口 |
| `QWEN_API_BASE` | `http://127.0.0.1:5440/v1` | Qwen OpenAI-compatible API 地址 |
| `QWEN_MODEL` | `Qwen3-Omni-30B-A3B-Instruct` | 模型名 |
| `QWEN_PROVIDER` | `auto` / 脚本默认 `vllm_omni` | Provider 适配模式 |
| `QWEN_MODALITIES` | `text,audio` | vLLM-Omni 输出 modalities |
| `QWEN_SPEAKER` | 空 | vLLM-Omni 音色 speaker |
| `QWEN_REQUEST_TIMEOUT` | `180` | 上游请求超时时间，秒 |
| `PREFILL_INTERVAL_MS` | `600` | 前端录音 chunk 大小 |
| `PREFILL_MODE` | `cumulative_probe` | prefill 探测模式，可设 `off` |
| `TARGET_SAMPLE_RATE` | `16000` | 后端输出 WAV 采样率 |
| `FINAL_MAX_TOKENS` | `512` | 最终推理最大 token |
| `MAX_HISTORY_TURNS` | `10` | history 最大轮数 |
| `STREAM_FINAL_OUTPUT` | `1` | 是否请求流式最终输出 |
| `SILERO_VAD_ENABLED` | `1` | 是否启用 Silero VAD |
| `SILERO_VAD_PRELOAD` | `1` | 启动时是否预加载 VAD |
| `SILERO_VAD_ONNX` | `0` | 是否使用 ONNX VAD |
| `SILERO_VAD_THRESHOLD` | `0.5` | VAD 阈值 |
| `SILERO_VAD_MIN_SPEECH_MS` | `180` | 判定说话开始的最短语音 |
| `SILERO_VAD_MIN_SILENCE_MS` | `450` | 判定说完的最短静默 |
| `SILERO_VAD_MAX_SPEECH_MS` | `30000` | 单轮最长语音 |
| `SILERO_VAD_SPEECH_PAD_MS` | `30` | VAD 语音边界 padding |
| `RUNTIME_SKILLS_DIR` | `runtime_skills` | runtime skill 目录 |
| `REALTIME_DEFAULT_SKILLS` | 空 | 默认勾选的 skill，逗号分隔 |
| `REALTIME_SKILL_MAX_CHARS` | `12000` | skill 注入字符上限 |
| `DEFAULT_CHAT_PROMPT` | 内置通用问答提示词 | `/chat` 页面默认 prompt |

## 代码结构

```text
.
├── app.py
├── pyproject.toml
├── run_demo.sh
├── run_demo_background.sh
├── stop_demo.sh
├── runtime_skills/
├── static/
│   ├── index.html
│   ├── demo.html
│   ├── chat.html
│   ├── chatbox.html
│   ├── realtime-audio-client.js
│   └── recorder-worklet.js
└── realtime_audio_demo/
    ├── main.py
    ├── config.py
    ├── sessions.py
    ├── events.py
    ├── routes/
    │   ├── pages.py
    │   ├── chat.py
    │   └── audio.py
    ├── services/
    │   ├── qwen.py
    │   ├── text_chat.py
    │   ├── skill_loader.py
    │   ├── intent_skill_router.py
    │   ├── component_tools.py
    │   ├── output_filter.py
    │   └── silero_vad.py
    └── utils/
        └── audio.py
```

主要文件说明：

- `app.py`：兼容 `uvicorn app:app` 的入口。
- `realtime_audio_demo/main.py`：创建 FastAPI 应用，挂载 `/static`，注册 routes，启动时预加载 Silero VAD。
- `realtime_audio_demo/config.py`：集中读取环境变量和默认配置。
- `realtime_audio_demo/sessions.py`：定义 WebSocket 音频会话状态。
- `realtime_audio_demo/events.py`：封装 WebSocket JSON 事件发送。
- `realtime_audio_demo/routes/pages.py`：页面路由、健康检查、skill 列表接口。
- `realtime_audio_demo/routes/chat.py`：普通文字聊天 `/api/chat/text`。
- `realtime_audio_demo/routes/audio.py`：核心语音和 Chatbox 业务路由，包括 `/ws/audio`、`/ws/vad`、`/api/chatbox/text`、`/api/chatbox/speech`。
- `realtime_audio_demo/services/qwen.py`：构造 Qwen 请求 payload、发送 HTTP/SSE 请求、解析模型文本和音频输出、处理 history。
- `realtime_audio_demo/services/text_chat.py`：文本问答通用封装。
- `realtime_audio_demo/services/skill_loader.py`：发现、读取、规范化 runtime skill，并生成 system prompt。
- `realtime_audio_demo/services/intent_skill_router.py`：意图 JSON 修复、意图 target 补全、按意图选择 skill。
- `realtime_audio_demo/services/component_tools.py`：本地 mock 组件调用。
- `realtime_audio_demo/services/output_filter.py`：拆分 `history_text` 和 `speech_text`。
- `realtime_audio_demo/services/silero_vad.py`：Silero VAD 加载和流式检测。
- `realtime_audio_demo/utils/audio.py`：Float32 PCM 转样本、线性重采样、WAV 编码。
- `static/realtime-audio-client.js`：前端实时语音客户端，负责录音、WebSocket、VAD 状态、播放、history。
- `static/recorder-worklet.js`：浏览器 AudioWorklet，把麦克风输入切成 PCM chunk。
- `static/chatbox.html`：Chatbox 页面 UI 和文字/语音业务交互。

## 请求 Payload 说明

语音最终请求在 `services/qwen.py:build_chat_payload` 中构造。

当前结构：

- system 消息：runtime skill 内容。
- history 消息：历史 user/assistant 文本。
- 当前 user 消息：音频 data URL + “请理解这段语音输入，并直接回答用户。”

这样可以避免把业务 prompt 当成用户当前输入，也能让音频作为当前用户输入被模型理解。

文字请求在 `services/qwen.py:build_text_payload` 中构造：

- history 放在前面。
- 当前用户输入作为最新 user 消息。
- `/chatbox` 的 prompt 只来自 skill。
- `/chat` 会使用普通文字聊天 prompt。

## 常见问题

### 浏览器没有麦克风权限

使用 `http://127.0.0.1:<port>` 或 HTTPS。远程服务器请使用 SSH 隧道。

### 页面能打开，但模型没反应

先确认模型服务可访问：

```bash
curl http://127.0.0.1:5440/v1/models
```

再看后端日志：

```bash
tail -f qwen3_omni_demo.log
```

### 只想测试最终推理，不想 prefill probe

```bash
PREFILL_MODE=off bash ./run_demo.sh
```

### Chatbox 没有 skill

确认目录存在：

```text
runtime_skills/<skill-name>/SKILL.md
```

刷新页面后查看右侧 Runtime skills。

### 语音播报没有声音

检查：

- `/chatbox` 右侧“语音输出”是否打开。
- 模型服务是否支持 audio 输出。
- `QWEN_MODALITIES` 是否包含 `audio`。
- JSON 输出里是否有 `content` 或 `soundsName`。

### 输出不是合法 JSON

优先检查当前选中的 `SKILL.md` 是否明确要求合法 JSON、字段格式和禁止 Markdown。`/chatbox` 不会再额外追加默认 system prompt，模型行为主要取决于当前 skill 内容。

## 开发建议

新增静态页面：

```python
@router.get("/new-page")
async def new_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "new-page.html")
```

新增后端业务路由：

1. 新建 `realtime_audio_demo/routes/xxx.py`。
2. 定义 `router = APIRouter()`。
3. 在 `realtime_audio_demo/main.py` 中 `app.include_router(xxx.router)`。

复用前端实时语音客户端：

```html
<script type="module">
  import { RealtimeAudioClient } from "/static/realtime-audio-client.js";

  const client = new RealtimeAudioClient({
    getModel: () => modelName,
    getPrompt: () => "",
    getRouteContext: () => "chatbox",
    getSkillNames: () => selectedSkills(),
    getPrefillMs: () => 600,
    getDisplayedText: () => resultBox.textContent,
  });

  client.on("mode", ({ mode }) => {
    console.log("mode", mode);
  });

  await client.loadServerConfig();
</script>
```

## Git 上传

查看改动：

```bash
git status
```

提交：

```bash
git add README.md realtime_audio_demo static run_demo.sh run_demo_background.sh stop_demo.sh app.py pyproject.toml
git commit -m "Document realtime chatbox demo"
```

推送当前分支：

```bash
git push origin "$(git branch --show-current)"
```
