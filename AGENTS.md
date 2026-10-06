# JAISVoice

This folder is the development root of Jais Voice, moved here on 17 September 2026 at Florian's request. Use `/Users/florian/Workspace/JAISVoice` for further development.

- macOS interface: `macos/Sources/JAISVoice/`.
- Agent: Pi harness in `agent/` (prompt, models, `extensions/jais/` extension), started by `scripts/run_agent.py`.
- Local ASR: `jais_runtime/asr_service.py`.
- Language model (Jais 3 under MLX, model catalog and downloads, `/v1/clean` cleanup, agent completions): `jais_runtime/`.
- Models and services: `.runtime/`; model catalog (Hugging Face repositories, sizes): `models/catalog.json`, managed by `scripts/models.py`.
- Python environment: `.venv/`.
- Documentation: `README.md` (users), `docs/models.md`, `docs/development.md`, `INTERFACES.md`. Built app and deliverables: `outputs/`. Installed app: `/Applications/Jais Voice.app`.
- Build: `.venv/bin/python scripts/build_app.py` (shareable self-contained app, data in `~/Library/Application Support/Jais Voice`); `--dev` for an app that runs this checkout and `.runtime/`; `--dmg` for the drag-to-install disk image. Pushing to `main` publishes a release through `.github/workflows/release.yml`.
- Repository: https://github.com/MBZUAI-IFM/JaisVoice (`main` branch).
- Python tests: `.venv/bin/python -m pytest -q`.
- Pi extension tests: `node --test agent/`.
- Native tests: `outputs/Jais Voice.app/Contents/MacOS/JAISVoice --self-test`.
- Language: code, comments, documentation and messages are in English. Interface strings are English keys passed to `L(...)`; add the French translation to `macos/Resources/fr.lproj/Localizable.strings` and the key to `en.lproj`.

Preserve weights, backups and logs. Do not reintroduce absolute paths to the old Documents/Codex folder.
