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
  --cpus 1 --ram-gib 1 --disk-gib 2 --timeout 900 -- npm run build
```

Les sources sont filtrées et envoyées sans archive locale supplémentaire.
Node/Python/Java, Chromium et les outils de compilation sont partagés, en lecture
seule. Les dépendances sont recréées pour Linux ARM64.

La capacité n’est pas découpée en parts figées inutilisables. Jusqu’à quatre
calculs cohabitent si leurs réservations tiennent réellement. Le volume dédié
reste disponible après les logiciels et une marge physique de 512 Mio. Les
résultats conservés occupent de la place : récupérez-les, vérifiez le manifeste
SHA256 et préservez-les avant leur libération explicite.

Le conteneur et le quota disque ont des plafonds stricts. Les gardes par calcul
surveillent les usages ; ils ne cloisonnent pas des personnes malveillantes.
Un refus demande d’attendre ou de choisir une autre machine compatible.

Le [widget Mac](widget.md) affiche deux VU-mètres à aiguilles, les agents actifs,
leurs réservations et l’espace restant. Graphite, ivoire et touche française,
avec de vraies mesures.

Un clone seul ne donne pas à une conversation ChatGPT distante accès au Mac.
L’installation automatique concerne les agents locaux compatibles MCP.
Le [guide d’intégration](integration.md) explique ces limites.

[Documentation complète](../README.md) · [Architecture](architecture.md) ·
[Sécurité](../SECURITY.md) · [Contribuer](../CONTRIBUTING.md)
