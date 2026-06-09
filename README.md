# Qwen3-Omni 实时语音测试页

这个目录提供一个最小可运行的 Web 测试页：

- 浏览器实时采集麦克风 PCM。
- 前端每 `600ms` 发送一个音频 chunk 到后端 WebSocket。
- 默认录音阶段按最新音频前缀请求模型做 chunk prefill 探测。
- 停止录音后，后端把整段音频合成一个 WAV，再请求 `http://127.0.0.1:5440/v1/chat/completions` 做最终推理。
- 如果模型服务返回音频 base64，页面会直接播放；否则显示文本结果。

## 重要说明

Qwen3-Omni 论文里的 chunked prefill 是模型服务内部能力：音频/视觉编码器按时间维输出 chunk，Thinker 和 Talker 异步预填充并复用缓存。当前 OpenAI-compatible `/v1/chat/completions` 是无状态接口，没有暴露可复用 KV/cache 的 prefill 句柄。

因此这里实现的是 API 层 chunk prefill 探测框架。默认 `PREFILL_MODE=cumulative_probe`，录音时对“截至当前 chunk 的累计音频前缀”发轻量请求，停止后再对整段音频做最终推理。这个模式不会复用服务端 KV cache；如果后续服务端开放原生 cacheful prefill 接口，只需要替换 `app.py` 里的 `run_prefill_probe` 函数。

## 模型服务

本项目只启动网页后端，不包含模型部署命令。运行前确认已有 Qwen3-Omni OpenAI-compatible 服务监听在 `5440`：

```bash
curl http://127.0.0.1:5440/v1/models
```

## 启动测试网页后端

再开一个 screen：

```bash
screen -S qwen3_omni_demo
cd /hpc2ssd/JH_DATA/spooler/jzheng688/Speech_to_Speech/qwen3_omni_realtime_demo
uv sync
bash ./run_demo.sh
```

如果要后台启动：

```bash
bash ./run_demo_background.sh
tail -f qwen3_omni_demo.log
```

停止后台服务：

```bash
bash ./stop_demo.sh
```

如果服务器文件系统不支持 uv cache 锁，先把 cache 放到本地临时目录：

```bash
export UV_CACHE_DIR="${TMPDIR:-/tmp}/uv-cache-${USER}"
uv sync
```

默认监听：

```text
http://0.0.0.0:55785
```

开发者调试页：

```text
http://127.0.0.1:55785/
```

简洁语音演示页：

```text
http://127.0.0.1:55785/demo
```

浏览器麦克风权限通常要求 `localhost` 或 HTTPS。建议从本地机器开 SSH 隧道：

```bash
ssh -L 55785:127.0.0.1:55785 jzheng688-IMdcQIlY@10.120.18.240 -p 6988
```

然后在本地浏览器打开：

```text
http://127.0.0.1:55785
```

如果需要换端口：

```bash
PORT=7861 bash ./run_demo.sh
```

也可以不使用脚本，直接通过 uv 启动：

```bash
uv run uvicorn app:app --host 0.0.0.0 --port 55785
```

如果只想录音后最终请求，不做 600ms prefill 探测：

```bash
PREFILL_MODE=off bash ./run_demo.sh
```

## 关键环境变量

```bash
QWEN_API_BASE=http://127.0.0.1:5440/v1
QWEN_MODEL=Qwen3-Omni-30B-A3B-Instruct
QWEN_PROVIDER=vllm_omni
PREFILL_INTERVAL_MS=600
PREFILL_MODE=cumulative_probe
TARGET_SAMPLE_RATE=16000
FINAL_MAX_TOKENS=512
MAX_HISTORY_TURNS=10
STREAM_FINAL_OUTPUT=1
```

页面会在当前浏览器标签页内保存多轮历史。再次点击“开始录音”时，前端会把历史消息发给后端；后端把历史放在当前音频消息之前再请求模型。历史默认最多保留 `MAX_HISTORY_TURNS` 轮，只保存文本上下文，不重复携带旧音频。

`STREAM_FINAL_OUTPUT=1` 时，最终推理会请求 vLLM-Omni streaming output；后端收到 text/audio delta 后立即转发给前端，前端按收到的音频段排队播放。若服务端不支持流式音频输出，可以设置 `STREAM_FINAL_OUTPUT=0` 回退到完整响应后播放。

页面只有一个主交互按钮：空闲时是“开始录音”，录音中变为“停止并推理”，模型输出或播报时变为“打断”。打断后前端会立即停止当前播报、清空未播放音频，只把已经播完音频对应的文本前缀写入历史，然后开始新一轮录音。

## 文件

- `app.py`: FastAPI/WebSocket 后端。
- `static/index.html`: 浏览器测试页，只负责页面 DOM 绑定和渲染。
- `static/demo.html`: 简洁语音演示页，复用 `RealtimeAudioClient`。
- `static/realtime-audio-client.js`: 可复用的实时语音客户端类，封装录音、WebSocket、流式播放、打断和历史。
- `static/recorder-worklet.js`: 麦克风 PCM 600ms 分块。
- `run_demo.sh`: 启动测试网页后端。
- `run_demo_background.sh`: 后台启动测试网页后端。
- `stop_demo.sh`: 停止后台服务。

其他页面复用时直接导入客户端类：

```html
<script type="module">
  import { RealtimeAudioClient } from "/static/realtime-audio-client.js";

  const client = new RealtimeAudioClient({
    getModel: () => modelInput.value,
    getPrompt: () => promptInput.value,
    getPrefillMs: () => prefillInput.value,
    getDisplayedText: () => resultBox.textContent,
  });

  client.on("mode", ({ mode }) => {
    actionButton.textContent = mode === "recording" ? "停止并推理" : mode === "idle" ? "开始录音" : "打断";
  });

  actionButton.onclick = () => client.handleAction();
  client.loadServerConfig();
</script>
```
