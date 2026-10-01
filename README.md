> Racine de développement : `/Users/florian/Workspace/JAISVoice`. Les modèles sont dans `.runtime/models/` et les livrables dans `outputs/`.

# JAIS Voice

Les réglages proposent un seul sélecteur **1B / 4B / 7B**, commun au **nettoyage et à l’agent**. Le choix est conservé après fermeture et appliqué à la prochaine demande audio ou texte. Les deux étapes gardent leurs prompts et paramètres propres, mais utilisent le même checkpoint, chargé une seule fois.

**Modèles à la demande.** `models/catalog.json` liste les modèles et leurs dépôts Hugging Face. Au lancement, l’app propose de télécharger les modèles requis absents ; les réglages permettent de télécharger, annuler (reprise possible), décharger ou supprimer chaque modèle. Les modèles K2 n’ont pas encore de dépôt MLX publié : ils apparaissent « Bientôt disponible » tant que leur entrée `download` vaut `null`, et les conversions déjà présentes sur ce Mac restent utilisables et supprimables. Le 7B MBZUAI a sa place réservée dans le catalogue.

La transcription utilise **JAIS-ASR-MLX F5 sur ce Mac**, sans serveur ASR distant. Le modèle est conservé en mémoire par un service local isolé, lancé avec l’application.

Assistant vocal natif pour ce Mac Apple Silicon : appuyer sur **⌥ Espace**, parler, relâcher. La capsule d’écoute laisse place à un petit panneau avec transcription, corrections, navigation et réponse. Les deux K2 fonctionnent sous MLX sur le Mac.

**Statut : prototype supervisé, profil mblab 1B/4B activé le 16 septembre 2026.** Les deux conversions MLX et la chaîne audio native fonctionnent. Le 1B réussit 12/16 cas de nettoyage stricts ; le 4B sait rechercher et ouvrir des pages, mais peut choisir un mauvais site et le présenter comme officiel. Voir `Validation-MBLab.md` dans le dossier de livraison pour les succès et échecs mesurés. L’ancien couple IFM reste utilisable via `models/catalog-ifm.json`.

## Architecture

- `macos/` : app SwiftUI/AppKit, panneaux non activants, raccourci global Carbon, audio AVFoundation. Elle orchestre chaque tour (ASR → nettoyage → agent) et ne dépend d’aucun framework tiers.
- `agent/` : harness d’agent [Pi](https://github.com/earendil-works/pi) (`@earendil-works/pi-coding-agent`, épinglé dans `package.json`) : prompt système, modèles locaux et extension `extensions/jais/` (shell macOS, Notes, radios, question de clarification, navigateur Playwright MCP, budget de 50 actions).
- `k2_runtime/` : architecture K2 fidèle (normalisation par groupes et YaRN), passerelle locale Chat Completions, nettoyage `/v1/clean` avec garde-fou, parseur de réponses et d’appels d’outils.
- `backend/horizon_voice/local_asr.py` : service JAIS-ASR F5 local.
- `.runtime/` : poids, navigateur Chromium dédié, état de Pi et journaux. `.venv/` contient les dépendances Python isolées.

Audio WAV → JAIS-ASR F5 local → LLM choisi (nettoyage, `/v1/clean`) → Pi avec le même LLM (agent) → outils navigateur / macOS / réponse textuelle.

L’app lance Pi en mode RPC comme processus enfant (`scripts/run_agent.py`) et dialogue avec lui en JSONL sur stdin/stdout : aucun serveur ni jeton n’est nécessaire entre l’interface et l’agent.

Les réglages livrés pour mblab sont `low` pour les deux modèles, avec les appels XML natifs et descriptions Markdown K2. Le catalogue IFM historique (`HORIZON_MODEL_CATALOG=models/catalog-ifm.json`) conserve `low` pour le nettoyeur et `medium` pour l’agent. Les champs `cleaner_reasoning_effort` et `agent_reasoning_effort` du catalogue définissent ces valeurs ; les variables explicites `HORIZON_CLEANER_REASONING_EFFORT` et `HORIZON_AGENT_REASONING_EFFORT` sont prioritaires. Le réglage « Garde-fou de correction », activé par défaut, peut être désactivé dans les réglages natifs. Ce choix est conservé et transmis à chaque demande audio ou texte ; il s’applique dès la prochaine demande. Désactivé, il laisse passer les corrections même si elles changent des mots ou le sens, mais les contrôles de réponse vide ou incomplète restent actifs. Le garde-fou lexical refuse certaines corrections incertaines avant toute action ; il ne remplace ni une validation du sens ni la vérification des sites choisis par l’agent.

Les événements par tour sont documentés dans `INTERFACES.md`. Les modèles ne partagent ni tokenizer ni cache. Le brut est affiché mais seul le texte nettoyé entre dans la conversation de l’agent. Les événements d’un tour annulé sont ignorés.

## Préparation et construction

Prérequis : macOS 26 sur Apple Silicon, Python 3.12 pour l’application, Python 3.11 pour F5, Node.js et outils de compilation Swift. Cette installation est préparée pour le Mac M4 Max 48 Go de la démo.

```sh
python3.12 scripts/setup.py
.venv/bin/python scripts/build_app.py --direct
```

La compilation native utilise `swiftc` avec le SDK Apple compatible avec le compilateur disponible ; aucun framework n’est téléchargé.

La commande de construction assemble le bundle avec ses deux lanceurs, `launch-services` (ASR et passerelle MLX) et `launch-agent` (Pi). L’application est signée localement pour les essais sur ce Mac ; ce n’est pas un paquet distribué via le Mac App Store.

Les dépendances Python et npm sont verrouillées dans `requirements.lock` et `package-lock.json`. Les références et conversions des checkpoints IFM sont enregistrées dans `.runtime/models/manifest.json`.

### Checkpoints privés mblab : 1B et 4B

Les modèles 1B et 4B du catalogue sont les conversions locales des exports suivants, copiés depuis `mblab:/datadisk4/florian-valade/` sans modifier les originaux :

| Rôle | Export MBZUAI-Paris | Révision | Conversion locale |
| --- | --- | --- | --- |
| Nettoyage | `A13x1-1Bcv5c189-sft-reasoningv9.5.2k2horCT-ckpt10711-FTPO_es035` | `32bfb4a00c1e56a9956fd279b2743e1336ea7fe6` | BF16 |
| Agent | `A13cs_A13x1-4Bc10248_sft_reasoningv9.5.1k2horCT_FTPO_es035` | `20e95a2e3f4452c6d26374d48cbc9902259d49bf` | 8 bits, groupes de 64 |

Les deux exports stockent physiquement des tenseurs FP32, y compris le « 4B » dont la configuration annonce BF16. La normalisation du 4B comporte deux groupes et ses décalages de poids sont déjà intégrés dans l’export : la conversion ne les ajoute pas une seconde fois. Les tokenizers, templates et fins de génération propres à chaque checkpoint sont conservés. Leur préfixe de raisonnement `low` est déjà fermé, contrairement aux anciens checkpoints IFM ; le parseur respecte ce préfixe réel.

Après copie des sources dans `.runtime/models/sources/mblab-1B-32bfb4a00c1e` et `mblab-4B-20e95a2e3f44`, avec leurs manifests de provenance et les SHA-256 des shards :

```sh
.venv/bin/python scripts/prepare_mblab_models.py --verify-only
.venv/bin/python scripts/prepare_mblab_models.py
```

La préparation vérifie l’intégrité, les formes des tenseurs et les révisions avant conversion. Elle écrit dans les dossiers référencés par `models/catalog.json`, que l’app détecte alors comme installés. Les sources Python téléchargées ne sont pas exécutées pour convertir ou servir les modèles.

La suite `scripts/evaluate_mblab.py` emploie 16 cas fixés avant la première inférence : trois régressions connues et treize nouvelles entrées. Elle distingue la justesse du texte de son acceptation par le garde-fou. Ses tests de proposition d’outil ne remplacent pas un essai du navigateur réel ; les résultats et limites mesurés sont consignés dans le bilan de validation livré.

## Lancement et fonctionnement

Ouvrir `JAIS Voice.app` dans le dossier de livraison. Son superviseur démarre JAIS-ASR F5 et la passerelle MLX ; l’app lance ensuite l’agent Pi, relié à elle par un tube. Les services écoutent uniquement sur la boucle locale.

| Service | Adresse locale |
| --- | --- |
| MLX (Chat Completions, `/v1/clean`) | `127.0.0.1:8788` |
| JAIS-ASR F5 | `127.0.0.1:8790` |
| Agent Pi | stdin/stdout de l’app, sans port |

Une petite fenêtre de réglages s’ouvre à chaque lancement : raccourci global, langue ASR (automatique, arabe, français, anglais, espagnol, italien), bouton d’autorisation microphone et état des modèles. Les choix sont persistants, et la langue est envoyée pour chaque demande sans recharger les modèles. Fermer cette fenêtre laisse l’assistant actif. Le menu « JAIS → Réglages… », ou la réouverture de l’application déjà lancée, permet de la retrouver même si macOS masque l’icône faute de place.

Le menu et les réglages affichent le chargement puis « Prêt ». Le microphone n’est utilisé que pendant l’enregistrement ; l’autorisation se demande explicitement depuis les réglages. Une prise ou un fichier importé dure au maximum 30 secondes, limite de l’ASR. Le panneau disparaît dix secondes après la fin, sauf pendant le survol ou une interaction ; le dernier résultat reste accessible dans le menu.

Chaque modèle possède son propre indicateur de chargement réel dans la fenêtre et dans le menu ouvert : attente, chargement, chargé ou erreur. L’état chargé suit le warmup MLX ; aucun pourcentage synthétique n’est affiché. En cas de perte de connexion, l’état devient indisponible plutôt que de conserver une fausse disponibilité.

Quitter l’application arrête les services qu’elle a lancés. Les ports déjà occupés sont signalés ; le lanceur ne termine pas les processus d’autres applications. Les appels navigateur d’un tour annulé ne sont pas poursuivis ; une action déjà réalisée n’est pas annulée rétroactivement.

## Actions macOS et shell

L’agent possède `shell_execute(command, reason)` en plus du navigateur, avec des outils spécialisés pour créer une note et gérer les radios. Le réglage **Actions sur le Mac** décide de leur exécution : *Ne jamais autoriser* les refuse toutes ; *Demander à chaque fois* (par défaut) affiche chaque commande dans la bulle de JAIS Voice avec les boutons **Refuser** et **Autoriser** ; sans réponse après 2 minutes ou à l’arrêt de la demande, elle est refusée ; *Toujours autoriser* les exécute sans confirmation, avec un avertissement dans les réglages. La lecture de l’état Wi-Fi/Bluetooth et la navigation web ne demandent pas de confirmation. L’extension Pi les lance dans `/bin/zsh -f`, dans un nouveau groupe de processus, depuis `~/Library/Caches/local.horizon.voice/Shell`. Les détails et les résultats restent visibles dans la bulle. Les commandes peuvent notamment utiliser `/usr/bin/osascript` et `/usr/bin/open`. Les pages web et sorties d’outils ne constituent jamais des instructions autorisées.

Ce mécanisme n’est **pas un bac à sable** : le modèle peut lancer des commandes avec les droits du compte courant et modifier des fichiers. Un environnement minimal évite d’hériter des secrets du processus ; cela ne bloque pas la lecture des fichiers accessibles au compte. Exécution : 30 s maximum ; sortie limitée ; arrêt/nouveau tour termine le groupe de processus possédé. Une modification déjà faite n’est pas annulée.

macOS demande séparément l’autorisation Automatisation pour Notes, et éventuellement Bluetooth. Aucun contournement ni réinitialisation des autorisations système. Notes peut synchroniser via le compte configuré, même si les deux modèles sont locaux.

Le « mode avion » choisi pour cette démo signifie Wi-Fi **et** Bluetooth désactivés, pas un interrupteur natif macOS. Il suit le même réglage que les autres actions. Le Bluetooth peut être contrôlé par blueutil, préparé uniquement dans le projet : `.venv/bin/python scripts/prepare_macos_tools.py`. Sa source officielle 2.14.0 est épinglée par SHA256 ; aucune radio n’est modifiée à la préparation. Cette dépendance utilise des API privées et ses résultats doivent être vérifiés. Couper le réseau empêche la navigation web (l’ASR et les modèles restent locaux), et couper le Bluetooth peut déconnecter clavier/souris. Prévoir un rétablissement manuel via les réglages système.

Le bundle utilise le dossier de travail voisin pour les modèles et les services. Conserver l’ensemble du dossier de la démo. Les journaux sont sous `.runtime/logs/`.

## Essais

```sh
PYTHONPATH=.:backend .venv/bin/python -m pytest backend/tests tests -q -o asyncio_mode=auto
node --test agent/
.venv/bin/python scripts/browser_smoke.py
"outputs/JAIS Voice.app/Contents/MacOS/JAISVoice" --smoke-file /chemin/vers/extrait.wav
```

Les tests unitaires emploient explicitement des substituts pour les erreurs réseau et les interruptions ; les tests Node exercent le vrai shell de l’extension (délai, annulation, groupe de processus). `browser_smoke` utilise le vrai navigateur ; `--smoke-file` et `--smoke-text` exécutent la véritable chaîne ASR → nettoyage → Pi depuis l’app.

Exemples à dicter :

- `أرسل التقرير غداً... لا، بعد غد، إلى أحمد... أقصد سارة` — vérifie les corrections de date et de destinataire.
- `لا ترسل التقرير غداً` — la négation doit rester.
- `ابحث عن متحف اللوفر في أبوظبي، لا أقصد في باريس، وافتح الموقع الرسمي` — recherche et navigation après correction de destination.

Le fichier de test fourni dans Téléchargements est un extrait WAV de 6,3 secondes ; il valide l’ASR et le transport, mais ne contient pas d’autocorrection évidente. Aucun résultat simulé n’est utilisé dans le fonctionnement normal. Le mode natif `--preview` est un aperçu visuel explicitement identifié.

## Composants

Le catalogue historique utilise `IFM/K2-Horizon-0.9B` et `IFM/K2-Horizon-7B` ; le catalogue par défaut utilise les deux exports privés `MBZUAI-Paris` identifiés ci-dessus. Pi, les bibliothèques MLX et Playwright MCP conservent leurs licences respectives. Les dépôts et révisions exacts sont conservés dans `models/catalog.json` et les scripts de préparation. L’archive des sources de l’application ne contient pas les poids privés.

## Langue de l’interface

Dans les réglages, « Langue de l’interface » permet de choisir Français ou English. Le changement est immédiat et conservé après réouverture. Ce choix est indépendant de la langue de transcription et ne traduit pas les échanges avec l’assistant. Les traductions se trouvent dans `macos/Resources/{fr,en}.lproj/Localizable.strings`. Les autorisations système suivent la langue macOS.

## ASR F5 local

Dépôt : https://huggingface.co/MBZUAI-Paris/JAIS-ASR-MLX ; révision installée : `de134588d64e222f5251b706a64b0281a0eeb091`.
Les poids et le runtime officiels sont conservés ensemble dans `.runtime/models/JAIS-ASR-MLX`, avec leur environnement `.venv` Python 3.11 et leurs licences. Les hashes se vérifient avec :

```sh
.venv/bin/python .runtime/models/JAIS-ASR-MLX/scripts/verify.py --profile F5 --sha256
```

Le service `backend/horizon_voice/local_asr.py` charge une seule instance F5 et sérialise l’inférence sur un thread Metal. Son journal est `.runtime/logs/asr.log`. Le superviseur ne lance ce service que si le modèle est installé, le démarre après un téléchargement et l’arrête après une suppression. L’inférence force les modes hors ligne Hugging Face et Transformers ; aucun téléchargement n’a lieu pendant une dictée.

Le mode automatique appelle le runtime publié sans modification. Pour une langue explicite, l’adaptateur reprend sa boucle de génération avec le `LanguagePin` fourni par le modèle, conservant son mécanisme de refus. L’arabe couvre les dialectes supportés. Le modèle conserve la limite publiée de 256 tokens ; une réponse tronquée ou refusée ne passe jamais au nettoyage ni à l’agent. Les enregistrements restent limités à 30 secondes.

## Libérer la mémoire

Dans les réglages ou le menu JAIS, **Décharger les modèles** arrête les services locaux possédés par l’application, y compris F5 et le LLM chargé. L’application reste ouverte et les fichiers de modèles restent sur disque. La commande est désactivée pendant une demande ou un déchargement. Le rechargement n’est proposé qu’après la fin du superviseur, qui attend et récupère ses processus enfants. **Recharger les modèles** redémarre les services et une nouvelle session vocale ; la conversation est réinitialisée.

Le sélecteur commun utilise la même instance de poids et de tokenizer pour le nettoyage et l’agent. Une seule taille est résidente : en charger une autre libère la précédente. Les prompts et caches de génération restent propres à chaque demande. **Supprimer** un modèle (icône corbeille, avec confirmation) le décharge puis efface son dossier.
