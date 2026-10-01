# JAIS Voice

## Installation

Requirements: an Apple Silicon Mac running macOS 26, at least 16 GB of memory, about 15 GB free for the app and models, plus 25 GB of temporary space while converting the language models.

1. Download `JAIS-Voice-<version>.zip` from the [Releases](https://github.com/MBZUAI-IFM/JaisVoice/releases), unzip it and drag **JAIS Voice.app** into **Applications**.
2. The app is not signed by an identified Apple developer, so macOS blocks it on first launch. Open **System Settings → Privacy & Security**, then click **Open Anyway** next to JAIS Voice. The Terminal equivalent, only for an archive obtained from this repository:

   ```sh
   xattr -dr com.apple.quarantine "/Applications/JAIS Voice.app"
   ```
3. On launch, the app offers to download the JAIS-ASR transcription model (4.8 GB). Install the language models as described below.

The app is self-contained: it embeds Python, Node.js, the Pi agent and Chromium. Its data (models, logs, agent state) lives in `~/Library/Application Support/JAIS Voice/`. Deleting that folder and the app uninstalls everything.

### 1B and 4B language models

The MLX conversions are not published yet. Until they are, each user converts the original exports on their own Mac. This needs read access to the `MBZUAI-Paris` repositories on Hugging Face and a token login (`hf auth login`, or the `HF_TOKEN` variable). The conversion downloads the FP32 sources at their pinned revision (about 4 GB for the 1B, 19 GB for the 4B), checks their SHA-256, then writes the MLX model where the app looks for it:

```sh
RES="/Applications/JAIS Voice.app/Contents/Resources"
"$RES/runtime/python/bin/python3.12" "$RES/app/scripts/prepare_mblab_models.py" --download \
  --runtime-dir "$HOME/Library/Application Support/JAIS Voice"
```

`--model k2-agent` (4B) or `--model k2-cleaner` (1B) converts a single model. The sources stay in `models/sources/` inside the data folder and can be deleted afterwards. Relaunch the app, or open the Models section of the settings, to see them installed.

The settings offer a single **1B / 4B / 7B** selector, shared by **cleanup and the agent**. The choice persists after quitting and applies to the next audio or text request. Both stages keep their own prompts and parameters but use the same checkpoint, loaded once.

**Models on demand.** `models/catalog.json` lists the models and their Hugging Face repositories. On launch, the app offers to download missing required models; the settings can download, cancel (resumable), unload or delete each model. The K2 models have no published MLX repository yet: they show as “Coming soon” while their `download` entry is `null`, and conversions already on this Mac remain usable and deletable. The MBZUAI 7B has a reserved slot in the catalog.

Transcription uses **JAIS-ASR-MLX F5 on this Mac**, with no remote ASR server. An isolated local service, started with the app, keeps the model in memory.

A native voice assistant for Apple Silicon Macs: press **⌥ Space**, speak, release. The listening capsule gives way to a small panel with the transcript, corrections, browsing and response. Both K2 models run under MLX on the Mac.

**Status: supervised prototype, mblab 1B/4B profile enabled on 16 September 2026.** Both MLX conversions and the native audio pipeline work. The 1B passes 12/16 strict cleanup cases; the 4B can search and open pages, but may pick the wrong site and present it as official. See `Validation-MBLab.md` in the delivery folder for measured successes and failures. The previous IFM pair remains usable through `models/catalog-ifm.json`.

## Architecture

- `macos/`: SwiftUI/AppKit app, non-activating panels, Carbon global shortcut, AVFoundation audio. It orchestrates each turn (ASR → cleanup → agent) and depends on no third-party framework.
- `agent/`: [Pi](https://github.com/earendil-works/pi) agent harness (`@earendil-works/pi-coding-agent`, pinned in `package.json`): system prompt, local models and the `extensions/jais/` extension (macOS shell, Notes, radios, clarifying question, Playwright MCP browser, 50-action budget).
- `k2_runtime/`: faithful K2 architecture (group normalization and YaRN), local Chat Completions gateway, `/v1/clean` cleanup with a safeguard, response and tool-call parser.
- `backend/horizon_voice/local_asr.py`: local JAIS-ASR F5 service.
- `.runtime/`: in development, weights, dedicated Chromium, Pi state and logs. `.venv/` holds the isolated Python dependencies. The distributed app uses `~/Library/Application Support/JAIS Voice/` instead.

WAV audio → local JAIS-ASR F5 → selected LLM (cleanup, `/v1/clean`) → Pi with the same LLM (agent) → browser / macOS tools / text response.

The app runs Pi in RPC mode as a child process (`scripts/run_agent.py`) and talks to it in JSONL over stdin/stdout: no server or token is needed between the interface and the agent.

The settings shipped for mblab are `low` for both models, with native XML calls and K2 Markdown descriptions. The historical IFM catalog (`HORIZON_MODEL_CATALOG=models/catalog-ifm.json`) keeps `low` for the cleaner and `medium` for the agent. The catalog's `cleaner_reasoning_effort` and `agent_reasoning_effort` fields set these values; the explicit `HORIZON_CLEANER_REASONING_EFFORT` and `HORIZON_AGENT_REASONING_EFFORT` variables take precedence. The “Correction safeguard” setting, on by default, can be turned off in the native settings. The choice persists and is sent with every audio or text request; it applies from the next request. When off, corrections pass even if they change words or meaning, but the empty or incomplete response checks stay active. The lexical safeguard rejects some uncertain corrections before any action; it replaces neither a check of the meaning nor a check of the sites the agent picks.

Per-turn events are documented in `INTERFACES.md`. The models share neither tokenizer nor cache. The raw transcript is displayed but only the cleaned text enters the agent's conversation. Events from a cancelled turn are ignored.

## Setup and build

Requirements: macOS 26 on Apple Silicon, Python 3.12 for the app, Python 3.11 for F5, Node.js and the Swift build tools. This setup was prepared for the demo's 48 GB M4 Max Mac.

```sh
python3.12 scripts/setup.py
.venv/bin/python scripts/build_app.py --direct --zip
```

The native build uses `swiftc` with the Apple SDK that matches the available compiler; no framework is downloaded.

The build produces a self-contained app in `outputs/` (about 1.6 GB) and, with `--zip`, the archive to publish in a GitHub Release. It embeds Python 3.12 and the service dependencies, Python 3.11 for the ASR environment (through `uv`), the current Node.js binary (which must be the official nodejs.org build, not Homebrew's), Pi installed with `npm ci --omit=dev`, and the Chromium revision Playwright expects. Downloads are cached in `.runtime/build-cache/`. The `launch-services` (ASR and MLX gateway), `launch-agent` (Pi) and `launch-models` launchers locate these components relative to the bundle and keep data in `~/Library/Application Support/JAIS Voice/` (or in `HORIZON_RUNTIME_DIR` when set). Python bytecode is precompiled: nothing is written into the signed bundle at run time.

`--dev` instead builds a small app whose launchers run this checkout (`.venv`, `scripts/`, `.runtime/`), for quick iteration. In both cases the app only has a local (ad hoc) signature, with no Developer ID or notarization.

Python and npm dependencies are locked in `requirements.lock` and `package-lock.json`. References and conversions of the IFM checkpoints are recorded in `.runtime/models/manifest.json`.

### Private mblab checkpoints: 1B and 4B

The catalog's 1B and 4B models are local conversions of the following exports, copied from `mblab:/datadisk4/florian-valade/` without modifying the originals:

| Role | MBZUAI-Paris export | Revision | Local conversion |
| --- | --- | --- | --- |
| Cleanup | `A13x1-1Bcv5c189-sft-reasoningv9.5.2k2horCT-ckpt10711-FTPO_es035` | `32bfb4a00c1e56a9956fd279b2743e1336ea7fe6` | BF16 |
| Agent | `A13cs_A13x1-4Bc10248_sft_reasoningv9.5.1k2horCT_FTPO_es035` | `20e95a2e3f4452c6d26374d48cbc9902259d49bf` | 8 bits, groups of 64 |

Both exports physically store FP32 tensors, including the “4B” whose configuration declares BF16. The 4B normalization has two groups and its weight offsets are already baked into the export: the conversion does not add them a second time. Each checkpoint's tokenizer, templates and end-of-generation tokens are kept. Their `low` reasoning prefix is already closed, unlike the older IFM checkpoints; the parser follows this actual prefix.

After copying the sources to `.runtime/models/sources/mblab-1B-32bfb4a00c1e` and `mblab-4B-20e95a2e3f44`, with their provenance manifests and the shards' SHA-256:

```sh
.venv/bin/python scripts/prepare_mblab_models.py --verify-only
.venv/bin/python scripts/prepare_mblab_models.py
```

Preparation checks integrity, tensor shapes and revisions before converting. It writes to the directories referenced by `models/catalog.json`, which the app then detects as installed. Downloaded Python sources are never executed to convert or serve the models.

The `scripts/evaluate_mblab.py` suite uses 16 cases fixed before the first inference: three known regressions and thirteen new inputs. It separates the correctness of the text from its acceptance by the safeguard. Its tool-proposal tests do not replace a trial with the real browser; the measured results and limits are recorded in the delivered validation report.

## Launch and operation

Open `JAIS Voice.app`. Its supervisor starts JAIS-ASR F5 and the MLX gateway; the app then starts the Pi agent, connected to it by a pipe. The services listen on the loopback interface only.

| Service | Local address |
| --- | --- |
| MLX (Chat Completions, `/v1/clean`) | `127.0.0.1:8788` |
| JAIS-ASR F5 | `127.0.0.1:8790` |
| Pi agent | the app's stdin/stdout, no port |

A small settings window opens at each launch: global shortcut, ASR language (automatic, Arabic, French, English, Spanish, Italian), microphone permission button and model status. The choices persist, and the language is sent with each request without reloading the models. Closing the window leaves the assistant running. The “JAIS → Settings…” menu, or reopening the running app, brings it back even when macOS hides the icon for lack of space.

The menu and the settings show loading, then “Ready”. The microphone is only used while recording; permission is requested explicitly from the settings. A recording or imported file lasts at most 30 seconds, the ASR limit. The panel disappears ten seconds after the end, except while hovered or in use; the last result stays available in the menu.

Each model has its own real loading indicator in the window and in the open menu: waiting, loading, loaded or error. The loaded state follows the MLX warmup; no synthetic percentage is shown. When the connection is lost, the state becomes unavailable rather than keeping a false readiness.

Quitting the app stops the services it started. Ports already in use are reported; the launcher does not terminate other applications' processes. Browser calls from a cancelled turn are not continued; an action already performed is not undone retroactively.

## macOS actions and shell

Besides the browser, the agent has `shell_execute(command, reason)` and specialized tools to create a note and manage the radios. The **Actions on this Mac** setting decides whether they run: *Never allow* refuses them all; *Ask every time* (the default) shows each command in the JAIS Voice bubble with **Refuse** and **Allow** buttons, and refuses it after 2 minutes without an answer or when the request stops; *Always allow* runs them without confirmation, with a warning in the settings. Reading the Wi-Fi/Bluetooth state and web browsing need no confirmation. The Pi extension runs commands in `/bin/zsh -f`, in a new process group, from `~/Library/Caches/ae.ac.mbzuai.jaisvoice/Shell`. Details and results stay visible in the bubble. Commands may use `/usr/bin/osascript` and `/usr/bin/open`. Web pages and tool output are never authorized instructions.

This mechanism is **not a sandbox**: the model can run commands with the current account's privileges and modify files. A minimal environment avoids inheriting the process's secrets; it does not prevent reading files the account can access. Execution: 30 s maximum; bounded output; stopping or starting a new turn ends the owned process group. A change already made is not undone.

macOS separately asks for Automation permission for Notes, and possibly Bluetooth. System permissions are never bypassed or reset. Notes may sync through the configured account, even though both models are local.

The “airplane mode” chosen for this demo means Wi-Fi **and** Bluetooth turned off, not a native macOS switch. It follows the same setting as the other actions. Bluetooth can be controlled by blueutil, prepared only inside the project: `.venv/bin/python scripts/prepare_macos_tools.py`. Its official 2.14.0 source is pinned by SHA256; no radio is changed during preparation. This dependency uses private APIs and its results must be checked. Turning the network off prevents web browsing (ASR and models stay local), and turning Bluetooth off can disconnect a keyboard or mouse. Plan a manual recovery through System Settings.

Logs are in `logs/` inside the data folder (`.runtime/logs/` for a `--dev` app).

## Tests

```sh
PYTHONPATH=.:backend .venv/bin/python -m pytest backend/tests tests -q -o asyncio_mode=auto
node --test agent/
.venv/bin/python scripts/browser_smoke.py
"outputs/JAIS Voice.app/Contents/MacOS/JAISVoice" --smoke-file /path/to/clip.wav
```

Unit tests explicitly use stand-ins for network errors and interruptions; the Node tests exercise the extension's real shell (timeout, cancellation, process group). `browser_smoke` uses the real browser; `--smoke-file` and `--smoke-text` run the real ASR → cleanup → Pi pipeline from the app. `HORIZON_ACTION_PERMISSION=never|ask|always` overrides the action setting for these runs.

Phrases to dictate:

- `أرسل التقرير غداً... لا، بعد غد، إلى أحمد... أقصد سارة` — checks date and recipient corrections.
- `لا ترسل التقرير غداً` — the negation must remain.
- `ابحث عن متحف اللوفر في أبوظبي، لا أقصد في باريس، وافتح الموقع الرسمي` — search and browsing after a destination correction.

The test file provided in Downloads is a 6.3-second WAV clip; it validates the ASR and the transport but contains no obvious self-correction. No simulated result is used in normal operation. The native `--preview` mode is an explicitly labelled visual preview.

## Components

The historical catalog uses `IFM/K2-Horizon-0.9B` and `IFM/K2-Horizon-7B`; the default catalog uses the two private `MBZUAI-Paris` exports identified above. Pi, the MLX libraries and Playwright MCP keep their respective licenses. Exact repositories and revisions are kept in `models/catalog.json` and the preparation scripts. The app's source archive does not contain the private weights.

## Interface language

In the settings, “Interface language” switches between English (the default) and Français. The change is immediate and persists after reopening. It is independent of the transcription language and does not translate the conversation with the assistant. Interface strings are written in English in the code; `macos/Resources/fr.lproj/Localizable.strings` maps them to French and `en.lproj` lists them for English. System permission prompts follow the macOS language.

## Local F5 ASR

Repository: https://huggingface.co/MBZUAI-Paris/JAIS-ASR-MLX; installed revision: `de134588d64e222f5251b706a64b0281a0eeb091`.
The official weights and runtime are kept together in `.runtime/models/JAIS-ASR-MLX`, with their Python 3.11 `.venv` environment and their licenses. Check the hashes with:

```sh
.venv/bin/python .runtime/models/JAIS-ASR-MLX/scripts/verify.py --profile F5 --sha256
```

The `backend/horizon_voice/local_asr.py` service loads a single F5 instance and serializes inference on one Metal thread. Its log is `.runtime/logs/asr.log`. The supervisor only runs this service when the model is installed, starts it after a download and stops it after a deletion. Inference forces the Hugging Face and Transformers offline modes; nothing is downloaded during dictation.

Automatic mode calls the published runtime unchanged. For an explicit language, the adapter reuses its generation loop with the `LanguagePin` provided by the model, keeping its refusal mechanism. Arabic covers the supported dialects. The model keeps the published 256-token limit; a truncated or refused response never reaches cleanup or the agent. Recordings stay limited to 30 seconds.

## Freeing memory

In the settings or the JAIS menu, **Unload models** stops the local services the app owns, including F5 and the loaded LLM. The app stays open and model files stay on disk. The command is disabled during a request or an unload. Reloading is only offered once the supervisor has exited, after waiting for and reaping its child processes. **Reload models** restarts the services and a new voice session; the conversation is reset.

The shared selector uses the same weights and tokenizer instance for cleanup and the agent. Only one size is resident: loading another frees the previous one. Prompts and generation caches stay specific to each request. **Delete** on a model (trash icon, with confirmation) unloads it and then erases its folder.
