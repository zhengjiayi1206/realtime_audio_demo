export class RealtimeAudioClient extends EventTarget {
  constructor(options = {}) {
    super();
    this.options = {
      healthUrl: "/health",
      recorderWorkletUrl: "/static/recorder-worklet.js",
      getWebSocketUrl: defaultWebSocketUrl,
      getModel: () => "",
      getPrompt: () => "",
      getPrefillMs: () => 600,
      ...options,
    };
    this.state = {
      ws: null,
      stream: null,
      audioContext: null,
      source: null,
      node: null,
      sink: null,
      startedAt: 0,
      chunks: 0,
      prefillOk: 0,
      history: [],
      maxHistoryTurns: 10,
      audioQueue: [],
      audioPlaying: false,
      currentAudio: null,
      audioTextByIndex: new Map(),
      playedAssistantText: "",
      finalReceived: false,
      finalText: "",
      historySavedForTurn: false,
      interrupted: false,
      streamedAudioCount: 0,
      streamingTextStarted: false,
      mode: "idle",
      timer: null,
    };
    this.setMode("idle");
  }

  on(type, listener) {
    this.addEventListener(type, (event) => listener(event.detail));
  }

  emit(type, detail = {}) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }

  log(message, level = "") {
    this.emit("log", { message, level });
  }

  setStatus(text, ok = false) {
    this.emit("status", { text, ok });
  }

  setVoiceState(text) {
    this.emit("voiceState", { text });
  }

  setMode(mode) {
    this.state.mode = mode;
    this.emit("mode", { mode });
  }

  emitMetrics() {
    const durationSeconds = this.state.startedAt
      ? (performance.now() - this.state.startedAt) / 1000
      : 0;
    this.emit("metrics", {
      chunks: this.state.chunks,
      prefillOk: this.state.prefillOk,
      durationSeconds,
    });
  }

  async loadServerConfig() {
    const response = await fetch(this.options.healthUrl);
    if (!response.ok) throw new Error(`health check failed: ${response.status}`);
    const config = await response.json();
    this.state.maxHistoryTurns = Number(config.max_history_turns || 10);
    this.emit("config", { config });
    this.log(
      `config model=${config.model} provider=${config.provider} prefill=${config.prefill_mode}`,
    );
    return config;
  }

  async handleAction() {
    if (this.state.mode === "idle") {
      await this.startRecording();
    } else if (this.state.mode === "recording") {
      await this.stopRecording();
    } else if (this.state.mode === "finalizing" || this.state.mode === "speaking") {
      await this.interruptAndRecord();
    }
  }

  async startRecording() {
    this.setMode("busy");
    this.emit("result", { text: "录音中。", replace: true });
    this.setVoiceState("倾听中");
    this.stopPlayback();
    this.emit("clearLog");

    this.state.chunks = 0;
    this.state.prefillOk = 0;
    this.state.streamedAudioCount = 0;
    this.state.streamingTextStarted = false;
    this.state.audioTextByIndex = new Map();
    this.state.playedAssistantText = "";
    this.state.finalReceived = false;
    this.state.finalText = "";
    this.state.historySavedForTurn = false;
    this.state.interrupted = false;
    this.emitMetrics();

    const socket = new WebSocket(this.options.getWebSocketUrl());
    this.state.ws = socket;
    socket.binaryType = "arraybuffer";
    socket.onmessage = (event) => this.onServerEvent(event, socket);
    socket.onerror = () => this.log("WebSocket 错误", "error");
    await new Promise((resolve, reject) => {
      socket.onopen = resolve;
      socket.onclose = () => reject(new Error("WebSocket closed before start"));
    });

    this.state.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });

    const chunkMs = Number(this.options.getPrefillMs() || 600);
    this.state.audioContext = new AudioContext({ latencyHint: "interactive" });
    await this.state.audioContext.audioWorklet.addModule(this.options.recorderWorkletUrl);
    this.state.source = this.state.audioContext.createMediaStreamSource(this.state.stream);
    this.state.node = new AudioWorkletNode(this.state.audioContext, "pcm-chunker", {
      processorOptions: { chunkMs },
    });
    this.state.sink = this.state.audioContext.createGain();
    this.state.sink.gain.value = 0;

    this.state.node.port.onmessage = (event) => {
      if (!event.data || event.data.type !== "chunk") return;
      if (!this.state.ws || this.state.ws !== socket || this.state.ws.readyState !== WebSocket.OPEN) return;
      socket.send(event.data.pcm);
      this.state.chunks += 1;
      this.emitMetrics();
    };

    this.state.source.connect(this.state.node);
    this.state.node.connect(this.state.sink);
    this.state.sink.connect(this.state.audioContext.destination);

    socket.send(
      JSON.stringify({
        type: "start",
        sampleRate: this.state.audioContext.sampleRate,
        prefillMs: chunkMs,
        model: this.options.getModel().trim(),
        prompt: this.options.getPrompt().trim(),
        history: this.state.history,
      }),
    );

    this.state.startedAt = performance.now();
    this.state.timer = setInterval(() => this.emitMetrics(), 100);
    this.setStatus("录音中", true);
    this.setMode("recording");
    this.log(`mic sampleRate=${this.state.audioContext.sampleRate}, chunkMs=${chunkMs}`);
  }

  async stopRecording() {
    this.setMode("finalizing");
    this.setStatus("停止录音，等待最终推理");
    this.setVoiceState("模型输出中");
    this.stopInputDevices();
    if (this.state.ws && this.state.ws.readyState === WebSocket.OPEN) {
      this.state.ws.send(JSON.stringify({ type: "stop" }));
    }
  }

  stopInputDevices() {
    if (this.state.node) this.state.node.disconnect();
    if (this.state.source) this.state.source.disconnect();
    if (this.state.sink) this.state.sink.disconnect();
    if (this.state.stream) this.state.stream.getTracks().forEach((track) => track.stop());
    if (this.state.audioContext) this.state.audioContext.close().catch(() => {});
    if (this.state.timer) clearInterval(this.state.timer);
    this.state.node = null;
    this.state.source = null;
    this.state.sink = null;
    this.state.stream = null;
    this.state.audioContext = null;
    this.state.timer = null;
  }

  onServerEvent(event, socket) {
    if (socket !== this.state.ws) return;
    const data = JSON.parse(event.data);
    switch (data.type) {
      case "ready":
        this.log(`session=${data.session_id}`);
        break;
      case "started":
        this.log(
          `server started model=${data.model} api=${data.qwen_api_base} history=${data.history_messages || 0}`,
        );
        break;
      case "chunk_received":
        this.log(`chunk ${data.chunk_index} received, duration=${data.duration_ms}ms`);
        break;
      case "prefill_ok":
        this.state.prefillOk += 1;
        this.emitMetrics();
        this.log(`prefill ${data.chunk_index} ok, latency=${data.latency_ms}ms`);
        break;
      case "prefill_error":
        this.log(`prefill ${data.chunk_index || ""} error: ${data.message || data.status_code}`, "error");
        break;
      case "finalizing":
        this.log(`finalizing ${data.chunks} chunks`);
        this.emit("result", { text: "模型输出中。", replace: true });
        this.setVoiceState("模型输出中");
        break;
      case "final_text_delta":
        if (!this.state.streamingTextStarted) {
          this.emit("result", { text: "", replace: true });
          this.state.streamingTextStarted = true;
        }
        this.emit("result", { text: data.text || "", append: true });
        break;
      case "final_audio_delta":
        this.state.streamedAudioCount += 1;
        this.enqueueAudio(data.audio_data_url, data.audio_index || this.state.streamedAudioCount);
        this.setVoiceState("正在播报中");
        this.log(`audio chunk ${data.audio_index || this.state.streamedAudioCount} received`);
        break;
      case "final_result":
        this.handleFinalResult(data, socket);
        break;
      case "final_error":
      case "error":
        this.emit("result", { text: data.message || "请求失败。", replace: true });
        this.setVoiceState("倾听中");
        this.setMode("idle");
        this.log(`${data.type}: ${data.message || data.status_code}`, "error");
        this.closeSocket(socket);
        break;
      default:
        this.log(JSON.stringify(data));
    }
  }

  handleFinalResult(data, socket) {
    this.setStatus("完成", true);
    this.state.finalReceived = true;
    const currentText = this.getDisplayedText();
    if (data.text) {
      this.state.finalText = data.text;
      this.emit("result", { text: data.text, replace: true });
    } else if (!this.state.streamingTextStarted) {
      this.emit("result", { text: "服务端没有返回文本。", replace: true });
      this.state.finalText = "";
    } else {
      this.state.finalText = currentText;
    }

    if (data.audio_data_url) {
      this.enqueueAudio(data.audio_data_url, this.state.streamedAudioCount + 1);
    } else if (this.state.streamedAudioCount === 0) {
      this.setVoiceState("播报完");
      this.saveCurrentTurnHistory(this.state.finalText || this.getDisplayedText());
      this.log("没有解析到音频输出。确认模型服务启用了音频输出和 stream。");
    }

    if (this.hasPendingPlayback()) {
      this.setMode("speaking");
    } else {
      this.saveCurrentTurnHistory(this.state.finalText || this.state.playedAssistantText || this.getDisplayedText());
      this.setMode("idle");
    }
    this.log(
      `final latency=${data.latency_ms}ms, audio_chunks=${data.audio_chunks || this.state.streamedAudioCount}, input=${data.saved_input_wav}`,
    );
    this.closeSocket(socket);
  }

  getDisplayedText() {
    return this.options.getDisplayedText ? this.options.getDisplayedText() : "";
  }

  closeWs() {
    if (this.state.ws && this.state.ws.readyState === WebSocket.OPEN) {
      this.state.ws.close();
    }
  }

  closeSocket(socket) {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.close();
    }
    if (this.state.ws === socket) {
      this.state.ws = null;
    }
  }

  hasPendingPlayback() {
    return this.state.audioPlaying || this.state.audioQueue.length > 0;
  }

  async interruptAndRecord() {
    if (this.state.mode !== "finalizing" && this.state.mode !== "speaking") return;
    this.state.interrupted = true;
    const historyText = this.state.playedAssistantText.trim();
    if (historyText) {
      this.saveCurrentTurnHistory(historyText);
    }
    this.stopPlayback();
    this.closeWs();
    this.setMode("busy");
    this.setStatus("已打断，准备录音", true);
    this.setVoiceState("倾听中");
    this.log(`interrupted, saved_played_text=${historyText ? "yes" : "no"}`);
    await this.startRecording();
  }

  stopPlayback() {
    if (this.state.currentAudio) {
      this.state.currentAudio.onended = null;
      this.state.currentAudio.onerror = null;
      this.state.currentAudio.pause();
      this.state.currentAudio = null;
    }
    this.state.audioQueue = [];
    this.state.audioPlaying = false;
  }

  enqueueAudio(audioDataUrl, audioIndex) {
    if (!audioDataUrl) return;
    const index = Number(audioIndex || this.state.streamedAudioCount || 1);
    this.state.audioTextByIndex.set(index, this.getDisplayedText());
    this.state.audioQueue.push({ url: audioDataUrl, index });
    if (!this.state.audioPlaying) {
      this.playNextAudio();
    }
  }

  playNextAudio() {
    const next = this.state.audioQueue.shift();
    if (!next) {
      this.state.audioPlaying = false;
      this.state.currentAudio = null;
      if (this.state.finalReceived && !this.state.interrupted) {
        this.saveCurrentTurnHistory(
          this.state.finalText || this.state.playedAssistantText || this.getDisplayedText(),
        );
        this.setMode("idle");
      }
      this.setVoiceState("播报完");
      return;
    }
    this.state.audioPlaying = true;
    this.setVoiceState("正在播报中");

    const player = new Audio(next.url);
    this.state.currentAudio = player;
    player.onended = () => {
      if (this.state.currentAudio === player) {
        this.state.currentAudio = null;
      }
      this.state.playedAssistantText =
        this.state.audioTextByIndex.get(next.index) || this.state.playedAssistantText || this.getDisplayedText();
      this.playNextAudio();
    };
    player.onerror = () => {
      this.log("audio chunk decode/play error, skip to next", "error");
      if (this.state.currentAudio === player) {
        this.state.currentAudio = null;
      }
      this.playNextAudio();
    };
    player.play().catch((err) => {
      this.log(`audio play blocked: ${err.message || err}`, "error");
      if (this.state.currentAudio === player) {
        this.state.currentAudio = null;
      }
      setTimeout(() => this.playNextAudio(), 0);
    });
  }

  rememberTurn(assistantText) {
    const text = (assistantText || "").trim();
    if (!text) return;
    this.state.history.push({
      role: "user",
      content: "[用户上一轮通过语音输入了一条消息]",
    });
    this.state.history.push({
      role: "assistant",
      content: text,
    });
    const maxMessages = Math.max(0, this.state.maxHistoryTurns * 2);
    if (maxMessages && this.state.history.length > maxMessages) {
      this.state.history = this.state.history.slice(-maxMessages);
    }
    this.log(`history saved, messages=${this.state.history.length}`);
  }

  saveCurrentTurnHistory(assistantText) {
    if (this.state.historySavedForTurn) return;
    const text = (assistantText || "").trim();
    if (!text || text === "模型输出中。" || text === "服务端没有返回文本。") return;
    this.state.historySavedForTurn = true;
    this.rememberTurn(text);
  }
}

function defaultWebSocketUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws/audio`;
}
