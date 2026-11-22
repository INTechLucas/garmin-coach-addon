# Changelog

## 0.3.0
- Panneau : planning de la semaine jour par jour. Chaque séance prévue indique son statut (faite, prévue,
  aujourd'hui, non faite), l'activité Garmin associée avec un mini-bilan (distance, durée, allure, FC
  comparée à la cible « FC ≤ … ») et les alertes d'enchaînement.
- Association automatique des activités Garmin aux séances, même faites un autre jour de la semaine
  (même type de séance, au plus 2 jours d'avance). Correction possible : « C'était une séance du plan ».
- Ressenti après séance : effort perçu 1–10, douleurs, note libre.
- Déplacement d'une séance dans la semaine, avec aperçu des enchaînements à risque avant de valider.
- « Faite sans montre » pour une séance non enregistrée.
- Synchro en tâche de fond toutes les 30 min et file d'événements à traiter par le coach.
- Nouveaux outils MCP : `get_suivi_semaine`, `list_evenements`, `marquer_evenements_traites`,
  `modifier_seance` ; `publish_fiche` accepte `sport` et `intensite` pour les séances.
- Message de démarrage sans le port 8766.

## 0.2.0
- Nouveau panneau « Coach » dans la barre latérale de Home Assistant (Ingress) : résumé de la semaine
  en page d'accueil (fiche du coach, volume par sport, forme jour par jour, activités), onglets Séances
  et Analyses. Accessible uniquement via Home Assistant.
- Nouveaux outils MCP pour le coach : `publish_fiche`, `list_fiches`, `get_fiche`, `delete_fiche`.
  Les fiches sont stockées dans `share/garmin-coach/coach/` (JSON + copie Markdown lisible).

## 0.1.1
- Les fichiers .fit sont rangés dans `share/garmin-coach/fit` (visibles via Samba) ; ceux de `/data/fit` sont déplacés automatiquement.
- `get_wellness` renvoie un résumé compact par jour (sommeil, VFC, journée) ; `brut=True` pour les données complètes.
- Valeurs arrondies dans `list_activities` et `get_activity_laps` ; calories par tour.
- Journal : IP réelle du client quand la requête passe par le proxy NGINX.

## 0.1.0
- Première version : lecture des activités, tours et séries .fit, sommeil, VFC, VO2max.
