# usevps

Exécution distante des tâches de développement sur le VPS ARM Hetzner, gérée par Coolify.

Le disque système d'origine est de 40 Go. Le volume ajouté de 30 Go est distinct,
monté sur `/mnt/HC_Volume_107251752`, UUID `527e635e-42d5-46e9-861d-643af1626668`.
Les projets existants conservent leurs données et leur fonctionnement.

Les tâches utilisent `/workspace` dans un conteneur non privilégié. Ce chemin est
lié au répertoire `usevps` du nouveau volume. Ce volume est dédié aux agents :
le quota ext4 utilise toute la capacité restante après les logiciels partagés,
avec seulement **512 Mio de marge technique**. La limite réelle est indiquée
par `status` ; elle est recalculée lors de la préparation du volume.
Un répertoire vide, inaccessible au compte de calcul et monté en lecture seule
sur `/volume-stat`, permet de mesurer l'espace physique séparément du quota ext4.
Une tâche à la fois, pic disque déclaré dans la limite de la place disponible, journaux de **20 Mio
maximum**, durée de **60 minutes maximum**. Le conteneur peut utiliser jusqu'à **5 Gio de RAM**,
256 processus et une priorité CPU basse. Le travail s'arrête si la RAM disponible
sur l'hôte tombe sous **2 Gio**. Une tâche arrêtée est signalée en échec et doit
être relancée ; elle ne reprend pas automatiquement au milieu d'un calcul.
En cas d'OOM, les processus de calcul ont une priorité de destruction plus élevée que les
services ordinaires. Les ressources disponibles déterminent la mémoire réellement
utilisable : le plafond de 5 Gio ne constitue pas une réservation de mémoire.

## Environnement de développement

Les logiciels résident sur le volume dédié dans `.usevps-dev`, monté en lecture
seule. L'image Docker contient seulement le lanceur et les règles : aucune grosse
couche de navigateur ou de SDK ne remplit le disque système. La préparation
occupe environ 2 Gio et son pic est borné à 4 Gio. Les versions et leur empreinte
sont disponibles dans `/opt/usevps-dev.json` et `ops/toolchain-lock.json`.

| Outils | Environnement |
| --- | --- |
| JavaScript | Node 22.20.0, npm, pnpm 10.32.1 |
| Python | Python 3.12, pip, venv, uv 0.9.7 |
| Navigateur | Chromium headless et Playwright 1.64.1 |
| JVM / Android | Java 17, Gradle 8.13, ADB et fastboot ARM64 |
| Compilation | GCC/G++, make, CMake, Ninja, pkg-config, en-têtes Python |
| Utilitaires | Git, SSH client, curl, rsync, jq, ripgrep, SQLite, ZIP/TAR/XZ, FFmpeg, RTK 0.43.0 |

Les dépendances propres aux projets restent dans leurs environnements de travail.
Ne pas copier les `node_modules`, `.venv` ou binaires macOS : les réinstaller pour
Linux ARM64. Les caches npm/pnpm, uv/pip et Gradle sont partagés et comptés dans
le quota. Java utilise le HOME et le répertoire temporaire du calcul, sans plafond
mémoire global ajouté aux moteurs.

Le navigateur utilise un profil temporaire par calcul, avec
`executablePath: process.env.CHROMIUM_PATH` et les arguments `--no-sandbox`,
`--disable-dev-shm-usage`. Ne pas télécharger un second navigateur par défaut.
Les recettes réelles sont fournies dans `examples/` : serveur HTTP + API + clic
et capture, venv + installation uv hors ligne + compilation native, et application
Java compilée et exécutée par Gradle.

Le NDK officiel Linux est distribué pour x86 ; les émulateurs et builds Android
qui exigent ces binaires restent sur le Mac et son SDK canonique. Le VPS prend en
charge les tâches web/JS, JVM et le client ADB natif ; il ne simule pas un test
sur appareil Android. Sources : [NDK](https://developer.android.com/ndk/downloads),
[configuration Linux](https://developer.android.com/studio/install).

## Accès et utilisation

La clé SSH dédiée reste sur le poste client. Le compte `usevps` ne peut appeler
que `status`, `run`, `fetch` et `release`, à travers une passerelle contrôlée.
Il n'a ni shell interactif, ni transfert de ports, ni accès libre au socket Docker.
La passerelle vérifie le volume, les limites et les montages du seul conteneur désigné.
Le service HTTP du conteneur fournit uniquement une sonde de santé.

Les règles de travail sont incluses dans l'image à `/app/AGENTS.md`. Les hooks de
démarrage et de santé refusent une identité, un volume ou une sonde physique mal
configurés. L'admission vérifie place, quota, RAM et disponibilité du worker ; le
contrôle après calcul vérifie encore le budget et enregistre les mesures dans le
reçu, même pour un processus très court. Un manque de ressources empêche un
nouveau calcul sans empêcher de récupérer les résultats terminés.

```sh
rtk proxy python3 vps.py status
rtk proxy python3 /Users/guillaumegaillard/.local/bin/workspace-budget.py --temp-gib 0.1 run /chemin/projet -- rtk proxy python3 /Users/guillaumegaillard/Documents/ChatGPT/usevps/vps.py run --source /chemin/projet --disk-gib 1 --timeout 900 -- npm test
```

Les sources transmises sont limitées à 64 Mio. Dépendances, sorties, fichiers
d'environnement et clés sont exclus du transfert. Préparer les dépendances dans
la tâche distante, en respectant le pic déclaré. Les outils disponibles sont Python,
Node.js 22, npm et Git, sur Linux ARM64. Le code dépendant de macOS, Metal/MLX ou d'un
GPU doit utiliser une machine compatible. Les conteneurs ne disposent pas du socket Docker.

Avant une écriture de plus de 1 Gio **dans une tâche distante**, utiliser le garde
livré dans le conteneur :

```sh
rtk proxy python3 /Users/guillaumegaillard/.local/bin/storage-guard.py --volume . --require-gib 2
```

Il vérifie simultanément la place physique, le quota et le pic restant déclaré
pour cette tâche. Sur le Mac, le script canonique conserve les réserves locales
de 50/20 Gio ; l'exception de 512 Mio concerne seulement le volume dédié du VPS.
Un tampon technique de 1 Mio permet de conserver le reçu d'échec si le quota
est atteint ; il est recréé après libération, sans supprimer de données de travail.

Une tâche retourne son identifiant, son code de sortie et son statut réel.
Pour rapatrier un résultat, sous le même encadrement `workspace-budget.py run` :

```sh
rtk proxy python3 /Users/guillaumegaillard/.local/bin/workspace-budget.py --temp-gib 0.6 run /chemin/projet -- rtk proxy python3 /Users/guillaumegaillard/Documents/ChatGPT/usevps/vps.py fetch IDENTIFIANT work/resultat.json stdout.log receipt.json
```

Le téléchargement, limité à 256 Mio par opération, vérifie un manifeste SHA256.
Il crée un répertoire neuf dans `TASK_WORKSPACE` et n'écrase aucun résultat existant.
Les sources et résultats distants restent présents jusqu'à confirmation :

```sh
rtk proxy python3 vps.py release IDENTIFIANT --confirmed
```

Récupérer et conserver les résultats utiles avant cette commande. Un manque de
place bloque les tâches suivantes ; aucune purge automatique de données utiles.
Les snapshots serveur Hetzner ne couvrent pas ce volume additionnel.

## Validation et déploiement

`scripts/checks.py` exécute toute la suite unitaire, construit l'image puis démarre
le vrai service pour tester transfert, calcul, retour vérifié, arrêt sur timeout et
plafond des journaux. Préfixer cette commande par `workspace-budget.py run` en local.
Un démon Docker existant peut être utilisé via `DOCKER_HOST=ssh://root@coolifyx.progsoft.eu`.

La CI GitHub `checks` exécute la même suite. Intégrer par pull request après CI verte
sur la dernière révision et branche à jour, puis déployer cette révision dans Coolify.
L'offre GitHub Free du compte ne permet pas les protections sur ce dépôt privé
(API de protection : HTTP 403) ; le passage par PR et les contrôles restent obligatoires.

Installer le hook local avec `rtk git config --local core.hooksPath .githooks`.
Il bloque les poussées directes vers main/master ainsi que le préfixe de branche
interdit. Son activation reste propre à ce dépôt ; la CI complète reste requise.

Le hook administrateur `ops/deploy_lock.py --hold` prend le même verrou que les
calculs et refuse immédiatement si une tâche est active. Maintenir ce processus
au premier plan pendant un déploiement Coolify ; les nouvelles tâches attendent
ou sont refusées sans démarrer. Envoyer `release` sur son entrée après la fin du
déploiement et le retour d'une santé valide. Une fermeture de la connexion libère
le verrou ; vérifier alors l'état du déploiement avant de reprendre. Le hook
installé est `/usr/local/lib/usevps/deploy_lock.py`, réservé à l'administration.

Le buildpack Docker Compose doit conserver les limites et les neuf bind mounts du fichier
`docker-compose.yaml`. Aucun déplacement du répertoire Docker global du serveur.
Les petites couches du lanceur restent sur le disque système Docker ; logiciels,
fichiers de travail, dépendances et caches vont sur le volume ajouté. Les sept
montages de logiciels sont tous en lecture seule ; seul `/workspace` est writable.

Pour préparer les outils, exécuter `ops/provision_env.py` comme administrateur
sur un répertoire neuf, après le contrôle de stockage du volume cible. Le script
vérifie les téléchargements, refuse un dossier existant non identifié, empêche le
démarrage de services pendant l'installation et conserve un manifeste. Une version
prête est réutilisée ; ne pas la modifier pendant qu'un conteneur l'utilise. Un
changement de versions nécessite une préparation isolée puis une bascule validée.
La CI Linux prépare son environnement temporaire dans `TASK_WORKSPACE`, exécute
les vraies recettes et conserve uniquement les petites preuves d'intégration.
