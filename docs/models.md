# Models

JAIS Voice uses one speech model and one language model at a time. `models/catalog.json` lists the models the app knows, their Hugging Face repository and pinned revision; the app's settings download, load, unload and delete them. Models live in `~/Library/Application Support/JAIS Voice/models/`.

| Role | Repository |
| --- | --- |
| Speech recognition | [MBZUAI-Paris/JAIS-ASR-MLX](https://huggingface.co/MBZUAI-Paris/JAIS-ASR-MLX) |
| Cleanup and agent | [IFM/Jais-3-3.7B](https://huggingface.co/IFM/Jais-3-3.7B) |
| Agent, smaller / larger | [IFM/Jais-3-0.9B](https://huggingface.co/IFM/Jais-3-0.9B) · [IFM/Jais-3-7B](https://huggingface.co/IFM/Jais-3-7B) |

## Speech recognition

On first launch the app offers to download JAIS-ASR (profile F5, 4.8 GB). The download is pinned to a commit, resumable, and checked file by file (size and SHA-256) before installation. Hugging Face access is needed while the repository is private: sign in with `hf auth login` or set `HF_TOKEN`.

## Language models

MLX builds of the language models are not published yet: their catalog entries have `"download": null` and show as “Coming soon”. Until they are, convert them on your Mac.

> The catalog shipped today still points to the pre-release `MBZUAI-Paris` exports of the 1B and 4B models; moving it to the `IFM/Jais-3` repositories above is pending.

The conversion downloads the FP32 sources at their pinned revision (about 4 GB for the 1B, 19 GB for the 4B), checks their SHA-256, then writes the MLX model where the app looks for it. It needs read access to the source repositories and about 25 GB of temporary space. It uses the Python embedded in the app:

```sh
RES="/Applications/JAIS Voice.app/Contents/Resources"
"$RES/runtime/python/bin/python3.12" "$RES/app/scripts/prepare_mblab_models.py" --download \
  --runtime-dir "$HOME/Library/Application Support/JAIS Voice"
```

`--model k2-agent` (4B) or `--model k2-cleaner` (1B) converts a single model. The sources stay in `models/sources/` inside the data folder and can be deleted afterwards. Relaunch the app, or open the Models section of the settings, to see them installed.

The conversion checks integrity, tensor shapes and revisions before converting, and never runs Python code downloaded with the weights. The 1B is stored as BF16, the 4B as 8-bit affine (groups of 64). Each checkpoint keeps its own tokenizer, chat template and end-of-generation tokens.

## One model for both stages

The settings offer a single **1B / 4B / 7B** selector, shared by transcript cleanup and the agent. Both stages keep their own prompts and parameters but use the same weights, loaded once; loading another size frees the previous one. Each catalog entry sets the reasoning effort of both stages (`cleaner_reasoning_effort`, `agent_reasoning_effort`); `HORIZON_CLEANER_REASONING_EFFORT` and `HORIZON_AGENT_REASONING_EFFORT` override them.

`HORIZON_MODEL_CATALOG=models/catalog-ifm.json` selects the older IFM K2-Horizon pair instead.
