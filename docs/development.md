# Development

## Architecture

```
WAV audio → Jais-ASR (local service) → cleanup (/v1/clean, selected LLM) → Pi agent (same LLM) → browser / macOS tools / answer
```

- `macos/`: SwiftUI/AppKit menu bar app. It records audio, orchestrates each turn and shows the bubble and settings. No third-party framework. See [macos/README.md](../macos/README.md).
- `agent/`: the [Pi](https://github.com/earendil-works/pi) agent harness: system prompt, local models, skills (`skills/`) and the `extensions/jais/` extension (macOS shell, Notes, the apps' windows, clarifying question, Playwright browser, the rules of Pi's file tools, 50-action budget, action approval).
- `jais_runtime/`: the Jais 3 architecture under MLX (`model.py`), the model catalog and downloads (`model_store.py`), and the local gateway (`gateway.py`): `/v1/clean` transcript cleanup (`prompts.py`) and streamed Chat Completions for the agent, with native reasoning and tool-call parsing (`protocol.py`).
- `jais_runtime/asr_service.py`: the local Jais-ASR service.
- `scripts/`: setup, build, disk image, service supervisor, agent launcher and model management.

The app runs Pi in RPC mode as a child process and talks to it in JSONL over stdin/stdout. The services listen on loopback only: the MLX gateway on `127.0.0.1:8788`, Jais-ASR on `127.0.0.1:8790`. Per-turn events and the approval protocol are documented in [INTERFACES.md](../INTERFACES.md).

## Actions on the Mac

The agent has `shell_execute` and `macos_create_note` besides the browser. The **Actions on this Mac** setting decides whether they run: *Never allow*, *Ask every time* (default: the bubble shows the exact command with **Refuse** and **Allow**, and refuses after 2 minutes or when the request stops), or *Always allow*. Commands run in `/bin/zsh -f`, in a new process group with a minimal environment, from `~/Library/Caches/ae.ac.mbzuai.jaisvoice/Shell`, for at most 30 seconds. This is **not a sandbox**: an allowed command runs with the user's privileges. macOS privacy permissions still apply.

## The apps' windows

The `desktop_*` tools give the text-only model the user's other apps the way Playwright gives it web pages: `desktop_snapshot` turns a window's accessibility tree into short lines with refs (`- button "Reply" [ref=e12]`), and `desktop_click`, `desktop_type`, `desktop_press_key`, `desktop_menu` and `desktop_scroll` act on those refs and return the window again. `desktop_menu` walks the menu bar by title, in the app's language. When a window exposes almost nothing (canvas apps, games), the snapshot adds the text Vision reads from a capture of that window, with refs clicked by position.

The extension (`agent/extensions/jais/desktop.ts`) starts the app's own binary with `--desktop-helper` (`macos/Sources/JAISVoice/Desktop*.swift`). Being in Jais Voice's process tree, it uses the app's **Accessibility** permission (required) and **Screen Recording** permission (text from pixels only); **Settings → Assistant** shows both and asks for them. Before the first read and the first control of each app in a request, the bubble asks the user (with *Ask every time*); *Never allow* refuses, *Always allow* accepts. Some apps are read-only (terminals, code editors, browsers: driving them would bypass the shell approval or act with the user's web accounts) and some are never accessible (Jais Voice itself, password managers, System Settings, system prompts); see `DesktopPolicy`. After the agent reads a window, opening a web page outside DuckDuckGo and the sites named in the request asks again.

Test the helper alone with JSON lines, for example `printf '{"id":1,"method":"list_windows"}\n' | macos/.build/direct/JAISVoice --desktop-helper`. A process started from a terminal uses the terminal's permissions instead of the app's.

macOS remembers these permissions by code signature. Apps are signed ad hoc by default, so every new build asks for them again: pass `--sign "<identity>"` (or set `JAIS_CODESIGN_IDENTITY`) to `scripts/build_app.py` to keep them across builds. In development, `tccutil reset Accessibility ae.ac.mbzuai.jaisvoice` clears a stale entry.

## The agent's tools and skills

Besides its own tools, the agent uses Pi's built-in file tools `read`, `ls`, `find`, `grep`, `write` and `edit` (not `bash`: commands go through `shell_execute` and its approval). Pi runs from the home folder, so paths such as `~/Documents/list.txt` work as the user says them. The extension (`agent/extensions/jais/files.ts`) asks once per request before files are read and before every write; it never reads or writes credential stores (SSH and cloud keys, shell startup files, `.env` files, keychains, browser profiles) and writes only the user's own files: the home folder, outside `~/Library`, hidden files and Jais Voice's own folders. `grep` and `find` run ripgrep and fd, pinned in `scripts/agent_tools.py` and installed by `setup.py` (into `.runtime/bin`) and `build_app.py` (into the bundle), since the agent runs offline.

Skills live in `agent/skills/<name>/SKILL.md` and are copied into the agent's directory at launch: `web-research`, `mac-apps` and `files`. Pi lists them in the system prompt and the model can read one with `read`; since a small model rarely does, the extension also joins an area's skill to the first result of its tools in a request. Add a skill by adding a folder with a `SKILL.md` (frontmatter `name` and `description`, then the instructions). Skills from `~/.agents/skills` and from projects are excluded (`agent/settings.json`).

The **Reasoning** setting (Settings → Assistant) sets Pi's thinking level, which the gateway passes to the chat template as `reasoning_effort`: `low` (default) answers fastest, `high` is what the model cards recommend and takes several times longer. The agent has a 32k-token context with up to 16k tokens for its answer, and samples as the cards recommend for thinking mode (temperature 0.7, top-p 0.95, top-k 20); cleanup keeps greedy decoding.

## Setup and build

Requirements: macOS 26 on Apple Silicon, Python 3.12, `uv`, the official Node.js build from nodejs.org (not Homebrew's), and the Swift build tools.

```sh
python3.12 scripts/setup.py
.venv/bin/python scripts/build_app.py --dmg
```

The build writes to `outputs/`:

- `Jais Voice.app` (about 670 MB): a self-contained app embedding Python 3.12 with the service dependencies (the ASR environment is built on a copy of it), Node.js and Pi. The browser is the Mac's Google Chrome, or a Chromium the agent downloads into the data folder. Its data lives in `~/Library/Application Support/Jais Voice/` (or `JAIS_RUNTIME_DIR`). Downloads are cached in `.runtime/build-cache/`.
- `Jais-Voice-<version>.dmg` (about 185 MB, with `--dmg`): a drag-to-install disk image laid out by Finder through AppleScript, so the first local run asks to allow controlling Finder.

`--dev` builds a small app whose launchers run this checkout (`.venv`, `scripts/`, `.runtime/`) for quick iteration. Apps are signed ad hoc unless `--sign` names an identity, with no notarization.

## Tests

```sh
.venv/bin/python -m pytest -q
node --test agent/
"outputs/Jais Voice.app/Contents/MacOS/JAISVoice" --self-test
"outputs/Jais Voice.app/Contents/MacOS/JAISVoice" --smoke-text "What is the capital of France?"
```

`--smoke-text` and `--smoke-file clip.wav` run the real pipeline with the installed models and print `JAIS_SMOKE_RESULT=`. `JAIS_ACTION_PERMISSION=never|ask|always` overrides the action setting for these runs. `--render-preview` and `--render-settings` export screenshots of the bubble and the settings; add `--settings-tab general|assistant|models` to pick the settings tab.

## Continuous integration and releases

`.github/workflows/release.yml` runs on a GitHub-hosted macOS 26 runner. On pull requests it runs the tests and builds the app and disk image (kept as a workflow artifact for 7 days). On every push to `main` it also publishes a release `v<major>.<minor>.<patch>` with the disk image and `.github/release-notes.md`. Each release bumps the patch of the latest release tag (`scripts/next_version.py`): 1.1.0, 1.1.1, 1.1.2… `CFBundleShortVersionString` in `macos/Info.plist` is the floor: set it to 1.2.0 or 2.0.0 to start a new series. The run number becomes `CFBundleVersion`.

These CI builds also get `JAISUpdateFeedURL`, the GitHub API URL of the latest release of the repository that built them: the app checks it at launch, once a day and from **Check for Updates…**, compares the release tag's version with its own, and offers the disk image. Local builds have no feed and never check. The feed must be readable without signing in, so update checks only work once the releases are public (or the feed points to a public copy).

## Interface language

Interface strings are English keys passed to `L(...)`. `macos/Resources/fr.lproj/Localizable.strings` and `ar.lproj` map them to French and Arabic, and `en.lproj` lists them for English; English is the default. The Arabic interface lays its windows, bubbles and menus out right to left; user and model text keeps its own direction.
