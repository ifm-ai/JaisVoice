## What's new in 1.1.0

- Smaller app bundle with one Python runtime and no bundled Chromium. When Google Chrome is unavailable, Chromium is downloaded on first use.
- Shorter assistant system prompt focused on its role, available capabilities and essential safeguards, with more freedom to choose its approach.

## Install

1. Download the `.dmg` below and open it.
2. Drag **Jais Voice** onto **Applications**.
3. The app is not notarized by Apple: on first launch macOS blocks it. Open **System Settings → Privacy & Security** and click **Open Anyway** next to Jais Voice.

Requires an Apple Silicon Mac with macOS 26 and at least 16 GB of memory.

## Models

The app does not include any model. On first launch it offers to download the Jais-ASR transcription model (4.8 GB). Jais 3 language models (0.9B, 3.7B, 7B) are downloaded as MLX builds from the `IFM` Hugging Face repositories when you click **Download** in the settings (read access needed, or download the files yourself): see [docs/models.md](https://github.com/MBZUAI-IFM/JaisVoice/blob/main/docs/models.md).

## Known limitations

- The cleanup model can rewrite some English requests into an unrelated text (for example a date); French and Arabic are the tested languages.
- Actions on the Mac (shell, Notes) ask for approval by default; see **Actions on this Mac** in the settings.
