#!/usr/bin/env python3
"""
RSI Scanner — Divergences RSI en vue D et vue W

Détecte les divergences RSI (régulières et cachées) sur toute la watchlist,
en vue journalière et hebdomadaire, produit un rapport HTML et envoie une
alerte Telegram pour chaque divergence récente.

Usage :
    python3 RSI_scanner.py                    # scan complet D + W
    python3 RSI_scanner.py --vue D            # journalier seulement
    python3 RSI_scanner.py --tickers AAPL,NVDA
    python3 RSI_scanner.py --no-telegram      # rapport seul
    python3 RSI_scanner.py --reset-etat       # réenvoie tout sur Telegram
"""

import argparse
import json
import os
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

# La console Windows est souvent en cp1252 : sans cela, le premier emoji
# affiché fait planter le programme sur un UnicodeEncodeError.
for _flux in (sys.stdout, sys.stderr):
    try:
        _flux.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

BASE_DIR      = Path(__file__).parent
CONFIG_FILE   = BASE_DIR / "config.json"
TICKERS_FILE  = BASE_DIR / "tickers.txt"
ETAT_FILE     = BASE_DIR / ".divergence_etat.json"
RAPPORTS_DIR  = BASE_DIR / "rapports"

# Valeurs utilisées si la section "divergence" est absente de config.json
DEFAUTS = {
    # Profondeur d'historique par vue. La mensuelle a besoin de bien plus
    # de recul : le RSI consomme déjà 14 mois avant de produire sa première
    # valeur, et trois ans ne laisseraient qu'une vingtaine de bougies
    # exploitables. Le téléchargement se fait une fois, sur la plus longue des
    # périodes demandées, puis chaque vue est tronquée à la sienne.
    "periode_historique": {"D": "3y", "W": "3y", "M": "20y"},
    # Faux = prix bruts, comme les plateformes de graphiques. Vrai fausse
    # la comparaison entre pivots distants sur les titres à dividende.
    "ajuster_dividendes": False,
    "rsi_periode": 14,
    "vues": {"D": True, "W": True, "M": True},
    "pivot": {
        "D": {"gauche": 5, "droite": 5},
        "W": {"gauche": 3, "droite": 3},
        "M": {"gauche": 2, "droite": 2},
    },
    "ecart_bougies": {
        "D": {"min": 5, "max": 160},
        "W": {"min": 4, "max": 60},
        "M": {"min": 3, "max": 36},
    },
    "seuils_duree": {
        "D": {"courte": 15, "moyenne": 40},
        "W": {"courte": 4, "moyenne": 12},
        "M": {"courte": 4, "moyenne": 10},
    },
    "rsi_delta_min": 6.0,
    # Écart maximal du prix entre les deux pivots. Mesuré en multiples d'ATR
    # plutôt qu'en pourcentage : entre deux creux, une action bouge de 20 % et
    # le bitcoin de 130 %, sans que la figure soit moins valable dans un cas
    # que dans l'autre. Un plafond en pourcentage écarte de fait tous les
    # actifs très volatils.
    "retracement": {"mode": "atr", "max_atr": 15.0, "max_pct": 45.0},
    # Une divergence ne dit quelque chose que si le marché était réellement
    # étiré. Les seuils sont par vue : le RSI hebdomadaire, bien plus lisse,
    # atteint rarement 25 ou 75, ce qui rend ces niveaux d'autant plus parlants.
    "zone_rsi": {
        "actif": True,
        "mode": "premier_pivot",          # ou "les_deux_pivots"
        "D": {"surachat": 70.0, "survente": 30.0},
        "W": {"surachat": 75.0, "survente": 25.0},
        "M": {"surachat": 75.0, "survente": 25.0},
    },
    "verifier_ligne": True,
    "verifier_ligne_rsi": False,
    # Teste chaque bougie contre la droite de prix, pas seulement les pivots
    # du RSI : sans ça une figure s'affiche avec sa droite visiblement percée.
    "verifier_ligne_bougies": True,
    "tolerance_cassure_prix_pct": 0.5,
    "tolerance_cassure_rsi": 2.0,
    "autoriser_pivot_provisoire": True,
    "max_par_type": 3,
    "types": {
        "haussiere_reguliere": True,
        "baissiere_reguliere": True,
        "haussiere_cachee": False,
        "baissiere_cachee": False,
    },
    "rapport": {
        "fraicheur_max_bougies": {"D": 40, "W": 13, "M": 4},
    },
    "prioritaire": {
        "rsi_delta_fort": 10.0,
    },
    # Ampleur du mouvement qui PRÉCÈDE la figure : la chute avant une
    # divergence haussière, la hausse avant une baissière. Une divergence
    # après une baisse de 40 % n'offre pas le même potentiel qu'un creux pris
    # dans une tendance plate. `fenetre` est le recul, en bougies, où l'on va
    # chercher le sommet de départ.
    "contexte": {
        "fenetre": {"D": 120, "W": 52, "M": 24},
        "min_pct": 0.0,                  # 0 = aucun filtre
        # Zone favorable, mesurée sur 1 303 figures et non choisie a priori.
        # La tranche 35-50 % est la meilleure des six combinaisons de vue et
        # de sens, et la tranche au-delà de 50 % la pire, sans exception :
        #   D haussières  35-50 : 100 % atteints, +9,0 % médian à 60 jours
        #                   50+ :  68 % atteints, -4,4 % médian à 20 jours
        #   D baissières  35-50 :  75 % atteints, +2,0 % médian à 60
        #                   50+ :  65 % atteints, -2,7 %
        #   W baissières  35-50 :  80 % atteints ;  50+ : -16,7 % à 26 sem.
        #   M baissières  35-50 :  94 % atteints ;  50+ : -13,9 % à 12 mois
        # Au-delà de 50 %, le mouvement préalable n'est pas un meilleur
        # signal : le couteau tombe encore.
        "zone_favorable": [35.0, 50.0],
    },
    # Historique : que valait la figure, une fois jouée ?
    # L'objectif est le niveau de prix du PREMIER pivot — celui d'où la
    # divergence est partie. L'atteindre signifie que le mouvement annoncé
    # a eu lieu ; le prix y revient et la figure est consommée.
    "historique": {
        "actif": True,
        # Bougies entre le signal et l'entrée : le temps que la figure soit
        # lisible. Mesurer depuis le pivot lui-même supposerait de l'avoir vu
        # au moment précis où il se formait.
        "decalage_entree": 3,
        # Rendements relevés à plusieurs échéances après l'entrée, pour voir
        # à quel moment la figure paie — ou cesse de payer.
        "horizons": {"D": [5, 10, 20, 40, 60], "W": [2, 4, 8, 13, 26],
                     "M": [1, 2, 3, 6, 12]},
        # Au-delà, l'objectif est réputé manqué.
        "horizon_objectif": {"D": 120, "W": 52, "M": 24},
        "max_par_type": 40,
    },
    "telegram": {
        "actif": True,
        "fraicheur_max_bougies": {"D": 10, "W": 4, "M": 2},
        "confirmees_seulement": False,
    },
}

TYPES_META = {
    "haussiere_reguliere": {
        "label": "Divergence haussière régulière",
        "court": "Haussière rég.",
        "emoji": "🟢",
        "sens": "bas",
        "biais": "haussier",
        "explication": "Le prix fait un creux plus bas (ou égal), le RSI un creux plus haut — la pression vendeuse s'essouffle.",
    },
    "baissiere_reguliere": {
        "label": "Divergence baissière régulière",
        "court": "Baissière rég.",
        "emoji": "🔴",
        "sens": "haut",
        "biais": "baissier",
        "explication": "Le prix fait un sommet plus haut (ou égal), le RSI un sommet plus bas — la pression acheteuse s'essouffle.",
    },
    "haussiere_cachee": {
        "label": "Divergence haussière cachée",
        "court": "Haussière cachée",
        "emoji": "🔵",
        "sens": "bas",
        "biais": "haussier",
        "explication": "Le prix fait un creux plus haut, le RSI un creux plus bas — continuation de tendance haussière.",
    },
    "baissiere_cachee": {
        "label": "Divergence baissière cachée",
        "court": "Baissière cachée",
        "emoji": "🟠",
        "sens": "haut",
        "biais": "baissier",
        "explication": "Le prix fait un sommet plus bas, le RSI un sommet plus haut — continuation de tendance baissière.",
    },
}

VUES_META = {
    "D": {"label": "Vue journalière", "court": "D", "unite": "jours"},
    "W": {"label": "Vue hebdomadaire", "court": "W", "unite": "semaines"},
    "M": {"label": "Vue mensuelle", "court": "M", "unite": "mois"},
}


# Watchlist écrite dans tickers.txt au premier lancement, si le fichier
# n'existe pas encore.
WATCHLIST_PAR_DEFAUT = """# Liste des tickers à surveiller
# Un ticker par ligne
# Suffixe .PA pour Euronext Paris (ex: MC.PA), .DE pour Francfort,
# .MC pour Madrid, .MI pour Milan, .L pour Londres, .SW pour Zurich
# Pas de suffixe pour les actions US (AAPL, TSLA...)
# Préfixe ^ pour les indices (^GSPC, ^FCHI...)
# Les lignes commençant par # sont ignorées


# ═══════════════════════════════════════════════════════════
#  LISTE D'ORIGINE
# ═══════════════════════════════════════════════════════════

# --- CAC 40 ---
SAN.PA

# --- US Tech ---
AAPL
MSFT
NVDA
GOOGL
META
TSLA
AMZN
QQQ
^GSPC

# --- Santé ---
JNJ
AMGN
LLY
UNH
MRK
SAN
ABBV
HON
IPN
MRNA

# --- Or ---
GOLD


# ═══════════════════════════════════════════════════════════
#  COMPLÉMENT — 50 valeurs, secteurs et zones diversifiés
# ═══════════════════════════════════════════════════════════

# --- Technologie & semi-conducteurs (8) ---
AMD
AVGO
ASML
TSM
ORCL
CRM
ADBE
QCOM

# --- Finance & paiements (6) ---
JPM
GS
V
MA
BNP.PA
ALV.DE

# --- Santé & équipement médical (5) ---
PFE
NVO
TMO
MDT
ISRG

# --- Énergie (4) ---
XOM
CVX
TTE.PA
SHEL

# --- Industrie & aéronautique (5) ---
CAT
GE
AIR.PA
SIE.DE
RTX

# --- Consommation de base (5) ---
KO
PEP
PG
WMT
COST

# --- Consommation discrétionnaire & luxe (5) ---
MCD
NKE
HD
MC.PA
RMS.PA

# --- Communication & médias (3) ---
NFLX
DIS
CMCSA

# --- Immobilier coté (2) ---
AMT
PLD

# --- Services aux collectivités (2) ---
NEE
IBE.MC

# --- Matériaux & mines (3) ---
LIN
BHP
RIO

# --- Indices & ETF (2) ---
IWM
^FCHI


# ═══════════════════════════════════════════════════════════
#  COMPLÉMENT — 30 valeurs CYCLIQUES
#  Sensibles au cycle économique : elles amplifient les
#  retournements, donc les divergences y sont plus marquées.
# ═══════════════════════════════════════════════════════════

# --- Automobile (4) ---
F
GM
STLA
VOW3.DE

# --- Transport aérien (3) ---
DAL
UAL
AF.PA

# --- Voyage, hôtellerie & loisirs (4) ---
CCL
RCL
BKNG
MAR

# --- Construction & habitat (3) ---
DHI
LEN
SGO.PA

# --- Chimie (3) ---
DOW
LYB
BAS.DE

# --- Acier, aluminium & métaux (4) ---
NUE
FCX
MT
AA

# --- Machines & équipement lourd (3) ---
DE
CMI
URI

# --- Équipement semi-conducteurs (2) ---
AMAT
LRCX

# --- Logistique (1) ---
FDX

# --- Distribution discrétionnaire (2) ---
TGT
LOW

# --- Services pétroliers (1) ---
SLB
"""


# ─── Config & watchlist ───────────────────────────────────────────────────────

def fusionner(defaut, perso):
    """Fusion récursive : les clés de `perso` écrasent celles de `defaut`."""
    resultat = dict(defaut)
    for cle, valeur in (perso or {}).items():
        if isinstance(valeur, dict) and isinstance(resultat.get(cle), dict):
            resultat[cle] = fusionner(resultat[cle], valeur)
        else:
            resultat[cle] = valeur
    return resultat


def charger_config():
    """
    Lit config.json s'il existe, sinon tourne sur les valeurs par défaut.

    Le programme doit pouvoir s'exécuter seul, posé n'importe où, sans aucun
    fichier d'accompagnement : sans config.json il scanne quand même, il
    n'enverra simplement pas d'alerte Telegram faute d'identifiants.
    """
    config = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
    config.setdefault("telegram", {})
    config["divergence"] = fusionner(DEFAUTS, config.get("divergence"))
    return config


def charger_tickers():
    """
    Lit tickers.txt, et le crée à partir de la liste intégrée s'il manque.

    Écrire le fichier plutôt que garder la liste en mémoire laisse une
    watchlist modifiable là où le programme a été lancé.
    """
    if not TICKERS_FILE.exists():
        TICKERS_FILE.write_text(WATCHLIST_PAR_DEFAUT, encoding="utf-8")
        print(f"tickers.txt créé avec la watchlist par défaut — {TICKERS_FILE}")
    tickers = []
    with open(TICKERS_FILE, "r", encoding="utf-8") as f:
        for ligne in f:
            ligne = ligne.strip()
            if ligne and not ligne.startswith("#"):
                tickers.append(ligne.upper())
    return list(dict.fromkeys(tickers))


# ─── Données & indicateurs ────────────────────────────────────────────────────

def telecharger(ticker, periode, ajuster_dividendes=False):
    """Télécharge l'historique journalier. Retourne None si indisponible."""
    try:
        # Prix BRUTS, ajustés des splits mais pas des dividendes — exactement
        # ce qu'affichent les plateformes de graphiques.
        #
        # L'ajustement des dividendes abaisse rétroactivement les prix anciens.
        # Comparer deux pivots distants revient alors à comparer des échelles
        # différentes : sur TotalEnergies, un sommet de septembre 0,12 % SOUS
        # celui de mars devenait 1,54 % AU-DESSUS une fois ajusté. Le biais a
        # un sens — il fabrique de fausses divergences baissières et masque de
        # vraies divergences haussières — et il grandit avec la portée de la
        # figure et le rendement du titre.
        df = yf.download(ticker, period=periode, interval="1d",
                         progress=False, auto_adjust=ajuster_dividendes)
    except Exception as e:
        print(f"  ✗ {ticker} — erreur téléchargement : {e}")
        return None
    if df is None or df.empty:
        print(f"  ✗ {ticker} — aucune donnée (ticker inconnu ?)")
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["High", "Low", "Close"])
    return df


def en_hebdomadaire(df):
    """
    Agrège les bougies journalières en bougies hebdomadaires, du lundi au
    vendredi, chaque bougie portant la date de son LUNDI d'ouverture.

    Dater la semaine à sa clôture du vendredi donnait des bougies correctes mais
    des dates trompeuses : on lit alors 31/07 pour une semaine qui commence le
    27/07. Les plateformes datent la bougie hebdomadaire à son ouverture.
    """
    regles = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    if "Volume" in df.columns:
        regles["Volume"] = "sum"
    lundi = df.index - pd.to_timedelta(df.index.weekday, unit="D")
    hebdo = df.groupby(lundi).agg(regles).dropna(subset=["High", "Low", "Close"])
    hebdo.index.name = df.index.name
    return hebdo


def en_mensuel(df):
    """
    Agrège en bougies mensuelles, chacune datée de son PREMIER jour de
    cotation — même convention que l'hebdomadaire, qui porte la date de son
    lundi d'ouverture plutôt que celle de sa clôture.
    """
    regles = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    if "Volume" in df.columns:
        regles["Volume"] = "sum"
    debut = df.index.to_period("M").to_timestamp()
    mensuel = df.groupby(debut).agg(regles).dropna(subset=["High", "Low", "Close"])
    mensuel.index.name = df.index.name
    return mensuel


def chute_prealable(highs, lows, idx_a, idx_b, sens, fenetre):
    """
    Ampleur du mouvement qui amène la figure, en pourcentage.

    Pour une divergence haussière, c'est la chute depuis le plus haut des
    `fenetre` bougies précédant le premier pivot jusqu'au creux de la figure.
    Pour une baissière, la hausse symétrique. C'est le contexte qui donne sa
    valeur au signal : un creux pris après une baisse de 40 % a bien plus de
    chemin à reprendre qu'un creux pris dans une tendance plate.

    Les deux cas sont rapportés au SOMMET du mouvement, jamais au creux. Une
    hausse rapportée à son point de départ n'est pas bornée — sur 24 mois une
    valeur de croissance affichait 2 886 % — alors qu'une baisse plafonne à
    100 %. Les deux sens n'étaient donc pas comparables et tout ce qui était
    baissier en vues W et M se retrouvait hors échelle. Rapportée au sommet,
    la même amplitude géométrique donne le même chiffre dans les deux sens.
    """
    debut = max(0, idx_a - fenetre)
    if sens == "bas":
        sommet = np.nanmax(highs[debut:idx_a + 1])
        creux = np.nanmin(lows[idx_a:idx_b + 1])
        return float((sommet - creux) / sommet * 100) if sommet > 0 else 0.0
    creux = np.nanmin(lows[debut:idx_a + 1])
    sommet = np.nanmax(highs[idx_a:idx_b + 1])
    return float((sommet - creux) / sommet * 100) if sommet > 0 else 0.0


def calc_atr(df, periode=14):
    """ATR méthode Wilder — sert d'unité de mesure propre à chaque actif."""
    haut, bas, clot = (df[c].astype(float) for c in ("High", "Low", "Close"))
    tr = pd.concat([haut - bas,
                    (haut - clot.shift()).abs(),
                    (bas - clot.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / periode, min_periods=1).mean()


def calc_rsi(closes, periode=14):
    """RSI méthode Wilder — série complète (identique TradingView)."""
    delta    = closes.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=periode - 1, min_periods=periode).mean()
    avg_loss = loss.ewm(com=periode - 1, min_periods=periode).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ─── Pivots ───────────────────────────────────────────────────────────────────

def detecter_pivots(valeurs, gauche, droite, sens, autoriser_provisoire=True):
    """
    Pivots hauts/bas d'une série.

    Un pivot bas à l'indice i est plus bas que les `gauche` bougies précédentes
    et pas plus haut que les `droite` suivantes. Un pivot dont la fenêtre droite
    est incomplète est marqué `confirme=False` : il est réel aujourd'hui mais
    peut être invalidé par les bougies à venir.

    La toute dernière bougie est éligible, validée sur sa seule fenêtre gauche.
    L'exclure revenait à ne rien voir avant la séance suivante, et à ne pouvoir
    rien confirmer avant `droite` séances de plus — une semaine de retard sur un
    signal qui se joue au moment où il se forme.
    """
    n = len(valeurs)
    pivots = []
    for i in range(gauche, n):
        dispo = min(droite, n - 1 - i)
        if not autoriser_provisoire and dispo < droite:
            continue
        v = valeurs[i]
        if np.isnan(v):
            continue
        # Les NaN sont écartés des fenêtres plutôt que de disqualifier le pivot :
        # le RSI n'a pas de valeur sur ses premières bougies, et une comparaison
        # avec NaN étant toujours fausse, tout pivot proche du début de série
        # était silencieusement perdu.
        fen_g = valeurs[i - gauche:i]
        fen_d = valeurs[i + 1:i + 1 + dispo]
        fen_g = fen_g[~np.isnan(fen_g)]
        fen_d = fen_d[~np.isnan(fen_d)]
        if fen_g.size == 0:
            continue
        if sens == "bas":
            ok = np.all(v < fen_g) and (fen_d.size == 0 or np.all(v <= fen_d))
        else:
            ok = np.all(v > fen_g) and (fen_d.size == 0 or np.all(v >= fen_d))
        if ok:
            pivots.append({"idx": i, "prix": float(v), "confirme": dispo >= droite})
    return pivots


def pivot_intermediaire_casse(pivots, a, b, cle, sens, tolerance_abs):
    """
    Vrai si un pivot intermédiaire traverse la droite reliant `a` à `b`.

    On ne teste que les pivots, pas chaque bougie : les bougies qui entourent
    immédiatement un point d'ancrage sont presque toujours du mauvais côté d'une
    droite en pente, ce qui rejetait les divergences les plus franches. Ce qui
    invalide vraiment une figure, c'est un creux — ou un sommet — intermédiaire
    plus extrême que la droite, autrement dit un pivot.
    """
    ecart_idx = b["idx"] - a["idx"]
    if ecart_idx <= 1:
        return False
    for q in pivots:
        if not (a["idx"] < q["idx"] < b["idx"]):
            continue
        ligne = a[cle] + (b[cle] - a[cle]) * (q["idx"] - a["idx"]) / ecart_idx
        marge = tolerance_abs(ligne)
        if sens == "bas" and q[cle] < ligne - marge:
            return True
        if sens == "haut" and q[cle] > ligne + marge:
            return True
    return False


def bougie_casse_ligne(lows, highs, a, b, sens, tol_pct, n, droite):
    """
    Vrai si le prix est sorti de la figure, par une bougie quelconque.

    `pivot_intermediaire_casse` ne teste que les pivots du RSI. Une bougie peut
    donc traverser la droite de prix sans être vue, puisqu'elle n'est un pivot
    que sur le RSI : sur BKNG, le creux du 05/10 à 154.06 perçait la ligne
    tracée à 155.36 sans rien déclencher, et la figure s'affichait avec une
    droite de support visiblement traversée.

    Le critère est le plancher de la figure — le plus extrême des deux
    ancrages — et non la droite interpolée. Tester la droite paraît plus
    rigoureux mais rejette presque tout : sur une figure longue et pentue, une
    bougie peut passer sous la droite tout en restant au-dessus des deux
    creux, ce qui n'invalide rien. Mesuré sur vingt valeurs, le test contre la
    droite supprimait 89 % des figures ; contre le plancher il ne retire que
    celles dont le creux s'est réellement déplacé.

    Au-delà du second pivot, le balayage s'arrête à la fenêtre de confirmation
    (`droite` bougies) et non à la fin de la série. Sans cette borne une figure
    de 2024 était invalidée par n'importe quelle bougie des deux années
    suivantes, ce qui supprimait 86 % de l'historique : passer sous un creux
    deux ans plus tard est le cours normal du marché, pas un défaut de la
    figure. Ce qui compte est la fenêtre où le pivot se valide — et, pour une
    figure encore en formation, elle s'arrête à aujourd'hui.

    La tolérance est un pourcentage du niveau : une mèche qui effleure le
    plancher ne casse rien, et le même seuil vaut à 50 $ comme à 900 $.
    """
    extremes = lows if sens == "bas" else highs
    signe = -1 if sens == "bas" else 1
    plancher = min(a["prix"], b["prix"]) if sens == "bas" else max(a["prix"], b["prix"])
    marge = abs(plancher) * tol_pct

    for k in range(a["idx"] + 1, min(n, b["idx"] + droite + 1)):
        if k == b["idx"]:
            continue
        v = extremes[k]
        if np.isfinite(v) and signe * (v - plancher) > marge:
            return True

    return False


def excursion_intermediaire(lows, highs, i1, i2, v1, v2, sens, unite_atr=None):
    """
    Plus grand écart du prix entre les deux pivots.

    Exprimé en multiples d'ATR si `unite_atr` est fourni, en pourcentage
    sinon. L'ATR rend la mesure comparable d'un actif à l'autre : un écart de
    130 % sur le bitcoin et de 20 % sur une action peuvent valoir le même
    nombre d'ATR, donc représenter le même degré d'anomalie.

    Pour une divergence de sommets on mesure jusqu'où le prix est descendu sous
    le plus bas des deux sommets ; pour une divergence de creux, jusqu'où il est
    monté au-dessus du plus haut des deux creux. Une valeur élevée signale deux
    mouvements distincts plutôt qu'une même structure qui s'essouffle.
    """
    if i2 <= i1 + 1:
        return 0.0
    if sens == "haut":
        reference = min(v1, v2)
        ecart = reference - float(np.nanmin(lows[i1 + 1:i2]))
    else:
        reference = max(v1, v2)
        ecart = float(np.nanmax(highs[i1 + 1:i2])) - reference
    ecart = max(0.0, ecart)
    if unite_atr is not None:
        return ecart / unite_atr if unite_atr and np.isfinite(unite_atr) else 0.0
    return ecart / reference * 100 if reference > 0 else 0.0


# ─── Détection des divergences ────────────────────────────────────────────────

def classer_duree(span, seuils):
    if span <= seuils["courte"]:
        return "courte"
    if span <= seuils["moyenne"]:
        return "moyenne"
    return "longue"


def detecter_divergences(df, vue, params):
    """Retourne la liste des divergences détectées sur un dataframe (une vue)."""
    closes = df["Close"].astype(float)
    rsi_serie = calc_rsi(closes, params["rsi_periode"])

    rsi_vals = rsi_serie.to_numpy(dtype=float)
    lows     = df["Low"].astype(float).to_numpy()
    highs    = df["High"].astype(float).to_numpy()
    dates    = df.index
    n        = len(df)

    cfg_pivot = params["pivot"][vue]
    cfg_ctx = params.get("contexte", {})
    fen_ctx = cfg_ctx.get("fenetre", {}).get(vue, 120)
    ctx_min = cfg_ctx.get("min_pct", 0.0)
    cfg_ecart = params["ecart_bougies"][vue]
    delta_min = params["rsi_delta_min"]
    cfg_retr  = params.get("retracement", {})
    retr_mode = cfg_retr.get("mode", "atr")
    retr_max  = cfg_retr.get("max_atr" if retr_mode == "atr" else "max_pct", 0)
    atr_vals  = calc_atr(df, params["rsi_periode"]).to_numpy(dtype=float)
    cfg_zone  = params.get("zone_rsi", {})

    # Les pivots sont cherchés sur le RSI lui-même, et le prix est lu sur la même
    # bougie. C'est ce qui garantit qu'un point du RSI est toujours un vrai
    # sommet ou un vrai creux de l'indicateur, et que les deux droites partagent
    # exactement les mêmes bornes : un seul indice sert aux deux courbes.
    def pivots_sur_rsi(sens):
        serie_prix = lows if sens == "bas" else highs
        pivots = detecter_pivots(rsi_vals, cfg_pivot["gauche"], cfg_pivot["droite"],
                                 sens, params["autoriser_pivot_provisoire"])
        for p in pivots:
            p["rsi"] = p.pop("prix")          # detecter_pivots renvoie la valeur lue
            p["rsi_idx"] = p["idx"]
            p["prix"] = float(serie_prix[p["idx"]])
        return [p for p in pivots if not np.isnan(p["prix"])]

    pivots_bas  = pivots_sur_rsi("bas")
    pivots_haut = pivots_sur_rsi("haut")

    candidats = []

    for type_cle, meta in TYPES_META.items():
        if not params["types"].get(type_cle, False):
            continue

        sens    = meta["sens"]
        pivots  = pivots_bas if sens == "bas" else pivots_haut
        cachee  = type_cle.endswith("cachee")

        for j in range(1, len(pivots)):
            b = pivots[j]
            for i in range(j - 1, -1, -1):
                a = pivots[i]
                span = b["idx"] - a["idx"]
                if span < cfg_ecart["min"]:
                    continue
                if span > cfg_ecart["max"]:
                    break

                d_rsi   = b["rsi"] - a["rsi"]
                ecart_p = (b["prix"] - a["prix"]) / a["prix"] if a["prix"] else 0.0

                # Géométrie attendue selon le type de divergence.
                #
                # Le prix ne doit JAMAIS partir dans le même sens que le RSI :
                # deux droites parallèles ne sont pas une divergence, juste une
                # tendance. Un prix strictement plat (écart nul) est accepté —
                # un double creux surmonté d'un RSI qui remonte diverge bien.
                if sens == "bas":
                    if cachee:
                        prix_ok = ecart_p >= 0          # creux plus haut
                        rsi_ok  = d_rsi <= -delta_min   # RSI plus bas
                    else:
                        prix_ok = ecart_p <= 0          # creux plus bas ou égal
                        rsi_ok  = d_rsi >= delta_min    # RSI plus haut
                else:
                    if cachee:
                        prix_ok = ecart_p <= 0          # sommet plus bas
                        rsi_ok  = d_rsi >= delta_min    # RSI plus haut
                    else:
                        prix_ok = ecart_p >= 0          # sommet plus haut ou égal
                        rsi_ok  = d_rsi <= -delta_min   # RSI plus bas

                if not (prix_ok and rsi_ok):
                    continue

                if params["verifier_ligne"]:
                    tol_p = params["tolerance_cassure_prix_pct"] / 100.0
                    tol_r = params["tolerance_cassure_rsi"]
                    if pivot_intermediaire_casse(pivots, a, b, "prix", sens,
                                                 lambda ligne: abs(ligne) * tol_p):
                        continue
                    if params.get("verifier_ligne_rsi", False):
                        if pivot_intermediaire_casse(pivots, a, b, "rsi", sens,
                                                     lambda _ligne: tol_r):
                            continue
                    if params.get("verifier_ligne_bougies", True):
                        if bougie_casse_ligne(lows, highs, a, b, sens, tol_p, n,
                                              cfg_pivot["droite"]):
                            continue

                # Filtre de retracement : deux sommets séparés par une chute
                # profonde appartiennent à des phases de marché différentes.
                # Les relier donne une droite valide mais sans portée pratique.
                profondeur = excursion_intermediaire(lows, highs, a["idx"], b["idx"],
                                                     a["prix"], b["prix"], sens,
                                                     atr_vals[b["idx"]] if retr_mode == "atr" else None)
                if retr_max and profondeur > retr_max:
                    continue

                # Filtre de zone : une divergence baissière n'a de sens que si le
                # RSI a atteint le surachat, et inversement en survente.
                if cfg_zone.get("actif", True):
                    # En mode « premier pivot », seul le pivot d'origine doit
                    # avoir atteint l'extrême : c'est lui qui atteste que le
                    # marché était tendu, le second marquant l'essoufflement.
                    # En mode « les deux pivots », le signal reste cantonné à
                    # la zone extrême de bout en bout — bien plus rare.
                    deux = cfg_zone.get("mode") == "les_deux_pivots"
                    if sens == "bas":
                        seuil = cfg_zone[vue]["survente"]
                        atteint = (max(a["rsi"], b["rsi"]) if deux
                                   else min(a["rsi"], b["rsi"])) <= seuil
                    else:
                        seuil = cfg_zone[vue]["surachat"]
                        atteint = (min(a["rsi"], b["rsi"]) if deux
                                   else max(a["rsi"], b["rsi"])) >= seuil
                    if not atteint:
                        continue

                # Contexte : l'ampleur du mouvement qui amène la figure.
                # Une divergence haussière après une chute de 40 % a bien plus
                # de chemin à reprendre qu'un creux pris en tendance plate.
                chute = chute_prealable(highs, lows, a["idx"], b["idx"],
                                        sens, fen_ctx)
                if ctx_min and chute < ctx_min:
                    continue

                candidats.append({
                    "chute_prealable": round(chute, 1),
                    "retracement_pct": round(profondeur, 1),
                    "retracement_unite": "ATR" if retr_mode == "atr" else "%",
                    "type": type_cle,
                    "vue": vue,
                    "idx_a": a["idx"], "idx_b": b["idx"],
                    "date_a": dates[a["idx"]], "date_b": dates[b["idx"]],
                    "prix_a": a["prix"], "prix_b": b["prix"],
                    "rsi_a": round(a["rsi"], 1), "rsi_b": round(b["rsi"], 1),
                    "rsi_idx_a": a["rsi_idx"], "rsi_idx_b": b["rsi_idx"],
                    "delta_rsi": round(d_rsi, 1),
                    "ecart_prix_pct": round(ecart_p * 100, 2),
                    "span": span,
                    "duree": classer_duree(span, params["seuils_duree"][vue]),
                    "confirmee": a["confirme"] and b["confirme"],
                    "fraicheur": n - 1 - b["idx"],
                    "score": abs(d_rsi) + span * 0.05,
                })

    return dedupliquer(candidats, params, cfg_pivot["gauche"]), rsi_serie


def dedupliquer(candidats, params, largeur_pivot):
    """
    Un même retournement génère plusieurs paires de pivots quasi identiques.
    On garde les plus significatives (RSI le plus divergent, portée la plus
    longue) en écartant celles qui pointent sur les mêmes pivots.
    """
    retenus = []
    par_type = {}
    for c in sorted(candidats, key=lambda x: (-x["idx_b"], -x["score"])):
        cle = (c["vue"], c["type"])
        deja = par_type.setdefault(cle, [])
        if len(deja) >= params["max_par_type"]:
            continue
        if any(abs(c["idx_a"] - d["idx_a"]) <= largeur_pivot
               and abs(c["idx_b"] - d["idx_b"]) <= largeur_pivot for d in deja):
            continue
        deja.append(c)
        retenus.append(c)
    return retenus


ANNEES_PERIODE = {"1y": 1, "2y": 2, "3y": 3, "5y": 5, "6mo": 1,
                  "10y": 10, "15y": 15, "20y": 20, "max": 100}


def periodes_par_vue(params, vues):
    """Profondeur demandée pour chaque vue, qu'elle soit réglée globalement
    (une chaîne, ancien format) ou vue par vue (un dictionnaire)."""
    reglage = params.get("periode_historique", "3y")
    if isinstance(reglage, str):
        return {v: reglage for v in vues}
    return {v: reglage.get(v, "3y") for v in vues}


def periode_la_plus_longue(periodes):
    return max(periodes.values(), key=lambda p: ANNEES_PERIODE.get(p, 3))


def tronquer(df, periode):
    """Ramène l'historique journalier à la profondeur voulue."""
    annees = ANNEES_PERIODE.get(periode, 3)
    if annees >= 100 or df.empty:
        return df
    depuis = df.index[-1] - pd.DateOffset(years=annees)
    return df[df.index >= depuis]


def analyser_ticker(ticker, params, vues):
    """Scanne un ticker sur les vues demandées. Retourne (divergences, erreur)."""
    periodes = periodes_par_vue(params, vues)
    df = telecharger(ticker, periode_la_plus_longue(periodes),
                     params.get("ajuster_dividendes", False))
    if df is None:
        return [], [], "téléchargement impossible"

    resultats, historique = [], []
    for vue in vues:
        # Une seule requête réseau couvre toutes les vues ; chacune est ensuite
        # ramenée à sa propre profondeur, pour que l'ajout du mensuel n'aille
        # pas gonfler l'historique du journalier au passage.
        base = tronquer(df, periodes[vue])
        data = (base if vue == "D" else
                en_hebdomadaire(base) if vue == "W" else en_mensuel(base))
        mini = params["rsi_periode"] + params["pivot"][vue]["gauche"] + params["ecart_bougies"][vue]["min"] + 5
        if len(data) < mini:
            print(f"  · {ticker} [{vue}] — historique trop court "
                  f"({len(data)} bougies, {mini} requises)")
            continue
        divergences, rsi_serie = detecter_divergences(data, vue, params)
        for d in divergences:
            d["ticker"] = ticker
            d["date_actuelle"] = data.index[-1]
            d["prix_actuel"] = float(data["Close"].iloc[-1])
            d["rsi_actuel"] = float(rsi_serie.iloc[-1])
            d["svg"] = rendre_svg(data, rsi_serie, d)
            resultats.append(d)

        # Historique : on rejoue la détection sans le plafond de déduplication,
        # qui ne garde que les figures les plus récentes et suffit au rapport
        # du jour mais tronquerait l'historique.
        cfg_h = params.get("historique", {})
        if cfg_h.get("actif", True):
            p_hist = fusionner(params, {"max_par_type": cfg_h.get("max_par_type", 40)})
            toutes, _ = detecter_divergences(data, vue, p_hist)
            for d in toutes:
                issue = issue_divergence(
                    data, d, cfg_h.get("decalage_entree", 3),
                    cfg_h.get("horizons", {}).get(vue, [5, 10, 20]),
                    cfg_h.get("horizon_objectif", {}).get(vue, 120))
                if issue is None:
                    continue
                historique.append({**{k: d[k] for k in
                                      ("vue", "type", "span", "delta_rsi", "date_a",
                                       "date_b", "prix_a", "prix_b", "rsi_a", "rsi_b",
                                       "chute_prealable")},
                                   "ticker": ticker, **issue})
    return resultats, historique, None


def issue_divergence(data, d, decalage, horizons, horizon_objectif):
    """
    Ce qu'a donné la figure, et à quel rythme.

    L'entrée est prise `decalage` bougies après le second pivot, le temps que
    la figure soit lisible. L'objectif est le niveau du PREMIER pivot : pour
    une divergence baissière il est sous le prix d'entrée, pour une haussière
    au-dessus, et le prix qui y revient signe la réalisation de la figure.

    Le rendement est compté dans le sens de la figure — une baissière gagne
    quand le prix descend — et relevé à chaque échéance de `horizons`, afin de
    voir à quel moment la figure paie plutôt que de la juger sur un seul
    instantané.
    """
    highs = data["High"].astype(float).to_numpy()
    lows = data["Low"].astype(float).to_numpy()
    clos = data["Close"].astype(float).to_numpy()
    n = len(data)

    i_e = d["idx_b"] + decalage
    if i_e >= n - 1:
        return None
    prix_e = clos[i_e]
    if not np.isfinite(prix_e) or prix_e <= 0:
        return None

    haussier = TYPES_META[d["type"]]["biais"] == "haussier"
    sens = 1 if haussier else -1
    objectif = d["prix_a"]

    # Rendement directionnel à chaque échéance ; None si l'historique s'arrête
    # avant, pour ne pas confondre « pas encore mesurable » et « nul ».
    rendements = {}
    for h in horizons:
        k = i_e + h
        rendements[h] = (sens * (clos[k] - prix_e) / prix_e * 100) if k < n else None

    # Première bougie qui touche l'objectif, et pire excursion d'ici là.
    borne = min(n - 1, i_e + horizon_objectif)
    atteint, delai, pire = False, None, 0.0
    for k in range(i_e + 1, borne + 1):
        contre = (lows[k] - prix_e) if haussier else (prix_e - highs[k])
        pire = min(pire, contre / prix_e * 100)
        if (haussier and highs[k] >= objectif) or (not haussier and lows[k] <= objectif):
            atteint, delai = True, k - i_e
            break

    # Une figure dont la fenêtre n'est pas encore écoulée n'est ni réussie ni
    # ratée : la compter comme ratée fausserait le taux de réalisation.
    tranchee = atteint or borne >= i_e + horizon_objectif

    # Les pivots d'une divergence sont presque de niveau — c'est ce qui la rend
    # lisible — et les trois bougies de décalage suffisent souvent à franchir
    # l'objectif avant même d'être entré. Une telle figure n'a plus rien à
    # offrir : la compter comme atteinte gonfle le taux de réussite sans rien
    # mesurer. On la marque, et les statistiques l'écartent.
    ecart = sens * (objectif - prix_e) / prix_e * 100
    atteignable = ecart > 0

    return {
        "date_entree": data.index[i_e],
        "prix_entree": float(prix_e),
        "objectif": float(objectif),
        "ecart_objectif": ecart,
        "atteignable": atteignable,
        "atteint": atteint,
        "delai": delai,
        "tranchee": tranchee,
        "rendements": rendements,
        "pire": float(pire),
    }


# ─── Mini-graphique SVG ───────────────────────────────────────────────────────

def rendre_svg(df, rsi_serie, d, largeur=440, h_prix=104, h_rsi=58, h_dates=15):
    """Vignette en bougies + RSI sur la fenêtre de la divergence, avec les 2 droites."""
    # Le graphique court jusqu'à la dernière bougie disponible, pas seulement
    # jusqu'au second pivot : ce qui s'est passé depuis la divergence est le
    # premier élément à regarder, et sans lui on ignore où en est le titre.
    marge = max(4, d["span"] // 6)
    debut = max(0, d["idx_a"] - marge)
    fin   = len(df) - 1
    if fin <= debut:
        return ""

    tranche = slice(debut, fin + 1)
    opens  = df["Open"].astype(float).to_numpy()[tranche] if "Open" in df.columns else None
    highs  = df["High"].astype(float).to_numpy()[tranche]
    lows   = df["Low"].astype(float).to_numpy()[tranche]
    closes = df["Close"].astype(float).to_numpy()[tranche]
    rsis   = rsi_serie.to_numpy(dtype=float)[tranche]
    dates  = df.index[tranche]
    if opens is None:
        opens = closes

    pad = 6
    n   = len(closes)
    pas = (largeur - 2 * pad) / max(1, n)
    corps = max(1.0, pas * 0.62)

    def x(idx_global):
        """Centre horizontal de la bougie."""
        return pad + (idx_global - debut + 0.5) * pas

    def echelle(valeur, vmin, vmax, haut, decalage):
        if vmax - vmin < 1e-9:
            return decalage + haut / 2
        return decalage + haut - (valeur - vmin) * (haut - 2 * pad) / (vmax - vmin) - pad

    p_min, p_max = float(np.nanmin(lows)), float(np.nanmax(highs))
    r_min, r_max = float(np.nanmin(rsis)), float(np.nanmax(rsis))
    r_min, r_max = min(r_min, 28.0), max(r_max, 72.0)

    def y_prix(v):
        return echelle(v, p_min, p_max, h_prix, 0)

    def y_rsi(v):
        return echelle(v, r_min, r_max, h_rsi, h_prix + 8)

    # ── Bougies ──
    bougies = []
    for k in range(n):
        o, h, b, c = opens[k], highs[k], lows[k], closes[k]
        if np.isnan(h) or np.isnan(b):
            continue
        coul = "#3b6d11" if c >= o else "#a32d2d"
        xc = x(debut + k)
        y_o, y_c = y_prix(o), y_prix(c)
        haut_corps = max(0.8, abs(y_o - y_c))
        bougies.append(
            f'<line x1="{xc:.1f}" y1="{y_prix(h):.1f}" x2="{xc:.1f}" y2="{y_prix(b):.1f}" '
            f'stroke="{coul}" stroke-width="0.7"/>'
            f'<rect x="{xc - corps / 2:.1f}" y="{min(y_o, y_c):.1f}" '
            f'width="{corps:.1f}" height="{haut_corps:.1f}" fill="{coul}"/>')

    ligne_rsi = " ".join(f"{x(debut + k):.1f},{y_rsi(rsis[k]):.1f}"
                         for k in range(n) if not np.isnan(rsis[k]))

    # ── Axe de dates : début, pivots, fin, sans étiquettes qui se chevauchent ──
    jours_couverts = (dates[-1] - dates[0]).days
    fmt = "%m/%y" if jours_couverts > 240 else "%d/%m"
    y_texte = h_prix + 8 + h_rsi + 11
    reperes, occupes = [], []
    # Les bornes d'abord : la date de fin est celle qui situe le titre
    # aujourd'hui. Les pivots ne prennent une étiquette que s'il reste
    # la place, leurs dates figurant déjà en colonnes du tableau.
    candidats = [(debut, "start"), (fin, "end"),
                 (d["idx_a"], "middle"), (d["idx_b"], "middle")]
    for idx, ancrage in candidats:
        xc = x(idx)
        largeur_txt = 30
        gauche = xc - (0 if ancrage == "start" else
                       largeur_txt / 2 if ancrage == "middle" else largeur_txt)
        if any(abs(gauche - g) < largeur_txt + 4 for g in occupes):
            continue
        occupes.append(gauche)
        pos = max(pad, min(largeur - pad, xc))
        reperes.append(
            f'<text x="{pos:.1f}" y="{y_texte}" font-size="9" fill="#999" '
            f'text-anchor="{ancrage}">{dates[idx - debut].strftime(fmt)}</text>')

    # repères verticaux sur les deux pivots, pour relier prix et RSI à l'œil
    verticales = "".join(
        f'<line x1="{x(i):.1f}" y1="0" x2="{x(i):.1f}" y2="{h_prix + 8 + h_rsi}" '
        f'stroke="#c9c4bb" stroke-width="0.6" stroke-dasharray="2,3"/>'
        for i in (d["idx_a"], d["idx_b"]))

    couleur = "#0f6e56" if TYPES_META[d["type"]]["biais"] == "haussier" else "#a32d2d"
    hauteur = h_prix + 8 + h_rsi + h_dates

    return f"""<svg class="mini" viewBox="0 0 {largeur} {hauteur}" width="{largeur}" height="{hauteur}">
<rect x="0" y="0" width="{largeur}" height="{h_prix}" fill="#fbfaf7"/>
<rect x="0" y="{h_prix + 8}" width="{largeur}" height="{h_rsi}" fill="#fbfaf7"/>
<line x1="0" y1="{y_rsi(70):.1f}" x2="{largeur}" y2="{y_rsi(70):.1f}" stroke="#ddd" stroke-dasharray="2,2"/>
<line x1="0" y1="{y_rsi(30):.1f}" x2="{largeur}" y2="{y_rsi(30):.1f}" stroke="#ddd" stroke-dasharray="2,2"/>
{verticales}
{"".join(bougies)}
<polyline points="{ligne_rsi}" fill="none" stroke="#378add" stroke-width="1.1"/>
<line x1="{x(d['idx_a']):.1f}" y1="{y_prix(d['prix_a']):.1f}" x2="{x(d['idx_b']):.1f}" y2="{y_prix(d['prix_b']):.1f}" stroke="{couleur}" stroke-width="1.5"/>
<line x1="{x(d['idx_a']):.1f}" y1="{y_rsi(d['rsi_a']):.1f}" x2="{x(d['idx_b']):.1f}" y2="{y_rsi(d['rsi_b']):.1f}" stroke="{couleur}" stroke-width="1.5"/>
<circle cx="{x(d['idx_a']):.1f}" cy="{y_prix(d['prix_a']):.1f}" r="2.6" fill="{couleur}"/>
<circle cx="{x(d['idx_b']):.1f}" cy="{y_prix(d['prix_b']):.1f}" r="2.6" fill="{couleur}"/>
<circle cx="{x(d['idx_a']):.1f}" cy="{y_rsi(d['rsi_a']):.1f}" r="2.6" fill="{couleur}"/>
<circle cx="{x(d['idx_b']):.1f}" cy="{y_rsi(d['rsi_b']):.1f}" r="2.6" fill="{couleur}"/>
{"".join(reperes)}
<rect x="4" y="4" width="17" height="13" rx="3" fill="#ece8e0" fill-opacity="0.92"/>
<text x="12.5" y="13.5" font-size="9" font-weight="700" fill="#6b6459" text-anchor="middle">{d['vue']}</text>
</svg>"""


# ─── Rapport HTML ─────────────────────────────────────────────────────────────

CSS = """*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f5f4f0;color:#1a1a1a;padding:2rem}
h1{font-size:22px;font-weight:500;margin-bottom:4px}
.meta{font-size:13px;color:#666;margin-bottom:1rem}
.note{font-size:12px;color:#888;background:#eef3fb;border-left:3px solid #378add;padding:8px 12px;border-radius:4px;margin-bottom:1.5rem}
.kpis{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:1.5rem}
.kpi{background:#fff;border-radius:10px;padding:14px 18px;min-width:130px;border:0.5px solid #e0ddd6}
.kpi.hauss{border-top:3px solid #0f6e56}.kpi.baiss{border-top:3px solid #a32d2d}
.kl{font-size:12px;color:#888;margin-bottom:4px}
.kv{font-size:20px;font-weight:500;color:#1a1a1a}
.kv.green{color:#0f6e56}.kv.red{color:#a32d2d}
.section-title{font-size:14px;font-weight:500;margin:1.5rem 0 10px}
.table-wrap{overflow-x:auto;border-radius:10px;border:0.5px solid #e0ddd6;margin-bottom:1rem;background:#fff}
table{width:100%;border-collapse:collapse;font-size:12px}
thead th{background:#f5f4f0;padding:8px 9px;text-align:left;font-weight:500;font-size:11px;color:#666;border-bottom:0.5px solid #e0ddd6;white-space:nowrap}
tbody td{padding:9px;border-bottom:0.5px solid #f0ede8;vertical-align:middle}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:#faf9f6}
tbody tr.hauss{border-left:3px solid #0f6e56}
tbody tr.baiss{border-left:3px solid #a32d2d}
.tk{font-weight:500;font-size:13px}
.badge{display:inline-block;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:500;white-space:nowrap}
.bg{background:#eaf3de;color:#3b6d11}.br{background:#fcebeb;color:#a32d2d}
.bn{background:#f0ede8;color:#666}
.fort{background:#fdf1e3;color:#ba7517;font-weight:600}
.vue-D{background:#e8eef7;color:#2c4a70;font-weight:600}
.vue-W{background:#f3e9f7;color:#5c2c70;font-weight:600}.bi{background:#e6f1fb;color:#185fa5}.bo{background:#fdf1e3;color:#ba7517}
.mini{border-radius:6px;border:0.5px solid #eee}
.sub{font-size:10px;color:#999;margin-top:2px}
.prio{border:1.5px solid #ba7517;border-radius:10px;padding:3px;background:#fdf6ec}
.prio .table-wrap{margin-bottom:0;border:none}
.kpi.prio-kpi{border-top:3px solid #ba7517}
.vide{background:#fff;border:0.5px solid #e0ddd6;border-radius:10px;padding:2rem;text-align:center;color:#888;font-size:13px}
.legende{background:#fff;border:0.5px solid #e0ddd6;border-radius:10px;padding:14px 18px;font-size:12px;color:#555;margin-bottom:1.5rem}
.legende div{margin-bottom:5px}.legende div:last-child{margin-bottom:0}
.footer{font-size:11px;color:#aaa;margin-top:1.5rem}
.barre-outils{display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap}
select{font-family:inherit;font-size:13px;padding:6px 10px;border-radius:7px;border:0.5px solid #d6d2ca;background:#fff;color:#1a1a1a;min-width:260px}
.resume{font-size:12.5px;color:#5d5750;background:#fff;border:0.5px solid #e0ddd6;border-radius:8px;padding:9px 13px}
.resume b{color:#1a1a1a;font-size:13px}
.delais{display:flex;gap:4px;align-items:flex-end;height:64px;margin:4px 0 2px}
.delais div{flex:1;background:#378add;border-radius:3px 3px 0 0;min-height:2px;position:relative}
.delais div span{position:absolute;top:-15px;left:0;right:0;text-align:center;font-size:10px;color:#5d5750}
.delais-leg{display:flex;gap:4px;font-size:10px;color:#8a8378}
.delais-leg div{flex:1;text-align:center}
.deux-blocs{display:flex;gap:26px;flex-wrap:wrap;margin-bottom:14px}
.deux-blocs table{font-size:12px;margin:0}"""


def badge_duree(d):
    classes = {"courte": "bn", "moyenne": "bi", "longue": "bo"}
    unite = VUES_META[d["vue"]]["unite"]
    return (f'<span class="badge {classes[d["duree"]]}">{d["duree"]} · '
            f'{d["span"]} {unite}</span>')


def libelle_fraicheur(d):
    """« dernière bougie », « hier », ou l'ancienneté en clair."""
    n = d["fraicheur"]
    if n == 0:
        return "dernière bougie"
    if n == 1:
        return "hier" if d["vue"] == "D" else "semaine dernière"
    return f'il y a {n} {VUES_META[d["vue"]]["unite"]}'


def tableau_html(liste, seuil_fort=None, zone_chute=(35.0, 50.0)):
    """
    Tableau complet pour une liste de divergences.

    `seuil_fort` marque d'une étoile les écarts de RSI les plus francs, ceux qui
    méritent d'être regardés en premier. `zone_chute` encadre l'ampleur du
    mouvement préalable qui s'est révélée la plus rentable sur l'historique :
    au-dessus, la figure est signalée comme excessive plutôt que comme forte.
    """
    lignes = ['<div class="table-wrap"><table><thead><tr>'
              '<th>Ticker</th><th>Vue</th><th>Type</th><th>Portée</th>'
              '<th>Chute avant</th>'
              '<th>Pivot 1</th><th>Pivot 2</th><th>Prix</th>'
              '<th>RSI</th><th>Δ RSI</th><th>Écart interm.</th>'
              '<th>Aujourd\'hui</th><th>Statut</th><th>Graphique</th>'
              '</tr></thead><tbody>']
    for d in liste:
        meta = TYPES_META[d["type"]]
        cls  = "hauss" if meta["biais"] == "haussier" else "baiss"
        b_delta = "bg" if d["delta_rsi"] > 0 else "br"
        fort = ('<span class="badge fort">★ forte</span>'
                if seuil_fort and abs(d["delta_rsi"]) >= seuil_fort else "")
        statut = ('<span class="badge bg">confirmée</span>' if d["confirmee"]
                  else '<span class="badge bo">en formation</span>')
        fraicheur = f'<div class="sub">{libelle_fraicheur(d)}</div>' 
        chute = d.get("chute_prealable")
        if chute is None:
            ctx = "<td>—</td>"
        else:
            # Vert dans la zone favorable, orange au-dessus : un mouvement
            # préalable démesuré n'est pas un meilleur signal mais un signal
            # moins fiable — le taux de réussite y retombe de 100 % à 68 %.
            bas, haut = zone_chute
            if chute > haut:
                ctx_cls, note = "bo", "excessive"
            elif chute >= bas:
                ctx_cls, note = "bg", "favorable"
            else:
                ctx_cls, note = "bn", ""
            ctx = (f'<td><span class="badge {ctx_cls}">{chute:.0f} %</span>'
                   + (f'<div class="sub">{note}</div>' if note else "") + '</td>')
        retr = d.get("retracement_pct", 0.0)
        unite = d.get("retracement_unite", "%")
        pivot_bas, pivot_haut = (5, 10) if unite == "ATR" else (15, 30)
        retr_cls = "bn" if retr <= pivot_bas else ("bi" if retr <= pivot_haut else "bo")
        if d.get("date_actuelle") is not None:
            depuis = (d["prix_actuel"] - d["prix_b"]) / d["prix_b"] * 100
            aujourdhui = (f'{d["prix_actuel"]:.2f} · RSI {d["rsi_actuel"]:.1f}'
                          f'<div class="sub">{d["date_actuelle"].strftime("%d/%m")} · '
                          f'{depuis:+.2f}% depuis le pivot</div>')
        else:
            aujourdhui = "—" 
        lignes.append(f"""<tr class="{cls}">
<td class="tk">{d['ticker']}</td>
<td><span class="badge vue-{d['vue']}">{d['vue']}</span></td>
<td>{meta['emoji']} {meta['court']}</td>
<td>{badge_duree(d)}</td>
{ctx}
<td>{d['date_a'].strftime('%d/%m/%Y')}</td>
<td>{d['date_b'].strftime('%d/%m/%Y')}{fraicheur}</td>
<td>{d['prix_a']:.2f} → {d['prix_b']:.2f}<div class="sub">{d['ecart_prix_pct']:+.2f}%</div></td>
<td>{d['rsi_a']:.1f} → {d['rsi_b']:.1f}</td>
<td><span class="badge {b_delta}">{d['delta_rsi']:+.1f}</span> {fort}</td>
<td><span class="badge {retr_cls}">{retr:.1f} {unite}</span></td>
<td>{aujourdhui}</td>
<td>{statut}</td>
<td>{d['svg']}</td>
</tr>""")
    lignes.append('</tbody></table></div>')
    return "".join(lignes)


def section_historique(historique, params, vues):
    """
    Historique des figures passées, filtrable par entreprise.

    Trois questions, dans cet ordre : la figure atteint-elle son objectif, en
    combien de temps, et que rapporte-t-elle en chemin. L'histogramme des
    délais répond à la deuxième, qui gouverne les deux autres — une figure qui
    met un an à se réaliser n'a pas la même valeur qu'une figure de trois
    semaines, même taux de réussite.

    Les figures dont l'objectif est déjà franchi au moment de l'entrée sont
    affichées mais exclues des statistiques : les deux pivots d'une divergence
    étant presque de niveau, le prix les dépasse souvent pendant les bougies
    de décalage, et les compter comme réussies ferait monter le taux à près de
    90 % sans rien mesurer.
    """
    cfg = params.get("historique", {})
    deca = cfg.get("decalage_entree", 3)
    horizons = cfg.get("horizons", {})
    tickers = sorted({h["ticker"] for h in historique})
    vue_ref = "D" if "D" in vues else vues[0]
    cols = horizons.get(vue_ref, [5, 10, 20])

    def stats(lignes):
        """n, % atteint, délai médian, rendement moyen par horizon."""
        utiles = [h for h in lignes if h.get("atteignable", True)]
        tranchees = [h for h in utiles if h["tranchee"]]
        atteints = [h for h in tranchees if h["atteint"]]
        delais = sorted(h["delai"] for h in atteints)
        med = delais[len(delais) // 2] if delais else None
        rend = {}
        for c in cols:
            vals = [h["rendements"].get(c) for h in utiles
                    if h["rendements"].get(c) is not None]
            rend[c] = sum(vals) / len(vals) if vals else None
        return len(lignes), len(utiles), len(tranchees), len(atteints), med, rend, delais

    def bloc_resume(lignes):
        n, n_u, n_tr, n_at, med, rend, _ = stats(lignes)
        pct = f"{n_at / n_tr * 100:.0f} %" if n_tr else "—"
        med_txt = (f"{med}</b> bougie{'s' if med > 1 else ''}"
                   if med is not None else "—</b> bougie")
        parts = " &middot; ".join(
            f"+{c} : <b>{rend[c]:+.1f} %</b>" if rend[c] is not None else f"+{c} : —"
            for c in cols)
        ecarte = (f" <span style='color:#8a8378'>({n - n_u} écartées, objectif déjà "
                  f"franchi)</span>" if n_u < n else "")
        return (f"<b>{n_u}</b> figures mesurables{ecarte} &middot; objectif atteint "
                f"<b>{pct}</b> &middot; délai médian <b>{med_txt}<br>"
                f"<span style='color:#8a8378'>rendement moyen — {parts}</span>")

    def histogramme(lignes):
        """Répartition du délai de réalisation, en cinq tranches."""
        _, _, n_tr, n_at, _, _, delais = stats(lignes)
        bornes = [(0, 5), (6, 10), (11, 20), (21, 40), (41, 10 ** 6)]
        libelles = ["1-5", "6-10", "11-20", "21-40", "41+"]
        comptes = [sum(1 for d in delais if a <= d <= b) for a, b in bornes]
        comptes.append(n_tr - n_at)          # jamais atteint
        libelles.append("jamais")
        # Mise à l'échelle sur la barre la plus haute, pas sur le total : c'est
        # la forme de la distribution qui répond à « quand », et la rapporter
        # au total l'aplatit jusqu'à la rendre illisible.
        total = max(1, max(comptes))
        barres = "".join(
            f'<div style="height:{c / total * 100:.0f}%;'
            f'{"background:#c9c4bb" if i == 5 else ""}"><span>{c or ""}</span></div>'
            for i, c in enumerate(comptes))
        legende = "".join(f"<div>{l}</div>" for l in libelles)
        return f'<div class="delais">{barres}</div><div class="delais-leg">{legende}</div>'

    def bloc_ambition(lignes):
        """
        Taux de réussite selon la distance de l'objectif.

        Un objectif à 1 % est touché par le bruit ; à 10 % il demande un vrai
        mouvement. Lire les deux colonnes ensemble dit ce que la figure peut
        raisonnablement viser, et au bout de combien de temps.
        """
        utiles = [h for h in lignes if h.get("atteignable", True)]
        rangs = [(0.0, "tous"), (1.0, "&gt; 1 %"), (3.0, "&gt; 3 %"),
                 (5.0, "&gt; 5 %"), (10.0, "&gt; 10 %")]
        corps = ""
        for seuil, lib in rangs:
            s = [h for h in utiles if h["ecart_objectif"] > seuil]
            tr = [h for h in s if h["tranchee"]]
            at = [h for h in tr if h["atteint"]]
            if not tr:
                continue
            d = sorted(h["delai"] for h in at)
            med = d[len(d) // 2] if d else None
            corps += (f"<tr><td>{lib}</td><td>{len(s)}</td>"
                      f"<td><span class='badge "
                      f"{'bg' if len(at) / len(tr) >= 0.5 else 'bn'}'>"
                      f"{len(at) / len(tr) * 100:.0f} %</span></td>"
                      f"<td>{med if med is not None else '—'}</td></tr>")
        return ("<table><thead><tr><th>Objectif à</th><th>Figures</th>"
                "<th>Atteint</th><th>Délai médian</th></tr></thead>"
                f"<tbody>{corps}</tbody></table>")

    n_u = sum(1 for h in historique if h.get("atteignable", True))
    out = ['<div class="section-title">Historique des divergences — '
           f'{len(historique)} figures</div>']
    out.append(
        '<p class="note">Pour chaque figure passée, l\'objectif est le niveau de prix du '
        '<b>premier pivot</b> : la figure est réalisée quand le prix y revient. '
        f'L\'entrée est prise <b>{deca} bougies après</b> le signal. '
        'Le rendement est compté dans le sens de la figure — une baissière gagne quand le '
        'prix descend — et relevé à plusieurs échéances, pour voir <b>à quel moment</b> '
        'elle paie. L\'histogramme donne la répartition des délais de réalisation : '
        'c\'est lui qui dit quand la divergence se valide le plus souvent.<br>'
        f'Les <b>{len(historique) - n_u}</b> figures dont l\'objectif était déjà franchi '
        f'après les {deca} bougies de décalage restent dans le tableau, marquées '
        '<span class="badge bo">déjà franchi</span>, mais sont exclues des statistiques : '
        'les deux pivots d\'une divergence sont presque de niveau, le prix les dépasse '
        'donc souvent tout seul, et les compter comme réussies gonflerait le taux sans '
        'rien mesurer.</p>')

    out.append('<div class="barre-outils">')
    out.append('<label for="selTicker" style="font-size:13px;color:#5d5750">Entreprise</label>')
    out.append('<select id="selTicker" onchange="filtrerHistorique()">')
    out.append(f'<option value="*">Toutes — {len(historique)} figures</option>')
    for tk in tickers:
        _, n_u, n_tr, n_at, med, _, _ = stats([h for h in historique
                                               if h["ticker"] == tk])
        pct = f"{n_at / n_tr * 100:.0f} %" if n_tr else "—"
        out.append(f'<option value="{tk}">{tk} — {n_u} mesurables, {pct} atteintes</option>')
    out.append('</select>')
    out.append(f'<span class="resume" id="resumeHist">{bloc_resume(historique)}</span>')
    out.append('</div>')

    out.append('<div class="deux-blocs">')
    out.append('<div style="flex:1 1 320px;max-width:420px">'
               '<div style="font-size:12px;color:#5d5750;margin-bottom:16px">'
               'Délai de réalisation (bougies)</div>'
               f'<div id="histoHist">{histogramme(historique)}</div></div>')
    out.append('<div style="flex:1 1 320px;max-width:420px">'
               '<div style="font-size:12px;color:#5d5750;margin-bottom:6px">'
               'Réussite selon l\'ambition de l\'objectif</div>'
               f'<div id="ambiHist">{bloc_ambition(historique)}</div></div>')
    out.append('</div>')

    entetes = "".join(f"<th>+{c}</th>" for c in cols)
    out.append('<div class="table-wrap"><table><thead><tr>'
               '<th>Ticker</th><th>Vue</th><th>Type</th><th>Signal</th><th>Portée</th>'
               '<th>Δ RSI</th><th>Entrée</th><th>Objectif</th><th>Écart visé</th>'
               f'<th>Atteint</th><th>Délai</th>{entetes}<th>Pire moment</th>'
               '</tr></thead><tbody>')

    for h in sorted(historique, key=lambda x: (x["ticker"], x["date_b"])):
        meta = TYPES_META[h["type"]]
        cls = "hauss" if meta["biais"] == "haussier" else "baiss"
        if not h.get("atteignable", True):
            att = '<span class="badge bo">déjà franchi</span>'
        elif not h["tranchee"]:
            att = '<span class="badge bo">en cours</span>'
        elif h["atteint"]:
            att = '<span class="badge bg">oui</span>'
        else:
            att = '<span class="badge bn">non</span>'
        unite = VUES_META[h["vue"]]["unite"]
        # Un objectif déjà franchi est touché à la première bougie par
        # construction : afficher ce « 1 » laisserait croire à un délai mesuré.
        delai_txt = (h["delai"] if h["delai"] is not None
                     and h.get("atteignable", True) else "—")
        cells = ""
        for c in horizons.get(h["vue"], cols):
            v = h["rendements"].get(c)
            cells += ('<td>—</td>' if v is None else
                      f'<td><span class="badge {"bg" if v > 0 else "br"}">{v:+.1f}</span></td>')
        out.append(
            f'<tr class="{cls}" data-hist-ticker="{h["ticker"]}">'
            f'<td class="tk">{h["ticker"]}</td>'
            f'<td><span class="badge vue-{h["vue"]}">{h["vue"]}</span></td>'
            f'<td>{meta["emoji"]} {meta["court"]}</td>'
            f'<td>{h["date_b"].strftime("%d/%m/%Y")}</td>'
            f'<td>{h["span"]} {unite}</td>'
            f'<td><span class="badge {"bg" if h["delta_rsi"] > 0 else "br"}">'
            f'{h["delta_rsi"]:+.1f}</span></td>'
            f'<td>{h["prix_entree"]:.2f}<div class="sub">'
            f'{h["date_entree"].strftime("%d/%m/%y")}</div></td>'
            f'<td>{h["objectif"]:.2f}</td>'
            f'<td>{h["ecart_objectif"]:+.1f} %</td>'
            f'<td>{att}</td>'
            f'<td>{delai_txt}</td>'
            f'{cells}'
            f'<td><span class="badge bn">{h["pire"]:+.1f} %</span></td></tr>')
    out.append('</tbody></table></div>')

    # Résumés et histogrammes pré-calculés : le filtre ne fait que masquer des
    # lignes et recopier le bloc correspondant, sans recalcul côté navigateur.
    def paquet(lignes):
        return {"resume": bloc_resume(lignes), "histo": histogramme(lignes),
                "ambi": bloc_ambition(lignes)}

    donnees = {"*": paquet(historique)}
    for tk in tickers:
        donnees[tk] = paquet([h for h in historique if h["ticker"] == tk])

    out.append("<script>const HIST = " + json.dumps(donnees, ensure_ascii=False) + ";")
    out.append("function filtrerHistorique() {"
               "  const v = document.getElementById('selTicker').value;"
               "  document.querySelectorAll('[data-hist-ticker]').forEach(function (el) {"
               "    el.style.display = (v === '*' || el.dataset.histTicker === v) ? '' : 'none';"
               "  });"
               "  const d = HIST[v];"
               "  if (d) {"
               "    document.getElementById('resumeHist').innerHTML = d.resume;"
               "    document.getElementById('histoHist').innerHTML = d.histo;"
               "    document.getElementById('ambiHist').innerHTML = d.ambi;"
               "  }"
               "}</script>")
    return "\n".join(out)


def generer_html(divergences, tickers, vues, erreurs, params, chemin,
                 anciennes=0, avec_confirmees=False, confirmees_ecartees=0,
                 historique=None):
    maintenant = datetime.now()
    r = params.get("retracement", {})
    retr_txt = (f"{r.get('max_atr')} ATR" if r.get("mode") == "atr"
                else f"{r.get('max_pct')} %")
    zone = params.get("zone_rsi", {})
    zone_txt = ""
    if zone.get("actif", True):
        detail = " / ".join(f"{v} : {zone[v]['survente']:.0f}–{zone[v]['surachat']:.0f}"
                            for v in vues if v in zone)
        cible = ("les deux pivots" if zone.get("mode") == "les_deux_pivots"
                 else "le premier pivot")
        zone_txt = f" · RSI extrême exigé sur {cible} ({detail})"
    hauss = [d for d in divergences if TYPES_META[d["type"]]["biais"] == "haussier"]
    baiss = [d for d in divergences if TYPES_META[d["type"]]["biais"] == "baissier"]
    recentes = [d for d in divergences
                if d["fraicheur"] <= params["telegram"]["fraicheur_max_bougies"][d["vue"]]]
    a_surveiller_kpi = [d for d in divergences if not d["confirmee"]]

    html = [f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<title>Divergences RSI — {maintenant.strftime('%d/%m/%Y')}</title>
<style>
{CSS}
</style>
</head>
<body>
<h1>Divergences RSI
<span style="font-size:14px;padding:3px 10px;border-radius:4px;background:#f0ede8;color:#555;margin-left:8px">{' + '.join(vues)}</span></h1>
<p class="meta">{len(tickers)} tickers analysés &nbsp;|&nbsp; RSI({params['rsi_periode']}) Wilder &nbsp;|&nbsp; historique {params['periode_historique']} &nbsp;|&nbsp; généré le {maintenant.strftime('%d/%m/%Y %H:%M')}</p>
<p class="note">Pivots : {params['pivot']['D']['gauche']}/{params['pivot']['D']['droite']} bougies en vue D, {params['pivot']['W']['gauche']}/{params['pivot']['W']['droite']} en vue W · Portée appariée de {params['ecart_bougies']['D']['min']} à {params['ecart_bougies']['D']['max']} bougies (D) — les divergences longues comme courtes sont détectées · Un pivot « en formation » n'a pas encore sa fenêtre droite complète et peut être invalidé par les prochaines bougies.<br>
Filtres anti-bruit : écart intermédiaire &le; {retr_txt} (deux pivots séparés par un mouvement plus ample appartiennent à des phases différentes){zone_txt}.</p>

<div class="kpis">
<div class="kpi"><div class="kl">Divergences</div><div class="kv">{len(divergences)}</div></div>
<div class="kpi hauss"><div class="kl">Haussières</div><div class="kv green">{len(hauss)}</div></div>
<div class="kpi baiss"><div class="kl">Baissières</div><div class="kv red">{len(baiss)}</div></div>
<div class="kpi prio-kpi"><div class="kl">En formation</div><div class="kv">{len(a_surveiller_kpi)}</div></div>
<div class="kpi"><div class="kl">Récentes (alertées)</div><div class="kv">{len(recentes)}</div></div>
<div class="kpi"><div class="kl">Confirmées écartées</div><div class="kv">{confirmees_ecartees}</div></div>
<div class="kpi"><div class="kl">Anciennes écartées</div><div class="kv">{anciennes}</div></div>
<div class="kpi"><div class="kl">Tickers en échec</div><div class="kv">{len(erreurs)}</div></div>
</div>"""]

    actifs = [c for c in TYPES_META if params["types"].get(c)]
    html.append('<div class="legende">')
    for cle in actifs:
        m = TYPES_META[cle]
        html.append(f'<div>{m["emoji"]} <b>{m["label"]}</b> — {m["explication"]}</div>')
    html.append('</div>')

    # ── Section prioritaire : uniquement ce qui est en train de se former.
    #    Inutile quand le rapport n'en contient déjà pas d'autres : on éviterait
    #    juste d'afficher deux fois les mêmes lignes. ──
    seuil_fort = params["prioritaire"]["rsi_delta_fort"]
    zone_chute = tuple(params.get("contexte", {}).get("zone_favorable", [35.0, 50.0]))
    en_formation = [d for d in divergences if not d["confirmee"]]
    if avec_confirmees:
        fortes = [d for d in en_formation if abs(d["delta_rsi"]) >= seuil_fort]
        html.append(f'<div class="section-title">En formation '
                    f'— {len(en_formation)} divergence(s), dont {len(fortes)} '
                    f'à RSI marqué (&ge; {seuil_fort:.0f} points)</div>')
        if en_formation:
            html.append('<div class="prio">')
            html.append(tableau_html(
                sorted(en_formation, key=lambda x: (-abs(x["delta_rsi"]), x["fraicheur"])),
                seuil_fort, zone_chute))
            html.append('</div>')
        else:
            html.append('<div class="vide">Aucune divergence en cours de formation.</div>')

    for vue in vues:
        du_vue = [d for d in divergences if d["vue"] == vue]
        suffixe = "" if avec_confirmees else " — en formation"
        html.append(f'<div class="section-title">{VUES_META[vue]["label"]}{suffixe} '
                    f'— {len(du_vue)} divergence(s)</div>')
        if not du_vue:
            html.append('<div class="vide">Aucune divergence détectée sur cette vue.</div>')
            continue

        cle = ((lambda x: (x["fraicheur"], x["ticker"])) if avec_confirmees
               else (lambda x: (-abs(x["delta_rsi"]), x["fraicheur"])))
        html.append(tableau_html(sorted(du_vue, key=cle), seuil_fort, zone_chute))

    if historique:
        html.append(section_historique(historique, params, vues))

    if erreurs:
        html.append('<div class="section-title">Tickers non analysés</div>')
        html.append('<div class="table-wrap"><table><thead><tr>'
                    '<th>Ticker</th><th>Raison</th></tr></thead><tbody>')
        for ticker, raison in erreurs:
            html.append(f'<tr><td class="tk">{ticker}</td><td>{raison}</td></tr>')
        html.append('</tbody></table></div>')

    html.append('<p class="footer">RSI Divergence Scanner — le prix des pivots est '
                'lu sur les mèches (Low/High), le RSI sur la clôture.</p>')
    html.append('</body></html>')

    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text("\n".join(html), encoding="utf-8")
    return chemin


# ─── Telegram ─────────────────────────────────────────────────────────────────

def identifiants_telegram(config):
    """Les variables d'environnement priment sur config.json."""
    tg = config.get("telegram", {})
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or tg.get("bot_token")
    chat  = os.environ.get("TELEGRAM_CHAT_ID")   or tg.get("chat_id")
    return token, chat


def envoyer_telegram(message, config):
    token, chat_id = identifiants_telegram(config)
    if not token or not chat_id or "REMPLACE" in str(token):
        print("  ! Telegram non configuré — message non envoyé")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"  ! Telegram HTTP {resp.status_code} : {resp.text[:120]}")
        return resp.status_code == 200
    except Exception as e:
        print(f"  ! Telegram exception : {e}")
        return False


def message_divergence(d):
    meta  = TYPES_META[d["type"]]
    unite = VUES_META[d["vue"]]["unite"]
    statut = "confirmée" if d["confirmee"] else "en formation (non confirmée)"
    return (
        f"{meta['emoji']} <b>{meta['label']} — {d['ticker']}</b>\n"
        f"Vue {d['vue']} · portée {d['duree']} ({d['span']} {unite}) · {statut}\n\n"
        f"Pivot 1 — {d['date_a'].strftime('%d/%m/%Y')} : "
        f"prix {d['prix_a']:.2f} | RSI {d['rsi_a']:.1f}\n"
        f"Pivot 2 — {d['date_b'].strftime('%d/%m/%Y')} : "
        f"prix {d['prix_b']:.2f} | RSI {d['rsi_b']:.1f}\n\n"
        f"Prix {d['ecart_prix_pct']:+.2f}% | RSI {d['delta_rsi']:+.1f} pts\n"
        f"{meta['explication']}\n\n"
        f"📅 {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )


# ─── État (anti-doublons entre deux lancements) ───────────────────────────────

def signature(d):
    return (f"{d['ticker']}|{d['vue']}|{d['type']}|"
            f"{d['date_a'].strftime('%Y%m%d')}|{d['date_b'].strftime('%Y%m%d')}")


def charger_etat():
    if not ETAT_FILE.exists():
        return set()
    try:
        return set(json.loads(ETAT_FILE.read_text(encoding="utf-8")).get("envoyees", []))
    except Exception:
        return set()


def sauver_etat(signatures):
    ETAT_FILE.write_text(
        json.dumps({"maj": datetime.now().isoformat(),
                    "envoyees": sorted(signatures)}, indent=2),
        encoding="utf-8",
    )


# ─── Point d'entrée ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Détecte les divergences RSI en vue D et W sur la watchlist.",
        formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--vue", default="DWM",
                        help="Vues à scanner : combinaison de D, W et M "
                             "(défaut : DWM)")
    parser.add_argument("--tickers", default=None,
                        help="Liste de tickers séparés par des virgules "
                             "(défaut : tickers.txt)")
    parser.add_argument("--no-telegram", action="store_true",
                        help="Génère le rapport sans envoyer d'alerte")
    parser.add_argument("--reset-etat", action="store_true",
                        help="Oublie les divergences déjà alertées et tout renvoyer")
    parser.add_argument("--toutes", action="store_true",
                        help="Alerte sur toutes les divergences, même anciennes")
    parser.add_argument("--confirmees", action="store_true",
                        help="Inclut aussi les divergences déjà confirmées")
    parser.add_argument("--historique", action="store_true",
                        help="Garde aussi les divergences anciennes dans le rapport")
    parser.add_argument("--sortie", default=None,
                        help="Chemin du rapport HTML")
    parser.add_argument("--ouvrir", action="store_true",
                        help="Ouvre le rapport dans le navigateur à la fin")
    args = parser.parse_args()

    config = charger_config()
    params = config["divergence"]

    vues = [v for v in ("D", "W", "M")
            if v in args.vue.upper() and params["vues"].get(v, True)]
    if not vues:
        print("Aucune vue active — vérifie --vue et config.json > divergence.vues")
        return 1

    tickers = ([t.strip().upper() for t in args.tickers.split(",") if t.strip()]
               if args.tickers else charger_tickers())
    if not tickers:
        print("Watchlist vide — remplis tickers.txt")
        return 1

    if args.reset_etat and ETAT_FILE.exists():
        ETAT_FILE.unlink()

    print(f"=== Scan divergences RSI — {len(tickers)} tickers, vues {'+'.join(vues)} ===")

    divergences, historique, erreurs = [], [], []
    for i, ticker in enumerate(tickers, 1):
        print(f"[{i}/{len(tickers)}] {ticker}...")
        try:
            trouvees, hist, erreur = analyser_ticker(ticker, params, vues)
        except Exception as e:
            trouvees, hist, erreur = [], [], f"erreur d'analyse : {e}"
        if erreur:
            erreurs.append((ticker, erreur))
            continue
        for d in trouvees:
            meta = TYPES_META[d["type"]]
            print(f"  {meta['emoji']} [{d['vue']}] {meta['court']} — "
                  f"{d['duree']} ({d['span']}) — "
                  f"{d['date_a'].strftime('%d/%m/%y')} → {d['date_b'].strftime('%d/%m/%y')} "
                  f"| RSI {d['delta_rsi']:+.1f}")
        divergences.extend(trouvees)
        historique.extend(hist)
        time.sleep(0.3)

    # ── On écarte l'historique ancien : une divergence vieille de deux ans
    #    n'a plus d'intérêt opérationnel, elle a déjà joué ou échoué. ──
    confirmees_ecartees = 0
    if not args.confirmees:
        avant_c = len(divergences)
        divergences = [d for d in divergences if not d["confirmee"]]
        confirmees_ecartees = avant_c - len(divergences)
        if confirmees_ecartees:
            print(f"\n({confirmees_ecartees} divergence(s) déjà confirmée(s) écartée(s) "
                  f"— --confirmees pour les voir)")

    anciennes = 0
    if not args.historique:
        # Les seuils se construisent sur les vues actives et non sur une liste
        # figée : ajouter une vue ne doit pas faire tomber le filtre.
        seuils = {v: max(params["rapport"]["fraicheur_max_bougies"].get(v, 40),
                         params["telegram"]["fraicheur_max_bougies"].get(v, 10))
                  for v in vues}
        avant_filtre = len(divergences)
        divergences = [d for d in divergences if d["fraicheur"] <= seuils[d["vue"]]]
        anciennes = avant_filtre - len(divergences)
        if anciennes:
            detail = " / ".join(f"{seuils[v]} {VUES_META[v]['unite']}" for v in vues)
            print(f"\n({anciennes} divergence(s) trop ancienne(s) écartée(s) — "
                  f"au-delà de {detail}, --historique pour les voir)")

    # ── Rapport ──
    horodatage = datetime.now().strftime("%Y%m%d_%H%M")
    chemin = Path(args.sortie) if args.sortie else RAPPORTS_DIR / f"divergences_{horodatage}.html"
    generer_html(divergences, tickers, vues, erreurs, params, chemin,
                 anciennes, args.confirmees, confirmees_ecartees, historique)
    print(f"\n📄 Rapport : {chemin}")
    if args.ouvrir:
        webbrowser.open(chemin.resolve().as_uri())

    # ── Alertes Telegram ──
    if args.no_telegram or not params["telegram"]["actif"]:
        print(f"✓ {len(divergences)} divergence(s) — Telegram désactivé")
        return 0

    deja_envoyees = charger_etat()
    a_alerter = []
    for d in divergences:
        if not args.toutes:
            if d["fraicheur"] > params["telegram"]["fraicheur_max_bougies"][d["vue"]]:
                continue
            if params["telegram"]["confirmees_seulement"] and not d["confirmee"]:
                continue
        if signature(d) in deja_envoyees:
            continue
        a_alerter.append(d)

    envoyees = 0
    for d in sorted(a_alerter, key=lambda x: (x["vue"], x["ticker"])):
        if envoyer_telegram(message_divergence(d), config):
            deja_envoyees.add(signature(d))
            envoyees += 1
        time.sleep(0.5)

    sauver_etat(deja_envoyees)
    print(f"✓ {len(divergences)} divergence(s) détectée(s) — "
          f"{envoyees} alerte(s) Telegram envoyée(s)")
    if erreurs:
        print(f"⚠ {len(erreurs)} ticker(s) non analysé(s) : "
              f"{', '.join(t for t, _ in erreurs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
