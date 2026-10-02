# Models

JAIS Voice uses one speech model and one language model. The same language model does both transcript cleanup and the agent's work. `models/catalog.json` lists the models the app knows, with their Hugging Face repository and pinned revision; the **Models** section of the settings downloads, loads, unloads and deletes them. Models live in `~/Library/Application Support/JAIS Voice/models/`.

| Role | Repository |
| --- | --- |
| Speech recognition | [MBZUAI-Paris/JAIS-ASR-MLX](https://huggingface.co/MBZUAI-Paris/JAIS-ASR-MLX) |
| Cleanup and agent (one model for both) | [IFM/Jais-3-3.7B-MLX-8bit](https://huggingface.co/IFM/Jais-3-3.7B-MLX-8bit) (default) · [IFM/Jais-3-0.9B-MLX-bf16](https://huggingface.co/IFM/Jais-3-0.9B-MLX-bf16) · [IFM/Jais-3-7B-MLX-8bit](https://huggingface.co/IFM/Jais-3-7B-MLX-8bit) |

These repositories currently require access on Hugging Face. The app uses your Hugging Face login: run `hf auth login` once, or set `HF_TOKEN`.

## Speech recognition

On first launch the app offers to download JAIS-ASR (profile F5, 4.8 GB). The download is pinned to a commit, resumable, and checked file by file (size and SHA-256) before installation.

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
hf download IFM/Jais-3-3.7B-MLX-8bit --revision b10d70beb6c8259b9af1f6dfcd465f469b8e95e2 --local-dir "$HOME/Library/Application Support/JAIS Voice/models/sources/Jais-3-3.7B-MLX-8bit"
```

Then click **Download** again: the app finds the complete files and installs a copy without contacting Hugging Face. The folder needs `config.json`, `model.safetensors.index.json`, every shard it lists, `tokenizer.json`, `tokenizer_config.json` and `chat_template.jinja`. Files downloaded by hand are checked for structure and tensor shapes, not against Hugging Face's hashes.

## One model for both stages

The settings offer a single **0.9B / 3.7B / 7B** selector, shared by transcript cleanup and the agent. Both stages keep their own prompts and parameters but use the same weights, loaded once; loading another size frees the previous one. Each catalog entry sets the reasoning effort of both stages (`cleaner_reasoning_effort`, `agent_reasoning_effort`); `HORIZON_CLEANER_REASONING_EFFORT` and `HORIZON_AGENT_REASONING_EFFORT` override them.

## Catalog entries

A language model entry's `download` points to a published MLX build:

```json
{"id": "jais-3-3.7b", "kind": "llm", "size": "3.7B", "display_name": "Jais 3 · 3.7B",
 "directory": "Jais-3-3.7B-MLX-8bit-b10d70beb6c8",
 "download": {"repo": "IFM/Jais-3-3.7B-MLX-8bit", "revision": "b10d70beb6c8259b9af1f6dfcd465f469b8e95e2", "bytes": 5395429334}}
```

`revision` must be a full commit hash; `bytes` is the size shown before downloading. `HORIZON_MODEL_CATALOG=models/catalog-ifm.json` selects the older IFM K2-Horizon pair instead.
