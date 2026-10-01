# Local K2 gateway

`python scripts/prepare_models.py` downloads pinned official IFM checkpoints and
prepares the cleaner as BF16 and agent as affine 8-bit, group size 64. Models and
provenance live under `.runtime/models` (`HORIZON_RUNTIME_DIR` overrides).

`python -m k2_runtime.gateway --host 127.0.0.1 --port 8788` exposes `/health` and
`/v1/chat/completions`. Aliases are `k2-cleaner` and `k2-agent`. The gateway keeps
both models resident in one dedicated MLX thread and serializes all generation.
It requires Metal GPU access. No remote model fallback exists.

The Pi agent selects a size by model ID: `k2-4b` and `k2-1b` are the agent role
(`k2-agent`) on that checkpoint. `POST /v1/clean` `{text, model_size?, guard?}`
runs the transcript cleaner with its fixed prompt and, unless `guard` is `false`,
the lexical `cleanup_guard`; it returns `{text}` or an error `{detail: {code,
message}}` and never falls back to the raw transcript.

## Explicit checkpoint profiles

`HORIZON_MODEL_MANIFEST=/absolute/path/to/manifest-mblab.json` selects another
local pair at process startup. A directly launched gateway without that variable
uses `.runtime/models/manifest.json` unchanged. A missing or invalid profile is an
error, never a silent fallback to other weights. Stop and restart the services
to change profiles.

For daily launches from the macOS application, persist a validated selection:

```
.venv/bin/python scripts/select_model_profile.py --manifest .runtime/models/manifest-mblab.json
```

This atomically writes only `.runtime/models/active-profile.json`. It does not
alter `manifest.json` or any checkpoint. The service launcher resolves, in order:
an explicit `HORIZON_MODEL_MANIFEST`, the persisted selection, then the original
IFM manifest if no selection exists. A broken selection stops startup with an
error rather than silently choosing a different model. The complete pair is
structurally checked before activation and before any service is started:
configuration, tokenizer/template presence, shard index, payload completeness,
tensor names, shapes and dtypes, including 8-bit affine/group-64 layouts. This
does not replace source checksum verification or real-inference quality tests.

To restore the original default on the next app launch:

```
.venv/bin/python scripts/select_model_profile.py --reset
```

Reset removes only the selector; all manifests and downloaded models remain.
The explicit environment override, if present, still has priority. These
commands accept `--runtime-dir` for a separate installation or test environment.

The manifest keeps the two role aliases `k2-cleaner` and `k2-agent`; each entry
requires `path` (relative to the manifest, or absolute). Existing provenance
fields such as `repo`, `revision` and `precision` are preserved. Optional fields:

| Field | Default / accepted values |
| --- | --- |
| `display_name` | `repo`, otherwise the alias; surfaced through `/health` |
| `loader` | `horizon-native-mlx-v1` only |
| `protocol` | `ifm-k2-v1` only |
| `context_limit` | 4096 cleaner / 16384 agent, including output reservation |
| `reasoning_efforts` | `["low", "medium", "high"]`; unsupported efforts fail |
| `default_reasoning_effort` | `low` cleaner / `medium` agent for legacy profiles; must belong to `reasoning_efforts` |
| `tool_call_format` | `xml`; also `xml_typed` or `json` |
| `tool_presentation_format` | `markdown`; also `xml` or `json` |

The service launcher uses each profile's `default_reasoning_effort` unless the
corresponding `HORIZON_CLEANER_REASONING_EFFORT` or
`HORIZON_AGENT_REASONING_EFFORT` is explicitly set. An unsupported override
fails before service startup. The MB lab profile selects `low` for both roles;
returning to the unchanged IFM profile restores cleaner `low` and agent
`medium`. These preferences do not modify weights or conversion provenance.

This is bounded support for compatible dense K2 checkpoints, not a generic
architecture loader. Downloaded Python model code is never executed. Profile
selection does not disable the transcript guard or alter browser permissions.

Some checkpoint templates, including the MB lab 1B/4B exports, already close
the empty reasoning block in the **low** generation prefix. The gateway checks
the actual rendered prefix and starts parsing final content only in that case.
Open reasoning prefixes still require a generated closing marker. Unexpected
prefixes fail explicitly; no template is patched and no effort is silently
changed. Template framing is checked before the gateway becomes ready.

## Architecture fidelity

The IFM 7B model uses four-group RMS normalization, including its final layer
norm. The IFM 0.9B model uses one group and YaRN RoPE with its published beta
values and attention factor. The MBZUAI 1B uses one group and default RoPE with
base 1,000,000; its 4B counterpart uses two groups and default RoPE with base
10,000,000. Query projection dimensions are derived from heads × head dimension,
not hidden size (4B: 4096 query channels for a hidden size of 2560).
The local implementation follows the pinned architecture files; it
does not monkey-patch the installed MLX-VLM package or execute downloaded Python.
Tokenizer files and native chat templates are loaded locally.

The official template defaults to `reasoning_effort=low`; `medium` and `high`
are also supported with their own reasoning markers. It uses IFM's default
Markdown tool descriptions and XML argument tags. Typed arguments are validated
against the declared JSON schema and re-encoded as structured OpenAI tool calls.
Assistant history is normalized
to the thinking field required by the 7B template. Reasoning deltas use the
separate `reasoning_content` field and never enter `content`.

Context limits include reserved output tokens: 4,096 for the cleaner and 16,384
for the agent. Truncation, incomplete reasoning, malformed tools, unknown tools
and empty final answers produce errors. Tool deltas are released only after a
complete, valid generation. Client disconnect cancels queued work or stops an
active generation at the next prefill/token boundary.

`GET /v1/models` lists the local aliases. `/health` reports the active request's
model, stage and generated-token count without exposing content. Diagnostics are
opt-in: `debug_trace: true` on a request, or `HORIZON_MLX_DIAGNOSTICS=1`, saves the
request and raw model output under `.runtime/validation/mlx-diagnostics`. Leave
this disabled for ordinary use because the trace contains transcript content.

## Converting the mblab 1B and 4B exports

The local source directories are `.runtime/models/sources/mblab-1B-32bfb4a00c1e`
and `.runtime/models/sources/mblab-4B-20e95a2e3f44`. Copy every indexed shard and
the original metadata before running the converter; temporary transfer files
are not accepted as complete checkpoints. This script does not download data
or change files on `mblab`.

```
.venv/bin/python scripts/prepare_mblab_models.py --verify-only
.venv/bin/python scripts/prepare_mblab_models.py
```

Both exports physically store FP32 tensors, despite the 4B config's BF16 label.
The converter rounds the 1B weights to BF16. For the 4B it rounds to BF16 and
quantizes 2D embedding/linear weights to 8-bit affine groups of 64, while keeping
normalization weights in BF16. Norm offsets are **already baked into the source**;
the conversion does not add another offset. Tokenizer, native template, generation
config and offset provenance are preserved. Downloaded Python is not copied to
the converted model or executed.

Verification checks the pinned repository, revision and reference-code SHA256,
complete index, all tensor names/shapes, physical byte spans and source shard
SHA256. The 1B expects `source-sha256.json` with a `files` mapping from shard name
to digest, independently read from the remote copy; the 4B uses the explicit
`file_details` digests in its `download-manifest.json`. Alternate hash files can
be supplied with `--hashes-1b` / `--hashes-4b`. A statement that hashes were checked
elsewhere, without the actual expected digests, is not accepted.

To process only a completed download, add `--model k2-cleaner` or `--model k2-agent`.
Output directories are `MBZUAI-1B-32bfb4a00c1e-bf16` and
`MBZUAI-4B-20e95a2e3f44-8bit`, under `.runtime/models`. Every converted file is hashed
and recorded with source audit data in `horizon_model.json`. The script writes
`manifest-mblab.json` separately and does not activate it; it is a usable pair
only once both roles have been converted and validated. Existing incomplete
targets or `.preparing` directories require inspection rather than automatic
overwrite. Re-running a completed conversion verifies its files before reuse.

Check numerical fidelity separately from linguistic quality:

```
.venv/bin/python -m k2_runtime.validate_quantization --source .runtime/models/sources/mblab-1B-32bfb4a00c1e --quantized .runtime/models/MBZUAI-1B-32bfb4a00c1e-bf16 --report .runtime/validation/mblab-1b-conversion.json
.venv/bin/python -m k2_runtime.validate_quantization --source .runtime/models/sources/mblab-4B-20e95a2e3f44 --quantized .runtime/models/MBZUAI-4B-20e95a2e3f44-8bit --report .runtime/validation/mblab-4b-conversion.json
```

The comparison loads the real source with its stored floating-point dtype, frees
it, then loads the converted model. Stop the app/gateway first to avoid competing
GPU allocations. It compares next-token distributions on three prompts; matching
top-1 and KL below 0.05 nats is a conversion check, **not** a guarantee of Arabic
correction or agent reliability. `scripts/evaluate_mblab.py` tests those behaviors
through the running gateway; real browser and native-app smoke tests remain
separate checks.

## Validation

```
python -m pytest -q tests/test_k2_protocol.py tests/test_k2_numeric.py tests/test_k2_gateway.py tests/test_k2_profiles.py tests/test_checkpoint_validation.py tests/test_mblab_conversion.py
python -m k2_runtime.validate --model .runtime/models/K2-Horizon-0.9B-bf16
python -m k2_runtime.validate --agent --model .runtime/models/K2-Horizon-7B-8bit
python -m k2_runtime.validate_quantization --source .runtime/models/sources/K2-Horizon-7B-ff325e226270 --quantized .runtime/models/K2-Horizon-7B-8bit
```

Optional direct upstream primitive parity (PyTorch stays outside production):

```
python -m pip install --target .runtime/validation-deps --no-deps torch==2.8.0
python -m k2_runtime.validate_reference --source .runtime/models/sources/K2-Horizon-7B-ff325e226270/modeling_k2_horizon.py --report .runtime/validation/k2-reference-parity.json
```

The tests with an explicit substitute worker cover HTTP framing and cancellation;
the validation commands use real model inference. The clean-up prompt selects
examples by the input's dominant script and quotes the transcript as source data.
The 0.9B model did not reliably follow a bare instruction or preserve all repairs
in held-out Arabic samples; higher effort alone did not solve this. Passing a
selected demo corpus is not a general Arabic accuracy guarantee. The backend
validates conservative edits before allowing the agent to act; always show both
original and corrected transcription.
