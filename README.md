# RSI Scanner — Divergences RSI en vue D et vue W

Programme **à la demande** : tu le lances, il scanne toute ta watchlist en vue
journalière **et** hebdomadaire, ouvre un rapport HTML et t'envoie une alerte
Telegram pour chaque divergence RSI récente.

```bash
python3 RSI_scanner.py --ouvrir
```

---

## Installation — Windows (PowerShell)

```powershell
# 1. Se placer dans son dossier personnel
cd $HOME

# 2. Récupérer le programme (crée le dossier RSI_scanner)
git clone https://github.com/paulinhoooooooooo/RSI_scanner.git
cd RSI_scanner

# 3. Installer les dépendances
python -m pip install -r requirements.txt

# 4. Premier test, sans Telegram
python RSI_scanner.py --tickers AAPL --no-telegram --ouvrir
```

Si `git` est introuvable, installe-le depuis https://git-scm.com/download/win, ou
télécharge le dépôt en ZIP depuis GitHub (bouton vert **Code → Download ZIP**)
et décompresse-le dans ton dossier personnel.

Le dépôt étant privé, `git clone` demandera de t'identifier : une fenêtre de
navigateur s'ouvre, tu te connectes à GitHub, et c'est mémorisé pour la suite.

## Installation — Mac / Linux

```bash
# 1. Récupérer le programme
git clone https://github.com/paulinhoooooooooo/RSI_scanner.git
cd RSI_scanner

# 2. Installer les dépendances
pip3 install -r requirements.txt

# 3. Premier test, sans Telegram
python3 RSI_scanner.py --tickers AAPL --no-telegram --ouvrir
```

Si le rapport s'ouvre dans ton navigateur, tout fonctionne.

> Sous Windows, remplace `python3` par `python` et `pip3` par `python -m pip`
> dans toutes les commandes de ce README.

---

## Configurer Telegram

Deux méthodes, au choix.

**Méthode 1 — variables d'environnement (recommandée).** Ton token ne se
retrouve jamais dans un fichier versionné :

```bash
export TELEGRAM_BOT_TOKEN="123456789:AAFxxxx..."
export TELEGRAM_CHAT_ID="123456789"
python3 RSI_scanner.py
```

Pour ne pas les retaper à chaque fois, ajoute ces deux lignes à la fin de ton
`~/.zshrc`.

**Méthode 2 — `config.json`.** Remplace `REMPLACE_PAR_TON_TOKEN` et
`REMPLACE_PAR_TON_CHAT_ID` par tes valeurs. Plus simple, mais ne pousse jamais
ce fichier sur GitHub ensuite.

Les variables d'environnement priment sur `config.json` quand elles existent.

### Obtenir un token et un chat_id

1. Dans Telegram, cherche **@BotFather**, envoie `/newbot` et suis les étapes
2. BotFather te donne le **token**
3. Envoie `/start` à ton bot
4. Ouvre `https://api.telegram.org/botTON_TOKEN/getUpdates` dans un navigateur
5. Le `"id"` dans `"chat"` est ton **chat_id**

---

## Ta watchlist — `tickers.txt`

Un ticker par ligne, les lignes commençant par `#` sont ignorées.

| Marché | Suffixe | Exemple |
|---|---|---|
| US | aucun | `AAPL`, `NVDA` |
| Euronext Paris | `.PA` | `LVMH.PA` |
| Frankfurt | `.DE` | `SAP.DE` |
| Madrid | `.MC` | `SAN.MC` |
| Milan | `.MI` | `ENI.MI` |
| Indices | `^` | `^GSPC` (S&P 500) |

---

## Options

| Option | Effet |
|---|---|
| `--vue D` / `--vue W` | Une seule vue (défaut : les deux) |
| `--tickers AAPL,NVDA` | Ignore `tickers.txt` pour ce scan |
| `--no-telegram` | Génère le rapport sans envoyer d'alerte |
| `--toutes` | Alerte aussi sur les divergences anciennes ou non confirmées |
| `--reset-etat` | Oublie ce qui a déjà été alerté et renvoie tout |
| `--historique` | Garde aussi les divergences anciennes dans le rapport |
| `--sortie chemin.html` | Choisit le fichier du rapport |
| `--ouvrir` | Ouvre le rapport à la fin du scan |

Le rapport est écrit dans `rapports/divergences_AAAAMMJJ_HHMM.html`. Il contient,
pour chaque divergence, un mini-graphique en **bougies japonaises** surmontant
le RSI, avec les deux droites tracées, un axe de dates et des repères verticaux
sur les deux pivots pour aligner le prix et le RSI à l'œil.

---

## Les quatre types de divergences

| Type | Prix | RSI | Lecture |
|---|---|---|---|
| 🟢 Haussière **régulière** | creux plus bas (ou égal) | creux plus haut | la baisse s'essouffle → retournement à la hausse |
| 🔴 Baissière **régulière** | sommet plus haut (ou égal) | sommet plus bas | la hausse s'essouffle → retournement à la baisse |
| 🔵 Haussière **cachée** | creux plus haut | creux plus bas | continuation de la tendance haussière |
| 🟠 Baissière **cachée** | sommet plus bas | sommet plus haut | continuation de la tendance baissière |

Par défaut seules les **régulières** sont actives : les cachées génèrent
beaucoup plus de signaux. Pour les activer, passe-les à `true` dans
`config.json` → `divergence.types`.

---

## Divergences courtes ET longues

Le programme n'appaire pas seulement les deux derniers creux : il teste **toutes
les paires de pivots** distantes de 5 à 130 bougies en vue D (4 à 60 en vue W).
Une divergence étalée sur cinq mois est donc détectée au même titre qu'une
divergence sur deux semaines. Chacune est étiquetée `courte`, `moyenne` ou
`longue` selon sa portée.

Les creux **quasi plats** comptent : un double creux au même niveau surmonté
d'un RSI qui remonte est une divergence.

En revanche, **le prix et le RSI doivent aller dans des sens opposés**. Si les
deux droites descendent, ou si les deux montent, il n'y a pas de divergence —
juste une tendance, et rien n'est affiché. C'est la règle qui distingue un vrai
signal d'un simple mouvement du marché.

---

## Comment la détection fonctionne

1. **Pivots** — ils sont cherchés **sur le RSI lui-même** : un creux est une
   valeur plus basse que les 5 bougies précédentes et les 5 suivantes (3 et 3 en
   vue W). Le prix est ensuite lu sur cette même bougie. Un seul indice sert
   donc aux deux courbes : le point du RSI est toujours un vrai extremum de
   l'indicateur, et les deux droites partagent exactement les mêmes bornes.
2. **Appariement** — toutes les paires de pivots dans la plage de portée sont
   testées, la géométrie prix/RSI déterminant le type de divergence.
3. **Validation** — une paire est rejetée si un **pivot intermédiaire**
   traverse la droite de tendance : un creux plus bas entre les deux invalide la
   figure. Le contrôle ne porte que sur les pivots, pas sur chaque bougie — les
   bougies qui entourent un point d'ancrage sont presque toujours du mauvais
   côté d'une droite en pente, ce qui écartait les divergences les plus
   franches.

La vue W est reconstruite en agrégeant les bougies journalières du lundi au
vendredi, chaque bougie portant la date de son **lundi d'ouverture** — comme sur
les plateformes de graphiques. Un seul téléchargement par ticker sert les deux
vues, ce qui garantit leur cohérence et divise par deux les requêtes.

Chaque vignette porte en haut à gauche un badge **D** ou **W** rappelant sa
timeframe.

---

## Filtres anti-bruit

Trois règles écartent les paires géométriquement valides mais sans portée
pratique.

**Sens opposés obligatoire.** Le prix et le RSI ne doivent jamais aller dans la
même direction — deux droites parallèles sont une tendance, pas une divergence.

**Écart intermédiaire plafonné** (`retracement_max_pct`, 45 % par défaut).
Au-delà, les deux pivots appartiennent à des régimes de prix sans rapport.

Ce plafond est volontairement large. Un rebond marqué entre les deux creux est
**normal** — c'est même la cassure de structure au cœur du motif recherché, qui
mesure 25 % sur le cas de référence. Un plafond serré écarterait donc la figure
que le programme est fait pour trouver. La contrepartie est que des paires
douteuses passent : le rapport affiche l'écart pour chaque ligne, colonne
« Écart interm. », ce qui permet de juger sur pièce et de resserrer le seuil si
tu le souhaites.

**RSI en zone** (`zone_rsi`). Une divergence baissière suppose que le RSI a
atteint le surachat (≥ 60 par défaut), une haussière qu'il a touché la survente
(≤ 40). Une divergence entièrement contenue entre 45 et 55 ne dit rien.

Pour les désactiver : `retracement_max_pct` à `0`, et `zone_rsi.actif` à
`false`.

## Seules les divergences récentes

Une divergence vieille de deux ans a déjà joué ou échoué. Le rapport ne garde
que celles dont le second pivot est récent : **40 jours** en vue D, **13
semaines** en vue W (`rapport.fraicheur_max_bougies`). Les autres sont comptées
dans le KPI « Anciennes écartées ».

Pour tout voir malgré tout : `--historique`.

## « À surveiller maintenant » — la section du haut

Le rapport s'ouvre sur une section encadrée qui ne retient que ce qui se joue
en ce moment :

- toutes les divergences **en formation**, quelle que soit leur date ;
- les divergences **confirmées** de moins de **5 jours** en vue D, **1 semaine**
  en vue W.

Une divergence confirmée depuis trois semaines a déjà donné ce qu'elle avait à
donner : elle reste consultable dans les tableaux par vue, en dessous, mais elle
n'encombre plus la tête du rapport.

Seuils réglables : `prioritaire.fraicheur_confirmees`.

## Confirmée vs en formation

Un pivot n'est certain qu'une fois ses bougies de droite passées. Une divergence
dont le dernier pivot est encore dans cette fenêtre est marquée **en
formation** : elle est réelle aujourd'hui, mais les prochaines bougies peuvent
l'invalider.

Le rapport les affiche toutes ; Telegram n'envoie que les **confirmées**, sauf
si tu passes `confirmees_seulement` à `false`.

---

## Réglages — `config.json`, section `divergence`

```json
"pivot": {
  "D": { "gauche": 5, "droite": 5 },   ← largeur des pivots en vue D
  "W": { "gauche": 3, "droite": 3 }    ←        idem en vue W
},
"ecart_bougies": {
  "D": { "min": 5, "max": 130 },       ← portée min/max d'une divergence
  "W": { "min": 4, "max": 60 }
},
"rsi_delta_min": 6.0,                  ← écart RSI minimum (points) — anti-bruit
"deplacement_max_rsi_bougies": {       ← distance max entre pivot de prix
  "D": 8, "W": 4                       ←   et extremum du RSI
},
"retracement_max_pct": 20.0,           ← écart intermédiaire maximum entre les 2 pivots
"zone_rsi": {
  "actif": true,
  "surachat": 60.0,                    ← RSI mini pour une divergence baissière
  "survente": 40.0                     ← RSI maxi pour une divergence haussière
},
"rapport": {
  "fraicheur_max_bougies": { "D": 40, "W": 13 }   ← au-delà, non affiché
},
"prioritaire": {
  "fraicheur_confirmees": { "D": 5, "W": 1 }      ← âge max d'une confirmée
},                                                ←   dans la section du haut
"verifier_ligne": true,                ← rejette les droites cassées
"max_par_type": 3,                     ← nb max de divergences par type et par vue
"telegram": {
  "fraicheur_max_bougies": { "D": 10, "W": 4 },  ← n'alerte que sur le récent
  "confirmees_seulement": true
}
```

**Trop d'alertes ?** Dans l'ordre d'efficacité : monte `rsi_delta_min` (6 à 8),
monte encore `rsi_delta_min` (8 à 10), baisse `retracement_max_pct` (30 — mais lance `test_motif_reference.py` après,
en dessous de 26 le motif de référence n'est plus détecté), monte `zone_rsi.surachat` à 70 et baisse `survente` à 30, augmente
`pivot.D.gauche`/`droite` (7 ou 8), puis `rsi_delta_min` (6 à 8). Des pivots
plus larges donnent moins de signaux, mais plus fiables.

**Pas assez ?** Fais l'inverse, et éventuellement active les divergences
cachées.

Toutes les clés ont une valeur par défaut interne : tu peux n'écrire dans
`config.json` que celles que tu veux modifier.

---

## Pas de doublons entre deux lancements

Les divergences déjà envoyées sont mémorisées dans `.divergence_etat.json`. Tu
peux relancer le programme plusieurs fois par jour sans recevoir deux fois la
même alerte. Pour tout réenvoyer : `--reset-etat`.

---

## Lancement automatique chaque jour

Le programme est prévu pour être lancé à la main. Pour un scan quotidien
automatique :

**Windows** — ouvre le *Planificateur de tâches*, crée une tâche de base
déclenchée tous les jours à l'heure voulue, action « Démarrer un programme » :
- Programme : `python`
- Arguments : `RSI_scanner.py`
- Commencer dans : le chemin du dossier `RSI_scanner`

**Mac / Linux** — `crontab -e`, puis :

```
0 19 * * 1-5 cd /chemin/vers/RSI_scanner && /usr/bin/python3 RSI_scanner.py >> scan.log 2>&1
```

Un scan à 19 h, du lundi au vendredi.

---

## Le motif de référence

`test_motif_reference.py` reproduit en données la figure que le programme doit
impérativement trouver : une base de six mois, des creux quasi plats très
légèrement descendants, un RSI qui remonte franchement d'un creux à l'autre, et
un fort rebond intermédiaire.

```bash
python3 test_motif_reference.py
```

Il vérifie que la divergence longue est détectée en vue D **et** en vue W. Lance-le
après chaque modification des seuils anti-bruit : c'est le garde-fou qui empêche
un réglage trop strict d'écarter silencieusement la figure recherchée.

## Limites à connaître

- Les données viennent de **Yahoo Finance** : un ticker mal orthographié ne
  renvoie rien. Le rapport liste ces échecs dans « Tickers non analysés » plutôt
  que de s'arrêter.
- Une divergence **n'est pas un signal d'achat** : c'est un signe
  d'essoufflement, qui peut durer longtemps avant que le prix ne retourne, voire
  ne jamais se concrétiser.
- Le premier scan télécharge 3 ans d'historique par ticker — compte une bonne
  minute pour une vingtaine de valeurs.
