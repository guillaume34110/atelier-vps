# usevps

Exécution distante des tâches de développement sur le VPS ARM Hetzner, gérée par Coolify.

Le disque système d'origine est de 40 Go. Le volume ajouté de 30 Go est distinct,
monté sur `/mnt/HC_Volume_107251752`, UUID `527e635e-42d5-46e9-861d-643af1626668`.
Les projets existants conservent leurs données et leur fonctionnement.

Les tâches utilisent `/workspace` dans un conteneur non privilégié. Ce chemin est
lié au répertoire `usevps` du nouveau volume. Un quota ext4 de **7 Gio** borne
l'ensemble des données de travail, caches et résultats. La réserve est de **20 Gio**.
Une tâche à la fois, pic disque déclaré de **5 Gio maximum**, journaux de **20 Mio
maximum**, durée de **60 minutes maximum**. Le conteneur peut utiliser jusqu'à **5 Gio de RAM**,
256 processus et une priorité CPU basse. Le travail s'arrête si la RAM disponible
sur l'hôte tombe sous **2 Gio**. Une tâche arrêtée est signalée en échec et doit
être relancée ; elle ne reprend pas automatiquement au milieu d'un calcul.
En cas d'OOM, ses processus ont une priorité de destruction plus élevée que les
services ordinaires. Les ressources disponibles déterminent la mémoire réellement
utilisable : le plafond de 5 Gio ne constitue pas une réservation de mémoire.

## Accès et utilisation

La clé SSH dédiée reste sur le poste client. Le compte `usevps` ne peut appeler
que `status`, `run`, `fetch` et `release`, à travers une passerelle contrôlée.
Il n'a ni shell interactif, ni transfert de ports, ni accès libre au socket Docker.
La passerelle vérifie le volume, les limites et les montages du seul conteneur désigné.
Le service HTTP du conteneur fournit uniquement une sonde de santé.

```sh
rtk proxy python3 vps.py status
rtk proxy python3 /Users/guillaumegaillard/.local/bin/workspace-budget.py run /chemin/projet -- rtk proxy python3 /Users/guillaumegaillard/Documents/ChatGPT/usevps/vps.py run --source /chemin/projet --disk-gib 1 --timeout 900 -- npm test
```

Les sources transmises sont limitées à 64 Mio. Dépendances, sorties, fichiers
d'environnement et clés sont exclus du transfert. Préparer les dépendances dans
la tâche distante, en respectant le pic déclaré. Les outils disponibles sont Python,
Node.js 22, npm et Git, sur Linux ARM64. Le code dépendant de macOS, Metal/MLX ou d'un
GPU doit utiliser une machine compatible. Les conteneurs ne disposent pas du socket Docker.

Une tâche retourne son identifiant, son code de sortie et son statut réel.
Pour rapatrier un résultat, sous le même encadrement `workspace-budget.py run` :

```sh
rtk proxy python3 /Users/guillaumegaillard/Documents/ChatGPT/usevps/vps.py fetch IDENTIFIANT work/resultat.json stdout.log receipt.json
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

Le buildpack Docker Compose doit conserver les limites et le bind mount du fichier
`docker-compose.yaml`. Aucun déplacement du répertoire Docker global du serveur.
Les petites couches de l'image restent sur le disque système Docker ; les fichiers
de travail, dépendances installées par les tâches et caches vont sur le volume ajouté.
