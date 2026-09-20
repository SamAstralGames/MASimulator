"""Simulateur de challenge prop firm.

Un backtest donne UNE trajectoire ; un challenge est un franchissement de
barrière sur une fenêtre courte dont l'issue dépend de la date de départ. On
simule donc de nombreux challenges partant de dates différentes et on compte
combien passent : P(réussite).

Port de ``RaptorBT/simulateur_propfirm.py``, sans affichage : les fonctions
renvoient des structures.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import numpy as np

ISSUES = ("REUSSI", "ECHEC_DD", "ECHEC_JOUR", "TEMPS")


@dataclass(frozen=True)
class Regles:
    cible_pct: float = 10.0
    dd_max_pct: float = 10.0
    perte_jour_pct: float = 5.0
    jours_max: int = 30
    dd_trailing: bool = False

    @property
    def cible(self):
        return self.cible_pct / 100.0

    @property
    def dd_max(self):
        return self.dd_max_pct / 100.0

    @property
    def perte_jour(self):
        return self.perte_jour_pct / 100.0

    def hasard_pur(self):
        """P(réussite) d'une stratégie sans edge ni délai : ruine du joueur."""
        return self.dd_max / (self.cible + self.dd_max)

    def __str__(self):
        return (f"cible +{self.cible_pct:g}% / DD -{self.dd_max_pct:g}%"
                f"{' trailing' if self.dd_trailing else ' fixe'}"
                f" / jour -{self.perte_jour_pct:g}% / {self.jours_max}j")


@dataclass(frozen=True)
class Simulation:
    levier: float
    n: int
    issues: dict           # ISSUES -> nombre
    duree_barres: int
    departs: tuple = ()    # indice de barre de chaque départ simulé
    resultats: tuple = ()  # issue de chaque départ, dans le même ordre

    @property
    def taux(self):
        return self.issues["REUSSI"] / self.n if self.n else 0.0

    def part(self, issue):
        return self.issues[issue] / self.n if self.n else 0.0


def _premier(indices):
    return indices[0] if len(indices) else np.inf


def _un_challenge(rends, debuts_jour, longueurs_jour, regles):
    """Issue d'UN challenge sur une série de rendements par barre."""
    equity = np.cumprod(1.0 + rends)

    i_cible = _premier(np.flatnonzero(equity >= 1.0 + regles.cible))

    if regles.dd_trailing:
        sommet = np.maximum.accumulate(np.maximum(equity, 1.0))
        i_dd = _premier(np.flatnonzero(equity <= sommet * (1.0 - regles.dd_max)))
    else:
        i_dd = _premier(np.flatnonzero(equity <= 1.0 - regles.dd_max))

    # equity à l'ouverture du jour de chaque barre (= clôture de la veille)
    veille = np.concatenate([[1.0], equity[debuts_jour[1:] - 1]])
    equity_ouverture = np.repeat(veille, longueurs_jour)
    i_jour = _premier(np.flatnonzero(equity <= equity_ouverture * (1.0 - regles.perte_jour)))

    premier = min(i_cible, i_dd, i_jour)
    if premier == np.inf:
        return "TEMPS"
    if premier == i_cible:
        return "REUSSI"
    if premier == i_dd:
        return "ECHEC_DD"
    return "ECHEC_JOUR"


def simuler(equity_curve, timestamps, regles=None, levier=1.0,
            n_departs=500, graine=42):
    """Lance `n_departs` challenges à des dates de départ différentes.

    Multiplier les rendements par barre revient à multiplier la taille des
    positions : c'est exactement le levier.
    """
    regles = regles or Regles()
    equity = np.asarray(equity_curve, float)
    ts = np.asarray(timestamps)

    rends = (np.diff(equity) / equity[:-1]) * levier
    jours = ts[1:].astype("datetime64[D]").astype(np.int64)

    total_jours = jours[-1] - jours[0]
    if total_jours <= regles.jours_max:
        raise ValueError(
            f"Fenêtre trop courte ({total_jours} j) pour un challenge de "
            f"{regles.jours_max} j.")
    duree = int(len(rends) / max(total_jours, 1) * regles.jours_max)
    depart_max = len(rends) - duree
    if depart_max < 10:
        raise ValueError("Historique trop court pour échantillonner des départs.")

    rng = np.random.default_rng(graine)
    departs = rng.choice(depart_max, size=min(n_departs, depart_max), replace=False)

    departs = np.sort(departs)
    resultats = []
    for d in departs:
        j = jours[d:d + duree]
        debuts = np.concatenate([[0], np.flatnonzero(np.diff(j) != 0) + 1])
        longueurs = np.diff(np.append(debuts, len(j)))
        resultats.append(_un_challenge(rends[d:d + duree], debuts, longueurs, regles))

    issues = Counter({k: 0 for k in ISSUES})
    issues.update(resultats)
    return Simulation(levier=levier, n=len(resultats), issues=dict(issues),
                      duree_barres=duree, departs=tuple(int(d) for d in departs),
                      resultats=tuple(resultats))


def temoin_sans_edge(equity_curve, timestamps, regles, levier, n_departs=500,
                     graine=7, close_actif=None, exposition=None):
    """P(réussite) d'un témoin de même volatilité, sans edge.

    Un témoin à dérive nulle est faux dès que l'actif monte : une stratégie
    long-only hérite de la hausse. Avec `close_actif` et `exposition` (part du
    temps en position), le témoin hérite de la dérive de l'actif au prorata de
    l'exposition, et l'écart mesure ce que le signal apporte.
    """
    equity = np.asarray(equity_curve, float)
    rends = np.diff(equity) / equity[:-1]
    rng = np.random.default_rng(graine)

    derive = 0.0
    if close_actif is not None:
        c = np.asarray(close_actif, float)
        derive = np.mean(np.diff(c) / c[:-1]) * (exposition if exposition is not None else 1.0)

    faux = rng.normal(derive, rends.std(), size=len(rends))
    eq_faux = np.concatenate([[equity[0]], equity[0] * np.cumprod(1 + faux)])
    return simuler(eq_faux, timestamps, regles, levier, n_departs).taux
