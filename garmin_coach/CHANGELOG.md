# Changelog

## 0.1.1
- Les fichiers .fit sont rangés dans `share/garmin-coach/fit` (visibles via Samba) ; ceux de `/data/fit` sont déplacés automatiquement.
- `get_wellness` renvoie un résumé compact par jour (sommeil, VFC, journée) ; `brut=True` pour les données complètes.
- Valeurs arrondies dans `list_activities` et `get_activity_laps` ; calories par tour.
- Journal : IP réelle du client quand la requête passe par le proxy NGINX.

## 0.1.0
- Première version : lecture des activités, tours et séries .fit, sommeil, VFC, VO2max.
