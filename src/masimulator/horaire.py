"""Analyse par tranche horaire : où, dans la journée, la stratégie gagne et perd-elle ?

Deux lectures complémentaires d'un même backtest :
- la CONTRIBUTION des barres : le rendement de chaque barre (variation de l'equity, position
  ouverte comprise) additionné par tranche. Elle dit où l'equity monte et où elle se dégrade ;
- les TRADES par tranche d'ENTRÉE : combien, quel taux de gain, quel PnL. Elle dit à quelle
  heure la décision d'entrer est bonne ou mauvaise.

Les bougies sont en UTC ; `decalage_h` déplace l'axe (heure de Paris = +1 ou +2, sans gestion
de l'heure d'été). Une tranche ne peut pas être plus fine que la barre : à partir de barres
H1 ou H4, la tranche s'élargit à la barre.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import data
from .engine import Resultat

MINUTES_JOUR = 24 * 60
TRADES_MIN_LISIBLE = 10          # en dessous, l'écart entre tranches ressemble beaucoup au hasard


@dataclass(frozen=True)
class Tranche:
    debut_min: int               # minute du jour, dans l'heure décalée
    fin_min: int
    n_barres: int
    contribution_pct: float      # somme des rendements par barre, en %
    n_trades: int                # trades ENTRÉS dans la tranche
    n_gagnants: int
    pnl_total: float

    @property
    def libelle(self):
        return f"{self.debut_min // 60:02d}:{self.debut_min % 60:02d}"

    @property
    def libelle_plage(self):
        fin = self.fin_min % MINUTES_JOUR
        return f"{self.libelle}-{fin // 60:02d}:{fin % 60:02d}"

    @property
    def taux_gain_pct(self):
        return self.n_gagnants / self.n_trades * 100 if self.n_trades else None

    @property
    def pnl_moyen(self):
        return self.pnl_total / self.n_trades if self.n_trades else None


def pas_effectif(unite, minutes=30):
    """La tranche demandée, élargie à la durée d'une barre si celle-ci est plus longue."""
    return max(minutes, data.MINUTES[unite])


def par_tranche(b: data.Bougies, res: Resultat, minutes=30, decalage_h=0):
    """Une `Tranche` par plage de la journée qui a au moins une barre, dans l'ordre horaire."""
    pas = pas_effectif(b.unite, minutes)
    n_tranches = -(-MINUTES_JOUR // pas)
    t = b.temps.astype("datetime64[ns]") + np.timedelta64(int(round(decalage_h * 60)), "m")
    minute = ((t - t.astype("datetime64[D]")) / np.timedelta64(1, "m")).astype(int)
    tranche = minute // pas

    eq = np.asarray(res.equity[:len(b)], float)
    rends = np.diff(eq) / eq[:-1]              # le rendement de la barre i est attribué à la barre i
    contribution = np.bincount(tranche[1:len(eq)], weights=rends, minlength=n_tranches) * 100
    barres = np.bincount(tranche, minlength=n_tranches)

    trades = np.zeros(n_tranches, int)
    gagnants = np.zeros(n_tranches, int)
    pnl = np.zeros(n_tranches)
    for tr in res.trades:
        if tr.entree_idx < len(b):
            k = tranche[tr.entree_idx]
            trades[k] += 1
            gagnants[k] += tr.pnl > 0
            pnl[k] += tr.pnl

    return [Tranche(k * pas, (k + 1) * pas, int(barres[k]), float(contribution[k]), int(trades[k]),
                    int(gagnants[k]), float(pnl[k]))
            for k in range(n_tranches) if barres[k]]


def resume(tranches, k=4):
    """Phrase de lecture : la part du gain et de la perte portée par les k meilleures et pires
    tranches, avec la mise en garde quand il y a trop peu de trades par tranche."""
    actives = [t for t in tranches if t.n_barres]
    if not actives:
        return ""
    total = sum(t.contribution_pct for t in actives)
    gain = sum(t.contribution_pct for t in actives if t.contribution_pct > 0)
    perte = sum(t.contribution_pct for t in actives if t.contribution_pct < 0)
    k = min(k, len(actives))
    ordre = sorted(actives, key=lambda t: t.contribution_pct)
    meilleures, pires = ordre[::-1][:k], ordre[:k]
    liste = lambda ts: ", ".join(t.libelle for t in sorted(ts, key=lambda t: t.debut_min))
    texte = [f"Somme des barres {total:+.1f} % (gain brut {gain:+.1f} %, perte brute {perte:+.1f} %) : "
             "additionnée sans composer, un peu différente du rendement affiché."]
    if gain > 0:
        g = sum(t.contribution_pct for t in meilleures if t.contribution_pct > 0)
        texte.append(f"Les {k} meilleures tranches ({liste(meilleures)}) font {g:+.1f} %, "
                     f"soit {g / gain * 100:.0f} % du gain brut.")
    if perte < 0:
        p = sum(t.contribution_pct for t in pires if t.contribution_pct < 0)
        texte.append(f"Les {k} pires ({liste(pires)}) font {p:+.1f} %, "
                     f"soit {p / perte * 100:.0f} % de la perte brute.")
    n_trades = sum(t.n_trades for t in actives)
    moyenne = n_trades / len(actives)
    if moyenne < TRADES_MIN_LISIBLE:
        texte.append(f"Attention : {n_trades} trades sur {len(actives)} tranches, soit "
                     f"{moyenne:.1f} par tranche. Sous ~{TRADES_MIN_LISIBLE}, les écarts entre "
                     "tranches peuvent n'être que du hasard : élargis la tranche ou la période.")
    return " ".join(texte)
