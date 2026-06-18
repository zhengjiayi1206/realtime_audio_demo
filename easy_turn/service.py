import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

import torch
import yaml

from realtime_audio_demo.config import (
    EASYTURN_CHECKPOINT,
    EASYTURN_CONFIG,
    EASYTURN_GPU,
    EASYTURN_LLM_PATH,
    EASYTURN_MAX_AUDIO_SECONDS,
    EASYTURN_PROMPT,
)
from easy_turn.features import (
    compute_log_mel_spectrogram,
    float32_pcm_bytes_to_waveform,
)


logger = logging.getLogger("uvicorn.error")

TURN_STATES = {
    "<COMPLETE>": "COMPLETE",
    "<INCOMPLETE>": "INCOMPLETE",
    "<BACKCHANNEL>": "BACKCHANNEL",
    "<WAIT>": "WAIT",
    "<complete>": "COMPLETE",
    "<incomplete>": "INCOMPLETE",
    "<backchannel>": "BACKCHANNEL",
    "<wait>": "WAIT",
}


@dataclass
class EasyTurnDecision:
    turn_state: str
    transcription: str
    raw_output: str
    latency_ms: int
    audio_seconds: float


class EasyTurnService:
    def __init__(self) -> None:
        self._model = None
        self._tokenizer = None
        self._torch_device = torch.device("cpu")
        self._lock = Lock()

    def judge(self, chunks: list[bytes], sample_rate: int) -> EasyTurnDecision:
        if not chunks:
            return EasyTurnDecision("IDLE", "", "", 0, 0.0)

        logger.info("EasyTurn judge requested chunks=%d source_sr=%d", len(chunks), sample_rate)
        self._ensure_loaded()
        pcm = b"".join(chunks)
        waveform = float32_pcm_bytes_to_waveform(pcm, sample_rate, 16000)
        max_samples = int(16000 * EASYTURN_MAX_AUDIO_SECONDS)
        if waveform.numel() > max_samples:
            waveform = waveform[-max_samples:]

        audio_seconds = waveform.numel() / 16000
        start = time.perf_counter()
        with self._lock:
            feats = compute_log_mel_spectrogram(
                waveform,
                sample_rate=16000,
                device=self._torch_device,
            )
            feat_lens = torch.tensor([feats.shape[1]], dtype=torch.int64)
            if self._torch_device.type == "cuda":
                feats = feats.to(self._torch_device)
                feat_lens = feat_lens.to(self._torch_device)

            use_amp = self._torch_device.type == "cuda"
            with torch.cuda.amp.autocast(enabled=use_amp, dtype=torch.bfloat16):
                with torch.no_grad():
                    output_text = self._model.generate(
                        wavs=feats,
                        wavs_len=feat_lens,
                        prompt=EASYTURN_PROMPT,
                    )

        raw = output_text[0] if isinstance(output_text, list) else str(output_text)
        transcription, turn_state = parse_turn_output(raw)
        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "EasyTurn judge result state=%s latency=%dms audio=%.2fs transcription=%r raw=%r",
            turn_state,
            latency_ms,
            audio_seconds,
            transcription,
            raw,
        )
        return EasyTurnDecision(turn_state, transcription, raw, latency_ms, audio_seconds)

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            self._load_model()

    def _load_model(self) -> None:
        config_path = Path(EASYTURN_CONFIG)
        checkpoint_path = Path(EASYTURN_CHECKPOINT)
        logger.info(
            "EasyTurn loading config=%s checkpoint=%s llm_path=%s gpu=%s",
            config_path,
            checkpoint_path,
            EASYTURN_LLM_PATH or "<config.yaml>",
            EASYTURN_GPU,
        )
        if not checkpoint_path.exists():
            raise FileNotFoundError(f"EASYTURN_CHECKPOINT not found: {checkpoint_path}")

        package_dir = Path(__file__).resolve().parent
        package_dir_str = str(package_dir)
        if package_dir_str not in sys.path:
            sys.path.insert(0, package_dir_str)

        from transformers import AutoTokenizer
        from wenet.llm_asr.init_llmasr import init_llmasr

        with config_path.open("r", encoding="utf-8") as f:
            configs = yaml.load(f, Loader=yaml.FullLoader)
        if EASYTURN_LLM_PATH:
            configs["llm_path"] = EASYTURN_LLM_PATH
            configs.setdefault("tokenizer_conf", {})["llm_path"] = EASYTURN_LLM_PATH

        class Args:
            pass

        args = Args()
        args.checkpoint = str(checkpoint_path)
        args.use_lora = configs.get("use_lora", False)
        args.only_optimize_lora = False
        args.jit = False
        args.enc_init = None

        if EASYTURN_GPU >= 0 and torch.cuda.is_available():
            torch.cuda.set_device(EASYTURN_GPU)
            self._torch_device = torch.device(f"cuda:{EASYTURN_GPU}")
        else:
            self._torch_device = torch.device("cpu")

        model, configs = init_llmasr(args, configs, is_inference=True)
        if self._torch_device.type == "cuda":
            model = model.to(self._torch_device).to(torch.bfloat16)
            model.llama_model = model.llama_model.to(self._torch_device).to(torch.bfloat16)
        else:
            model = model.to(self._torch_device).to(torch.float32)
        model.eval()

        tokenizer = AutoTokenizer.from_pretrained(
            configs["llm_path"],
            use_fast=False,
            trust_remote_code=True,
        )
        tokenizer.add_special_tokens({"pad_token": "[PAD]"})
        tokenizer.padding_side = "right"

        self._model = model
        self._tokenizer = tokenizer
        logger.info("EasyTurn loaded device=%s checkpoint=%s llm=%s", self._torch_device, checkpoint_path, configs["llm_path"])


def parse_turn_output(raw_output: str) -> tuple[str, str]:
    transcription = raw_output
    turn_state = "INCOMPLETE"
    raw_lower = raw_output.lower()
    for label, state in TURN_STATES.items():
        if label.lower() in raw_lower:
            transcription = raw_output.replace(label, "").replace(label.upper(), "").replace(label.lower(), "").strip()
            turn_state = state
            break
    return transcription, turn_state


_service = EasyTurnService()


def judge_turn(chunks: list[bytes], sample_rate: int) -> EasyTurnDecision:
    return _service.judge(chunks, sample_rate)


def preload_easy_turn() -> None:
    _service._ensure_loaded()
