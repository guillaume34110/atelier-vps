# Règles du worker partagé

Lire `README.md` avant une modification. Ces règles sont également incluses en
lecture seule dans l'image, à `/app/AGENTS.md`.

- Utiliser le compte SSH restreint et le client fournis. Vérifier le statut réel
  avant une tâche ; conserver le verrou partagé, la limite d'une tâche active et
  l'attente bornée. Aucun shell hôte, socket Docker ou autre worker de débordement.
- Préserver les projets existants. Conserver le volume ajouté distinct du disque
  système : quota dur global 7 Gio, réserve physique 20 Gio, pic déclaré 5 Gio au
  maximum, journaux 20 Mio, durée 3 600 secondes. Les sources, dépendances,
  téléchargements, caches et résultats comptent dans le budget.
- Conserver le compte 10001:10001, la racine en lecture seule, les capacités
  supprimées, le refus des nouveaux privilèges, la RAM maximale de 5 Gio sans
  swap supplémentaire, la réserve hôte de 2 Gio et la priorité basse. Ne pas
  modifier Docker, Coolify, le système ou les réglages des autres projets.
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
  avant une libération explicite, uniquement hors calcul. Aucun nettoyage
  automatique des résultats, données personnelles ou caches encore utiles.
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
