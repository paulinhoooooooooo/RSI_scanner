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
    "periode_historique": "3y",
    "rsi_periode": 14,
    "vues": {"D": True, "W": True},
    "pivot": {
        "D": {"gauche": 5, "droite": 5},
        "W": {"gauche": 3, "droite": 3},
    },
    "ecart_bougies": {
        "D": {"min": 5, "max": 160},
        "W": {"min": 4, "max": 60},
    },
    "seuils_duree": {
        "D": {"courte": 15, "moyenne": 40},
        "W": {"courte": 4, "moyenne": 12},
    },
    "rsi_delta_min": 4.0,
    "retracement_max_pct": 45.0,
    "zone_rsi": {"actif": True, "surachat": 60.0, "survente": 40.0},
    "tolerance_rsi_pivot_bougies": 3,
    "deplacement_max_rsi_bougies": {"D": 8, "W": 4},
    "verifier_ligne": True,
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
        "fraicheur_max_bougies": {"D": 40, "W": 13},
    },
    "prioritaire": {
        "fraicheur_confirmees": {"D": 5, "W": 1},
    },
    "telegram": {
        "actif": True,
        "fraicheur_max_bougies": {"D": 10, "W": 4},
        "confirmees_seulement": True,
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
}


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
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)
    config["divergence"] = fusionner(DEFAUTS, config.get("divergence"))
    return config


def charger_tickers():
    tickers = []
    with open(TICKERS_FILE, "r", encoding="utf-8") as f:
        for ligne in f:
            ligne = ligne.strip()
            if ligne and not ligne.startswith("#"):
                tickers.append(ligne.upper())
    return list(dict.fromkeys(tickers))


# ─── Données & indicateurs ────────────────────────────────────────────────────

def telecharger(ticker, periode):
    """Télécharge l'historique journalier. Retourne None si indisponible."""
    try:
        df = yf.download(ticker, period=periode, interval="1d",
                         progress=False, auto_adjust=True)
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
    """Agrège les bougies journalières en bougies hebdomadaires (clôture vendredi)."""
    regles = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    if "Volume" in df.columns:
        regles["Volume"] = "sum"
    hebdo = df.resample("W-FRI").agg(regles).dropna(subset=["High", "Low", "Close"])
    return hebdo


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
    est incomplète (fin de série) est marqué `confirme=False` : il est réel
    aujourd'hui mais peut être invalidé par les bougies à venir.
    """
    n = len(valeurs)
    pivots = []
    for i in range(gauche, n):
        dispo = min(droite, n - 1 - i)
        if dispo < 1 or (not autoriser_provisoire and dispo < droite):
            continue
        v = valeurs[i]
        if np.isnan(v):
            continue
        fen_g = valeurs[i - gauche:i]
        fen_d = valeurs[i + 1:i + 1 + dispo]
        if sens == "bas":
            ok = np.all(v < fen_g) and np.all(v <= fen_d)
        else:
            ok = np.all(v > fen_g) and np.all(v >= fen_d)
        if ok:
            pivots.append({"idx": i, "prix": float(v), "confirme": dispo >= droite})
    return pivots


def rsi_au_pivot(rsi_vals, idx, tolerance, sens, deplacement_max):
    """
    Valeur du RSI correspondant à un pivot de prix.

    L'extremum du RSI est rarement aligné sur celui du prix, et il peut en être
    éloigné de plusieurs bougies. Se contenter de la valeur dans une fenêtre
    étroite fait tomber la droite du RSI sur une pente au lieu du sommet ou du
    creux — le tracé devient faux.

    On part donc du pivot de prix et on remonte de proche en proche jusqu'à
    l'extremum local du RSI : à chaque tour on cherche l'extremum dans une
    fenêtre de +/- `tolerance`, on s'y recentre, et on s'arrête dès qu'on n'a
    plus bougé. Le déplacement total reste borné par `deplacement_max`, faute de
    quoi on dériverait vers un extremum sans rapport avec le pivot de départ.
    """
    n = len(rsi_vals)
    pos = idx
    for _ in range(deplacement_max + 1):
        a = max(0, pos - tolerance)
        b = min(n, pos + tolerance + 1)
        fenetre = rsi_vals[a:b]
        if np.all(np.isnan(fenetre)):
            return None, None
        j = int(np.nanargmin(fenetre)) if sens == "bas" else int(np.nanargmax(fenetre))
        candidat = a + j
        if candidat == pos or abs(candidat - idx) > deplacement_max:
            break
        pos = candidat
    if np.isnan(rsi_vals[pos]):
        return None, None
    return float(rsi_vals[pos]), pos


def ligne_cassee(valeurs, i1, v1, i2, v2, sens, tolerance_abs):
    """
    Vrai si la droite reliant les deux pivots est traversée entre eux.

    Une divergence dont la ligne de tendance est cassée par une bougie
    intermédiaire n'est pas une divergence valide : les deux points n'appartiennent
    pas au même mouvement.
    """
    if i2 <= i1 + 1:
        return False
    for k in range(i1 + 1, i2):
        v = valeurs[k]
        if np.isnan(v):
            continue
        ligne = v1 + (v2 - v1) * (k - i1) / (i2 - i1)
        marge = tolerance_abs(ligne)
        if sens == "bas" and v < ligne - marge:
            return True
        if sens == "haut" and v > ligne + marge:
            return True
    return False


def excursion_intermediaire(lows, highs, i1, i2, v1, v2, sens):
    """
    Profondeur, en %, du plus grand écart du prix entre les deux pivots.

    Pour une divergence de sommets on mesure jusqu'où le prix est descendu sous
    le plus bas des deux sommets ; pour une divergence de creux, jusqu'où il est
    monté au-dessus du plus haut des deux creux. Une valeur élevée signale deux
    mouvements distincts plutôt qu'une même structure qui s'essouffle.
    """
    if i2 <= i1 + 1:
        return 0.0
    if sens == "haut":
        reference = min(v1, v2)
        extreme   = float(np.nanmin(lows[i1 + 1:i2]))
        if reference <= 0:
            return 0.0
        return max(0.0, (reference - extreme) / reference * 100)
    reference = max(v1, v2)
    extreme   = float(np.nanmax(highs[i1 + 1:i2]))
    if reference <= 0:
        return 0.0
    return max(0.0, (extreme - reference) / reference * 100)


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
    cfg_ecart = params["ecart_bougies"][vue]
    tol_rsi_p = params["tolerance_rsi_pivot_bougies"]
    depl_rsi  = params["deplacement_max_rsi_bougies"][vue]
    delta_min = params["rsi_delta_min"]
    cfg_retr  = params.get("retracement_max_pct", 0)
    cfg_zone  = params.get("zone_rsi", {})

    pivots_bas  = detecter_pivots(lows,  cfg_pivot["gauche"], cfg_pivot["droite"],
                                  "bas",  params["autoriser_pivot_provisoire"])
    pivots_haut = detecter_pivots(highs, cfg_pivot["gauche"], cfg_pivot["droite"],
                                  "haut", params["autoriser_pivot_provisoire"])

    # RSI associé à chaque pivot de prix
    for p in pivots_bas:
        p["rsi"], p["rsi_idx"] = rsi_au_pivot(rsi_vals, p["idx"], tol_rsi_p, "bas", depl_rsi)
    for p in pivots_haut:
        p["rsi"], p["rsi_idx"] = rsi_au_pivot(rsi_vals, p["idx"], tol_rsi_p, "haut", depl_rsi)

    pivots_bas  = [p for p in pivots_bas  if p["rsi"] is not None]
    pivots_haut = [p for p in pivots_haut if p["rsi"] is not None]

    candidats = []

    for type_cle, meta in TYPES_META.items():
        if not params["types"].get(type_cle, False):
            continue

        sens    = meta["sens"]
        pivots  = pivots_bas if sens == "bas" else pivots_haut
        serie_p = lows if sens == "bas" else highs
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
                    if ligne_cassee(serie_p, a["idx"], a["prix"], b["idx"], b["prix"],
                                    sens, lambda ligne: abs(ligne) * tol_p):
                        continue
                    if ligne_cassee(rsi_vals, a["rsi_idx"], a["rsi"], b["rsi_idx"], b["rsi"],
                                    sens, lambda _ligne: tol_r):
                        continue

                # Filtre de retracement : deux sommets séparés par une chute
                # profonde appartiennent à des phases de marché différentes.
                # Les relier donne une droite valide mais sans portée pratique.
                profondeur = excursion_intermediaire(lows, highs, a["idx"], b["idx"],
                                                     a["prix"], b["prix"], sens)
                if cfg_retr and profondeur > cfg_retr:
                    continue

                # Filtre de zone : une divergence baissière n'a de sens que si le
                # RSI a atteint le surachat, et inversement en survente.
                if cfg_zone.get("actif", True):
                    if sens == "bas":
                        if min(a["rsi"], b["rsi"]) > cfg_zone["survente"]:
                            continue
                    else:
                        if max(a["rsi"], b["rsi"]) < cfg_zone["surachat"]:
                            continue

                candidats.append({
                    "retracement_pct": round(profondeur, 1),
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


def analyser_ticker(ticker, params, vues):
    """Scanne un ticker sur les vues demandées. Retourne (divergences, erreur)."""
    df = telecharger(ticker, params["periode_historique"])
    if df is None:
        return [], "téléchargement impossible"

    resultats = []
    for vue in vues:
        data = df if vue == "D" else en_hebdomadaire(df)
        mini = params["rsi_periode"] + params["pivot"][vue]["gauche"] + params["ecart_bougies"][vue]["min"] + 5
        if len(data) < mini:
            print(f"  · {ticker} [{vue}] — historique trop court "
                  f"({len(data)} bougies, {mini} requises)")
            continue
        divergences, rsi_serie = detecter_divergences(data, vue, params)
        for d in divergences:
            d["ticker"] = ticker
            d["svg"] = rendre_svg(data, rsi_serie, d)
            resultats.append(d)
    return resultats, None


# ─── Mini-graphique SVG ───────────────────────────────────────────────────────

def rendre_svg(df, rsi_serie, d, largeur=440, h_prix=104, h_rsi=58, h_dates=15):
    """Vignette en bougies + RSI sur la fenêtre de la divergence, avec les 2 droites."""
    marge = max(4, d["span"] // 6)
    debut = max(0, d["idx_a"] - marge)
    fin   = min(len(df) - 1, d["idx_b"] + marge)
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
    candidats = [(debut, "start"), (d["idx_a"], "middle"),
                 (d["idx_b"], "middle"), (fin, "end")]
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
<line x1="{x(d['rsi_idx_a']):.1f}" y1="{y_rsi(d['rsi_a']):.1f}" x2="{x(d['rsi_idx_b']):.1f}" y2="{y_rsi(d['rsi_b']):.1f}" stroke="{couleur}" stroke-width="1.5"/>
<circle cx="{x(d['idx_a']):.1f}" cy="{y_prix(d['prix_a']):.1f}" r="2.6" fill="{couleur}"/>
<circle cx="{x(d['idx_b']):.1f}" cy="{y_prix(d['prix_b']):.1f}" r="2.6" fill="{couleur}"/>
<circle cx="{x(d['rsi_idx_a']):.1f}" cy="{y_rsi(d['rsi_a']):.1f}" r="2.6" fill="{couleur}"/>
<circle cx="{x(d['rsi_idx_b']):.1f}" cy="{y_rsi(d['rsi_b']):.1f}" r="2.6" fill="{couleur}"/>
{"".join(reperes)}
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
.footer{font-size:11px;color:#aaa;margin-top:1.5rem}"""


def badge_duree(d):
    classes = {"courte": "bn", "moyenne": "bi", "longue": "bo"}
    unite = VUES_META[d["vue"]]["unite"]
    return (f'<span class="badge {classes[d["duree"]]}">{d["duree"]} · '
            f'{d["span"]} {unite}</span>')


def tableau_html(liste):
    """Tableau complet pour une liste de divergences."""
    lignes = ['<div class="table-wrap"><table><thead><tr>'
              '<th>Ticker</th><th>Vue</th><th>Type</th><th>Portée</th>'
              '<th>Pivot 1</th><th>Pivot 2</th><th>Prix</th>'
              '<th>RSI</th><th>Δ RSI</th><th>Écart interm.</th>'
              '<th>Statut</th><th>Graphique</th>'
              '</tr></thead><tbody>']
    for d in liste:
        meta = TYPES_META[d["type"]]
        cls  = "hauss" if meta["biais"] == "haussier" else "baiss"
        b_delta = "bg" if d["delta_rsi"] > 0 else "br"
        statut = ('<span class="badge bg">confirmée</span>' if d["confirmee"]
                  else '<span class="badge bo">en formation</span>')
        fraicheur = (f'<div class="sub">il y a {d["fraicheur"]} '
                     f'{VUES_META[d["vue"]]["unite"]}</div>')
        retr = d.get("retracement_pct", 0.0)
        retr_cls = "bn" if retr <= 15 else ("bi" if retr <= 30 else "bo")
        lignes.append(f"""<tr class="{cls}">
<td class="tk">{d['ticker']}</td>
<td><span class="badge vue-{d['vue']}">{d['vue']}</span></td>
<td>{meta['emoji']} {meta['court']}</td>
<td>{badge_duree(d)}</td>
<td>{d['date_a'].strftime('%d/%m/%Y')}</td>
<td>{d['date_b'].strftime('%d/%m/%Y')}{fraicheur}</td>
<td>{d['prix_a']:.2f} → {d['prix_b']:.2f}<div class="sub">{d['ecart_prix_pct']:+.2f}%</div></td>
<td>{d['rsi_a']:.1f} → {d['rsi_b']:.1f}</td>
<td><span class="badge {b_delta}">{d['delta_rsi']:+.1f}</span></td>
<td><span class="badge {retr_cls}">{retr:.1f}%</span></td>
<td>{statut}</td>
<td>{d['svg']}</td>
</tr>""")
    lignes.append('</tbody></table></div>')
    return "".join(lignes)


def generer_html(divergences, tickers, vues, erreurs, params, chemin, anciennes=0):
    maintenant = datetime.now()
    zone = params.get("zone_rsi", {})
    zone_txt = ("" if not zone.get("actif", True) else
                f" · RSI en zone (&ge; {zone['surachat']:.0f} pour une baissière,"
                f" &le; {zone['survente']:.0f} pour une haussière)")
    hauss = [d for d in divergences if TYPES_META[d["type"]]["biais"] == "haussier"]
    baiss = [d for d in divergences if TYPES_META[d["type"]]["biais"] == "baissier"]
    recentes = [d for d in divergences
                if d["fraicheur"] <= params["telegram"]["fraicheur_max_bougies"][d["vue"]]]
    seuil_kpi = params["prioritaire"]["fraicheur_confirmees"]
    a_surveiller_kpi = [d for d in divergences
                        if (not d["confirmee"]) or d["fraicheur"] <= seuil_kpi[d["vue"]]]

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
Filtres anti-bruit : écart intermédiaire &le; {params['retracement_max_pct']}% (deux pivots séparés par un mouvement plus ample appartiennent à des phases différentes){zone_txt}.</p>

<div class="kpis">
<div class="kpi"><div class="kl">Divergences</div><div class="kv">{len(divergences)}</div></div>
<div class="kpi hauss"><div class="kl">Haussières</div><div class="kv green">{len(hauss)}</div></div>
<div class="kpi baiss"><div class="kl">Baissières</div><div class="kv red">{len(baiss)}</div></div>
<div class="kpi prio-kpi"><div class="kl">À surveiller</div><div class="kv">{len(a_surveiller_kpi)}</div></div>
<div class="kpi"><div class="kl">Récentes (alertées)</div><div class="kv">{len(recentes)}</div></div>
<div class="kpi"><div class="kl">Anciennes écartées</div><div class="kv">{anciennes}</div></div>
<div class="kpi"><div class="kl">Tickers en échec</div><div class="kv">{len(erreurs)}</div></div>
</div>"""]

    actifs = [c for c in TYPES_META if params["types"].get(c)]
    html.append('<div class="legende">')
    for cle in actifs:
        m = TYPES_META[cle]
        html.append(f'<div>{m["emoji"]} <b>{m["label"]}</b> — {m["explication"]}</div>')
    html.append('</div>')

    # ── Section prioritaire : ce qui se joue maintenant ──
    seuil_conf = params["prioritaire"]["fraicheur_confirmees"]
    a_surveiller = [d for d in divergences
                    if (not d["confirmee"]) or d["fraicheur"] <= seuil_conf[d["vue"]]]
    html.append(f'<div class="section-title">À surveiller maintenant '
                f'— {len(a_surveiller)} divergence(s)</div>')
    if a_surveiller:
        html.append('<div class="prio">')
        html.append(tableau_html(sorted(a_surveiller,
                                        key=lambda x: (x["fraicheur"], x["ticker"]))))
        html.append('</div>')
    else:
        html.append('<div class="vide">Rien en formation, et aucune divergence '
                    'confirmée assez récente.</div>')

    for vue in vues:
        du_vue = [d for d in divergences if d["vue"] == vue]
        html.append(f'<div class="section-title">{VUES_META[vue]["label"]} '
                    f'— {len(du_vue)} divergence(s)</div>')
        if not du_vue:
            html.append('<div class="vide">Aucune divergence détectée sur cette vue.</div>')
            continue

        html.append(tableau_html(sorted(du_vue, key=lambda x: (x["fraicheur"], x["ticker"]))))

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
    parser.add_argument("--vue", default="DW",
                        help="Vues à scanner : D, W ou DW (défaut : DW)")
    parser.add_argument("--tickers", default=None,
                        help="Liste de tickers séparés par des virgules "
                             "(défaut : tickers.txt)")
    parser.add_argument("--no-telegram", action="store_true",
                        help="Génère le rapport sans envoyer d'alerte")
    parser.add_argument("--reset-etat", action="store_true",
                        help="Oublie les divergences déjà alertées et tout renvoyer")
    parser.add_argument("--toutes", action="store_true",
                        help="Alerte sur toutes les divergences, même anciennes")
    parser.add_argument("--historique", action="store_true",
                        help="Garde aussi les divergences anciennes dans le rapport")
    parser.add_argument("--sortie", default=None,
                        help="Chemin du rapport HTML")
    parser.add_argument("--ouvrir", action="store_true",
                        help="Ouvre le rapport dans le navigateur à la fin")
    args = parser.parse_args()

    config = charger_config()
    params = config["divergence"]

    vues = [v for v in ("D", "W") if v in args.vue.upper() and params["vues"].get(v, True)]
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

    divergences, erreurs = [], []
    for i, ticker in enumerate(tickers, 1):
        print(f"[{i}/{len(tickers)}] {ticker}...")
        try:
            trouvees, erreur = analyser_ticker(ticker, params, vues)
        except Exception as e:
            trouvees, erreur = [], f"erreur d'analyse : {e}"
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
        time.sleep(0.3)

    # ── On écarte l'historique ancien : une divergence vieille de deux ans
    #    n'a plus d'intérêt opérationnel, elle a déjà joué ou échoué. ──
    anciennes = 0
    if not args.historique:
        seuils = {v: max(params["rapport"]["fraicheur_max_bougies"][v],
                         params["telegram"]["fraicheur_max_bougies"][v])
                  for v in ("D", "W")}
        avant_filtre = len(divergences)
        divergences = [d for d in divergences if d["fraicheur"] <= seuils[d["vue"]]]
        anciennes = avant_filtre - len(divergences)
        if anciennes:
            print(f"\n({anciennes} divergence(s) trop ancienne(s) écartée(s) — "
                  f"au-delà de {seuils['D']} jours / {seuils['W']} semaines, "
                  f"--historique pour les voir)")

    # ── Rapport ──
    horodatage = datetime.now().strftime("%Y%m%d_%H%M")
    chemin = Path(args.sortie) if args.sortie else RAPPORTS_DIR / f"divergences_{horodatage}.html"
    generer_html(divergences, tickers, vues, erreurs, params, chemin, anciennes)
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
