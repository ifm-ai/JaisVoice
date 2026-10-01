# Models

JAIS Voice uses one speech model and one language model. The same language model does both transcript cleanup and the agent's work. `models/catalog.json` lists the models the app knows, with their Hugging Face repository and pinned revision; the **Models** section of the settings downloads, loads, unloads and deletes them. Models live in `~/Library/Application Support/JAIS Voice/models/`.

| Role | Repository |
| --- | --- |
| Speech recognition | [MBZUAI-Paris/JAIS-ASR-MLX](https://huggingface.co/MBZUAI-Paris/JAIS-ASR-MLX) |
| Cleanup and agent (one model for both) | [IFM/Jais-3-3.7B](https://huggingface.co/IFM/Jais-3-3.7B) (default) · [IFM/Jais-3-0.9B](https://huggingface.co/IFM/Jais-3-0.9B) · [IFM/Jais-3-7B](https://huggingface.co/IFM/Jais-3-7B) |

These repositories currently require access on Hugging Face. The app uses your Hugging Face login: run `hf auth login` once, or set `HF_TOKEN`.

## Speech recognition

On first launch the app offers to download JAIS-ASR (profile F5, 4.8 GB). The download is pinned to a commit, resumable, and checked file by file (size and SHA-256) before installation.

## Language models

Clicking **Download** on a Jais 3 model:

1. checks that your account can read the repository;
2. downloads the original checkpoint at its pinned revision (weights, configuration, tokenizer and chat template; never Python files) into `models/sources/<repository name>`, resumably;
3. checks every file's size and SHA-256 against Hugging Face;
4. converts it to MLX on your Mac: BF16 for the 0.9B, 8-bit affine (groups of 64) for the 3.7B and 7B;
5. validates the converted tensors, installs the model and deletes the downloaded source.

| Model | Download | On disk after conversion |
| --- | --- | --- |
| Jais 3 · 0.9B | 2.2 GB | about 2 GB |
| Jais 3 · 3.7B | 10.1 GB | about 5 GB |
| Jais 3 · 7B | 18 GB | about 8 GB |

### Without access to the repository

If your account cannot read the repository, the app explains how to get access and where to put the files instead. You can download the repository files yourself (for example from the **Files** tab on Hugging Face, or with `hf download`) into the folder the message gives:

```sh
hf download IFM/Jais-3-3.7B --revision 4ba23d50550ab52a0398dd1d735a3116b7a66679 --local-dir "$HOME/Library/Application Support/JAIS Voice/models/sources/Jais-3-3.7B"
```

Then click **Download** again: the app finds the complete checkpoint and converts it without contacting Hugging Face. The folder needs `config.json`, `model.safetensors.index.json`, every shard it lists, `tokenizer.json`, `tokenizer_config.json` and `chat_template.jinja`. Files downloaded by hand are checked for structure and tensor shapes, not against Hugging Face's hashes.

## One model for both stages

The settings offer a single **0.9B / 3.7B / 7B** selector, shared by transcript cleanup and the agent. Both stages keep their own prompts and parameters but use the same weights, loaded once; loading another size frees the previous one. Each catalog entry sets the reasoning effort of both stages (`cleaner_reasoning_effort`, `agent_reasoning_effort`); `HORIZON_CLEANER_REASONING_EFFORT` and `HORIZON_AGENT_REASONING_EFFORT` override them.

## Catalog entries

A language model entry has either `download` (a published MLX build, fetched as is) or `convert` (an original checkpoint converted on the Mac):

```json
{"id": "jais-3-3.7b", "kind": "llm", "size": "3.7B", "display_name": "Jais 3 · 3.7B",
 "directory": "Jais-3-3.7B-8bit-4ba23d50550a",
 "convert": {"repo": "IFM/Jais-3-3.7B", "revision": "4ba23d50550ab52a0398dd1d735a3116b7a66679", "bits": 8, "bytes": 10137265961}}
```

`revision` must be a full commit hash. When MLX builds are published, replace `convert` with `download` pointing to them. `HORIZON_MODEL_CATALOG=models/catalog-ifm.json` selects the older IFM K2-Horizon pair instead.
