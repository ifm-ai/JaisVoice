# JAISVoice

Ce dossier est la racine de développement de JAIS Voice, déplacée ici le 17 septembre 2026 à la demande de Florian. Utiliser `/Users/florian/Workspace/JAISVoice` pour la suite du développement.

- Interface macOS : `macos/Sources/HorizonVoice/`.
- Agent : harness Pi dans `agent/` (prompt, modèles, extension `extensions/jais/`), lancé par `scripts/run_agent.py`.
- ASR local : `backend/horizon_voice/local_asr.py`. Nettoyage et garde-fou : `k2_runtime/` (`/v1/clean`).
- Inférence locale : `k2_runtime/`.
- Modèles et services : `.runtime/` ; catalogue des modèles (dépôts Hugging Face, tailles) : `models/catalog.json`, géré par `scripts/models.py`.
- Environnement Python : `.venv/`.
- Application construite et livrables : `outputs/`. Application installée : `/Applications/JAIS Voice.app`.
- Construire : `.venv/bin/python scripts/build_app.py --direct`.
- Tests Python : `PYTHONPATH=.:backend .venv/bin/python -m pytest backend/tests tests -q -o asyncio_mode=auto`.
- Tests de l’extension Pi : `node --test agent/`.
- Tests natifs : `outputs/JAIS Voice.app/Contents/MacOS/JAISVoice --self-test`.

Préserver les poids, sauvegardes et journaux. Ne pas réintroduire de chemins absolus vers l’ancien dossier Documents/Codex.
