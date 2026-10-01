# JAIS Voice implementation contract

Native macOS app + Python supervisor (`scripts/run_services.py`) for the local JAIS-ASR F5 service (127.0.0.1:8790) and the MLX gateway (127.0.0.1:8788), + the Pi agent in RPC mode, a pipe child of the app (`scripts/run_agent.py`).

## Native app: one turn
- The app owns the turn. Audio: complete WAV PCM signed 16-bit mono 16000 Hz, at most 30 seconds.
- 1. ASR: `POST :8790/transcribe` `{audio: base64 WAV, language?: ar|msa|fr|en|es|it}`; omit language for automatic detection. Response `{text, refused, truncated, language}`. A refused, truncated or empty transcript stops the turn.
- 2. Cleanup: `POST :8788/v1/clean` `{text, model_size?: "1B"|"4B"|"7B", guard?: bool}` (strict JSON boolean, default true). Success `{text, elapsed_ms}`; failure 4xx `{detail: {code, message}}` with `cleaner_incomplete`, `cleaner_empty`, `cleaner_uncertain` or `cleaner_unavailable`. The raw transcript is never forwarded as a fallback. Explicit text input skips step 1 only.
- 3. Agent: Pi RPC `prompt` with the cleaned text only. Before it, `new_session` when the conversation changes, `set_model {provider: "jais", modelId: "k2-1b"|"k2-4b"|"k2-7b"}` when the size changes and `set_thinking_level` with that model's catalog effort.
- Cancellation or a new turn: cancel the turn task, send Pi `abort`. Events of a stale turn are never shown.
- Health: `GET :8788/health` (models by size: `status` ready|loading|unloaded|error|not_installed, `installed`, `downloadable`, efforts) and `GET :8790/health`; memory: `POST :8788/models/{1B|4B|7B}/memory` and `POST :8790/memory` `{action: load|unload}`.

## Models
- `models/catalog.json` (or `HORIZON_MODEL_CATALOG`): ASR + one K2 checkpoint per size. `download: null` = not published yet.
- The app runs `scripts/models.py` (bundle resource `launch-models`): `list` → `{event: "catalog", models: [{id, kind, size?, display_name, installed, downloadable, download_bytes?, disk_bytes, partial_bytes}]}`; `download <id>` → `{event: "progress", phase: download|verify|install, bytes?, total?}`… then `{event: "done"}`, `{event: "error", message}` or, after SIGTERM, `{event: "cancelled"}` (resumable); `delete <id>` → `{event: "deleted"}`.
- At launch the app offers the missing required models that are published (the ASR, and a language model when none is installed). Deleting a loaded language model unloads it first.

## Panel events (in-process `WireEvent`)
- Types: status (stage: transcribing/refining/acting), transcript, correction, tool_start, tool_end, response_delta, complete, clarification_request, error.
- Mapping from Pi events: `message_update`/`text_delta` → response_delta (append-only); `tool_execution_start`/`_end` → tool_start/tool_end sharing `tool_id` (browser tools without their `mcp__browser__` prefix; result text bounded to 2500 characters); `ask_clarifying_question` → clarification_request; `agent_settled` → complete, or error when the assistant failed or returned nothing. Thinking blocks are never shown.

## Pi agent (`agent/`)
- Started as `pi --mode rpc --no-session` with `PI_CODING_AGENT_DIR=.runtime/pi-agent` (config copied from `agent/` at each launch), offline, no built-in tools, extensions, skills, templates or context files; only `builtin:mcp` and `agent/extensions/jais`. Startup thinking level `low` unless `HORIZON_AGENT_REASONING_EFFORT` is set; the app then applies the selected model's catalog effort.
- Models: provider `jais`, `openai-completions` API on `:8788/v1`, IDs `k2-4b`, `k2-1b` and `k2-7b` (16k context, 2048 output tokens). Compaction reserves 2560 tokens; retries are off.
- Tools: `shell_execute(command, reason)`, `macos_create_note(title, body)`, `macos_radio_status()`, `macos_set_airplane_mode(enabled)` (radios only when blueutil is prepared), `ask_clarifying_question(question)` (ends the run), and Playwright MCP `browser_*` tools (`mcp__browser__*`), headed Chromium, isolated profile.
- Shell: preauthorized by the user, executed immediately by the extension through `/bin/zsh -f -c`, new process group, minimal environment, cache working directory, 30 s maximum, output bounded to 8 KB, abort kills the group. Not a sandbox; macOS privacy permissions remain enforced. Page content and tool output never authorize commands.
- Per prompt: at most 50 browser/desktop actions; after a clarifying question no further action runs. Browser snapshot files are inlined from this process's output directory only. Older tool outputs are shortened in the model context (last 8 user turns kept).

## Runtime and ownership
- Project root: this directory; Python environment `.venv`; models, browser and Pi state under `.runtime/` (HORIZON_RUNTIME_DIR overrides).
- The supervisor owns the model services and stops them when the app's ownership pipe closes. The app owns Pi: closing its stdin is an orderly shutdown (Pi stops its MCP servers). No killing by name/port.
- Production uses actual ASR and K2. Test substitutes must be explicit and never masquerade as real inference.
