#!/usr/bin/env python3
"""
Test du motif de référence — le cas que le scanner doit impérativement détecter.

Reproduit la configuration recherchée : une base longue de six mois où le prix
fait des creux quasi plats, très légèrement descendants, pendant que le RSI
remonte franchement d'un creux à l'autre. C'est la divergence haussière
régulière longue, avec un fort rebond intermédiaire (cassure de structure).

Ce fichier existe pour qu'aucun réglage anti-bruit futur ne puisse écarter ce
motif sans que le test échoue.

    python3 test_motif_reference.py
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import RSI_scanner as rs


def construire_motif():
    """
    Six mois de base, trois creux, un fort rebond intermédiaire.

    Les jambes de baisse sont de plus en plus lentes : c'est ce qui fait monter
    le RSI d'un creux à l'autre alors que le prix revient au même niveau.
    """
    segments = [
        (100.0, 200),  # préambule — donne à la vue W un historique réaliste
        (100.0, 25),   # ligne de départ
        (78.0,  16),   # chute rapide      -> creux 1, RSI très bas
        (92.0,  26),   # rebond
        (80.5,  30),   # chute plus lente  -> creux 2, RSI plus haut
        (95.0,  28),   # rebond (cassure de structure)
        (77.5,  40),   # chute très lente  -> creux 3, RSI encore plus haut
        (84.0,  10),   # amorce de retournement
    ]
    rng = np.random.default_rng(4)
    prix = []
    for cible, n in segments:
        prix += list(np.linspace(prix[-1] if prix else segments[0][0], cible, n))
    prix = np.array(prix) + rng.normal(0, 0.45, len(prix))
    ouv = prix + rng.normal(0, 0.5, len(prix))
    return pd.DataFrame(
        {
            "Open":  ouv,
            "High":  np.maximum(prix, ouv) + np.abs(rng.normal(0.5, 0.2, len(prix))),
            "Low":   np.minimum(prix, ouv) - np.abs(rng.normal(0.5, 0.2, len(prix))),
            "Close": prix,
            "Volume": rng.integers(1_000_000, 5_000_000, len(prix)),
        },
        index=pd.bdate_range("2024-11-01", periods=len(prix)),
    )


def main():
    df = construire_motif()
    params = rs.charger_config()["divergence"]

    echecs = []
    for vue in ("D", "W"):
        data = df if vue == "D" else rs.en_hebdomadaire(df)
        divergences, _ = rs.detecter_divergences(data, vue, params)
        haussieres = [d for d in divergences if d["type"] == "haussiere_reguliere"]

        print(f"\n── Vue {vue} — {len(data)} bougies ──")
        if not haussieres:
            print("   ✗ AUCUNE divergence haussière régulière détectée")
            echecs.append(vue)
            continue

        for d in haussieres:
            print(f"   ✓ {d['duree']:8} portée {d['span']:3} · "
                  f"prix {d['prix_a']:.2f} → {d['prix_b']:.2f} ({d['ecart_prix_pct']:+.2f}%) · "
                  f"RSI {d['rsi_a']:.1f} → {d['rsi_b']:.1f} ({d['delta_rsi']:+.1f}) · "
                  f"écart interm. {d['retracement_pct']:.1f}%")

        # Le motif recherché est la divergence LONGUE, celle qui relie le premier
        # creux au dernier. Une divergence courte trouvée au passage ne suffit pas.
        longue = max(haussieres, key=lambda d: d["span"])
        seuil = 60 if vue == "D" else 12
        if longue["span"] < seuil:
            print(f"   ✗ la plus longue ne fait que {longue['span']} bougies "
                  f"({seuil} attendues au minimum)")
            echecs.append(vue)

    print()
    if echecs:
        print(f"ÉCHEC — le motif de référence n'est pas détecté en vue {', '.join(echecs)}")
        print("Les réglages anti-bruit de config.json sont probablement trop stricts.")
        return 1
    print("SUCCÈS — le motif de référence est détecté en vue D et en vue W")
    return 0


if __name__ == "__main__":
    sys.exit(main())
