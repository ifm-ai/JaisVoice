# JAIS Voice implementation contract

Native macOS app + Python supervisor (`scripts/run_services.py`) for the local JAIS-ASR F5 service (127.0.0.1:8790) and the MLX gateway (127.0.0.1:8788), + the Pi agent in RPC mode, a pipe child of the app (`scripts/run_agent.py`). One Jais 3 model, chosen by size (0.9B, 3.7B or 7B), serves both transcript cleanup and the agent.

## Native app: one turn
- The app owns the turn. Audio: complete WAV PCM signed 16-bit mono 16000 Hz, at most 30 seconds.
- 1. ASR: `POST :8790/transcribe` `{audio: base64 WAV, language?: ar|msa|fr|en|es|it}`; omit language for automatic detection. Response `{text, refused, truncated, language}`. A refused, truncated or empty transcript stops the turn.
- 2. Cleanup: `POST :8788/v1/clean` `{text, model_size?: "0.9B"|"3.7B"|"7B"}`. Success `{text, elapsed_ms}`; failure 4xx `{detail: {code, message}}` with `cleaner_incomplete`, `cleaner_empty` or `cleaner_unavailable`. The raw transcript is never forwarded as a fallback. Explicit text input skips step 1 only.
- 3. Agent: Pi RPC `prompt` with the cleaned text only. Before it, `new_session` when the conversation changes and `set_model {provider: "jais", modelId: "jais-3-0.9b"|"jais-3-3.7b"|"jais-3-7b"}` when the size changes.
- Cancellation or a new turn: cancel the turn task, send Pi `abort`. Events of a stale turn are never shown.
- Health: `GET :8788/health` → `{status, message, models: {"0.9B": {display_name, installed, status: ready|loading|unloaded|error|not_installed, error?}, …}}` and `GET :8790/health`. Memory: `POST :8788/models/{0.9B|3.7B|7B}/memory` and `POST :8790/memory` `{action: load|unload}`. Only one language model is resident; loading one unloads the other.
- The app starts the supervisor with `JAIS_MODEL_SIZE` set to the selected size; the gateway loads it, or another installed size when it is missing.

## Models
- `models/catalog.json`: the ASR and one Jais 3 MLX build per size, each pinned to a Hugging Face commit.
- The app runs `scripts/models.py` (bundle resource `launch-models`): `list` → `{event: "catalog", models: [{id, kind, size?, display_name, installed, download_bytes?, disk_bytes, partial_bytes}]}`; `download <id>` → `{event: "progress", phase: download|verify|install, bytes?, total?}`… then `{event: "done"}`, `{event: "error", message}` (with `code: "no_access", repo, folder` when the repository is not accessible) or, after SIGTERM, `{event: "cancelled"}` (resumable); `delete <id>` → `{event: "deleted"}`.
- At launch the app offers the missing required models (the ASR, and the 3.7B when no language model is installed). Deleting a loaded language model unloads it first.

## Panel events (in-process `WireEvent`)
- Types: status (stage: transcribing/refining/acting), transcript, correction, tool_start, tool_end, response_delta, complete, clarification_request, error.
- Mapping from Pi events: `message_update`/`text_delta` → response_delta (append-only; the panel shows text written before an action above that action, and only the text after the last action as the response); `tool_execution_start`/`_end` → tool_start/tool_end sharing `tool_id` (browser tools without their `mcp__browser__` prefix; result text bounded to 2500 characters); `ask_clarifying_question` → clarification_request; `agent_settled` → complete, or error when the assistant failed or returned nothing. Thinking blocks are never shown.

## Pi agent (`agent/`)
- Started as `pi --mode rpc --no-session` with `PI_CODING_AGENT_DIR=.runtime/pi-agent` (config copied from `agent/` at each launch), offline, no built-in tools, extensions, skills, templates or context files; only `builtin:mcp` and `agent/extensions/jais`. Default model `jais-3-3.7b`, thinking level `low`.
- Models: provider `jais`, `openai-completions` API on `:8788/v1` (streamed), IDs `jais-3-0.9b`, `jais-3-3.7b` and `jais-3-7b` (16k context, 2048 output tokens). Pi compaction is off: it estimated the raw page snapshots, fired after the first page and replaced the request with an English summary; the extension's `context` hook bounds what the model sees instead, and appends a reply-in-Arabic reminder to Arabic requests and their tool results. Retries are off.
- Tools: `shell_execute(command, reason)`, `macos_create_note(title, body)`, `ask_clarifying_question(question)` (ends the run), and Playwright MCP `browser_*` tools (`mcp__browser__*`), headed Chromium, isolated profile.
- Approval: before `shell_execute` and `macos_create_note`, the extension sends an `extension_ui_request` (`method: "confirm"`, `title`: tool label, `message`: JSON `{reason, details}`, 125 s timeout). The app answers with `extension_ui_response` `{id, confirmed}` from the “Actions on this Mac” setting: `never` refuses, `always` accepts, `ask` (default) shows the action in the result bubble with Refuse / Allow buttons; no answer within 120 s, or a stopped turn, refuses it. `JAIS_ACTION_PERMISSION` overrides the setting for scripted tests. A refusal fails the tool call with an instruction not to retry.
- Shell: once approved, executed by the extension through `/bin/zsh -f -c`, new process group, minimal environment, cache working directory, 30 s maximum, output bounded to 8 KB, abort kills the group. Not a sandbox; macOS privacy permissions remain enforced. Page content and tool output never authorize commands.
- Per prompt: at most 50 browser/desktop actions; after a clarifying question no further action runs. Browser snapshot files are inlined from this process's output directory only. Older tool outputs are shortened in the model context (last 8 user turns kept).

## Runtime and ownership
- Project root: this directory; Python environment `.venv`; models, browser and Pi state under `.runtime/` (`JAIS_RUNTIME_DIR` overrides; the shared app uses `~/Library/Application Support/JAIS Voice`).
- The supervisor owns the model services and stops them when the app's ownership pipe closes. The app owns Pi: closing its stdin is an orderly shutdown (Pi stops its MCP servers). No killing by name/port.
- Production uses actual ASR and Jais 3 inference. Test substitutes must be explicit and never masquerade as real inference.
