"""Lecture du cache cTrader et contrôle sanitaire des bougies.

Les fichiers ``.zbars`` sont des fichiers gzip contenant des enregistrements
de six entiers 64 bits little-endian : timestamp (ms UTC), open, high, low,
close (prix x 100 000) et tick volume.

Tout est en numpy : un ``Bougies`` porte des tableaux, pas un DataFrame.
"""

from __future__ import annotations

import gzip
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# Cache dans lequel `ctrader-cli backtest` écrit ce qu'il télécharge. Une copie
# plus ancienne peut traîner ailleurs (ex. /mnt/data/Data/cTrader) : MASIM_CTRADER
# permet de la choisir.
RACINE_DEFAUT = "~/.config/Spotware/Cache/Spotware/BacktestingCache"

# unité -> minutes par barre
MINUTES = {"m1": 1, "m5": 5, "m15": 15, "h1": 60}

_STRUCTURE = np.dtype([
    ("timestamp", "<i8"), ("open", "<i8"), ("high", "<i8"),
    ("low", "<i8"), ("close", "<i8"), ("volume", "<i8"),
])
_FACTEUR_PRIX = 100_000.0


@dataclass(frozen=True)
class Bougies:
    symbole: str
    unite: str
    temps: np.ndarray      # datetime64[ns], UTC
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray

    def __len__(self):
        return len(self.temps)

    def timestamps_ns(self):
        """Ce que RaptorBT attend pour annualiser correctement."""
        return self.temps.astype("datetime64[ns]").astype(np.int64)

    def tranche(self, i, j):
        return Bougies(self.symbole, self.unite, self.temps[i:j],
                       self.open[i:j], self.high[i:j], self.low[i:j],
                       self.close[i:j], self.volume[i:j])


@dataclass(frozen=True)
class InfoSymbole:
    symbole: str
    dossier: Path
    unites: tuple[str, ...]
    premiere_m1: np.datetime64 | None
    derniere_m1: np.datetime64 | None


@dataclass(frozen=True)
class Couverture:
    """Jours ouvrés d'une période pour lesquels le cache n'a aucun fichier M1."""
    manquants: int
    premier_manquant: np.datetime64 | None
    dernier_manquant: np.datetime64 | None

    @property
    def complete(self):
        return self.manquants == 0


@dataclass(frozen=True)
class Alerte:
    niveau: str            # "erreur" | "avertissement"
    message: str


def racine_cache(racine=None):
    """Racine du cache : argument, variable MASIM_CTRADER, puis défaut.

    Elle peut ne pas exister encore : un premier téléchargement par ctrader-cli
    la créera, et `lister_symboles` renvoie alors un dictionnaire vide.
    """
    return Path(racine or os.environ.get("MASIM_CTRADER") or RACINE_DEFAUT).expanduser()


def couverture(symbole, debut, fin, racine=None):
    """Quels jours ouvrés de [debut, fin] n'ont pas de fichier M1 dans le cache ?

    cTrader écrit un fichier par jour (vide le week-end) : seul un jour ouvré
    sans fichier signale un vrai trou. `fin` est ramenée à hier, la journée en
    cours n'étant jamais complète.
    """
    debut = np.datetime64(debut, "D")
    fin = min(np.datetime64(fin, "D"), np.datetime64("today") - np.timedelta64(1, "D"))
    info = lister_symboles(racine).get(symbole.upper())
    presents = set()
    if info is not None and "m1" in info.unites:
        for f in (info.dossier / "m1").glob("*.zbars"):
            d = _date_fichier(f.stem)
            if d is not None:
                presents.add(d)
    if fin < debut:
        return Couverture(0, None, None)
    jours = np.arange(debut, fin + np.timedelta64(1, "D"))
    ouvres = jours[np.is_busday(jours)]
    manquants = [j for j in ouvres if j not in presents]
    if not manquants:
        return Couverture(0, None, None)
    return Couverture(len(manquants), manquants[0], manquants[-1])


def _date_fichier(stem):
    """Début de la période couverte par un fichier, ou None si inconnu."""
    try:
        if len(stem) == 8:
            return np.datetime64(f"{stem[:4]}-{stem[4:6]}-{stem[6:]}", "D")
        if len(stem) == 6:
            return np.datetime64(f"{stem[:4]}-{stem[4:]}", "M").astype("datetime64[D]")
    except ValueError:
        pass
    return None


def lister_symboles(racine=None):
    """Symboles présents dans le cache, avec la fraîcheur de leur M1."""
    racine = racine_cache(racine)
    trouves = {}
    if not racine.is_dir():
        return trouves
    for dossier in sorted(p for p in racine.rglob("*") if p.is_dir()):
        unites = tuple(sorted(
            u.name for u in dossier.iterdir()
            if u.is_dir() and u.name in MINUTES and any(u.glob("*.zbars"))
        ))
        if not unites:
            continue
        m1 = sorted((dossier / "m1").glob("*.zbars")) if "m1" in unites else []
        trouves[dossier.name.upper()] = InfoSymbole(
            symbole=dossier.name.upper(), dossier=dossier, unites=unites,
            premiere_m1=_date_fichier(m1[0].stem) if m1 else None,
            derniere_m1=_date_fichier(m1[-1].stem) if m1 else None,
        )
    return trouves


def _regrouper(temps, o, h, l, c, v, minutes):
    """M1 -> barres de `minutes` minutes, étiquetées à l'ouverture (label=left)."""
    pas = np.int64(minutes) * 60 * 1_000_000_000
    ns = temps.astype("datetime64[ns]").astype(np.int64)
    seau = ns // pas
    debuts = np.concatenate([[0], np.flatnonzero(np.diff(seau)) + 1])
    return (
        (seau[debuts] * pas).astype("datetime64[ns]"),
        o[debuts],
        np.maximum.reduceat(h, debuts),
        np.minimum.reduceat(l, debuts),
        c[np.concatenate([debuts[1:], [len(c)]]) - 1],
        np.add.reduceat(v, debuts),
    )


def charger(symbole, unite="m5", debut=None, fin=None, racine=None):
    """Charge les bougies d'un symbole, reconstruites depuis les M1.

    `debut` / `fin` : dates UTC incluses (str, date ou datetime64). Une `fin`
    sans heure inclut la journée entière.
    """
    symbole = symbole.upper()
    if unite not in MINUTES:
        raise ValueError(f"Unité non prise en charge : {unite}")
    info = lister_symboles(racine).get(symbole)
    if info is None:
        raise FileNotFoundError(f"Aucune donnée {symbole} dans le cache cTrader")
    if "m1" not in info.unites:
        raise FileNotFoundError(f"Pas de bougies M1 pour {symbole}")

    t_debut = np.datetime64(debut, "ns") if debut is not None else None
    t_fin = None
    if fin is not None:
        t_fin = np.datetime64(fin, "ns")
        # une date seule ("2026-07-31") vaut la journée entière
        if len(str(fin).strip()) <= 10:
            t_fin = t_fin + np.timedelta64(1, "D") - np.timedelta64(1, "ns")

    blocs = []
    for fichier in sorted((info.dossier / "m1").glob("*.zbars")):
        premier = _date_fichier(fichier.stem)
        if premier is not None:
            pas_fichier = np.timedelta64(1, "D") if len(fichier.stem) == 8 \
                else np.timedelta64(31, "D")
            if t_debut is not None and premier + pas_fichier <= t_debut.astype("datetime64[D]"):
                continue
            if t_fin is not None and premier > t_fin.astype("datetime64[D]"):
                continue
        with gzip.open(fichier, "rb") as flux:
            contenu = flux.read()
        if not contenu:      # fichiers vides le week-end
            continue
        if len(contenu) % _STRUCTURE.itemsize:
            raise ValueError(f"Fichier cTrader corrompu : {fichier}")
        blocs.append(np.frombuffer(contenu, dtype=_STRUCTURE))
    if not blocs:
        raise ValueError(f"Aucune donnée {symbole} sur la période demandée")

    brut = np.concatenate(blocs)
    brut = brut[np.argsort(brut["timestamp"], kind="stable")]
    # doublons : on garde le dernier enregistrement de chaque timestamp
    garde = np.append(np.diff(brut["timestamp"]) != 0, True)
    brut = brut[garde]

    temps = brut["timestamp"].astype("datetime64[ms]").astype("datetime64[ns]")
    o, h, l, c = (brut[k] / _FACTEUR_PRIX for k in ("open", "high", "low", "close"))
    v = brut["volume"].astype(np.float64)
    if unite != "m1":
        temps, o, h, l, c, v = _regrouper(temps, o, h, l, c, v, MINUTES[unite])

    masque = np.ones(len(temps), bool)
    if t_debut is not None:
        masque &= temps >= t_debut
    if t_fin is not None:
        masque &= temps <= t_fin
    if not masque.any():
        raise ValueError(f"Aucune bougie {symbole}/{unite} sur la période demandée")
    return Bougies(symbole, unite, temps[masque], o[masque], h[masque],
                   l[masque], c[masque], v[masque])


def controle(b, seuil_saut_pct=2.0):
    """Contrôle sanitaire : renvoie une liste d'alertes (vide = rien à signaler)."""
    alertes = []
    for nom in ("open", "high", "low", "close"):
        arr = getattr(b, nom)
        n_nan, n_neg = int(np.isnan(arr).sum()), int((arr <= 0).sum())
        if n_nan or n_neg:
            alertes.append(Alerte("erreur", f"{nom} : {n_nan} NaN, {n_neg} valeurs <= 0"))

    casse = ((b.high < b.low) | (b.high < b.open) | (b.high < b.close)
             | (b.low > b.open) | (b.low > b.close))
    if casse.any():
        alertes.append(Alerte(
            "erreur", f"{int(casse.sum())} bougies incohérentes (high < low, etc.), "
                      f"première le {b.temps[casse][0]}"))

    if len(b) > 1:
        ret = np.diff(b.close) / b.close[:-1] * 100
        suspects = np.flatnonzero(np.abs(ret) > seuil_saut_pct)
        for i in suspects[:3]:
            alertes.append(Alerte(
                "avertissement",
                f"Saut de {ret[i]:+.2f} % le {b.temps[i + 1]} "
                f"({b.close[i]:.5g} -> {b.close[i + 1]:.5g})"))
        if len(suspects) > 3:
            alertes.append(Alerte("avertissement",
                                  f"... et {len(suspects) - 3} autres sauts > {seuil_saut_pct} %"))

    duree = (b.temps[-1] - b.temps[0]) / np.timedelta64(1, "D") if len(b) > 1 else 0
    if duree < 30:
        alertes.append(Alerte("avertissement",
                              f"Seulement {duree:.0f} jours de données : trop court "
                              "pour simuler un challenge de 30 jours."))
    return alertes
