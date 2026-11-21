# Garmin Coach – dépôt d'add-ons Home Assistant

Add-on qui expose un serveur MCP (lecture seule) sur l'API personnelle Garmin Connect,
utilisé comme connecteur personnalisé par un coach Claude.

## Installation

Dans Home Assistant : Paramètres > Modules complémentaires > Boutique > ⋮ > Dépôts,
ajouter `https://github.com/INTechLucas/garmin-coach-addon`, puis installer **Garmin Coach MCP**.

Voir [garmin_coach/DOCS.md](garmin_coach/DOCS.md) pour la configuration.

Aucun secret n'est stocké dans ce dépôt : la clé Garmin et le jeton d'accès se saisissent
dans les options de l'add-on.

Endpoints et authentification d'après le client officiel
[garmin/connect-personal-api-python](https://github.com/garmin/connect-personal-api-python).
