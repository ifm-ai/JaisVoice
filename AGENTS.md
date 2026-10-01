# JAISVoice

This folder is the development root of JAIS Voice, moved here on 17 September 2026 at Florian's request. Use `/Users/florian/Workspace/JAISVoice` for further development.

- macOS interface: `macos/Sources/HorizonVoice/`.
- Agent: Pi harness in `agent/` (prompt, models, `extensions/jais/` extension), started by `scripts/run_agent.py`.
- Local ASR: `backend/horizon_voice/local_asr.py`. Cleanup and safeguard: `k2_runtime/` (`/v1/clean`).
- Local inference: `k2_runtime/`.
- Models and services: `.runtime/`; model catalog (Hugging Face repositories, sizes): `models/catalog.json`, managed by `scripts/models.py`.
- Python environment: `.venv/`.
- Documentation: `README.md` (users), `docs/models.md`, `docs/development.md`, `INTERFACES.md`. Built app and deliverables: `outputs/`. Installed app: `/Applications/JAIS Voice.app`.
- Build: `.venv/bin/python scripts/build_app.py --direct` (shareable self-contained app, data in `~/Library/Application Support/JAIS Voice`); `--dev` for an app that runs this checkout and `.runtime/`; `--dmg` for the drag-to-install disk image. Pushing to `main` publishes a release through `.github/workflows/release.yml`.
- Repository: https://github.com/MBZUAI-IFM/JaisVoice (`main` branch).
- Python tests: `PYTHONPATH=.:backend .venv/bin/python -m pytest backend/tests tests -q -o asyncio_mode=auto`.
- Pi extension tests: `node --test agent/`.
- Native tests: `outputs/JAIS Voice.app/Contents/MacOS/JAISVoice --self-test`.
- Language: code, comments, documentation and messages are in English. Interface strings are English keys passed to `L(...)`; add the French translation to `macos/Resources/fr.lproj/Localizable.strings` and the key to `en.lproj`.

Preserve weights, backups and logs. Do not reintroduce absolute paths to the old Documents/Codex folder.
