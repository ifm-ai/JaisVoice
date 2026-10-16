## What's new in 1.1.0

- Smaller app bundle with one Python runtime and no bundled Chromium. When Google Chrome is unavailable, Chromium is downloaded on first use.
- Shorter assistant system prompt focused on its role, available capabilities and essential safeguards, with more freedom to choose its approach.

## Install

1. Download the `.dmg` file below (you only need this file).
2. Double-click the `.dmg` file, then drag **Jais Voice** onto **Applications**.
3. The app is not notarized by Apple yet, so macOS blocks it on first launch. To open it anyway:
   1. Open **System Settings**.
   2. Go to **Privacy & Security** and scroll down to the **Security** section.
   3. Click **Open Anyway** next to Jais Voice.

Requires an Apple Silicon Mac with macOS 26 and at least 16 GB of memory.

## Models

The app does not include any model. On first launch it offers to download the Jais-ASR transcription model (7.0 GB). Jais 3 language models (0.9B, 3.7B, 7B) are downloaded as MLX builds from the `IFM` Hugging Face repositories when you click **Download** in the settings (read access needed, or download the files yourself): see [docs/models.md](https://github.com/ifm-ai/JaisVoice/blob/main/docs/models.md).

## Known limitations

- The cleanup model can rewrite some English requests into an unrelated text (for example a date); French and Arabic are the tested languages.
- Actions on the Mac (shell, Notes) ask for approval by default; see **Actions on this Mac** in the settings.
