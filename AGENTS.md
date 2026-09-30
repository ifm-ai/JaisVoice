# JAISVoice

Ce dossier est la racine de développement de JAIS Voice, déplacée ici le 17 septembre 2026 à la demande de Florian. Utiliser `/Users/florian/Workspace/JAISVoice` pour la suite du développement.

- Interface macOS : `macos/Sources/HorizonVoice/`.
- Pipeline voix et agent : `backend/horizon_voice/`.
- Inférence locale : `k2_runtime/`.
- Modèles et services : `.runtime/`; profil actif : `.runtime/models/active-profile.json`.
- Environnement Python : `.venv/`.
- Application construite et livrables : `outputs/`. Application installée : `/Applications/JAIS Voice.app`.
- Construire : `.venv/bin/python scripts/build_app.py --direct`.
- Tests Python : `PYTHONPATH=.:backend .venv/bin/python -m pytest backend/tests tests -q -o asyncio_mode=auto`.
- Tests natifs : `outputs/JAIS Voice.app/Contents/MacOS/JAISVoice --self-test`.

Préserver les poids, sauvegardes et journaux. Ne pas réintroduire de chemins absolus vers l’ancien dossier Documents/Codex.
