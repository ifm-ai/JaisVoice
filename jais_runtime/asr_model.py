"""Jais-ASR under MLX: Qwen3-Omni audio encoder, projector and the Jais 3 decoder.

Runs the MLX build published as `IFM/Jais-ASR-MLX-8bit` without mlx-vlm. The
audio encoder and the merge follow mlx-vlm (`models/qwen3_omni_moe/audio.py` and
the `jais_asr` model of the repository's `mlx-vlm-jais-asr.patch`, MIT); the
decoder is `jais_runtime.model`, the Jais 3 architecture (`k2_horizon`).
Decoding matches `mlx_vlm.generate` with the model card's settings: greedy, at
most 256 tokens, repetition penalty 1.05 over the last 20 tokens.

The repository's own processor (`processing_jais_asr.py`, with `language.py`
and `postprocess.py`) computes the mel features and the prompt, pins the
language and splits the decision tag from the transcript.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import importlib
import json
from pathlib import Path
import sys
import types

import mlx.core as mx
import mlx.nn as nn
from mlx_lm.sample_utils import make_repetition_penalty
import numpy as np
from transformers import PreTrainedTokenizerFast, WhisperFeatureExtractor

from jais_runtime.model import Model as Decoder, ModelArgs, checkpoint_weights

MAX_NEW_TOKENS = 256
REPETITION_PENALTY = 1.05
REPETITION_CONTEXT = 20
PROCESSOR_PACKAGE = "jais_asr_processor"


@dataclass
class AudioArgs:
    d_model: int = 1280
    encoder_layers: int = 32
    encoder_attention_heads: int = 20
    encoder_ffn_dim: int = 5120
    num_mel_bins: int = 128
    output_dim: int = 2048
    downsample_hidden_size: int = 480
    activation_function: str = "gelu"
    scale_embedding: bool = False
    conv_chunksize: int = 500
    n_window: int = 50
    n_window_infer: int = 800
    max_source_positions: int = 1500

    @classmethod
    def from_dict(cls, config):
        if config.get("model_type") != "qwen3_omni_moe_audio_encoder" or config.get("activation_function", "gelu") != "gelu":
            raise ValueError("Unsupported Jais-ASR audio encoder")
        return cls(**{k: v for k, v in config.items() if k in {f.name for f in fields(cls)}})


def output_length(mel_frames: int) -> int:
    """Encoder frames for a mel length: 13 per 100 mel frames (three stride-2 convolutions)."""
    leave = mel_frames % 100
    if leave == 0:
        return mel_frames // 100 * 13
    after = (leave - 1) // 2 + 1
    return ((after - 1) // 2 + 1 - 1) // 2 + 1 + mel_frames // 100 * 13


class AudioAttention(nn.Module):
    def __init__(self, args: AudioArgs):
        super().__init__()
        self.num_heads = args.encoder_attention_heads
        self.scaling = (args.d_model // self.num_heads) ** -0.5
        self.q_proj = nn.Linear(args.d_model, args.d_model)
        self.k_proj = nn.Linear(args.d_model, args.d_model)
        self.v_proj = nn.Linear(args.d_model, args.d_model)
        self.out_proj = nn.Linear(args.d_model, args.d_model)

    def __call__(self, x, windows):
        length = x.shape[0]
        q, k, v = (projection(x).reshape(length, self.num_heads, -1).transpose(1, 0, 2)[None]
                   for projection in (self.q_proj, self.k_proj, self.v_proj))
        # Attention stays within each window of the clip (block-diagonal).
        outputs, start = [], 0
        for size in windows:
            end = start + size
            weights = mx.softmax((q[:, :, start:end] @ k[:, :, start:end].swapaxes(-2, -1)) * self.scaling, axis=-1)
            outputs.append(weights @ v[:, :, start:end])
            start = end
        return self.out_proj(mx.concatenate(outputs, axis=2).transpose(0, 2, 1, 3).reshape(length, -1))


class AudioLayer(nn.Module):
    def __init__(self, args: AudioArgs):
        super().__init__()
        self.self_attn = AudioAttention(args)
        self.self_attn_layer_norm = nn.LayerNorm(args.d_model)
        self.fc1 = nn.Linear(args.d_model, args.encoder_ffn_dim)
        self.fc2 = nn.Linear(args.encoder_ffn_dim, args.d_model)
        self.final_layer_norm = nn.LayerNorm(args.d_model)

    def __call__(self, x, windows):
        x = x + self.self_attn(self.self_attn_layer_norm(x), windows)
        return x + self.fc2(nn.gelu(self.fc1(self.final_layer_norm(x))))


class AudioEncoder(nn.Module):
    def __init__(self, args: AudioArgs):
        super().__init__()
        self.args = args
        hidden = args.downsample_hidden_size
        self.conv2d1 = nn.Conv2d(1, hidden, 3, 2, padding=1)
        self.conv2d2 = nn.Conv2d(hidden, hidden, 3, 2, padding=1)
        self.conv2d3 = nn.Conv2d(hidden, hidden, 3, 2, padding=1)
        self.conv_out = nn.Linear(hidden * ((((args.num_mel_bins + 1) // 2 + 1) // 2 + 1) // 2), args.d_model, bias=False)
        self.layers = [AudioLayer(args) for _ in range(args.encoder_layers)]
        self.ln_post = nn.LayerNorm(args.d_model)
        self.proj1 = nn.Linear(args.d_model, args.d_model)
        self.proj2 = nn.Linear(args.d_model, args.output_dim)
        channels = args.d_model
        increment = np.log(10000) / (channels // 2 - 1)
        scaled = np.arange(args.max_source_positions)[:, None] * np.exp(-increment * np.arange(channels // 2, dtype=np.float32))[None, :]
        # Float32 like mlx-vlm's table, so the encoder layers run in float32 there too.
        self._positions = np.concatenate([np.sin(scaled), np.cos(scaled)], axis=1)

    def __call__(self, features):
        """One clip's mel features (mel bins, frames) -> encoder frames (n, output_dim)."""
        args, frames = self.args, features.shape[-1]
        step = args.n_window * 2
        chunks = [step] * (frames // step) + ([frames % step] if frames % step else [])
        padded = mx.zeros((len(chunks), args.num_mel_bins, max(chunks)), dtype=features.dtype)
        for index, size in enumerate(chunks):
            padded[index, :, :size] = features[:, index * step:index * step + size]
        hidden = padded[:, :, :, None]  # NHWC: mel bins x frames, one channel
        convolved = []
        for start in range(0, len(chunks), args.conv_chunksize):
            part = hidden[start:start + args.conv_chunksize]
            for conv in (self.conv2d1, self.conv2d2, self.conv2d3):
                part = nn.gelu(conv(part))
            convolved.append(part)
        hidden = mx.concatenate(convolved, axis=0)
        batch, height, width, channels = hidden.shape
        hidden = self.conv_out(hidden.transpose(0, 2, 3, 1).reshape(batch, width, channels * height))
        hidden = hidden + mx.array(self._positions[:width])[None]
        lengths = [output_length(size) for size in chunks]
        keep = np.concatenate([index * width + np.arange(length) for index, length in enumerate(lengths)])
        hidden = mx.take(hidden.reshape(-1, hidden.shape[-1]), mx.array(keep.astype(np.int32)), axis=0)
        window = max(lengths) * (args.n_window_infer // step)
        total = output_length(frames)
        windows = [window] * (total // window) + ([total % window] if total % window else [])
        for index, layer in enumerate(self.layers):
            hidden = layer(hidden, windows)
            if index % 2 == 0:
                mx.eval(hidden)
        return self.proj2(nn.gelu(self.proj1(self.ln_post(hidden))))


class Projector(nn.Module):
    """Audio width -> decoder width: LayerNorm, Linear, GELU, Linear."""

    def __init__(self, audio_width: int, text_width: int):
        super().__init__()
        self.norm = nn.LayerNorm(audio_width)
        self.linear_1 = nn.Linear(audio_width, text_width)
        self.linear_2 = nn.Linear(text_width, text_width)

    def __call__(self, x):
        return self.linear_2(nn.gelu(self.linear_1(self.norm(x))))


class JaisASR(nn.Module):
    def __init__(self, config: dict):
        super().__init__()
        if config.get("model_type") != "jais_asr":
            raise ValueError("Not a Jais-ASR configuration")
        audio, text = AudioArgs.from_dict(config["audio_config"]), ModelArgs.from_dict(config["text_config"])
        self.audio_token_id = config["audio_token_id"]
        self.audio_tower = AudioEncoder(audio)
        self.multi_modal_projector = Projector(audio.output_dim, text.hidden_size)
        self.language_model = Decoder(text)

    def embed(self, ids: list[int], features: mx.array) -> mx.array:
        """Prompt embeddings with the projected audio frames written over the audio placeholders, in order."""
        tokens = mx.array([ids])
        embeddings = self.language_model.model.embed_tokens(tokens)
        audio = self.multi_modal_projector(self.audio_tower(features.astype(self.audio_tower.conv_out.weight.dtype)))
        placeholders = (tokens == self.audio_token_id).reshape(-1)
        if int(placeholders.sum()) != audio.shape[0]:
            raise ValueError("Audio placeholders and encoder frames differ; build the prompt with the Jais-ASR processor.")
        order = mx.maximum(mx.cumsum(placeholders.astype(mx.int32)) - 1, 0)
        width = embeddings.shape[-1]
        merged = mx.where(placeholders[:, None], audio.astype(embeddings.dtype)[order], embeddings.reshape(-1, width))
        return merged.reshape(embeddings.shape)


def load_model(path: Path) -> JaisASR:
    config = json.loads((path / "config.json").read_text())
    model = JaisASR(config)
    weights = checkpoint_weights(path)
    # Convolutions exported in PyTorch layout (out, in, kh, kw) become MLX's (out, kh, kw, in).
    for name in ("conv2d1", "conv2d2", "conv2d3"):
        key = f"audio_tower.{name}.weight"
        if key in weights and weights[key].shape != getattr(model.audio_tower, name).weight.shape:
            weights[key] = weights[key].transpose(0, 2, 3, 1)
    quantization = config.get("quantization")
    if quantization:
        nn.quantize(model, group_size=quantization["group_size"], bits=quantization["bits"],
                    class_predicate=lambda name, module: name + ".scales" in weights)
    model.load_weights(list(weights.items()), strict=True)
    model.eval()
    mx.eval(model.parameters())
    return model


def load_processor(path: Path):
    """The repository's processor and language module, imported as one package from the model directory."""
    package = types.ModuleType(PROCESSOR_PACKAGE)
    package.__path__ = [str(path)]
    sys.modules[PROCESSOR_PACKAGE] = package
    processing = importlib.import_module(f"{PROCESSOR_PACKAGE}.processing_jais_asr")
    language = importlib.import_module(f"{PROCESSOR_PACKAGE}.language")
    # Built from its parts: from_pretrained would follow the config's auto_map and ask to trust remote code.
    options = json.loads((path / "processor_config.json").read_text())
    processor = processing.JaisASRProcessor(
        WhisperFeatureExtractor.from_pretrained(str(path), local_files_only=True),
        PreTrainedTokenizerFast.from_pretrained(str(path), local_files_only=True),
        **{key: options[key] for key in ("audio_token", "boa_token", "eoa_token") if key in options})
    return processor, language


class Transcriber:
    def __init__(self, path: Path):
        self.model = load_model(path)
        self.processor, self.language = load_processor(path)
        self.prompt = self.processor.tokenizer.apply_chat_template(
            [{"role": "user", "content": [{"type": "audio"}]}], tokenize=False, add_generation_prompt=True)
        self.eos = set(json.loads((path / "generation_config.json").read_text())["eos_token_id"])

    def __call__(self, waveform: np.ndarray, language: str | None = None):
        """A 16 kHz mono float32 clip -> the processor's Transcription (text, language, refused, truncated...)."""
        batch = self.processor(text=[self.prompt], audio=[waveform], sampling_rate=16000)
        ids = [int(token) for token in batch["input_ids"][0]]
        embeddings = self.model.embed(ids, mx.array(np.asarray(batch["input_features"][0])))
        decoder = self.model.language_model
        cache = decoder.make_cache()
        # As mlx-vlm: prefill all but the last prompt position, then decode from it.
        if len(ids) > 1:
            decoder(None, cache, embeddings[:, :-1])
            mx.eval([entry.state for entry in cache])
        logits = decoder(None, cache, embeddings[:, -1:])[:, -1, :]
        penalty = make_repetition_penalty(REPETITION_PENALTY, REPETITION_CONTEXT)
        pin = self.language.LanguagePin(language) if language else None
        thinking = pin is not None and self.language.has_open_thinking(ids)
        context, tokens = [ids[-1]], []
        while len(tokens) < MAX_NEW_TOKENS:
            logits = penalty(mx.array(context), logits)
            if pin is not None and (thinking or len(tokens) <= 2):
                # The pin decides between abstaining and a tag from the same scores.
                scores = np.array(logits[0].astype(mx.float32))
                token = int(pin.apply(tokens, scores, thinking=thinking).argmax())
            else:
                token = int(mx.argmax(logits, axis=-1).item())
            tokens.append(token)
            context.append(token)
            if token in self.eos:
                break
            logits = decoder(mx.array([[token]]), cache)[:, -1, :]
        return self.processor.post_process_transcription([ids + tokens], input_lengths=len(ids))[0]
