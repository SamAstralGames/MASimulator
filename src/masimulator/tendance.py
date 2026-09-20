"""Filtre de tendance de fond : prix au-dessus d'une moyenne, ou croisement de deux moyennes.

Calculé éventuellement sur un timeframe supérieur (reconstruit depuis les M1) et
reporté sur les barres tradées sans regard vers le futur : une barre H1 n'est
connue qu'une fois clôturée, soit à son ouverture + une heure.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import data, moyennes

MODES = {"aucune": "Aucune", "niveau": "Prix vs une moyenne", "croisement": "Croisement de deux moyennes"}


@dataclass(frozen=True)
class Tendance:
    mode: str = "aucune"          # "aucune" | "niveau" | "croisement"
    ma_type: str = "EMA"
    unite: str = "h1"
    periode: int = 200            # mode "niveau"
    rapide: int = 20              # mode "croisement"
    lente: int = 50

    @property
    def active(self):
        return self.mode != "aucune"

    def libelle(self):
        if not self.active:
            return "aucun filtre de tendance"
        if self.mode == "niveau":
            return f"tendance : prix vs {self.ma_type} {self.periode} {self.unite.upper()}"
        return f"tendance : {self.ma_type} {self.rapide}/{self.lente} {self.unite.upper()}"

    def _bars_necessaires(self):
        return self.periode if self.mode == "niveau" else max(self.rapide, self.lente)


def _aligner(temps_tf, duree_tf, valeurs_tf, temps_cible):
    """Dernière valeur du timeframe supérieur DÉJÀ clôturée à chaque instant cible (NaN avant)."""
    connu_a_partir_de = temps_tf + duree_tf
    k = np.searchsorted(connu_a_partir_de, temps_cible, side="right") - 1
    return np.where(k >= 0, np.asarray(valeurs_tf, float)[np.maximum(k, 0)], np.nan)


def calculer(b: data.Bougies, t: Tendance, racine=None):
    """Tableau booléen aligné sur `b` (True = tendance haussière), ou None si inactif.

    Une barre sans historique suffisant compte comme « pas de tendance haussière
    confirmée » : on n'invente pas une direction.
    """
    if not t.active:
        return None
    if t.ma_type not in moyennes.CATALOGUE:
        raise ValueError(f"Moyenne inconnue : {t.ma_type}")
    if t.unite == b.unite:
        tf = b
    else:
        minutes = data.MINUTES[t.unite]
        chauffe = np.timedelta64(t._bars_necessaires() * minutes * 3, "m")   # amorçage de la moyenne
        debut = np.datetime_as_string((b.temps[0] - chauffe).astype("datetime64[D]"), unit="D")
        fin = np.datetime_as_string(b.temps[-1].astype("datetime64[D]"), unit="D")
        tf = data.charger(b.symbole, t.unite, debut, fin, racine)

    cat = moyennes.CATALOGUE[t.ma_type]
    if t.mode == "niveau":
        ma = cat(tf.close, t.periode)
        valeurs = ma
    else:
        rapide, lente = cat(tf.close, t.rapide), cat(tf.close, t.lente)
        valeurs = np.where(np.isnan(rapide) | np.isnan(lente), np.nan, (rapide > lente).astype(float))

    if tf is b:
        alignees = valeurs
    else:
        alignees = _aligner(tf.temps, np.timedelta64(data.MINUTES[t.unite], "m"), valeurs, b.temps)

    with np.errstate(invalid="ignore"):
        if t.mode == "niveau":
            haussiere = b.close > alignees
        else:
            haussiere = alignees == 1.0
    return np.where(np.isnan(alignees), False, haussiere)
