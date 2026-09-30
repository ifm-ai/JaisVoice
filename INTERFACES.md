# Horizon Voice implementation contract

Native macOS app + local LiveKit server (127.0.0.1:7880, devkey/secret), Python control server (127.0.0.1:8787), dedicated MLX gateway (127.0.0.1:8788), LiveKit Python agent and Playwright MCP child.

## Native/control
- GET /health: JSON {status: "starting"|"ready"|"error", message: string, models?: object}.
- `models[alias]` includes `display_name`, `status: waiting|loading|ready|error`, optional `error`. Both entries are exposed before weight loading; readiness follows actual model warmup. The configuration window and open menu show independent live states, with no synthetic progress percentage.
- POST /session with optional {client_id:string}: JSON {url:string, token:string, room:string}. Create and explicitly dispatch one `horizon-voice` agent into the room. Only return ready session credentials when upstream services are available.
- GET /session readiness is not assumed; Swift watches agent participant joining before upload.

## LiveKit transport
- Audio: complete WAV PCM signed 16-bit mono 16000 Hz, at most 30 seconds, byte stream topic `horizon.audio`, attributes {turn_id: UUID string, language?: ar|msa|fr|en|es|it}, MIME audio/wav. Omit language for automatic detection. Language is scoped to a single turn and forwarded as an optional ASR multipart field. Finish stream explicitly.
- UI events: reliable data packets topic `horizon.events`, UTF-8 JSON. Fields: type, turn_id, optional text, stage, tool_id, tool_name, arguments (object), result (string), elapsed_ms, error_code.
- Event types: status (stage: transcribing/refining/acting), transcript, correction, tool_start, tool_end, response_delta, complete, error, cancelled.
- response_delta.text is an append-only answer delta; other text fields are complete values. `complete.text` contains final answer. Tool events share tool_id. No internal reasoning in UI packets.
- Control: reliable data packets topic `horizon.control`, {type:"cancel",turn_id:string}. Starting new turn invalidates previous turn. Late events ignored by native client and backend.
- Desktop tool: `shell_execute(command, reason)` sends `shell_request {turn_id,tool_id,command,reason}` only after cleanup. The authenticated native app validates then executes the immutable command immediately, as explicitly requested by the user. No per-command Horizon approval remains; no Python-side shell executes. Commands run through `/bin/zsh -f -c` for at most 30 s with a minimal environment, app-cache working directory, bounded output and owned process-group cleanup. OS privacy permissions remain enforced.
- Native control reply: `shell_result {turn_id,tool_id,approved:bool,exit_code:int|null,output:string,error:string|null,timed_out:bool}`. The legacy `approved` field means accepted under the user's global authorization, not a per-command click. Validation failures have no exit code and `timed_out:false`. Results match a single authenticated pending turn/call pair; duplicates/stale packets are ignored. Native rejection blocks further shell requests in the same turn.
- `shell_cancel {turn_id,tool_id}` cancels a pending or executing native request even after its turn becomes stale. Already-completed filesystem/OS side effects cannot be rolled back by cancellation. Shell and browser share the same serial lock and 50-action budget.
- Explicit text input: authenticated control `text_request {turn_id:UUID,text:string}` (at most 4000 UTF-8 bytes) skips ASR only, still runs the actual cleaner and agent. Native CLI `--smoke-text` labels this mode. No unauthenticated shell HTTP endpoint exists.

## MLX gateway
- GET /health: JSON {status:"starting"|"ready"|"error", message:string, models: object}.
- POST /v1/chat/completions: OpenAI-style protocol, aliases `k2-cleaner` (0.9B) and `k2-agent` (7B), supports stream and tools. Streaming content and structured tool calls, finish_reason stop/tool_calls/length; preserve reasoning_content separately.
- Cleaner request messages = system cleanup instruction + user raw transcript. Only final content is cleaned transcript; length or empty final content is an error, no raw automatic execution fallback.
- Models resident in one process, serial GPU generation, disconnected requests cancel generation. Tool parsing uses native K2 templates. Context defaults 4096 cleaner / 16384 agent.

## Runtime and ownership
- Project root: work/horizon-voice; Python environment `.venv`; dependencies, binaries, browser and models under `.runtime/` (HORIZON_RUNTIME_DIR overrides).
- Python package backend/horizon_voice. Control and agent modules belong to backend agent. `k2_runtime/` package and scripts/prepare_models.py belong to MLX agent. Native app under macos/ belongs to native agent. Root owns setup/build/start scripts, tests integration, guide and packaging.
- All launched children scoped to app process tree; launcher keeps PID ownership and terminates owned children on app exit. No killing by name/port.
- Production demo uses actual ASR and K2. Test substitutes must be explicit and never masquerade as real inference.
