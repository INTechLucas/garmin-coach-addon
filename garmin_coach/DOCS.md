# Garmin Coach MCP

Serveur MCP **en lecture seule** sur l'API personnelle Garmin Connect, pour qu'un
coach Claude puisse lire vos activités (tours, allure, FC, cadence, point par point)
et vos données de récupération (sommeil, VFC, FC de repos, VO2max).

## Configuration

| Option | Rôle |
| --- | --- |
| `garmin_api_key` | Clé API personnelle créée sur Garmin Connect (Paramètres > Personal API Key). Ne la collez jamais ailleurs. |
| `access_token` | Jeton secret d'au moins 32 caractères qui forme l'URL. Laissez vide au premier démarrage : un jeton est généré et affiché **une fois** dans le journal ; recopiez-le ici. |
| `ssl`, `certfile`, `keyfile` | Certificat Let's Encrypt présent dans `/ssl` (add-on Duck DNS / Let's Encrypt). |
| `log_level` | Niveau de journalisation. |

## Accès depuis l'extérieur

1. Rediriger le port TCP **8766** de la box internet (et du routeur intermédiaire)
   vers l'adresse IP de Home Assistant, port 8766.
2. URL du connecteur à ajouter dans Claude (Paramètres > Connecteurs > Ajouter un connecteur personnalisé) :

   `https://<votre-domaine>.duckdns.org:8766/<access_token>/mcp`

Toute requête qui ne commence pas par le jeton reçoit une erreur 404 et est journalisée
(adresse IP). Le jeton n'est jamais écrit dans le journal, sauf à sa génération.

## Données stockées

- `/data/fit/` : fichiers `.fit` des activités consultées (téléchargés une seule fois).
- `/data/access_token` : jeton généré automatiquement (si l'option est vide).

## Renouvellement de la clé Garmin

La clé Garmin expire (30 jours à 1 an selon votre choix). Garmin envoie un e-mail une
semaine avant : créez une nouvelle clé, collez-la dans `garmin_api_key` et redémarrez l'add-on.

## Outils exposés

`list_activities`, `get_activity_summary`, `get_activity_laps`, `get_activity_streams`,
`get_wellness`, `get_fitness_metrics`, `list_activity_types`, `garmin_get`.
