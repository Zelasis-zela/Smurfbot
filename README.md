# SmurfWatch

SmurfWatch est la refonte de Smurfbot. Ce bot Discord analyse un profil Valorant pour estimer la probabilité qu'un compte soit un smurf, c'est-à-dire un joueur expérimenté sur un compte à bas niveau ou à bas rang. Il aide aussi les membres à signaler les cas sérieux à Riot Games par les canaux officiels.

Le résultat est toujours **indicatif**. Il ne constitue jamais une preuve, et la décision finale appartient à Riot Games. Le bot ne se moque jamais d'un joueur et n'accuse personne de façon définitive. Il n'utilise que des données de jeu, jamais d'informations personnelles hors jeu.

Le Riot ID se saisit dans un seul champ, au format `Nom#TAG`. L'autocomplétion propose d'abord ton compte lié (⭐), puis les joueurs déjà analysés **sur ce serveur** (jamais ceux des autres serveurs). Si tu as lié ton compte avec `/link`, tu peux laisser le champ vide pour `/check`, `/compare`, `/smurf-match` et `/historique`.

## Les commandes

| Commande | Qui | Visibilité |
|---|---|---|
| `/check [riot_id] [prive]` | tout le monde | publique (privée avec `prive`) |
| `/compare [riot_id]` | tout le monde | publique |
| `/smurf-match [riot_id]` | tout le monde | publique |
| `/historique [riot_id]` | tout le monde | publique |
| `/report riot_id [observations]` | tout le monde | privée (le dossier part dans le salon des modérateurs) |
| `/watchlist [statut]` | modérateurs | privée |
| `/report-setup [salon]` | administrateurs | privée |
| `/link riot_id` · `/unlink` | tout le monde | privée |
| `/watch riot_id` · `/unwatch riot_id` · `/suivis` | tout le monde | publique |
| `/help` | tout le monde | privée |
| `/botstats` | tout le monde | privée |
| `/broadcast` | propriétaire du bot | privée |

Toutes les commandes, sauf `/help`, `/link`, `/unlink`, `/botstats` et `/broadcast`, s'utilisent sur un serveur. Pour éviter de saturer l'API, chaque membre attend 15 s entre deux `/check`, `/compare`, `/historique`, `/link` ou `/watch`, et 30 s entre deux `/report` ou `/smurf-match`. Les erreurs (joueur introuvable, API saturée…) s'affichent toujours en privé.

### `/check`: l'analyse complète
Le bot répond par un embed « 🔍 Analyse de Nom#TAG », coloré selon le niveau de l'indice. L'icône du rang actuel apparaît en vignette. L'embed contient :
- un rappel : un bon joueur, un joueur qui revient d'une pause ou un joueur qui a progressé vite peut déclencher des signaux sans être un smurf ;
- **Profil** : le rang actuel (avec les RR, ou « En placements »), le pic (avec la saison), le niveau du compte et le nombre total de parties classées ;
- **Stats clés (20 dernières parties)** : K/D, HS %, ACS, ADR et winrate, par exemple « 55 % (11V-9D) » ;
- **Indice de suspicion** : l'emoji du niveau, la note sur 100, le libellé et une courte explication ;
- **Signaux détectés** : chaque critère déclenché, avec sa raison chiffrée et ses points ;
- **Contexte** : les remarques utiles (compte ancien, retour de pause probable, placements…) et chaque **donnée manquante**, toujours écrite en toutes lettres ;
- le pied de page « Analyse indicative, ne constitue pas une preuve. »

Quand l'indice atteint 61 ou plus, l'embed rappelle que `/report` prépare un dossier privé pour qui a lui-même affronté le joueur, et qu'un seul dossier par personne suffit (pas de signalements en groupe). Deux boutons accompagnent la réponse. **📜 Dernières parties** affiche en privé les parties analysées : résultat, carte, agent, K/D/A, ACS et HS %. Ce bouton reste utilisable tant que l'analyse est en cache (10 minutes, une seule si elle était incomplète), puis il faut relancer `/check`. **Tracker.gg** ouvre le profil du joueur.

### `/compare`: le joueur face à la moyenne de son rang
Ce tableau compare ses K/D, HS %, ACS, ADR et niveau à la moyenne des joueurs **de son rang**. Ces joueurs sont ceux qu'il a croisés dans ses dernières parties analysées : une ligne par joueur et par partie (« performances de joueurs »), donc un coéquipier croisé plusieurs fois compte plusieurs fois. Si l'échantillon est trop petit, la comparaison s'élargit aux rangs proches, puis à tout le lobby (le titre dit alors « face à la moyenne de ses lobbys »). Pour le niveau, la colonne « Moyenne » donne la médiane. La colonne « Écart » donne un ratio (×) pour le K/D, l'ACS et l'ADR, et des points pour le HS %. La lecture finale indique sur combien de stats (sur 4) il est au-dessus de la moyenne. Elle rappelle que cela ne suffit pas à conclure. Sans partie classée récente, ou sans détail des parties, le bot l'explique en privé ; ces demandes ne comptent pas comme des consultations.

### `/smurf-match`: la dernière partie classée
Pour les 10 joueurs de la dernière partie classée, le bot donne une estimation rapide, équipe par équipe : indice sur 60, agent, rang, niveau, K/D, ACS et HS %, puis les critères qui ont fait monter l'indice (niveau bas, HS %, ACS/ADR). La flèche ⬅️ marque le joueur demandé. Comme elle ne repose que sur une seule partie et trois critères, l'estimation est plafonnée à 60 : `/check` reste l'analyse de référence. Le rappel sur les bons joueurs et les retours de pause figure en tête. Le joueur demandé passe par la protection anti-acharnement, comme pour `/check`.

### `/historique`: l'évolution d'un joueur
Cette commande liste les dernières analyses du joueur **sur ce serveur**, avec la date, l'indice, le rang, le K/D et le nombre de parties. Les analyses faites avant la refonte portent la mention « (ancien calcul) ». Leur échelle était différente, donc elles ne comptent pas dans la tendance ; quand l'ancien bot n'avait enregistré aucune stat, le K/D est affiché « inconnu ». Beaucoup d'anciennes analyses ne sont rattachées à aucun serveur : elles n'apparaissent pas, et le bot répond « Aucune analyse de … n'est enregistrée pour ce serveur ». Afficher l'historique d'un joueur compte comme une consultation pour la protection anti-acharnement.

### `/report`: préparer un dossier
Voir [Signalement](#signalement-report-et-salon-reports-smurf).

### `/watchlist`: les comptes signalés (modérateurs)
Cette commande affiche une ligne par compte signalé sur le serveur : nombre de dossiers et de membres, indice maximal, date du dernier dossier et son numéro. Les icônes indiquent l'état : ⏳ au moins un dossier en attente, ✅ validé, ❌ rejeté. La liste peut être filtrée par statut (en attente, validés, rejetés ou tous) et montre 20 comptes au maximum. La réponse est privée. Il faut la permission « Gérer les messages », « Gérer le serveur » ou être administrateur.

### `/report-setup`: le salon des dossiers (administrateurs)
Ce salon privé reçoit les dossiers `/report` **et** les alertes anti-acharnement : sans lui, les modérateurs ne sont pas prévenus.
- **Sans option** : le bot crée `#reports-smurf`, invisible pour @everyone, et lui donne les droits d'y écrire. S'il existe déjà un salon `#reports-smurf`, le bot le réutilise. Il faut ensuite donner l'accès à tes rôles de modération.
- **Avec `salon`** : le bot enregistre ce salon après avoir vérifié qu'il peut y écrire et y publier des embeds. Si @everyone peut voir le salon, le bot prévient : `/report` refusera d'y publier tant qu'il n'est pas privé.

### `/link`, `/unlink`
Ces commandes lient ou délient ton compte Riot. Le Riot ID est vérifié auprès de l'API et enregistré tel que Riot l'écrit.

### `/watch`, `/unwatch`, `/suivis`: le suivi de rang
`/watch` annonce dans le salon les montées et descentes de rang d'un joueur, ainsi que ses variations de RR. Les rangs sont affichés en français. Un serveur suit au plus 5 joueurs, avec une vérification toutes les 30 minutes. Les placements sont ignorés jusqu'à l'obtention d'un rang. Si le salon disparaît ou devient inaccessible, le suivi est retiré. `/unwatch` est réservé au membre qui a lancé le suivi ou à un membre pouvant gérer le serveur. `/suivis` liste les joueurs suivis.

### `/help`
Cette commande affiche la liste des commandes réellement disponibles (dont `/botstats`), l'échelle de l'indice et les règles.

## L'indice de suspicion

L'indice va de 0 à 100. C'est la somme des points des 9 critères, plafonnée à 100.

| Indice | Niveau | Sens |
|---|---|---|
| 0–30 | 🟢 Faible | profil cohérent avec le rang |
| 31–60 | 🟡 Modéré | quelques signaux, à surveiller |
| 61–80 | 🟠 Élevé | plusieurs signaux convergents |
| 81–100 | 🔴 Très élevé | profil fortement atypique |

La plupart des critères sont **progressifs**. Juste au-dessus du seuil, un critère vaut 60 % de ses points. Il atteint 100 % au niveau « plein », avec une progression linéaire entre les deux. Le **rang de référence** est le rang actuel. En placements, c'est le rang médian des lobbys, sinon le rang de la dernière partie, sinon celui du dernier acte terminé.

| # | Critère | Points max | Se déclenche quand… |
|---|---|---|---|
| 1 | Niveau bas pour le rang | 20 | niveau < 50 avec un rang de référence Or ou plus, ou avec une montée rapide (critère 6 ou 9). Plus le niveau est bas (plein à 25) et plus le rang est haut (×0,6 sous Platine, ×0,85 en Platine, ×1 dès Diamant), plus c'est fort |
| 2 | HS % anormal pour le rang | 18 | au moins 5 parties et 150 tirs. HS % au-dessus du seuil de son rang : 30 % (Fer–Argent), 33 % (Or–Platine), 36 % (Diamant–Ascendant), 40 % (Immortel–Radiant). Plein à seuil + 10 points. Si son pic est au moins 6 divisions au-dessus (retour de pause probable), c'est le seuil du rang de son pic qui s'applique |
| 3 | ACS / ADR face au lobby | 20 | au moins 3 parties détaillées. ACS ou ADR moyen > 1,5× celui des 9 autres joueurs (plein à 2×) |
| 4 | K/D élevé et régulier | 16 | au moins 10 parties. K/D > 1,8 (plein à 2,6) **et** au moins 70 % des parties à 1,2 ou plus |
| 5 | Winrate | 14 | au moins 10 parties. Winrate > 70 % (plein à 90 %) |
| 6 | Gains de RR / sauts de rang | 18 | le plus fort de quatre signaux. (a) Au moins 4 victoires avec un gain moyen de plus de 25 RR (plein à +32). (b) Au moins 4 victoires et 3 défaites, avec un gain moyen d'au moins 22 RR et une perte moyenne d'au plus 14 RR (60 %). (c) Un saut de 2 divisions ou plus en une partie (100 %). (d) Une montée de plus de 4 divisions sur au moins 5 parties classées de l'historique (plein à 8). Les placements, remboursements et protections de rang sont ignorés |
| 7 | Peu de parties, haut niveau | 14 | au moins 5 parties récentes ; moins de 100 parties classées au total, avec un K/D ≥ 1,4 **et** un ACS ≥ 250 (ou 1,3× le lobby). Plus il y a peu de parties, plus c'est fort (plein à 30) |
| 8 | Agents maîtrisés d'emblée | 10 | compte récent (≤ 150 parties ou niveau < 60), au moins 3 agents joués 2 fois ou plus avec K/D ≥ 1,3 et ACS ≥ 230, et un K/D ≥ 1,3 dès ses parties les plus anciennes (60 %, 80 % avec 4 agents, 100 % avec 5 ou plus) |
| 9 | Placement très élevé | 14 | premier acte classé terminé au-dessus d'Argent 3 en 40 parties ou moins, sur 6 actes joués au plus (plein à Diamant 1) |

Remarques et garde-fous :
- **Moins de 5 parties classées récentes** : l'indice est plafonné à 60, donc jamais assez pour un dossier.
- **Compte ancien** (niveau 200 ou plus), **pic bien au-dessus du rang actuel** (retour de pause probable), **placements en cours** et **rang de référence estimé** (lobbys, dernière partie, dernier acte) : ces cas sont signalés dans « Contexte » et ne rapportent aucun point.
- Une donnée manquante n'est jamais inventée. Le critère concerné ne compte pas, et l'embed le dit, y compris quand il y a trop peu de parties pour un critère (5 pour le HS % et le nombre de parties, 8 pour le pool d'agents, 10 pour le K/D et le winrate) ou quand le rang de référence est inconnu.

## Signalement (`/report`) et salon `#reports-smurf`

Riot ne propose pas d'API publique de signalement : SmurfWatch ne signale personne lui-même. Il prépare un dossier pour les modérateurs, puis explique au membre comment faire un signalement officiel.

1. **Mise en place** : un administrateur lance `/report-setup`, puis donne l'accès au salon à ses rôles de modération. Le salon doit rester privé : `/report` refuse de publier dans un salon visible par @everyone.
2. **Le membre lance `/report Nom#TAG`**, avec en option ses observations de jeu (1 000 caractères au maximum, sans information personnelle). On ne peut pas signaler son propre compte lié.
3. Le bot vérifie la protection anti-acharnement, puis la règle des 7 jours.
4. Le bot analyse le joueur. **En dessous de 61/100, le dossier est refusé**. Le refus explique l'indice, les signaux et la raison du seuil : en dessous, les signaux ne sont pas assez convergents. Si l'analyse était incomplète parce que l'API était saturée, le refus le dit et ne compte pas comme une tentative.
5. **À partir de 61/100**, un dossier « 🗂️ Dossier n°X » est publié **uniquement dans le salon des modérateurs**. Il contient le Riot ID, la date, l'auteur du signalement, l'indice, les stats anormales, les stats clés, les ID des parties concernées et les observations du membre. Les parties concernées sont celles où le joueur a le plus dominé son lobby, puis ses meilleurs ACS, 8 au maximum. Les liens, e-mails, pseudos de réseaux (`@…`), mentions, adresses IP et numéros de téléphone sont retirés des observations (« [retiré] ») et le membre en est prévenu. Si d'autres dossiers existent déjà sur ce compte dans le serveur, le dossier l'indique, pour repérer d'éventuels signalements en groupe.
6. Un modérateur clique **✅ Valider** ou **❌ Rejeter**. Le statut du dossier est mis à jour avec son nom et la date, puis les boutons se désactivent. Un dossier ne se traite qu'une fois.
7. Le membre reçoit en privé le numéro du dossier et la marche à suivre pour un signalement officiel :
   - **En jeu** : écran de fin de partie ou historique → sélectionner le joueur → **Signaler** → catégorie appropriée (ex. « Manipulation du classement / smurf » si disponible) ;
   - **Support Riot** : ouvrir un ticket sur support-valorant.riotgames.com en joignant le dossier. Le dossier est fourni en texte brut (1 500 caractères au maximum), prêt à copier, avec un bouton « Support Riot ».

Rien n'est publié ailleurs que dans le salon des modérateurs. Si le salon n'existe pas, est public ou si le bot ne peut pas y écrire, `/report` explique quoi corriger.

## Protection anti-acharnement

- **Un dossier par compte et par membre tous les 7 jours.**
- **6 consultations** (`/check`, `/compare`, `/smurf-match` et `/historique` affichés) du même joueur par le même membre en 24 h : la 7e est refusée poliment.
- **3 tentatives de `/report`** (acceptées ou refusées) sur le même joueur en 7 jours : les suivantes sont refusées poliment.
- À chaque blocage, les modérateurs du serveur reçoivent une alerte dans le salon des dossiers, au plus une fois toutes les 24 h par membre et par joueur. L'alerte n'est envoyée que si ce salon est privé (voir `/report-setup`) ; elle n'est comptée qu'une fois réellement envoyée, et le refus ne dit « les modérateurs ont été prévenus » que dans ce cas.
- Les demandes bloquées par cette protection ne sont pas comptées : le blocage se lève de lui-même quand la fenêtre expire. Un résultat qui n'a pas pu être affiché (pas de comparaison possible, erreur) ne compte pas non plus. En revanche, un `/report` refusé pour doublon ou indice insuffisant compte comme une tentative.
- Le compte lié avec `/link` suit les mêmes règles que les autres : la liaison n'est pas vérifiée, elle ne doit pas permettre de contourner la protection. L'alerte précise alors aux modérateurs que le joueur visé est le compte lié du membre.
- Le journal des consultations n'est gardé que 30 jours.
- Pas de classement public des « pires smurfs » (« name and shame »). Les dossiers restent dans le salon des modérateurs.

## API HenrikDev : coût et cache

La clé Basic permet 30 requêtes par minute, partagées par tout le bot.

| Action | Requêtes |
|---|---|
| Analyse complète (`/check`, `/compare`, `/report`) | **9** : compte 1 + MMR 1 + 20 parties classées 1 + historique de RR 1 + détail de 5 parties 5 |
| Analyse déjà en cache (moins de **10 minutes**) | 0. Deux demandes simultanées sur le même joueur n'en font qu'une |
| `/smurf-match` | 3 (compte, dernière partie, détail), ou 0 à 1 si le joueur vient d'être analysé |
| `/link` | 1 |
| `/watch` | 2 (compte et rang actuel) |
| Vérification du suivi, toutes les 30 min | 1 par joueur suivi, même s'il est suivi sur plusieurs serveurs, avec 4 s entre deux joueurs |

Le bot lit le budget restant dans les en-têtes de l'API. Quand il reste peu d'appels, il limite le détail des parties et l'indique dans « Contexte » ; une telle analyse incomplète n'est gardée en cache qu'une minute. La vérification du suivi de rang attend qu'il reste au moins 12 appels, pour ne pas gêner les commandes des membres, et reprend au tour suivant si l'API reste saturée. Après un refus de l'API (429), il cesse tout appel pendant le délai demandé et affiche « L'API Valorant est très sollicitée. Réessaie dans … ».

## Commandes supprimées ou renommées

| Avant | Maintenant |
|---|---|
| `/smurf pseudo tag` | `/check riot_id` (un seul champ `Nom#TAG`) |
| `/leaderboard-smurfs` (« Hall of Shame ») | supprimée : un classement public des « pires smurfs » est du « name and shame » |
| `/watchlist` (joueurs suivis) | `/suivis`. `/watchlist` liste désormais les comptes signalés (modérateurs) |
| Message public « 🚨 ALERTE SMURF : TICKET RECOMMANDÉ » après `/smurf` | supprimé, remplacé par `/report` (dossier privé validé par les modérateurs) |
| `/smurf-match`, `/historique`, `/link` avec `pseudo` + `tag` | même nom, un seul champ `riot_id` |

## Données

- La base SQLite `smurfs_leaderboard.db` est migrée automatiquement en version 2 au démarrage, sans perte de lignes. Une copie de la base d'avant la migration est gardée dans `backups/avant-migration-v2.db`.
- Une sauvegarde quotidienne a lieu à 04:00 UTC dans `backups/`, et les 7 dernières sont conservées. Au même moment, le journal anti-acharnement de plus de 30 jours est supprimé.
- Quand le bot quitte un serveur, les suivis de rang et les réglages de ce serveur sont supprimés.

## Déploiement

Le bot tourne sur le panel Pterodactyl, lancé par `Start.py`. Au démarrage du serveur, `Start.py` met à jour le dossier `Smurfbot/` depuis le dépôt GitHub (`git fetch` + `reset --hard`). Il réinstalle aussi `requirements.txt` quand ce fichier a changé : c'est ainsi qu'`aiohttp` est installé.

1. Lancer les tests (ci-dessous).
2. Faire un `git push` depuis le PC.
3. Redémarrer le serveur sur le panel.

Les commandes slash ne sont envoyées à Discord que si elles ont changé, d'après l'empreinte gardée dans `.commands-synced`. Pour forcer l'envoi, ajouter `SYNC_COMMANDS=1` au `.env`. Si un cog ne se charge pas, la synchronisation est bloquée, pour ne pas faire disparaître ses commandes. Le fichier `.env` doit contenir `DISCORD_TOKEN` et `API_KEY` (voir `.env.example`). `.env`, les bases `*.db`, `backups/` et `logs/` ne sont jamais versionnés.

## Tests

```
cd C:/Users/weebman/Documents/Smurfbot
C:/Users/weebman/Documents/CommunityBot/.venv/Scripts/python.exe -m unittest discover -s tests -t .
```

Les tests n'appellent ni Discord ni l'API : ils utilisent des réponses réelles enregistrées et anonymisées (`tests/fixtures/`), avec une base temporaire. Ils ne lisent jamais `.env` et n'ouvrent pas la vraie base.

## Organisation du code

- `app.py` : le bot, le chargement des cogs, la synchronisation des commandes et le gestionnaire d'erreurs global.
- `core/` : la logique sans Discord, plus les outils partagés.
  - `henrik.py` : le client API.
  - `analyzer.py` : l'orchestration et le cache.
  - `analysis.py` et `scoring.py` : les calculs et les 9 critères.
  - `ranks.py` et `fmt.py` : les rangs en français et le format des nombres.
  - `db.py` : la base.
  - `guard.py` : l'anti-acharnement.
  - `errors.py` : les messages d'erreur.
  - `embeds.py` : tous les embeds.
  - `discord_helpers.py` : le Riot ID, l'autocomplétion, les droits et le salon des dossiers.
- `cogs/` : les commandes.
  - `smurfwatch.py` : `/check`, `/compare`, `/help`, `/smurf-match`, `/historique`, `/link`, `/unlink`.
  - `reports.py` : `/report`, `/watchlist`, `/report-setup`.
  - `watch.py` : le suivi de rang.
  - `maintenance.py` : les sauvegardes et le nettoyage.
  - `admin_utils.py` : `/botstats` et `/broadcast`.
