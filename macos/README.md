# JAIS Voice native app

AppKit/SwiftUI menu bar app with two non-activating panels, a Carbon shortcut and native audio capture. `LocalPipeline` orchestrates each turn: transcription by the local ASR service, cleanup by the MLX gateway (`/v1/clean`), then the Pi agent run as a child process (`AgentProcess`, JSONL over stdin/stdout).

## Build

The app has no third-party dependency. A regular SwiftPM build works on a consistent Xcode/Command Line Tools installation:

```sh
swift build -c release
```

On this Mac, the installed SwiftPM crashes before processing the project and SDK 27.0 is newer than the compiler. The direct path builds with `swiftc` and the installed SDK 26.5:

```sh
python3 build_direct.py
```

Its output is the binary that the parent project's `scripts/build_app.py` assembles, adding the `launch-services`, `launch-agent` and `launch-models` launchers.

## Check

The bundle supports explicit validation modes:

- `--self-test [--audio-fixture /path/clip.wav]`: turn states, cancellation, Arabic diff, dismissal delay, WAV and keeping the last sound while resampling. No microphone recording.
- `--preview`: preview labelled “PREVIEW”, without starting services and without inference. `--preview-followup`, `--preview-clarification` and `--preview-approval` add a follow-up turn, a clarifying question or an action approval.
- `--render-preview /path/preview.png`: the same preview, exports the panel content then quits. `--render-settings` does the same for the settings window.
- `--smoke-file /path/clip.wav` and `--smoke-text "…"`: real ASR → cleanup → Pi → response pipeline, without microphone capture. Shows the panel, prints `HORIZON_SMOKE_RESULT=` with the JSON result, then quits. `--smoke-stay` keeps the app open for visual inspection.

The core tests can also be compiled separately:

```sh
swiftc -sdk /Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk \
  -module-cache-path .build/ModuleCache -swift-version 5 \
  Sources/HorizonVoice/TurnState.swift Sources/HorizonVoice/AudioRecorder.swift \
  Sources/HorizonVoice/SelfTests.swift Tests/RunCoreTests.swift \
  -o .build/NativeCoreTests
.build/NativeCoreTests
```

## Launch configuration

The bundle contains `Resources/launch-services`, an executable prepared during assembly. It starts the services and remains their owner. Quitting the app sends SIGTERM and waits for the supervisor to exit. A private pipe ties its lifetime to the app's: even a forced quit triggers cleanup. The supervisor waits up to 15 seconds for the lock of an instance that is shutting down, then refuses to share another instance's services. Closing the settings deliberately keeps the assistant in the menu bar.

Optional development variables: `HORIZON_LAUNCHER`, `HORIZON_AGENT_LAUNCHER` and `HORIZON_MODELS_LAUNCHER` (launcher paths), `HORIZON_MLX_URL` (default `http://127.0.0.1:8788`), `HORIZON_ASR_URL` (default `http://127.0.0.1:8790`), `HORIZON_ACTION_PERMISSION` (`never`, `ask` or `always`, overrides the action setting). The shortcut chosen in the menu persists in the native preferences.

The shortcut also ends when the modifier keys are released before Space: the handler watches their system state during the press. Key repeats are ignored. Only the finished 16 kHz mono PCM file is sent to the local ASR.
