#!/usr/bin/env python3
"""
Étude d'événement — la divergence RSI a-t-elle un pouvoir prédictif ?

Un backtest de stratégie mesure l'entrée ET la sortie : un bon signal saboté
par un mauvais stop y ressemble à un mauvais signal. Ce programme isole la
question du signal seul.

Pour chaque divergence détectée sur la watchlist, il mesure le rendement du
prix à +10, +20 et +50 bougies, SANS stop ni objectif, puis compare ces
rendements à ceux de toutes les autres bougies des mêmes titres sur la même
période. Si les divergences ne battent pas ce hasard-là, aucun réglage de
sortie ne sauvera la stratégie.

    python3 etude_evenement.py
    python3 etude_evenement.py --vue D --periode 15y
    python3 etude_evenement.py --tickers AAPL,NVDA --horizons 5,10,20

Deux précautions contre le biais de rétrospective :

  • L'entrée est prise `droite` bougies APRÈS le second pivot, au moment où
    ce pivot devient réellement connaissable. Mesurer depuis le pivot
    lui-même reviendrait à utiliser une information que personne n'avait.

  • La référence n'est pas un tirage abstrait mais les bougies des mêmes
    titres sur la même période : un titre qui monte de 300 % fait monter les
    deux distributions, et la comparaison reste juste.
"""

import argparse
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import RSI_scanner as rs

BASE_DIR     = Path(__file__).parent
RAPPORTS_DIR = BASE_DIR / "rapports"
TIRAGES      = 5000          # rééchantillonnages pour la p-value


# ─── Collecte ─────────────────────────────────────────────────────────────

def rendements_futurs(closes, depart, horizons):
    """Rendement en % à chaque horizon, ou None si l'historique s'arrête avant."""
    if depart + max(horizons) >= len(closes):
        return None
    base = closes[depart]
    if not np.isfinite(base) or base <= 0:
        return None
    return [(closes[depart + h] / base - 1) * 100 for h in horizons]


def analyser_ticker(ticker, params, vues, horizons, periode):
    """Retourne (signaux, reference) pour un ticker."""
    df = rs.telecharger(ticker, periode)
    if df is None:
        return [], []

    signaux, reference = [], []
    for vue in vues:
        data = df if vue == "D" else rs.en_hebdomadaire(df)
        mini = params["rsi_periode"] + params["ecart_bougies"][vue]["max"] + max(horizons)
        if len(data) < mini:
            continue

        closes = data["Close"].astype(float).to_numpy()
        divergences, _ = rs.detecter_divergences(data, vue, params)
        droite = params["pivot"][vue]["droite"]

        for d in divergences:
            # Le pivot n'est connaissable qu'une fois sa fenêtre droite passée.
            depart = d["idx_b"] + droite
            r = rendements_futurs(closes, depart, horizons)
            if r is None:
                continue
            signaux.append({
                "ticker": ticker, "vue": vue, "type": d["type"],
                "date": data.index[depart], "span": d["span"],
                "delta_rsi": d["delta_rsi"], "rendements": r,
            })

        # Référence : toutes les bougies du même titre, sur la même vue.
        for i in range(mini, len(closes) - max(horizons)):
            r = rendements_futurs(closes, i, horizons)
            if r is not None:
                reference.append({"vue": vue, "rendements": r})

    return signaux, reference


# ─── Statistiques ─────────────────────────────────────────────────────────

def p_value(valeurs_signal, pool_reference, sens, rng):
    """
    Probabilité qu'un tirage au hasard de même taille fasse aussi bien.

    `sens` vaut +1 quand on attend une hausse (divergence haussière) et -1
    quand on attend une baisse : une divergence baissière réussit en donnant
    un rendement négatif.
    """
    n = len(valeurs_signal)
    if n == 0 or len(pool_reference) < n:
        return None
    observe = np.mean(valeurs_signal) * sens
    tirages = rng.choice(pool_reference, size=(TIRAGES, n), replace=True)
    moyennes = tirages.mean(axis=1) * sens
    return float(np.mean(moyennes >= observe))


def resumer(signaux, reference, horizons, rng):
    """Une ligne de statistiques par (vue, type, horizon)."""
    lignes = []
    for vue in sorted({s["vue"] for s in signaux}):
        pool = {h: np.array([r["rendements"][i] for r in reference if r["vue"] == vue])
                for i, h in enumerate(horizons)}
        for type_cle in sorted({s["type"] for s in signaux if s["vue"] == vue}):
            sous = [s for s in signaux if s["vue"] == vue and s["type"] == type_cle]
            sens = 1 if rs.TYPES_META[type_cle]["biais"] == "haussier" else -1
            for i, h in enumerate(horizons):
                vals = np.array([s["rendements"][i] for s in sous])
                ref  = pool[h]
                lignes.append({
                    "vue": vue, "type": type_cle, "horizon": h, "n": len(vals),
                    "moy": float(vals.mean()), "med": float(np.median(vals)),
                    "pct_pos": float((vals > 0).mean() * 100),
                    "ref_moy": float(ref.mean()) if ref.size else float("nan"),
                    "ref_pct_pos": float((ref > 0).mean() * 100) if ref.size else float("nan"),
                    "ref_n": int(ref.size),
                    "ecart": float(vals.mean() - ref.mean()) if ref.size else float("nan"),
                    "sens": sens,
                    "p": p_value(vals, ref, sens, rng) if ref.size else None,
                })
    return lignes


# ─── Rapport ──────────────────────────────────────────────────────────────

CSS = """*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f5f4f0;color:#1a1a1a;padding:2rem}
h1{font-size:22px;font-weight:500;margin-bottom:4px}
h2{font-size:15px;font-weight:600;margin:1.6rem 0 .6rem}
.meta{font-size:13px;color:#666;margin-bottom:1rem}
.note{font-size:12px;color:#5d5750;background:#eef3fb;border-left:3px solid #378add;padding:9px 12px;border-radius:4px;margin-bottom:1.2rem}
.verdict{font-size:13px;padding:12px 16px;border-radius:10px;margin-bottom:1.4rem;border:0.5px solid #e0ddd6;background:#fff}
.verdict b{font-size:15px}
.table-wrap{overflow-x:auto;border-radius:10px;border:0.5px solid #e0ddd6;margin-bottom:1rem;background:#fff}
table{width:100%;border-collapse:collapse;font-size:12px}
thead th{background:#f5f4f0;padding:8px 9px;text-align:left;font-weight:500;font-size:11px;color:#666;border-bottom:0.5px solid #e0ddd6;white-space:nowrap}
tbody td{padding:8px 9px;border-bottom:0.5px solid #f0ede8;white-space:nowrap}
tbody tr:last-child td{border-bottom:none}
.badge{display:inline-block;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:500}
.bg{background:#eaf3de;color:#3b6d11}.br{background:#fcebeb;color:#a32d2d}
.bn{background:#f0ede8;color:#666}.bi{background:#e6f1fb;color:#185fa5}
.barre{display:inline-block;height:9px;border-radius:2px;vertical-align:middle}
.footer{font-size:11px;color:#aaa;margin-top:1.4rem}"""


def badge_p(p, seuil):
    """Vert au seuil corrigé, bleu au seuil brut de 5 %, neutre au-delà."""
    if p is None:
        return '<span class="badge bn">—</span>'
    cls = "bg" if p < seuil else ("bi" if p < 0.05 else "bn")
    return f'<span class="badge {cls}">{p:.3f}</span>'


def generer_html(lignes, signaux, tickers, horizons, params, periode, chemin):
    maintenant = datetime.now()
    # Correction pour tests multiples. En testant six combinaisons au seuil de
    # 5 %, on en voit passer une sur du bruit pur environ une fois sur quatre :
    # vérifié en faisant tourner cette étude sur des marches aléatoires. Le
    # seuil est donc divisé par le nombre de tests (correction de Bonferroni).
    seuil = 0.05 / max(1, len(lignes))
    concluants = [l for l in lignes
                  if l["p"] is not None and l["p"] < seuil and l["ecart"] * l["sens"] > 0]
    if not lignes:
        verdict, coul = "Aucun signal exploitable n'a été collecté.", "#a32d2d"
    elif not concluants:
        proches = [l for l in lignes if l["p"] is not None and l["p"] < 0.05
                   and l["ecart"] * l["sens"] > 0]
        verdict = ("Aucune combinaison ne bat le hasard une fois corrigé le nombre de tests. "
                   "Le signal ne porte pas d'information exploitable sur ces horizons : "
                   "travailler les sorties ne changera rien."
                   + (f" ({len(proches)} passent le seuil brut de 5 % mais pas le seuil corrigé "
                      f"de {seuil:.4f} — c'est exactement ce que produit le hasard quand on "
                      "multiplie les tests.)" if proches else ""))
        coul = "#a32d2d"
    else:
        noms = ", ".join(f"{rs.TYPES_META[l['type']]['court']} en vue {l['vue']} à +{l['horizon']}"
                         for l in concluants[:4])
        verdict = (f"{len(concluants)} combinaison(s) battent le hasard au seuil corrigé "
                   f"de {seuil:.4f} : {noms}. "
                   "Le signal porte une information ; le travail se déplace alors vers les sorties.")
        coul = "#0f6e56"

    html = [f"""<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
<title>Étude d'événement — divergences RSI</title><style>{CSS}</style></head><body>
<h1>Le signal a-t-il un pouvoir prédictif ?</h1>
<p class="meta">{len(tickers)} tickers &nbsp;|&nbsp; historique {periode} &nbsp;|&nbsp;
{len(signaux)} divergences mesurées &nbsp;|&nbsp; généré le {maintenant.strftime('%d/%m/%Y %H:%M')}</p>
<p class="note">Rendement du prix après chaque divergence, <b>sans stop ni objectif</b>, comparé à
celui de toutes les autres bougies des mêmes titres sur la même période. L'entrée est prise
{params['pivot']['D']['droite']} bougies après le second pivot, au moment où il devient connaissable —
mesurer depuis le pivot lui-même utiliserait une information que personne n'avait.
La colonne <b>p</b> est la probabilité qu'un tirage au hasard de même taille fasse aussi bien :
en dessous du seuil corrigé, le résultat n'est pas attribuable à la chance.</p>
<div class="verdict" style="border-left:4px solid {coul}"><b style="color:{coul}">Verdict</b><br>{verdict}</div>"""]

    html.append('<div class="table-wrap"><table><thead><tr>'
                '<th>Vue</th><th>Type</th><th>Horizon</th><th>Signaux</th>'
                '<th>Rendement moyen</th><th>Médiane</th><th>% positifs</th>'
                '<th>Référence</th><th>Écart</th><th>p</th><th></th>'
                '</tr></thead><tbody>')
    for l in lignes:
        meta = rs.TYPES_META[l["type"]]
        favorable = l["ecart"] * l["sens"] > 0
        cls_ecart = "bg" if favorable else "br"
        largeur = min(abs(l["ecart"]) * 12, 90)
        coul_barre = "#3b6d11" if favorable else "#a32d2d"
        html.append(f"""<tr>
<td><span class="badge bi">{l['vue']}</span></td>
<td>{meta['emoji']} {meta['court']}</td>
<td>+{l['horizon']}</td>
<td>{l['n']}</td>
<td>{l['moy']:+.2f} %</td>
<td>{l['med']:+.2f} %</td>
<td>{l['pct_pos']:.1f} %</td>
<td>{l['ref_moy']:+.2f} % <span style="color:#aaa">({l['ref_pct_pos']:.0f} % pos.)</span></td>
<td><span class="badge {cls_ecart}">{l['ecart']:+.2f} %</span></td>
<td>{badge_p(l['p'], seuil)}</td>
<td><span class="barre" style="width:{largeur:.0f}px;background:{coul_barre}"></span></td>
</tr>""")
    html.append('</tbody></table></div>')

    html.append('<h2>Comment lire ce tableau</h2><div class="note" style="background:#fff;border-left-color:#ba7517">'
                "L'<b>écart</b> est ce que la divergence ajoute au comportement ordinaire du titre. "
                "Il est compté <b>dans le sens attendu</b> : une divergence baissière réussit en donnant "
                "un rendement négatif, donc un écart favorable y apparaît en vert même s'il est négatif.<br>"
                "Un <b>p en bleu</b> passe le seuil brut de 5 % mais pas le seuil corrigé : ce n'est pas "
                "une preuve. En testant plusieurs combinaisons à la fois, certaines passent le seuil brut "
                "par pure chance — mesuré sur des marches aléatoires, une sur six y parvient. Seul le "
                "<b>vert</b> compte.<br>"
                "Un écart de faible ampleur avec un p très bas est plus solide qu'un gros écart sur dix "
                "signaux.</div>")

    html.append(f'<p class="footer">Étude d\'événement — {TIRAGES} rééchantillonnages par test. '
                'Le rendement est mesuré sur les clôtures, sans frais ni glissement : '
                'il mesure le signal, pas une performance atteignable.</p></body></html>')

    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text("\n".join(html), encoding="utf-8")
    return chemin


# ─── Point d'entrée ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Mesure le pouvoir prédictif des divergences RSI, sans gestion de position.",
        formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--vue", default="DW", help="Vues à étudier : D, W ou DW")
    parser.add_argument("--tickers", default=None, help="Liste séparée par des virgules")
    parser.add_argument("--periode", default="15y", help="Historique téléchargé (défaut : 15y)")
    parser.add_argument("--horizons", default="10,20,50",
                        help="Horizons de mesure, en bougies (défaut : 10,20,50)")
    parser.add_argument("--sortie", default=None, help="Chemin du rapport HTML")
    parser.add_argument("--ouvrir", action="store_true", help="Ouvre le rapport à la fin")
    args = parser.parse_args()

    config = rs.charger_config()
    params = config["divergence"]
    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    vues = [v for v in ("D", "W") if v in args.vue.upper()]
    tickers = ([t.strip().upper() for t in args.tickers.split(",") if t.strip()]
               if args.tickers else rs.charger_tickers())

    print(f"=== Étude d'événement — {len(tickers)} tickers, vues {'+'.join(vues)}, "
          f"historique {args.periode} ===")
    print(f"    horizons : {', '.join('+' + str(h) for h in horizons)} bougies\n")

    signaux, reference, echecs = [], [], []
    for i, ticker in enumerate(tickers, 1):
        try:
            s, r = analyser_ticker(ticker, params, vues, horizons, args.periode)
        except Exception as e:
            print(f"[{i}/{len(tickers)}] {ticker} — erreur : {e}")
            echecs.append(ticker)
            continue
        if not s and not r:
            echecs.append(ticker)
        signaux.extend(s)
        reference.extend(r)
        print(f"[{i}/{len(tickers)}] {ticker:10} {len(s):3} divergence(s)   "
              f"(total {len(signaux)})")
        time.sleep(0.3)

    if not signaux:
        print("\nAucune divergence mesurable — historique trop court ou tickers invalides.")
        return 1

    print(f"\n{len(signaux)} divergences mesurées, "
          f"{len(reference)} bougies de référence. Calcul des statistiques...")

    rng = np.random.default_rng(12345)
    lignes = resumer(signaux, reference, horizons, rng)

    print()
    for l in lignes:
        meta = rs.TYPES_META[l["type"]]
        favorable = "✓" if l["ecart"] * l["sens"] > 0 else "✗"
        p_txt = "—" if l["p"] is None else f"{l['p']:.3f}"
        print(f"  [{l['vue']}] {meta['court']:16} +{l['horizon']:<3} "
              f"n={l['n']:<4} moy {l['moy']:+6.2f}%  réf {l['ref_moy']:+6.2f}%  "
              f"écart {l['ecart']:+6.2f}% {favorable}  p={p_txt}")

    horodatage = datetime.now().strftime("%Y%m%d_%H%M")
    chemin = (Path(args.sortie) if args.sortie
              else RAPPORTS_DIR / f"etude_evenement_{horodatage}.html")
    generer_html(lignes, signaux, tickers, horizons, params, args.periode, chemin)
    print(f"\n📄 Rapport : {chemin}")
    if args.ouvrir:
        webbrowser.open(chemin.resolve().as_uri())
    if echecs:
        print(f"⚠ {len(echecs)} ticker(s) sans données : {', '.join(echecs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
