"""Catalogue de moyennes mobiles et signaux de croisement.

Port de ``RaptorBT/moyennes.py`` (numpy pur, récursif là où il le faut).
Toutes ces moyennes cherchent à réduire le retard d'une EMA classique ; aucune
ne crée de pouvoir prédictif en soi.
"""

from __future__ import annotations

import numpy as np


def _ema(x, periode):
    x = np.asarray(x, float)
    alpha = 2.0 / (periode + 1.0)
    out = np.empty_like(x)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = alpha * x[i] + (1 - alpha) * out[i - 1]
    return out


def wma(x, periode):
    """Moyenne pondérée linéairement : la barre la plus récente pèse le plus."""
    x = np.asarray(x, float)
    poids = np.arange(1, periode + 1, dtype=float)
    poids /= poids.sum()
    out = np.full(len(x), np.nan)
    if len(x) >= periode:
        out[periode - 1:] = np.convolve(x, poids[::-1], mode="valid")
    return out


def zlema(x, periode):
    """Zero-Lag EMA (Ehlers & Way) : lisse un prix corrigé de son retard."""
    x = np.asarray(x, float)
    lag = int((periode - 1) / 2)
    corrige = np.copy(x)
    if lag > 0:
        corrige[lag:] = 2.0 * x[lag:] - x[:-lag]
    return _ema(corrige, periode)


def hma(x, periode):
    """Hull MA : WMA(2*WMA(x, n/2) - WMA(x, n), sqrt(n))."""
    x = np.asarray(x, float)
    demi = max(int(periode / 2), 1)
    racine = max(int(np.sqrt(periode)), 1)
    brut = 2.0 * wma(x, demi) - wma(x, periode)
    brut = np.nan_to_num(brut, nan=x[0])
    return wma(brut, racine)


def kama(x, periode=10, rapide=2, lente=30):
    """Kaufman Adaptive MA : rapide quand le marché va droit, lente en zigzag."""
    x = np.asarray(x, float)
    n = len(x)
    out = np.full(n, np.nan)
    if n <= periode:
        return out

    variation = np.abs(np.diff(x, prepend=x[0]))
    cum = np.cumsum(variation)
    volatilite = np.empty(n)
    volatilite[:periode] = np.nan
    volatilite[periode:] = cum[periode:] - cum[:-periode]

    direction = np.full(n, np.nan)
    direction[periode:] = np.abs(x[periode:] - x[:-periode])

    with np.errstate(invalid="ignore", divide="ignore"):
        er = np.where(volatilite > 0, direction / volatilite, 0.0)
    er = np.nan_to_num(er)

    sc_rapide = 2.0 / (rapide + 1.0)
    sc_lente = 2.0 / (lente + 1.0)
    sc = (er * (sc_rapide - sc_lente) + sc_lente) ** 2

    out[periode] = x[periode]
    for i in range(periode + 1, n):
        out[i] = out[i - 1] + sc[i] * (x[i] - out[i - 1])
    return out


def supersmoother(x, periode=10):
    """Filtre SuperSmoother 2 pôles (Ehlers)."""
    x = np.asarray(x, float)
    a1 = np.exp(-1.414 * np.pi / periode)
    b1 = 2.0 * a1 * np.cos(1.414 * np.pi / periode)
    c2, c3 = b1, -a1 * a1
    c1 = 1.0 - c2 - c3

    out = np.copy(x)
    for i in range(2, len(x)):
        out[i] = c1 * (x[i] + x[i - 1]) / 2.0 + c2 * out[i - 1] + c3 * out[i - 2]
    return out


def vidya(x, periode=14, periode_cmo=9):
    """Variable Index Dynamic Average (Chande) : lissage piloté par le CMO."""
    x = np.asarray(x, float)
    n = len(x)
    d = np.diff(x, prepend=x[0])
    hausses = np.where(d > 0, d, 0.0)
    baisses = np.where(d < 0, -d, 0.0)

    cum_h, cum_b = np.cumsum(hausses), np.cumsum(baisses)
    su = np.full(n, np.nan)
    sd = np.full(n, np.nan)
    su[periode_cmo:] = cum_h[periode_cmo:] - cum_h[:-periode_cmo]
    sd[periode_cmo:] = cum_b[periode_cmo:] - cum_b[:-periode_cmo]

    with np.errstate(invalid="ignore", divide="ignore"):
        cmo = np.where((su + sd) > 0, np.abs((su - sd) / (su + sd)), 0.0)
    cmo = np.nan_to_num(cmo)

    alpha = 2.0 / (periode + 1.0)
    out = np.copy(x)
    for i in range(1, n):
        k = alpha * cmo[i]
        out[i] = x[i] * k + out[i - 1] * (1.0 - k)
    return out


CATALOGUE = {
    "EMA": _ema,
    "WMA": wma,
    "ZLEMA": zlema,
    "HMA": hma,
    "KAMA": lambda x, p: kama(x, p),
    "SSMOOTH": supersmoother,
    "VIDYA": lambda x, p: vidya(x, p),
}
TYPES = tuple(CATALOGUE)


def rapide_au_dessus(close, nom, rapide, lente):
    """(au_dessus, valide) : la moyenne rapide est-elle au-dessus de la lente ?

    Les deux signaux de croisement s'en déduisent ; on le sépare pour pouvoir
    le mettre en cache quand on crible plusieurs paires entrée x sortie.
    """
    r = CATALOGUE[nom](close, rapide)
    l = CATALOGUE[nom](close, lente)
    return r > l, ~np.isnan(r) & ~np.isnan(l)


def croisement_haussier(au_dessus, valide):
    avant = np.roll(au_dessus, 1)
    avant[0] = False
    return au_dessus & ~avant & valide


def croisement_baissier(au_dessus, valide):
    avant = np.roll(au_dessus, 1)
    avant[0] = False
    return ~au_dessus & avant & valide
