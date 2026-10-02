# Development

## Architecture

```
WAV audio → JAIS-ASR (local service) → cleanup (/v1/clean, selected LLM) → Pi agent (same LLM) → browser / macOS tools / answer
```

- `macos/`: SwiftUI/AppKit menu bar app. It records audio, orchestrates each turn and shows the bubble and settings. No third-party framework. See [macos/README.md](../macos/README.md).
- `agent/`: the [Pi](https://github.com/earendil-works/pi) agent harness: system prompt, local models and the `extensions/jais/` extension (macOS shell, Notes, clarifying question, Playwright browser, 50-action budget, action approval).
- `jais_runtime/`: the Jais 3 architecture under MLX (`model.py`), the model catalog and downloads (`model_store.py`), and the local gateway (`gateway.py`): `/v1/clean` transcript cleanup (`prompts.py`) and streamed Chat Completions for the agent, with native reasoning and tool-call parsing (`protocol.py`).
- `jais_runtime/asr_service.py`: the local JAIS-ASR service.
- `scripts/`: setup, build, disk image, service supervisor, agent launcher and model management.

The app runs Pi in RPC mode as a child process and talks to it in JSONL over stdin/stdout. The services listen on loopback only: the MLX gateway on `127.0.0.1:8788`, JAIS-ASR on `127.0.0.1:8790`. Per-turn events and the approval protocol are documented in [INTERFACES.md](../INTERFACES.md).

## Actions on the Mac

The agent has `shell_execute` and `macos_create_note` besides the browser. The **Actions on this Mac** setting decides whether they run: *Never allow*, *Ask every time* (default: the bubble shows the exact command with **Refuse** and **Allow**, and refuses after 2 minutes or when the request stops), or *Always allow*. Commands run in `/bin/zsh -f`, in a new process group with a minimal environment, from `~/Library/Caches/ae.ac.mbzuai.jaisvoice/Shell`, for at most 30 seconds. This is **not a sandbox**: an allowed command runs with the user's privileges. macOS privacy permissions still apply.

## Setup and build

Requirements: macOS 26 on Apple Silicon, Python 3.12, `uv`, the official Node.js build from nodejs.org (not Homebrew's), and the Swift build tools.

```sh
python3.12 scripts/setup.py
.venv/bin/python scripts/build_app.py --dmg
```

The build writes to `outputs/`:

- `JAIS Voice.app` (about 1.6 GB): a self-contained app embedding Python 3.12 with the service dependencies, Python 3.11 for the ASR environment, Node.js, Pi and Chromium. Its data lives in `~/Library/Application Support/JAIS Voice/` (or `JAIS_RUNTIME_DIR`). Downloads are cached in `.runtime/build-cache/`.
- `JAIS-Voice-<version>.dmg` (about 530 MB, with `--dmg`): a drag-to-install disk image laid out by Finder through AppleScript, so the first local run asks to allow controlling Finder.

`--dev` builds a small app whose launchers run this checkout (`.venv`, `scripts/`, `.runtime/`) for quick iteration. Apps are signed ad hoc only, with no Developer ID or notarization.

## Tests

```sh
.venv/bin/python -m pytest -q
node --test agent/
"outputs/JAIS Voice.app/Contents/MacOS/JAISVoice" --self-test
"outputs/JAIS Voice.app/Contents/MacOS/JAISVoice" --smoke-text "What is the capital of France?"
```

`--smoke-text` and `--smoke-file clip.wav` run the real pipeline with the installed models and print `JAIS_SMOKE_RESULT=`. `JAIS_ACTION_PERMISSION=never|ask|always` overrides the action setting for these runs. `--render-preview` and `--render-settings` export screenshots of the bubble and the settings; add `--settings-tab general|assistant|models` to pick the settings tab.

## Continuous integration and releases

`.github/workflows/release.yml` runs on a GitHub-hosted macOS 26 runner. On pull requests it runs the tests and builds the app and disk image (kept as a workflow artifact for 7 days). On every push to `main` it also publishes a release `v<version>-build.<run number>` with the disk image and `.github/release-notes.md`. The version comes from `CFBundleShortVersionString` in `macos/Info.plist`; the run number becomes `CFBundleVersion`.

These CI builds also get `JAISUpdateFeedURL`, the GitHub API URL of the latest release: the app checks it at launch, once a day and from **Check for Updates…**, compares the release tag's build number with its own, and offers the disk image. Local builds have no feed and never check. The feed must be readable without signing in, so update checks only work once the releases are public (or the feed points to a public copy).

## Interface language

Interface strings are English keys passed to `L(...)`. `macos/Resources/fr.lproj/Localizable.strings` maps them to French and `en.lproj` lists them for English; English is the default.
