# Jais Voice native app

AppKit/SwiftUI menu bar app with two non-activating panels, a Carbon shortcut and native audio capture. `LocalPipeline` orchestrates each turn: transcription by the local ASR service, cleanup by the MLX gateway (`/v1/clean`), then the Pi agent run as a child process (`AgentProcess`, JSONL over stdin/stdout).

## Build

The app has no third-party dependency. `build_direct.py` compiles it with `swiftc` against the installed SDK (26.5 when present, since SDK 27.0 is newer than the compiler), without Swift Package Manager:

```sh
python3 build_direct.py
```

Its output is the binary that the parent project's `scripts/build_app.py` assembles, adding the `launch-services`, `launch-agent` and `launch-models` launchers.

## Check

The bundle supports explicit validation modes:

- `--self-test`: preferences, localization, requests, model state, turn states, app access policy, key combinations and menu matching, Arabic diff, dismissal delay, WAV and resampling. No microphone, services or models.
- `--render-preview /path/preview.png`: renders the result bubble for an example turn, without services or inference, then quits.
- `--render-settings /path/settings.png [--settings-tab general|assistant|models]`: the same for the settings window, with example models.
- `--smoke-file /path/clip.wav` and `--smoke-text "…"`: real ASR → cleanup → Pi → response pipeline, without microphone capture. Prints `JAIS_SMOKE_RESULT=` with the JSON result, then quits. `--smoke-text` can be repeated: each text is a turn in a new conversation, with one result line each.

The same binary also serves the agent: `--desktop-helper` reads and uses other apps through the accessibility API, for the Pi extension (JSON lines on stdin/stdout, see [docs/development.md](../docs/development.md)).

## Launch configuration

The bundle contains `Resources/launch-services`, an executable prepared during assembly. It starts the services and remains their owner. Quitting the app sends SIGTERM and waits for the supervisor to exit. A private pipe ties its lifetime to the app's: even a forced quit triggers cleanup. The supervisor waits up to 15 seconds for the lock of an instance that is shutting down, then refuses to share another instance's services. Closing the settings deliberately keeps the assistant in the menu bar.

Optional development variables: `JAIS_LAUNCHER`, `JAIS_AGENT_LAUNCHER` and `JAIS_MODELS_LAUNCHER` (launcher paths), `JAIS_MLX_URL` (default `http://127.0.0.1:8788`), `JAIS_ASR_URL` (default `http://127.0.0.1:8790`), `JAIS_ACTION_PERMISSION` (`never`, `ask` or `always`, overrides the action setting), `JAIS_DESKTOP_HELPER` (the binary the agent starts with `--desktop-helper`; this app's own by default). The shortcut chosen in the menu persists in the native preferences.

The shortcut also ends when the modifier keys are released before Space: the handler watches their system state during the press. Key repeats are ignored. Only the finished 16 kHz mono PCM file is sent to the local ASR.
