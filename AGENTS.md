# Règles du worker partagé

Lire `README.md` avant une modification. Ces règles sont également incluses en
lecture seule dans l'image, à `/app/AGENTS.md`.

- Utiliser le compte SSH restreint et le client fournis. Vérifier le statut réel
  avant une tâche ; conserver les réservations atomiques, au plus quatre tâches et
  l'attente bornée. Aucun shell hôte, socket Docker ou autre worker de débordement.
- Privilégier ce pool pour les travaux compatibles Linux ARM64 ; utiliser les
  outils MCP `atelier-vps` quand ils sont disponibles. Déclarer CPU et pic disque.
  Utiliser `--ram-gib auto`, ou omettre `ram_gib` dans MCP, pour réutiliser les pics
  mesurés avec marge. Déclarer un pic RAM explicite connu pour un travail nouveau
  ou plus lourd ; ne pas réduire une réservation pour contourner un refus.
  `busy` peut être vrai en même temps que `admit` : il reste
  alors une part disponible. Respecter les réservations et les refus réels.
- Préserver les projets existants. Conserver le volume ajouté distinct du disque
  système : tout le volume ajouté est dédié au service ; quota dur calculé sur
  toute la place restante après les logiciels, marge physique de 512 Mio,
  pic déclaré selon la place réellement disponible, journaux 20 Mio, durée
  3 600 secondes. Les sources, dépendances,
  téléchargements, caches et résultats comptent dans le budget.
- Conserver le compte 10001:10001, la racine en lecture seule, les capacités
  supprimées, le refus des nouveaux privilèges, la RAM maximale de 5 Gio sans
  swap supplémentaire, la réserve hôte de 1 Gio et la priorité basse. Ne pas
  modifier Docker, Coolify, le système ou les réglages des autres projets.
- Réutiliser les logiciels Ubuntu 24.04 de `.usevps-dev` montés en lecture seule,
  conformément à `ops/toolchain-lock.json`. Node 22/pnpm, Python 3.12/uv, Java 17/
  Gradle, ADB, Chromium/Playwright et les utilitaires partagés sont disponibles.
  Garder les dépendances des projets séparées, avec les caches partagés seulement
  entre travaux d'un même développeur. Ne pas
  installer un second SDK ou navigateur sans besoin concret et budget explicite.
- Réutiliser les caches de dépendances gérés par projet et empreinte des fichiers
  de verrouillage. Leur dernière utilisation prolonge la rétention de 30 jours ;
  seul le nettoyage au repos sous verrou exclusif peut retirer ces caches
  identifiés. Ne jamais y stocker sources, secrets ou résultats irremplaçables.
  Les outils de base, caches anciens non suivis et données des calculs restent
  préservés. La marge RAM automatique s'applique une fois, sans multiplicateur ×2.
- Pour le navigateur, utiliser `CHROMIUM_PATH`, un profil temporaire et les
  arguments des exemples fournis. Les binaires macOS et les outils Android Linux
  x86 ne fonctionnent pas sur ce VPS ARM64 : conserver émulateurs/NDK concernés
  sur le Mac. Ne déclarer aucune recette Android appareil sans appareil réel.
- Avant une écriture distante de plus de 1 Gio, lancer le garde livré dans l'image
  à `/app/storage-guard.py` via RTK, dans le calcul
  dont le pic est déclaré. Il contrôle quota, réserve et budget restant de cette
  tâche. Les gardes locaux du Mac et leurs réserves restent inchangés.
- Les hooks de démarrage et de santé doivent vérifier l'identité, le volume et
  la sonde physique en lecture seule. La sonde de santé vérifie la configuration ;
  elle doit rester saine en cas de manque de place ou de RAM pour permettre la
  récupération et la libération des résultats. Le statut d'admission détermine
  si un nouveau calcul peut démarrer.
- Garder les contrôles avant, pendant et après le calcul, même si le processus
  se termine très vite. Un dépassement, redémarrage, timeout, OOM ou reçu
  incomplet ne constitue jamais une réussite. Préserver les reçus d'échec et
  nettoyer seulement les descendants du calcul concerné.
- Récupérer les résultats avec vérification SHA256 et préserver les données utiles
  avant une libération explicite de cette tâche terminée. Les autres calculs
  peuvent continuer. Aucun nettoyage
  automatique des résultats, données personnelles ou caches encore utiles.
- Fournir un libellé public avec `run --agent "nom court de la tâche"` pour le
  widget Mac. Le statut expose seulement des compteurs et huit activités publiques,
  sans commandes ni secrets. Garder cette télémétrie facultative indépendante des
  contrôles d'admission ; aucun port ni service de surveillance supplémentaire.
  Le suivi et la mini barre utilisent seulement `status`, un délai court et la
  connexion SSH existante. L'onglet Gestion n'apparaît qu'après authentification
  via l'alias administrateur séparé et utilise get/preview/apply avec révision.
  Vérifier le build macOS et son rendu réel à plusieurs tailles, avec les budgets locaux,
  en plus de toute la suite Linux avant une poussée.
- Les fiches d'échec restent privées : récupération via `fetch` et manifeste
  SHA256, huit fiches dérivées bornées, extrait de journal de 4 Kio avec masquage
  des formats de secrets connus. Ne publier ni journal ni motif brut dans le
  statut et ne pas effacer les reçus/résultats originaux lors de leur rotation.
  Garder le fond PNG partagé des cadrans à ratio fixe ; seule l'aiguille varie.
- Le mode équipe utilise des workers, accès SSH, espaces et quotas ext4 distincts.
  L'identité est fixée par la passerelle ; une étiquette d'agent n'autorise jamais
  l'accès d'un autre développeur. Les outils `admin_settings_*` nécessitent une
  identité administrateur séparée et ne servent pas aux calculs courants. Préserver
  les plafonds cumulés, les réseaux privés et les transactions de réglages en
  attente ; reprendre leur candidat exact sans effacer le marqueur de sécurité.
- Préserver le filtre seccomp supplémentaire du worker : il interdit de modifier
  les identifiants de quota et les attributs des inodes, sans remplacer le filtre
  Docker. Les tâches 32 bits ne sont pas prises en charge. Ne pas contourner ce
  garde avec un autre point d'entrée ou un accès Docker administrateur.
- Ne jamais lire ni transférer de fichier `CLAUDE.md`. Ne jamais versionner ou
  afficher une clé privée, un jeton, un fichier d'environnement ou autre secret.
- Exécuter toute la suite CI dans `scripts/checks.py`, incluant les vrais services
  et scénarios d'intégration, avant de pousser une branche. Installer le hook local
  avec `rtk git config --local core.hooksPath .githooks` ; il bloque les poussées
  directes vers main/master et le préfixe de branche interdit. Il ne remplace pas
  les contrôles CI ni les protections GitHub.
- Passer par une PR, vérifier tous les contrôles obligatoires sur sa dernière
  révision et la mise à jour de la branche avant fusion. Déployer seulement une
  révision dont la CI est verte, épinglée dans Coolify ; ne pas activer le
  déploiement automatique ni contourner un contrôle. Ne pas ajouter d'attribution
  d'assistant, de générateur ou de co-auteur aux artefacts ; utiliser l'identité
  Git configurée.
- Avant un déploiement administrateur, prendre le verrou via
  `ops/deploy_lock.py --hold` et le garder au premier plan jusqu'à la validation
  du nouveau service, puis envoyer `release`. Un refus du verrou bloque le
  déploiement ; ne jamais interrompre un calcul actif pour installer une mise à jour.
- Le registre privé `connections.json` est partagé par CLI, MCP et widget. Une
  équipe correspond à un serveur ; Global additionne les pools authentifiés
  accessibles, sans doublons physiques ni mesures de plus de 60 secondes.
  Une tâche complète tient sur un seul serveur compatible : consulter son plan
  RAM et son admission, garder ses réservations, ne pas relancer arbitrairement
  une commande exécutée. Conserver les références `connexion:identifiant` pour
  récupérer et libérer les résultats sur leur serveur d'origine.
- Les invitations privées expirent après 30 minutes et inscrivent un appareil.
  La clé privée reste sur cet appareil ; seule sa clé publique est transmise.
  Ne jamais lire ou publier le lien dans un chat, journal ou dépôt. Les comptes
  temporaires n'ont ni shell, ni transfert de ports, ni espace de calcul. Garder
  les clés existantes ; les identités administrateur restent distinctes.
- La mise à jour des helpers communs exige le verrou canonique du propriétaire,
  le verrou de réglages et tous les autres workers au repos sous leurs verrous.
  L'installation SSH compare les règles des accès existants avant rechargement.
  La recette complète inclut une vraie authentification SSH isolée : mauvais
  hôte refusé, ancien accès conservé, réutilisation du lien et shell refusés.
- Le bureau distant optionnel utilise le même pool et la même limite de 3 600 s.
  Un seul programme graphique par membre, avec les calculs ordinaires conservés.
  Vérifier le vrai lancement des programmes et le transport RFB authentifié avant
  de déclarer cette fonction validée. La connexion aux comptes reste une action
  humaine dans les programmes officiels ; aucune clé API d'inférence n'est créée.
  Les profils et projets sont persistants hors des caches gérés et exclus des
  sources transférées. Les tickets VNC et codes de connexion ne doivent jamais
  entrer dans le suivi, les journaux, le dépôt ou les réponses MCP. Aucun port
  VNC public, hyperviseur, privilège ou worker supplémentaire de débordement.
  Conserver les neuf montages hôte explicites et le seul `/tmp` temporaire de
  8 Mio en RAM, sans exécution, avec UID/GID 10001. La limite de 512 processus
  et threads laisse de la place au contrôle pendant le démarrage des interfaces ;
  la recette vérifie l'absence de dépassement et une marge de 32 threads.
