# Garmin Coach MCP

Serveur MCP **en lecture seule** sur l'API personnelle Garmin Connect, pour qu'un
coach Claude puisse lire vos activités (tours, allure, FC, cadence, point par point)
et vos données de récupération (sommeil, VFC, FC de repos, VO2max).

## Configuration

| Option | Rôle |
| --- | --- |
| `athletes` | Liste des athlètes suivis (voir ci-dessous). Le premier est l'athlète par défaut. |
| `garmin_api_key` | Ancienne option (v0.4) : clé du premier athlète s'il n'en a pas dans `athletes`. |
| `access_token` | Jeton secret d'au moins 32 caractères qui forme l'URL. Laissez vide au premier démarrage : un jeton est généré et affiché **une fois** dans le journal ; recopiez-le ici. |
| `ssl`, `certfile`, `keyfile` | Certificat Let's Encrypt présent dans `/ssl` (add-on Duck DNS / Let's Encrypt). |
| `log_level` | Niveau de journalisation. |

### Athlètes

```yaml
athletes:
  - id: lucas
    nom: Lucas
    garmin_api_key: "<clé personnelle Garmin de Lucas>"
    utilisateurs_ha: lucas
  - id: anais
    nom: Anaïs
    garmin_api_key: "<clé personnelle Garmin d'Anaïs>"
    utilisateurs_ha: chaton
```

- `garmin_api_key` : clé API personnelle créée sur **le compte Garmin Connect de l'athlète**
  (Paramètres > Personal API Key). Ne la collez jamais ailleurs.
- `utilisateurs_ha` : identifiant(s) ou nom(s) affiché(s) Home Assistant (séparés par des virgules) qui
  ouvrent le panneau sur cet athlète. Le nom et l'id de l'athlète sont aussi reconnus.

## Accès depuis l'extérieur

Les connecteurs personnalisés de Claude n'atteignent pas un port non standard comme 8766 :
le serveur doit être exposé sur le port **443**. Si le 443 sert déjà à Home Assistant,
passer par l'add-on officiel **NGINX Home Assistant SSL proxy** :

1. Options du proxy : `domain` = votre domaine, `use_ssl_backend: true` si Home Assistant fait
   son propre HTTPS, `customize.active: true`.
2. Déposer dans le dossier `share` un fichier `nginx_proxy_default_garmin.conf` qui envoie
   `^/[A-Za-z0-9_-]{32,}/mcp` vers `https://<hostname-de-l-add-on>.local.hass.io:8766`
   (voir l'exemple dans le dépôt : `examples/nginx_proxy_default_garmin.conf`).
3. Home Assistant : activer X-Forwarded-For et déclarer `172.30.33.0/24` comme proxy de confiance
   (Paramètres > Système > Réseau).
4. Box/routeur : port externe 443 → port 443 de Home Assistant. Le port 8766 n'a pas besoin
   d'être ouvert vers l'extérieur.

URL du connecteur dans Claude (authentification « No sign-in ») :

`https://<votre-domaine>.duckdns.org/<access_token>/mcp`

Toute requête dont l'URL ne commence pas par le jeton reçoit une erreur 404 et est journalisée
avec l'IP du client (l'IP réelle est lue dans X-Forwarded-For quand la requête vient du proxy).
Le jeton n'est jamais écrit dans le journal, sauf à sa génération.

## Panneau Coach

L'add-on ajoute un panneau **Coach** dans la barre latérale de Home Assistant (aussi dans l'appli
mobile). Il n'est accessible qu'à travers Home Assistant (Ingress) : aucun port à ouvrir.

- **Semaine** (accueil) : résumé publié par le coach, volume par sport, forme jour par jour
  (VFC, sommeil, FC de repos, Body Battery), séances prévues et activités réalisées.
- **Séances** : séances à venir et passées.
- **Analyses** : plan d'entraînement et analyses.

Les données Garmin sont mises en cache 5 minutes pour la semaine en cours.

## Données stockées

Un dossier par athlète, `share/garmin-coach/athletes/<id>/` (visible via Samba) :

- `fit/` : fichiers `.fit` des activités consultées (téléchargés une seule fois).
- `coach/` : fiches du panneau (un dossier par type, JSON + copie `.md`), `suivi.json`, `montre.json`.
- `/data/athletes/<id>/garmin_connect` : jetons Garmin Connect de l'athlète (privés).

Les données d'une v0.4 (`share/garmin-coach/coach` et `fit`) sont déplacées automatiquement vers le premier athlète.
- `/data/access_token` : jeton généré automatiquement (si l'option est vide).

## Renouvellement de la clé Garmin

La clé Garmin expire (30 jours à 1 an selon votre choix). Garmin envoie un e-mail une
semaine avant : créez une nouvelle clé, collez-la dans `garmin_api_key` et redémarrez l'add-on.

## Outils exposés

Tous les outils acceptent `athlete` (id ; défaut : le premier athlète). `list_athletes` liste les athlètes.

Lecture : `list_activities`, `get_activity_summary`, `get_activity_laps`, `get_activity_streams`,
`get_wellness`, `get_fitness_metrics`, `list_activity_types`, `garmin_get`.

Fiches du panneau : `publish_fiche`, `list_fiches`, `get_fiche`, `delete_fiche`.

## Suivi des séances (v0.3)

Dans l'onglet **Semaine**, les séances prévues par le coach sont rangées jour par jour.

- **Faite** : l'add-on vérifie Garmin toutes les 30 minutes et associe chaque activité à la séance prévue
  du même sport, le même jour, ou un autre jour de la semaine s'il s'agit du même type de séance.
- **Mon ressenti** : effort perçu, douleurs et note, transmis au coach.
- **Déplacer** : choisir un autre jour de la semaine ; le panneau signale les enchaînements à éviter.
- **Faite sans montre** : pour une séance non enregistrée sur la montre.

Tout ce qui se passe dans le panneau rejoint une file d'événements que le coach traite lors de son point
du soir (outil `list_evenements`). L'état est stocké dans `share/garmin-coach/athletes/<id>/coach/suivi.json`.

## Binôme (v0.5)

- Le panneau s'ouvre sur l'athlète du compte Home Assistant connecté ; les boutons en haut à droite
  permettent de voir l'autre athlète (ressentis et déplacements s'appliquent à l'athlète affiché).
- Une séance en binôme porte le badge « 👥 avec … ». La déplacer depuis le panneau la déplace aussi chez
  le partenaire (si elle n'est pas déjà faite), et le coach en est informé dans la file de chacun.
- Onglet **Montre** : chaque athlète connecte son propre compte Garmin.

## Envoi des séances vers la montre (v0.4)

1. Onglet **Montre** : connecte-toi avec ton compte Garmin (et le code de double authentification si
   demandé). Seul le jeton de connexion est enregistré (`/data/garmin_connect`), jamais le mot de passe.
2. Les séances que le coach publie avec une structure arrivent dans ton calendrier Garmin, puis sur la
   montre quand elle se synchronise avec l'appli Garmin Connect.
3. Changement de programme : « Déplacer » ou « Je la fais maintenant » déplace aussi l'entraînement
   dans le calendrier Garmin. Les entraînements de la semaine restent disponibles dans
   Entraînement › Mes entraînements sur la montre.

La connexion utilise la bibliothèque non officielle `garminconnect` : si Garmin modifie sa connexion,
l'envoi s'arrête (l'onglet Montre l'indique) mais le reste de l'add-on continue de fonctionner.
