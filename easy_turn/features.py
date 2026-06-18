import numpy as np
import torch
import torchaudio


def float32_pcm_bytes_to_waveform(pcm_bytes: bytes, sample_rate: int, target_sample_rate: int = 16000) -> torch.Tensor:
    audio_np = np.frombuffer(pcm_bytes, dtype="<f4").copy()
    waveform = torch.from_numpy(audio_np).float()
    if sample_rate != target_sample_rate:
        waveform = torchaudio.transforms.Resample(sample_rate, target_sample_rate)(
            waveform.unsqueeze(0)
        ).squeeze(0)
    return waveform


def compute_log_mel_spectrogram(
    waveform: torch.Tensor,
    sample_rate: int = 16000,
    n_fft: int = 400,
    hop_length: int = 160,
    n_mels: int = 80,
    device: torch.device = torch.device("cpu"),
) -> torch.Tensor:
    if waveform.dim() > 1:
        waveform = waveform.squeeze(0)

    waveform = waveform.to(device)
    window = torch.hann_window(n_fft, device=device)
    stft = torch.stft(waveform, n_fft, hop_length, window=window, return_complex=True)
    magnitudes = stft[..., :-1].abs() ** 2

    filters = torch.from_numpy(
        _mel_filterbank(sr=sample_rate, n_fft=n_fft, n_mels=n_mels)
    ).to(device).to(magnitudes.dtype)

    mel_spec = filters @ magnitudes
    log_spec = torch.clamp(mel_spec, min=1e-10).log10()
    log_spec = torch.maximum(log_spec, log_spec.max() - 8.0)
    log_spec = (log_spec + 4.0) / 4.0
    return log_spec.transpose(0, 1).unsqueeze(0).to(torch.float32)


def _mel_filterbank(sr: int, n_fft: int, n_mels: int) -> np.ndarray:
    import librosa

    return librosa.filters.mel(sr=sr, n_fft=n_fft, n_mels=n_mels)

