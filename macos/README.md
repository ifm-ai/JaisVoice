# Application native JAIS Voice

Application AppKit/SwiftUI de barre des menus, deux panneaux non activants, raccourci Carbon et capture audio native. Le SDK LiveKit Swift transporte le WAV complet et reçoit les événements réels de l’agent.

## Compiler

Le paquet Swift utilise le SDK binaire officiel LiveKit 2.16.0, verrouillé. Une compilation SwiftPM normale est possible sur une installation cohérente de Xcode/Command Line Tools :

```sh
swift build -c release
```

Sur ce Mac, le gestionnaire SwiftPM installé plante avant de traiter le projet et le SDK 27.0 est plus récent que le compilateur. Le chemin direct compile avec `swiftc` et le SDK 26.5 installé :

```sh
python3 build_direct.py --macos-only
```

`--macos-only` récupère uniquement les entrées macOS des archives officielles depuis leurs URLs HTTPS de release verrouillées et vérifie le CRC de chaque entrée. Sans cette option, le script télécharge les archives complètes et vérifie leurs SHA-256 publiés. Les composants sont mis en cache dans `.frameworks/`, leur provenance dans les fichiers `.source.json`. L’archive upstream LiveKit est signée ad hoc et contient des interfaces Swift, sans les modules binaires référencés par son ancien sceau de ressources ; le bundle final est donc signé localement lors de l’assemblage.

La sortie du script donne le binaire et les trois dossiers de frameworks à passer au script `scripts/build_app.py` du projet parent. Le binaire utilise `@executable_path/../Frameworks` et doit être exécuté depuis le bundle assemblé.

## Vérifier

Le bundle prend en charge des modes explicites de validation :

- `--self-test [--audio-fixture /chemin/extrait.wav]` : états de tours, annulation, diff arabe, délai de fermeture, WAV et conservation du dernier son pendant le rééchantillonnage. Aucun enregistrement microphone.
- `--preview` : aperçu marqué « APERÇU », sans démarrer les services et sans inférence.
- `--render-preview /chemin/apercu.png` : même aperçu, export du contenu du panneau puis fermeture.
- `--smoke-file /chemin/extrait.wav` : chaîne réelle LiveKit → ASR → K2 → réponse, sans capture microphone. Affiche le panneau, imprime `HORIZON_SMOKE_RESULT=` avec le résultat JSON puis quitte. `--smoke-stay` conserve l’application ouverte pour l’inspection visuelle.

Les tests de cœur peuvent également être compilés sans les frameworks LiveKit :

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

Variables optionnelles de développement : `HORIZON_LAUNCHER` (chemin d’un lanceur), `HORIZON_CONTROL_URL` (défaut `http://127.0.0.1:8787`). Le raccourci choisi dans le menu est conservé dans les préférences natives.

Le raccourci se termine aussi lorsque les touches de modification sont relâchées avant Espace : le gestionnaire observe leurs états système pendant l’appui. Les répétitions sont ignorées. La session ne publie aucune piste microphone LiveKit ; seul le fichier PCM mono 16 kHz terminé est envoyé.
