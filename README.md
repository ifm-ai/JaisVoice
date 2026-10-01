<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/jais-logo-dark.png">
    <img src="docs/images/jais-logo.png" alt="Jais" width="260">
  </picture>
</p>

<h1 align="center">JAIS Voice</h1>

<p align="center">
  A private voice assistant for macOS that runs entirely on your Mac.<br>
  Hold a shortcut, speak in Arabic, French or English, and let it answer, browse or act for you.
</p>

<p align="center">
  <a href="https://github.com/MBZUAI-IFM/JaisVoice/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/MBZUAI-IFM/JaisVoice?label=release"></a>
  <img alt="macOS 26+" src="https://img.shields.io/badge/macOS-26%2B-black?logo=apple">
  <img alt="Apple Silicon" src="https://img.shields.io/badge/Apple%20Silicon-required-black">
  <a href="https://github.com/MBZUAI-IFM/JaisVoice/actions/workflows/release.yml"><img alt="Build" src="https://github.com/MBZUAI-IFM/JaisVoice/actions/workflows/release.yml/badge.svg"></a>
</p>

<p align="center">
  <img src="docs/images/bubble.png" alt="A request corrected and answered in the JAIS Voice bubble" width="300">
  &nbsp;&nbsp;
  <img src="docs/images/approval.png" alt="JAIS Voice asking before running a command" width="300">
</p>

## Features

- **Local speech recognition** with JAIS-ASR, including Arabic dialects.
- **Self-correction cleanup**: “to Ahmed… I mean Sara” becomes “to Sara” before anything runs.
- **An agent that acts**: it answers, browses the web in a dedicated Chromium, creates notes and runs macOS commands.
- **You stay in control**: every action on your Mac is shown for approval by default (never, ask, or always).
- **No cloud**: speech, models and agent run on your Mac with MLX; only the web pages you ask for are fetched.

## Installation

1. Download `JAIS-Voice-<version>.dmg` from the [latest release](https://github.com/MBZUAI-IFM/JaisVoice/releases/latest).
2. Open it and drag **JAIS Voice** onto **Applications**.
3. The app is not notarized by Apple yet. On first launch, open **System Settings → Privacy & Security** and click **Open Anyway**.

Requires an Apple Silicon Mac with macOS 26 and at least 16 GB of memory. On first launch, the app offers to download the speech model; language models are installed as described below.

## Models

| Role | Model |
| --- | --- |
| Speech recognition | [MBZUAI-Paris/JAIS-ASR-MLX](https://huggingface.co/MBZUAI-Paris/JAIS-ASR-MLX) |
| Cleanup and agent | [IFM/Jais-3-3.7B](https://huggingface.co/IFM/Jais-3-3.7B) |
| Agent, smaller / larger | [IFM/Jais-3-0.9B](https://huggingface.co/IFM/Jais-3-0.9B) · [IFM/Jais-3-7B](https://huggingface.co/IFM/Jais-3-7B) |

These repositories currently require access on Hugging Face. The app downloads the speech model itself; MLX builds of the language models are not published yet, so they are converted on your Mac for now. See [docs/models.md](docs/models.md).

## Usage

- Hold **⌥ Space**, speak, release. The answer appears in a small bubble at the bottom of the screen.
- Hold the shortcut again while the bubble is visible to follow up in the same conversation.
- Open the settings from the menu bar to change the shortcut, the transcription language, the model size, the interface language (English or French) and **Actions on this Mac**.

## Build from source

```sh
python3.12 scripts/setup.py
.venv/bin/python scripts/build_app.py --direct --dmg
```

The app and the disk image land in `outputs/`. Every push to `main` builds, tests and publishes a release through GitHub Actions. See [docs/development.md](docs/development.md) for the architecture, tests and release process.
