# Application native JAIS Voice

Application AppKit/SwiftUI de barre des menus, deux panneaux non activants, raccourci Carbon et capture audio native. `LocalPipeline` orchestre chaque tour : transcription par le service ASR local, nettoyage par la passerelle MLX (`/v1/clean`), puis agent Pi lancé en processus enfant (`AgentProcess`, JSONL sur stdin/stdout).

## Compiler

L’app n’a aucune dépendance tierce. Une compilation SwiftPM normale est possible sur une installation cohérente de Xcode/Command Line Tools :

```sh
swift build -c release
```

Sur ce Mac, le gestionnaire SwiftPM installé plante avant de traiter le projet et le SDK 27.0 est plus récent que le compilateur. Le chemin direct compile avec `swiftc` et le SDK 26.5 installé :

```sh
python3 build_direct.py
```

La sortie donne le binaire à assembler par `scripts/build_app.py` du projet parent, qui ajoute les lanceurs `launch-services` et `launch-agent`.

## Vérifier

Le bundle prend en charge des modes explicites de validation :

- `--self-test [--audio-fixture /chemin/extrait.wav]` : états de tours, annulation, diff arabe, délai de fermeture, WAV et conservation du dernier son pendant le rééchantillonnage. Aucun enregistrement microphone.
- `--preview` : aperçu marqué « APERÇU », sans démarrer les services et sans inférence.
- `--render-preview /chemin/apercu.png` : même aperçu, export du contenu du panneau puis fermeture.
- `--smoke-file /chemin/extrait.wav` : chaîne réelle ASR → nettoyage → Pi → réponse, sans capture microphone. Affiche le panneau, imprime `HORIZON_SMOKE_RESULT=` avec le résultat JSON puis quitte. `--smoke-stay` conserve l’application ouverte pour l’inspection visuelle.

Les tests de cœur peuvent également être compilés séparément :

```sh
swiftc -sdk /Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk \
  -module-cache-path .build/ModuleCache -swift-version 5 \
  Sources/HorizonVoice/TurnState.swift Sources/HorizonVoice/AudioRecorder.swift \
  Sources/HorizonVoice/SelfTests.swift Tests/RunCoreTests.swift \
  -o .build/NativeCoreTests
.build/NativeCoreTests
```

## Configuration de lancement

Le bundle contient `Resources/launch-services`, exécutable préparé par l’assemblage. Il démarre les services et reste leur propriétaire. Quitter l’application transmet SIGTERM et attend la fin du superviseur. Un canal privé relie sa durée de vie à celle de l’app : même une fermeture forcée déclenche le nettoyage. Le superviseur attend jusqu’à 15 secondes la libération du verrou d’une instance en cours d’arrêt, puis refuse de partager les services d’une autre instance. Fermer les réglages conserve volontairement l’assistant dans la barre de menus.

Variables optionnelles de développement : `HORIZON_LAUNCHER` et `HORIZON_AGENT_LAUNCHER` (chemins des lanceurs), `HORIZON_MLX_URL` (défaut `http://127.0.0.1:8788`), `HORIZON_ASR_URL` (défaut `http://127.0.0.1:8790`). Le raccourci choisi dans le menu est conservé dans les préférences natives.

Le raccourci se termine aussi lorsque les touches de modification sont relâchées avant Espace : le gestionnaire observe leurs états système pendant l’appui. Les répétitions sont ignorées. Seul le fichier PCM mono 16 kHz terminé est envoyé à l’ASR local.
