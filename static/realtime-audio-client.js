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
      getSkillNames: () => [],
      getOutputAudio: () => false,
      getStreamSpeechAudio: () => false,
      getVadConfig: () => null,
      getBargeInVadConfig: () => null,
      getVadMonitorWebSocketUrl: defaultVadMonitorWebSocketUrl,
      useWebAudioPlayback: false,
      enableBargeIn: false,
      bargeInAfterFinalResultOnly: false,
      bargeInChunkMs: 120,
      bargeInCooldownMs: 800,
      autoVad: false,
      vadFrameMs: 50,
      vadThreshold: 0.018,
      vadMinSpeechMs: 180,
      vadMinSilenceMs: 850,
      vadMaxSpeechMs: 18000,
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
      finalHistoryText: "",
      historySavedForTurn: false,
      interrupted: false,
      streamedAudioCount: 0,
      streamingTextStarted: false,
      mode: "idle",
      timer: null,
      flushResolve: null,
      playbackContext: null,
      playbackGeneration: 0,
      bargeInWs: null,
      bargeInStream: null,
      bargeInAudioContext: null,
      bargeInSource: null,
      bargeInNode: null,
      bargeInSink: null,
      bargeInStartedAt: 0,
      bargeInStarting: false,
      bargeInTriggered: false,
      vad: this.createVadState(),
    };
    this.setMode("idle");
  }

  createVadState() {
    return {
      enabled: Boolean(this.options.autoVad),
      speechStarted: false,
      autoStopping: false,
      speechMs: 0,
      silenceMs: 0,
      totalSpeechMs: 0,
      latestRms: 0,
    };
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
    this.state.finalHistoryText = "";
    this.state.historySavedForTurn = false;
    this.state.interrupted = false;
    this.state.bargeInTriggered = false;
    this.state.vad = this.createVadState();
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
      processorOptions: { chunkMs, vadFrameMs: this.options.vadFrameMs },
    });
    this.state.sink = this.state.audioContext.createGain();
    this.state.sink.gain.value = 0;

    this.state.node.port.onmessage = (event) => {
      if (!event.data) return;
      if (event.data.type === "level") {
        this.handleVadLevel(event.data);
        return;
      }
      if (event.data.type === "flushed") {
        this.resolveFlush();
        return;
      }
      if (event.data.type !== "chunk") return;
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
        skillNames: this.options.getSkillNames(),
        outputAudio: Boolean(this.options.getOutputAudio()),
        streamSpeechAudio: Boolean(this.options.getStreamSpeechAudio()),
        vad: this.options.getVadConfig(),
        history: this.state.history,
      }),
    );

    this.state.startedAt = performance.now();
    this.state.timer = setInterval(() => this.emitMetrics(), 100);
    this.setStatus("录音中", true);
    this.setMode("recording");
    this.log(`mic sampleRate=${this.state.audioContext.sampleRate}, chunkMs=${chunkMs}`);
    if (this.options.autoVad) {
      this.setVoiceState("等待说话");
      this.emit("vad", { event: "waiting", speaking: false, rms: 0 });
    }
  }

  async unlockAudioOutput() {
    if (!this.options.useWebAudioPlayback) return;
    const context = this.ensurePlaybackContext();
    if (context.state === "suspended") {
      await context.resume();
    }
    const source = context.createBufferSource();
    source.buffer = context.createBuffer(1, 1, context.sampleRate);
    source.connect(context.destination);
    source.start();
  }

  ensurePlaybackContext() {
    if (!this.state.playbackContext) {
      const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextCtor) throw new Error("WebAudio is not supported by this browser");
      this.state.playbackContext = new AudioContextCtor();
    }
    return this.state.playbackContext;
  }

  async startBargeInMonitor() {
    if (!this.options.enableBargeIn || this.state.bargeInWs || this.state.bargeInStarting) return;
    if (!this.state.audioPlaying) return;

    this.state.bargeInStarting = true;
    const generation = this.state.playbackGeneration;
    let socket = null;
    try {
      socket = new WebSocket(this.options.getVadMonitorWebSocketUrl());
      this.state.bargeInWs = socket;
      socket.binaryType = "arraybuffer";
      socket.onmessage = (event) => this.onBargeInEvent(event, socket, generation);
      socket.onerror = () => this.log("barge-in VAD WebSocket 错误", "error");
      await new Promise((resolve, reject) => {
        socket.onopen = resolve;
        socket.onclose = () => reject(new Error("barge-in VAD WebSocket closed before start"));
      });

      if (generation !== this.state.playbackGeneration || !this.state.audioPlaying) {
        this.stopBargeInMonitor();
        return;
      }

      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      this.state.bargeInStream = stream;
      const audioContext = new AudioContext({ latencyHint: "interactive" });
      this.state.bargeInAudioContext = audioContext;
      await audioContext.audioWorklet.addModule(this.options.recorderWorkletUrl);
      const source = audioContext.createMediaStreamSource(stream);
      const node = new AudioWorkletNode(audioContext, "pcm-chunker", {
        processorOptions: {
          chunkMs: Number(this.options.bargeInChunkMs || 120),
          vadFrameMs: this.options.vadFrameMs,
        },
      });
      const sink = audioContext.createGain();
      sink.gain.value = 0;

      if (generation !== this.state.playbackGeneration || !this.state.audioPlaying) {
        this.stopBargeInMonitor();
        return;
      }

      this.state.bargeInSource = source;
      this.state.bargeInNode = node;
      this.state.bargeInSink = sink;
      this.state.bargeInStartedAt = performance.now();

      node.port.onmessage = (event) => {
        if (!event.data || event.data.type !== "chunk") return;
        if (this.state.bargeInWs !== socket || socket.readyState !== WebSocket.OPEN) return;
        socket.send(event.data.pcm);
      };

      source.connect(node);
      node.connect(sink);
      sink.connect(audioContext.destination);

      const vad = this.options.getBargeInVadConfig() || this.options.getVadConfig();
      socket.send(
        JSON.stringify({
          type: "start",
          sampleRate: audioContext.sampleRate,
          vad,
        }),
      );
      this.log("barge-in VAD monitor started");
    } catch (err) {
      this.log(`barge-in monitor start failed: ${err.message || err}`, "error");
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.close();
      }
      this.stopBargeInMonitor();
    } finally {
      this.state.bargeInStarting = false;
    }
  }

  stopBargeInMonitor() {
    if (this.state.bargeInNode) this.state.bargeInNode.disconnect();
    if (this.state.bargeInSource) this.state.bargeInSource.disconnect();
    if (this.state.bargeInSink) this.state.bargeInSink.disconnect();
    if (this.state.bargeInStream) {
      this.state.bargeInStream.getTracks().forEach((track) => track.stop());
    }
    if (this.state.bargeInAudioContext) {
      this.state.bargeInAudioContext.close().catch(() => {});
    }
    if (this.state.bargeInWs && this.state.bargeInWs.readyState < WebSocket.CLOSING) {
      try {
        if (this.state.bargeInWs.readyState === WebSocket.OPEN) {
          this.state.bargeInWs.send(JSON.stringify({ type: "stop" }));
        }
        this.state.bargeInWs.close();
      } catch (_err) {
        // Ignore close races while tearing down barge-in monitoring.
      }
    }
    this.state.bargeInWs = null;
    this.state.bargeInStream = null;
    this.state.bargeInAudioContext = null;
    this.state.bargeInSource = null;
    this.state.bargeInNode = null;
    this.state.bargeInSink = null;
    this.state.bargeInStartedAt = 0;
    this.state.bargeInStarting = false;
  }

  onBargeInEvent(event, socket, generation) {
    if (this.state.bargeInWs !== socket || generation !== this.state.playbackGeneration) return;
    const data = JSON.parse(event.data);
    switch (data.type) {
      case "ready":
      case "vad_ready":
      case "vad_monitor_started":
        this.log(`barge-in ${data.type}`);
        break;
      case "vad_speech_start":
        if (performance.now() - this.state.bargeInStartedAt < Number(this.options.bargeInCooldownMs || 800)) {
          this.log("barge-in ignored during cooldown");
          return;
        }
        this.emit("bargeIn", { probability: data.probability, timeMs: data.time_ms });
        this.bargeInAndRecord().catch((err) => {
          this.log(`barge-in interrupt failed: ${err.message || err}`, "error");
          this.setMode("idle");
        });
        break;
      case "vad_error":
      case "error":
        this.log(`barge-in VAD error: ${data.message || data.type}`, "error");
        this.stopBargeInMonitor();
        break;
      default:
        break;
    }
  }

  async bargeInAndRecord() {
    if (!this.options.enableBargeIn || this.state.bargeInTriggered) return;
    if (!this.state.audioPlaying && this.state.mode !== "speaking" && this.state.mode !== "finalizing") return;

    this.state.bargeInTriggered = true;
    this.state.interrupted = true;
    const historyText = this.state.playedAssistantText.trim();
    if (historyText) {
      this.saveCurrentTurnHistory(historyText);
    }
    this.stopBargeInMonitor();
    this.stopPlayback();
    this.closeWs();
    this.setMode("busy");
    this.setStatus("检测到插话，开始录音", true);
    this.setVoiceState("正在听你说");
    this.log(`barge-in detected, saved_played_text=${historyText ? "yes" : "no"}`);
    await this.startRecording();
  }

  async stopRecording() {
    if (this.state.mode !== "recording") return;
    this.setMode("finalizing");
    this.setStatus("停止录音，等待最终推理");
    this.setVoiceState("模型输出中");
    await this.flushInput();
    this.stopInputDevices();
    if (this.state.ws && this.state.ws.readyState === WebSocket.OPEN) {
      this.state.ws.send(JSON.stringify({ type: "stop" }));
    }
  }

  async flushInput() {
    if (!this.state.node) return;
    await new Promise((resolve) => {
      const timer = setTimeout(() => {
        if (this.state.flushResolve === done) {
          this.state.flushResolve = null;
        }
        resolve();
      }, 250);
      const done = () => {
        clearTimeout(timer);
        resolve();
      };
      this.state.flushResolve = done;
      this.state.node.port.postMessage({ type: "flush" });
    });
  }

  resolveFlush() {
    if (!this.state.flushResolve) return;
    const resolve = this.state.flushResolve;
    this.state.flushResolve = null;
    resolve();
  }

  handleVadLevel(data) {
    const vad = this.state.vad;
    if (!vad.enabled || this.state.mode !== "recording" || vad.autoStopping) return;

    const durationMs = Number(data.durationMs || this.options.vadFrameMs || 50);
    const rms = Number(data.rms || 0);
    const speaking = rms >= Number(this.options.vadThreshold || 0.018);
    vad.latestRms = rms;

    if (speaking) {
      vad.speechMs += durationMs;
      vad.totalSpeechMs += durationMs;
      vad.silenceMs = 0;
      if (!vad.speechStarted && vad.speechMs >= Number(this.options.vadMinSpeechMs || 180)) {
        vad.speechStarted = true;
        this.setVoiceState("正在听你说");
        this.emit("vad", { event: "speech_start", speaking: true, rms });
      }
      if (vad.speechStarted && vad.totalSpeechMs >= Number(this.options.vadMaxSpeechMs || 18000)) {
        this.autoStopByVad("max_speech");
      }
      return;
    }

    vad.speechMs = 0;
    if (!vad.speechStarted) {
      this.emit("vad", { event: "silence", speaking: false, rms });
      return;
    }

    vad.silenceMs += durationMs;
    this.emit("vad", { event: "speech_silence", speaking: false, rms, silenceMs: vad.silenceMs });
    if (vad.silenceMs >= Number(this.options.vadMinSilenceMs || 850)) {
      this.autoStopByVad("speech_end");
    }
  }

  autoStopByVad(reason) {
    const vad = this.state.vad;
    if (vad.autoStopping || this.state.mode !== "recording") return;
    vad.autoStopping = true;
    this.setVoiceState("检测到说完，正在推理");
    this.emit("vad", { event: reason, speaking: false, rms: vad.latestRms });
    this.stopRecording().catch((err) => {
      this.log(`vad stop error: ${err.message || err}`, "error");
      this.setMode("idle");
    });
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
        if (Array.isArray(data.skills) && data.skills.length) {
          this.log(`skills ${data.skills.join(", ")}`);
        }
        if (Array.isArray(data.missing_skills) && data.missing_skills.length) {
          this.log(`missing skills ${data.missing_skills.join(", ")}`, "error");
        }
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
      case "vad_ready":
        this.setVoiceState("等待说话");
        this.emit("vad", { event: "ready", engine: data.engine, threshold: data.threshold });
        this.log(
          `vad ready engine=${data.engine} threshold=${data.threshold} silence=${data.min_silence_ms}ms`,
        );
        break;
      case "vad_speech_start":
        this.setVoiceState("正在听你说");
        this.emit("vad", { event: "speech_start", speaking: true, probability: data.probability });
        break;
      case "vad_speech_end":
        this.setVoiceState("检测到说完，正在推理");
        this.emit("vad", { event: data.event || "speech_end", speaking: false, probability: data.probability });
        this.stopRecording().catch((err) => {
          this.log(`server vad stop error: ${err.message || err}`, "error");
          this.setMode("idle");
        });
        break;
      case "vad_error":
        this.log(`vad error: ${data.message}`, "error");
        this.setVoiceState("VAD 不可用");
        this.emit("vad", { event: "error", message: data.message });
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
        this.enqueueAudio(
          data.audio_data_url,
          data.audio_index || this.state.streamedAudioCount,
          data.history_text || data.speech_text || "",
        );
        if (this.state.mode === "finalizing") {
          this.setMode("speaking");
        }
        this.setVoiceState("正在播报中");
        this.log(`audio chunk ${data.audio_index || this.state.streamedAudioCount} received`);
        break;
      case "component_call_started":
        this.stopPlayback();
        this.emit("result", { text: "正在查询开户网点...", replace: true, loading: true });
        this.setVoiceState("正在查询开户网点");
        this.log(`component call ${data.components || ""} started`);
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
      this.state.finalHistoryText = typeof data.history_text === "string" ? data.history_text : data.text;
      this.emit("result", { text: data.text, replace: true });
    } else if (!this.state.streamingTextStarted) {
      this.emit("result", { text: "服务端没有返回文本。", replace: true });
      this.state.finalText = "";
      this.state.finalHistoryText = "";
    } else {
      this.state.finalText = currentText;
      this.state.finalHistoryText = typeof data.history_text === "string" ? data.history_text : currentText;
    }

    if (data.audio_data_url) {
      this.enqueueAudio(data.audio_data_url, this.state.streamedAudioCount + 1, this.state.finalHistoryText);
    } else if (this.state.streamedAudioCount === 0) {
      this.setVoiceState("播报完");
      this.saveCurrentTurnHistory(this.state.finalHistoryText || this.state.finalText || this.getDisplayedText());
      this.log("没有解析到音频输出。确认模型服务启用了音频输出和 stream。");
    }

    if (this.hasPendingPlayback()) {
      this.setMode("speaking");
      if (this.shouldStartBargeInMonitor()) {
        this.startBargeInMonitor().catch((err) => {
          this.log(`barge-in monitor error: ${err.message || err}`, "error");
        });
      }
    } else {
      this.saveCurrentTurnHistory(
        this.state.finalHistoryText || this.state.finalText || this.state.playedAssistantText || this.getDisplayedText(),
      );
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
    if (this.state.ws && this.state.ws.readyState < WebSocket.CLOSING) {
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

  shouldStartBargeInMonitor() {
    if (!this.options.enableBargeIn) return false;
    if (this.options.bargeInAfterFinalResultOnly && !this.state.finalReceived) return false;
    return true;
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
    this.stopBargeInMonitor();
    this.state.playbackGeneration += 1;
    if (this.state.currentAudio) {
      this.state.currentAudio.onended = null;
      this.state.currentAudio.onerror = null;
      if (typeof this.state.currentAudio.pause === "function") {
        this.state.currentAudio.pause();
      } else if (typeof this.state.currentAudio.stop === "function") {
        try {
          this.state.currentAudio.stop();
        } catch (_err) {
          // Already stopped.
        }
      }
      this.state.currentAudio = null;
    }
    this.state.audioQueue = [];
    this.state.audioPlaying = false;
  }

  enqueueAudio(audioDataUrl, audioIndex, historyText = "") {
    if (!audioDataUrl) return;
    const index = Number(audioIndex || this.state.streamedAudioCount || 1);
    this.state.audioTextByIndex.set(index, historyText || this.getDisplayedText());
    this.state.audioQueue.push({ url: audioDataUrl, index });
    if (!this.state.audioPlaying) {
      this.playNextAudio();
    }
  }

  playNextAudio() {
    const next = this.state.audioQueue.shift();
    if (!next) {
      this.stopBargeInMonitor();
      this.state.audioPlaying = false;
      this.state.currentAudio = null;
      if (this.state.finalReceived && !this.state.interrupted) {
        this.saveCurrentTurnHistory(
          this.state.finalHistoryText || this.state.finalText || this.state.playedAssistantText || this.getDisplayedText(),
        );
        this.setMode("idle");
      }
      this.setVoiceState("播报完");
      return;
    }
    this.state.audioPlaying = true;
    this.setVoiceState("正在播报中");
    if (this.shouldStartBargeInMonitor()) {
      this.startBargeInMonitor().catch((err) => {
        this.log(`barge-in monitor error: ${err.message || err}`, "error");
      });
    }

    if (this.options.useWebAudioPlayback) {
      const generation = this.state.playbackGeneration;
      this.playNextAudioWithWebAudio(next, generation).catch((err) => {
        this.log(`web audio play error: ${err.message || err}`, "error");
        if (generation === this.state.playbackGeneration) {
          this.playNextAudioWithElement(next);
        }
      });
      return;
    }

    this.playNextAudioWithElement(next);
  }

  async playNextAudioWithWebAudio(next, generation) {
    const context = this.ensurePlaybackContext();
    if (context.state === "suspended") {
      await context.resume();
    }
    const arrayBuffer = await this.audioDataUrlToArrayBuffer(next.url);
    const audioBuffer = await context.decodeAudioData(arrayBuffer);
    if (generation !== this.state.playbackGeneration || !this.state.audioPlaying) return;

    const source = context.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(context.destination);
    this.state.currentAudio = source;
    source.onended = () => {
      if (this.state.currentAudio === source) {
        this.state.currentAudio = null;
      }
      this.state.playedAssistantText =
        this.state.audioTextByIndex.get(next.index) || this.state.playedAssistantText || this.getDisplayedText();
      this.playNextAudio();
    };
    source.start();
  }

  playNextAudioWithElement(next) {
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

  async audioDataUrlToArrayBuffer(dataUrl) {
    const commaIndex = dataUrl.indexOf(",");
    if (dataUrl.startsWith("data:") && commaIndex >= 0) {
      const header = dataUrl.slice(0, commaIndex);
      const body = dataUrl.slice(commaIndex + 1);
      if (header.includes(";base64")) {
        const binary = atob(body);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i += 1) {
          bytes[i] = binary.charCodeAt(i);
        }
        return bytes.buffer;
      }
      return new TextEncoder().encode(decodeURIComponent(body)).buffer;
    }
    const response = await fetch(dataUrl);
    if (!response.ok) throw new Error(`audio fetch failed: ${response.status}`);
    return response.arrayBuffer();
  }

  rememberTurn(assistantText) {
    const text = (assistantText || "").trim();
    if (!text) return;
    this.appendHistoryTurn("[用户上一轮通过语音输入了一条消息]", text);
  }

  rememberTextTurn(userText, assistantText) {
    const user = (userText || "").trim();
    const assistant = (assistantText || "").trim();
    if (!user || !assistant || assistant === "服务端没有返回文本。") return;
    this.appendHistoryTurn(user, assistant);
  }

  appendHistoryTurn(userContent, assistantContent) {
    this.state.history.push({ role: "user", content: userContent });
    this.state.history.push({ role: "assistant", content: assistantContent });
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

function defaultVadMonitorWebSocketUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws/vad`;
}
