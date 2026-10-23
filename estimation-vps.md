**Déporter des tâches sur le VPS Hetzner — étude du 23 octobre 2026**

Oui, réutiliser le VPS peut être intéressant pour les compilations Linux, les tests, les conversions et le traitement de fichiers. Je recommande un environnement de travail isolé avec des conteneurs, un accès SSH et une file commune à tous les agents. Une machine virtuelle supplémentaire apporte ici une consommation de RAM et de disque difficile à justifier.

Cette étude est un chiffrage préliminaire. Le VPS n'a pas encore été identifié ni interrogé : ses caractéristiques, ses services, son espace libre et sa facture actuelle restent à mesurer. Les 4 à 5 Go libres viennent de ton indication. Aucun déploiement, achat, nettoyage ou changement du serveur n'a été effectué.

**Ce que cela permet**

Un agent prépare les seuls fichiers nécessaires, les transfère par SSH/rsync, soumet une tâche et récupère le résultat après vérification du transfert. Les sources restent à leur emplacement canonique. Une seule tâche distante s'exécute à la fois au départ ; les autres attendent dans la même file, quel que soit l'agent qui les soumet.

Les calculs utilisent la RAM du VPS. La RAM du Mac et celle du VPS ne s'additionnent pas pour un même processus. Un processus déjà lancé ne se déplace pas automatiquement : on choisit son lieu d'exécution avant son démarrage, ou on le relance avec un point de reprise si le logiciel le permet. Le VPS n'augmente pas la capacité de raisonnement du modèle ni les limites de l'abonnement OpenAI.

Un VPS CPU convient aux outils Linux. Les tâches dépendantes de macOS, de MLX/Metal ou d'un GPU doivent rester sur une machine compatible. Il faut aussi vérifier l'architecture du VPS, les dépendances et le volume de fichiers à transférer avant de choisir où lancer chaque tâche.

La documentation OpenAI décrit les connexions de projets par SSH. La sélection automatique entre deux machines et la file partagée proposées ici demandent un outil et des instructions communs ; leur présence dans tous les agents existants n'est pas automatique. [Connexions distantes OpenAI](https://learn.chatgpt.com/docs/remote-connections)

**Comment donner la priorité aux projets existants**

Le nouvel environnement utilise un compte dédié, son propre répertoire et des droits limités. Ses conteneurs ne reçoivent pas l'accès aux secrets, aux volumes ni au socket Docker des autres projets. Le stockage du nouveau moteur, ses images et ses fichiers temporaires sont placés sur le volume ajouté, sans déplacer les installations existantes.

La priorité CPU des tâches est basse : elles peuvent utiliser les processeurs libres et cèdent une part du CPU en cas de concurrence. Une priorité disque basse est envisageable selon les contrôleurs disponibles. Ces réglages dépendent de la hiérarchie des cgroups et doivent être vérifiés sur le serveur réel. [Contrôle des ressources systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html)

Pour la RAM, il faut une limite ferme et une réserve pour le système et les autres services. Un petit contrôleur refuse les nouvelles tâches si la réserve est insuffisante, puis arrête les tâches de calcul si la pression augmente. Le plafond peut être relevé quand les ressources redeviennent disponibles. Mettre un processus en pause ne libère pas sa RAM ; une tâche arrêtée doit pouvoir être relancée, éventuellement depuis un point de reprise.

Les limites souples de mémoire ne constituent pas une garantie de restitution immédiate. Un conteneur Docker lancé sans limites peut consommer les ressources de l'hôte : les limites sont donc une partie obligatoire de la configuration. [Limites de ressources Docker](https://docs.docker.com/engine/containers/resource_constraints/)

Cette approche réduit les interférences, mais le système, le noyau et les ressources restent partagés. Elle ne garantit pas l'absence d'impact lors d'un pic brutal. Si les projets exigent une isolation stricte ou s'il reste trop peu de RAM libre, il faudra un VPS séparé, à chiffrer après l'audit.

**Stockage et coût mensuel supplémentaire**

Le calculateur officiel Hetzner affiche **0,044 € HT par Go et par mois** pour les Cloud Volumes. Les montants TTC ci-dessous appliquent une TVA française de 20 %, si elle est applicable à ton compte. [Tarif officiel Block Storage](https://www.hetzner.com/cloud/block-storage/)

| Capacité ajoutée | Supplément mensuel HT | Supplément mensuel TTC à 20 % |
| --- | ---: | ---: |
| 20 Go | 0,88 € | 1,06 € |
| 40 Go | 1,76 € | 2,11 € |
| 50 Go | 2,20 € | 2,64 € |
| 100 Go | 4,40 € | 5,28 € |

Les volumes sont facturés à l'heure avec un plafond mensuel, selon la capacité allouée. Le tarif n'est pas limité aux fichiers effectivement présents. Les tailles vont de 10 Go à 10 To et peuvent augmenter par pas de 1 Go. [Facturation et tailles](https://docs.hetzner.com/cloud/volumes/overview/)

Ces montants supposent que le VPS est un **Hetzner Cloud Server** : les Cloud Volumes ne s'attachent pas directement aux serveurs dédiés. Le prix total de ton hébergement sera ta facture actuelle plus le volume choisi ; ta facture actuelle n'a pas été consultée. [Compatibilité des volumes](https://www.hetzner.com/cloud/block-storage/)

Un volume ajouté est un disque séparé. Il ne transforme pas automatiquement les 4 à 5 Go libres de la partition système en 24 à 25 Go. Il faut y installer le nouvel espace de travail et les données de son moteur de conteneurs. L'espace de la partition système reste à surveiller. Un volume peut être agrandi sans arrêt, puis son système de fichiers doit être étendu ; sa capacité ne peut pas être réduite. [Extension d'un volume](https://docs.hetzner.com/cloud/volumes/faq/)

Je privilégie **50 Go supplémentaires**, soit **2,20 € HT/mois**, pour garder une réserve et accueillir les images, les dépendances et une tâche temporaire. C'est 1,32 € HT/mois de plus que 20 Go. Un volume de 20 Go est possible pour de petites tâches, mais il faut vérifier qu'il respecte les réserves de stockage imposées et le pic réel de chaque opération ; il ne constitue pas un budget universel de compilation.

Les conteneurs, SSH et la file de tâches n'ajoutent pas de licence payante. Sur le VPS existant, cette solution ne nécessite pas d'appels OpenAI supplémentaires pour exécuter de simples commandes distantes. Les transferts restent soumis au trafic inclus dans le forfait, qui reste à vérifier. Ce chiffrage n'inclut ni serveur supplémentaire ni prestation de maintenance.

Les sauvegardes et snapshots du serveur Hetzner ne couvrent pas les volumes attachés. Les entrées doivent rester disponibles à leur emplacement canonique et les résultats utiles doivent être rapatriés avant retrait des fichiers temporaires. Une sauvegarde indépendante serait à chiffrer si le volume hébergeait des données uniques. [Périmètre des sauvegardes Hetzner](https://docs.hetzner.com/storage/general/which-storage-is-right-for-me/)

**Mise en place proposée et charge de travail**

| Étape | Résultat attendu | Estimation de travail |
| --- | --- | ---: |
| Audit en lecture seule | Plan Hetzner, CPU, RAM/swap, disques/inodes, services, conteneurs et accès SSH identifiés | 30 à 60 min |
| Environnement isolé | Compte dédié, volume monté, moteur de conteneurs et limites des seules nouvelles tâches | 1 à 2 h |
| Outil commun aux agents | Transfert ciblé, file unique, soumission, statut et récupération vérifiée | 1 à 2 h |
| Contrôle et validation | Admission selon les ressources, arrêt des tâches, essais sous concurrence et documentation | 1 à 3 h |

Prévoir **environ 4 à 8 heures** sur un Linux récent avec SSH et cgroups compatibles. C'est une estimation technique, pas une facture. Un environnement ancien ou une exigence de reprise fine des calculs peut augmenter cette charge. Aucun tarif horaire de prestation n'a été supposé.

Le démarrage doit rester borné : une tâche à la fois, espace temporaire de 5 Gio maximum par passe, journaux cumulés de 20 Mio maximum, contrôle préalable du stockage et réserve conforme à tes règles. Les sources, données utiles et historiques restent protégés. Le retrait de fichiers temporaires intervient après récupération des preuves et vérification des références.

La validation doit prouver le transfert aller-retour, le traitement réel d'un fichier, le comportement de la file avec plusieurs demandes, l'application des plafonds et l'arrêt des seuls travaux déportés sous pression. Les scénarios qui nécessitent une API ou un autre service doivent démarrer ces services et exécuter leurs assertions réelles. Rien de cela n'a encore été exécuté sur le VPS.

La prochaine information nécessaire est le nom ou l'adresse IP du VPS. Elle permettra de mesurer les ressources réelles, de vérifier le type de serveur et de confirmer le choix du volume avant tout achat ou déploiement.

**Chiffrage du gain pour le développement**

Un relevé local du 23 octobre 2026 indique un Mac Apple M2, 8 CPU logiques, 24 Gio de RAM, environ 3,34 Gio de swap utilisé et 10,51 Gio occupés par le compresseur mémoire. Ce relevé confirme l'utilisation de la compression et du swap ; il ne mesure pas le temps perdu et ne prouve pas, à lui seul, un ralentissement actuel. Le swap peut rester occupé après un pic. Aucune tâche en cours n'a été interrompue.

Le gain potentiel concerne d'abord la disponibilité du Mac pendant les calculs et la réduction de la concurrence pour sa RAM. Il faut déplacer des processus réellement coûteux et compatibles Linux pour en bénéficier. Les caractéristiques du VPS restent inconnues : aucun facteur d'accélération CPU ni volume de RAM libérable n'est établi. Un VPS partagé peut aussi exécuter une tâche plus lentement que le M2.

Le tableau suivant est un calcul de scénarios, pas un résultat mesuré. Il suppose 20 jours de développement et 160 heures disponibles par mois. Le temps récupéré doit être du temps auparavant réellement bloquant, après déduction des transferts, de l'attente dans la file et des relances. Un traitement déjà exécuté en arrière-plan pendant du travail utile ne constitue pas automatiquement du temps gagné.

| Temps net récupéré par jour | Temps récupéré par mois | Part d'un mois de 160 h | Stockage de 50 Go par heure récupérée |
| --- | ---: | ---: | ---: |
| 10 min | 3 h 20 | 2,1 % | 0,66 € HT |
| 30 min | 10 h | 6,25 % | 0,22 € HT |
| 60 min | 20 h | 12,5 % | 0,11 € HT |

Le coût par heure ci-dessus ne comprend que le supplément de stockage de 2,20 € HT/mois. Il exclut le travail initial de mise en place, la maintenance et toute autre ressource payante éventuelle. Le forfait VPS existant reste payé dans les deux scénarios.

Avec 4 à 8 heures de mise en place, le temps investi serait récupéré après 24 à 48 jours travaillés si le gain net est de 10 minutes par jour, 8 à 16 jours à 30 minutes par jour, ou 4 à 8 jours à une heure par jour. Ces délais supposent que le gain persiste et ne comprennent pas une maintenance supplémentaire.

Une valorisation illustrative à 50 €/h représenterait respectivement environ 167 €, 500 € et 1 000 € de capacité de travail mensuelle. Ce ne sont ni des économies de trésorerie garanties, ni un revenu supplémentaire, ni un tarif de prestation retenu pour ce projet.

Pour confirmer le gain, il faudra comparer quelques tâches représentatives sur les deux machines : durée de bout en bout, transferts compris ; temps pendant lequel le développement est effectivement bloqué ; mémoire locale sollicitée ; et comportement quand un projet existant démarre sur le VPS. Les générations dépendantes de MLX/Metal et le temps de réponse du modèle OpenAI ne bénéficient pas directement de cette installation.
