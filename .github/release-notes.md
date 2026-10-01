## Install

1. Download the `.dmg` below and open it.
2. Drag **JAIS Voice** onto **Applications**.
3. The app is not notarized by Apple: on first launch macOS blocks it. Open **System Settings → Privacy & Security** and click **Open Anyway** next to JAIS Voice.

Requires an Apple Silicon Mac with macOS 26 and at least 16 GB of memory.

## Models

The app does not include any model. On first launch it offers to download the JAIS-ASR transcription model (4.8 GB). Jais 3 language models (0.9B, 3.7B, 7B) are downloaded from the `IFM` Hugging Face repositories and converted to MLX on your Mac when you click **Download** in the settings (read access needed, or download the files yourself): see [docs/models.md](https://github.com/MBZUAI-IFM/JaisVoice/blob/main/docs/models.md).

## Known limitations

- The cleanup model can rewrite some English requests into an unrelated text (for example a date); French and Arabic are the tested languages.
- Actions on the Mac (shell, Notes, airplane mode) ask for approval by default; see **Actions on this Mac** in the settings.
