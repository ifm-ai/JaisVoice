# Models

Jais Voice uses one speech model and one language model. The same language model does both transcript cleanup and the agent's work. `models/catalog.json` lists the models the app knows, with their Hugging Face repository and pinned revision; the **Models** section of the settings downloads, loads, unloads and deletes them. Models live in `~/Library/Application Support/Jais Voice/models/`.

| Role | Repository |
| --- | --- |
| Speech recognition | [IFM/Jais-ASR-MLX-8bit](https://huggingface.co/IFM/Jais-ASR-MLX-8bit) |
| Cleanup and agent (one model for both) | [IFM/Jais-3-3.7B-MLX-8bit](https://huggingface.co/IFM/Jais-3-3.7B-MLX-8bit) (default) · [IFM/Jais-3-0.9B-MLX-bf16](https://huggingface.co/IFM/Jais-3-0.9B-MLX-bf16) · [IFM/Jais-3-7B-MLX-8bit](https://huggingface.co/IFM/Jais-3-7B-MLX-8bit) |

These repositories currently require access on Hugging Face. The app uses your Hugging Face login: run `hf auth login` once, or set `HF_TOKEN`.

## Speech recognition

On first launch the app offers to download Jais-ASR, the 8-bit MLX build (7.0 GB: 8-bit decoder, BF16 audio encoder). Like a language model, it is pinned to a commit, downloaded resumably, checked file by file against Hugging Face (size, SHA-256 for weights, git hash for the other files), validated against the architecture, then installed; without access to the repository, the files can be downloaded by hand (see below).

The ASR service runs it with MLX on the app's own Python, without mlx-vlm: `jais_runtime/asr_model.py` holds the audio encoder (Qwen3-Omni) and the projector, and reuses the Jais 3 decoder of `jais_runtime/model.py`. Decoding follows the model card: greedy, at most 256 tokens, repetition penalty 1.05. The repository's processor (`processing_jais_asr.py`, `language.py`, `postprocess.py`) computes the mel features and the prompt, pins the language when the user chose one (the model can still decline audio without speech) and separates the language tag from the transcript; these are the only Python files the app downloads and runs. For the ASR, the folder of files downloaded by hand also needs these three files, `preprocessor_config.json`, `processor_config.json` and `generation_config.json`.

## Language models

Clicking **Download** on a Jais 3 model:

1. checks that your account can read the repository;
2. downloads the MLX build at its pinned revision (weights, configuration, tokenizer and chat template; never Python files), resumably;
3. checks every file's size and SHA-256 against Hugging Face;
4. validates the tensors against the architecture and installs the model.

| Model | Precision | Download |
| --- | --- | --- |
| Jais 3 · 0.9B | BF16 | 2.2 GB |
| Jais 3 · 3.7B | 8-bit affine (groups of 64) | 5.4 GB |
| Jais 3 · 7B | 8-bit affine (groups of 64) | 9.6 GB |

The collection [IFM/jais-3-mlx](https://huggingface.co/collections/IFM/jais-3-mlx) also has 4-bit builds and larger models; the runtime loads BF16 and 8-bit only.

### Without access to the repository

If your account cannot read the repository, the app explains how to get access and where to put the files instead. You can download the repository files yourself (for example from the **Files** tab on Hugging Face, or with `hf download`) into the folder the message gives:

```sh
hf download IFM/Jais-3-3.7B-MLX-8bit --revision b10d70beb6c8259b9af1f6dfcd465f469b8e95e2 --local-dir "$HOME/Library/Application Support/Jais Voice/models/sources/Jais-3-3.7B-MLX-8bit"
```

Then click **Download** again: the app finds the complete files and installs a copy without contacting Hugging Face. The folder needs `config.json`, `model.safetensors.index.json`, every shard it lists, `tokenizer.json`, `tokenizer_config.json` and `chat_template.jinja`. Files downloaded by hand are checked for structure and tensor shapes, not against Hugging Face's hashes.

## One model for both stages

The settings offer a single **0.9B / 3.7B / 7B** selector, shared by transcript cleanup and the agent. Both stages keep their own prompts and parameters but use the same weights, loaded once; loading another size frees the previous one. Cleanup always uses the `low` reasoning effort and greedy decoding, to reproduce the transcript faithfully. The agent uses the **Reasoning** setting (`low` by default, `medium` or `high`, which the model cards recommend but which is much slower), samples as the cards recommend for thinking mode (temperature 0.7, top-p 0.95, top-k 20), and has a 32k-token context with up to 16k tokens for its answer.

## Larger models through an API

**Models › Advanced settings…** opens a separate window where cleanup and the agent can use an OpenAI-compatible API instead of the local Jais 3 model, for much larger models: a hosted service (OpenAI, OpenRouter…) or your own server (vLLM, Ollama, LM Studio…). Enter the base URL ending in `/v1`, the API key (optional for local servers) and the model name, check them with **Test connection**, switch on **Use an OpenAI-compatible API** and save. Turn on **Reasoning model** only when the API accepts `reasoning_effort`: the **Reasoning** setting is then sent with each agent request.

Transcription stays on the Mac, but your requests, the cleaned transcripts and everything the agent reads (web pages, files, windows) are sent to that server. While the API is on, the local language model is unloaded and none needs to be installed; switching the API off loads the selected size again. Cleanup uses the same prompt with greedy decoding (`temperature: 0`).

## Catalog entries

A language model entry's `download` points to its MLX build:

```json
{"id": "jais-3-3.7b", "kind": "llm", "size": "3.7B", "display_name": "Jais 3 · 3.7B",
 "directory": "Jais-3-3.7B-MLX-8bit-b10d70beb6c8",
 "download": {"repo": "IFM/Jais-3-3.7B-MLX-8bit", "revision": "b10d70beb6c8259b9af1f6dfcd465f469b8e95e2", "bytes": 5395429334}}
```

`revision` must be a full commit hash; `bytes` is the size shown before downloading. The runtime loads this dense architecture (its `config.json` says `model_type: k2_horizon`) in BF16 or 8-bit affine (groups of 64).
