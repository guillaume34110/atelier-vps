# Atelier VPS

**Vos agents. Un serveur. Chacun sa part.**

Atelier VPS déporte les builds, tests et traitements compatibles Linux ARM64
sur votre propre VPS. Plusieurs agents réservent leur part de CPU, RAM et disque.
Les cœurs inutilisés sont prêtés aux calculs actifs. Les autres projets gardent
leur réserve de RAM et la priorité sur le CPU.

Le client, MCP et le widget Mac passent par une clé SSH restreinte. Aucun accès
administrateur n’est fourni aux calculs. Les secrets restent dans votre
configuration personnelle, hors du dépôt.

1. Préparez votre VPS avec le [guide serveur](deployment.md).
2. Configurez l’alias SSH, puis lancez `python3 scripts/install.py --host dev-vps`.
3. Vérifiez `atelier-vps status`. Avec uv et un CLI Codex fonctionnel,
   l’installation enregistre MCP et les consignes de priorité VPS. Relancez
   l’application pour découvrir ces outils.
4. Déclarez les besoins du calcul :

```sh
atelier-vps run --source ./mon-projet --agent "Build · frontend" \
  --cpus 1 --ram-gib auto --disk-gib 2 --timeout 900 -- npm run build
```

Les sources sont filtrées et envoyées sans archive locale supplémentaire.
Node/Python/Java, Chromium et les outils de compilation sont partagés, en lecture
seule. Les dépendances sont recréées pour Linux ARM64.
Les outils de base restent installés, sans immobiliser leur RAM au repos. Les
caches de dépendances sont réutilisés par projet et version du verrouillage :
chaque usage renouvelle leur rétention de 30 jours. Un nettoyage horaire au repos,
sous verrou exclusif, retire seulement les caches gérés arrivés à expiration.
Les sources, résultats, outils et anciens caches non suivis restent préservés.

Le mode RAM `auto` apprend le plus grand pic observé pour cette commande et ses
dépendances, puis ajoute 25 % et 64 Mio de marge. La première passe réserve
jusqu’à 1 Gio. Cette marge ne s'applique qu'une fois : pas de réservation ×2.
Une valeur explicite reste préférable pour un nouveau travail
lourd dont le pic est connu. Un refus ne réduit jamais la réservation en silence.

Deux modes sont disponibles : opportuniste pour utiliser la capacité libre d’un
VPS partagé, ou alloué avec un conteneur, un accès SSH et des quotas privés par
développeur. Les logiciels communs restent en lecture seule. Les réglages MCP
administrateur utilisent une identité distincte, sans droits supplémentaires
pour les calculs. Voir le [guide des pools et du mode équipe](team.md).

La capacité n’est pas découpée en parts figées inutilisables. Jusqu’à quatre
calculs cohabitent si leurs réservations tiennent réellement. Le volume dédié
reste disponible après les logiciels et une marge physique de 512 Mio. Les
résultats conservés occupent de la place : récupérez-les, vérifiez le manifeste
SHA256 et préservez-les avant leur libération explicite.

Le conteneur et le quota disque ont des plafonds stricts. Les gardes par calcul
surveillent les usages ; ils ne cloisonnent pas des personnes malveillantes.
Un refus demande d’attendre ou de choisir une autre machine compatible.

Le [widget Mac](widget.md) affiche deux VU-mètres à aiguilles, les agents actifs,
leurs réservations et l’espace de l’utilisateur connecté. Français et anglais,
transferts mesurés, heures de début/fin et disposition automatique selon la taille.
sections repliables, fenêtre redimensionnable ; collecte arrêtée lorsqu’il est
masqué et espacée au repos. Graphite, ivoire et touche française.

Un clone seul ne donne pas à une conversation ChatGPT distante accès au Mac.
L’installation automatique concerne les agents locaux compatibles MCP.
Le [guide d’intégration](integration.md) explique ces limites.

[Documentation complète](../README.md) · [Architecture](architecture.md) ·
[Sécurité](../SECURITY.md) · [Contribuer](../CONTRIBUTING.md)
