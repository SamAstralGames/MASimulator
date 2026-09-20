"""Détails d'une paire du criblage : politiques de sortie et long/short.

Port de ``exit_policy.comparer_sorties`` et ``long_short.comparer_tendance_long_short``
(RaptorBT), avec la colonne « gain » de ``labo_propfirm`` : l'écart entre la
P(réussite) de la stratégie et celle d'un témoin sans edge. Rien n'est affiché,
tout est renvoyé sous forme de lignes.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import raptorbt

from . import engine, propfirm
from .data import Bougies

TRAILINGS = (0.001, 0.002, 0.003, 0.005, 0.0075, 0.01, 0.015, 0.02, 0.025)
MULT_ATR = (1.5, 2.0, 3.0)


@dataclass(frozen=True)
class LigneLabo:
    nom: str
    rendement_pct: float
    dd_pct: float
    sharpe: float | None
    profit_factor: float | None
    win_rate_pct: float | None
    n_trades: int
    n_longs: int | None = None
    n_shorts: int | None = None
    via_stop: int | None = None        # sorties autres que le croisement
    levier: float | None = None
    p_reussite: float | None = None
    temoin: float | None = None
    gain: float | None = None          # P(réussite) - témoin, en fraction

    @property
    def rend_dd(self):
        return self.rendement_pct / self.dd_pct if self.dd_pct else None


def buy_and_hold(close):
    """(rendement %, drawdown max %) de l'actif seul, pour ne jamais perdre le nord."""
    close = np.asarray(close, float)
    eq = close / close[0]
    sommet = np.maximum.accumulate(eq)
    return (close[-1] / close[0] - 1) * 100, float(np.max((sommet - eq) / sommet) * 100)


def _meilleur_levier(res, b, regles, leviers, n_departs, avec_temoin):
    """(levier, P, témoin, gain) : le levier qui maximise le gain face au témoin,
    ou la P(réussite) quand il n'y a pas de témoin. None si l'historique est trop court."""
    meilleur = None
    for lev in leviers:
        try:
            p = propfirm.simuler(res.equity, b.temps, regles, lev, n_departs).taux
            t = (propfirm.temoin_sans_edge(res.equity, b.temps, regles, lev, n_departs,
                                           close_actif=b.close, exposition=res.exposition)
                 if avec_temoin else None)
        except ValueError:
            return None
        critere = p - t if avec_temoin else p
        if meilleur is None or critere > meilleur[0]:
            meilleur = (critere, lev, p, t)
    if meilleur is None:
        return None
    _, lev, p, t = meilleur
    return lev, p, t, (p - t if t is not None else None)


def _ligne(nom, res, b, regles, leviers, n_departs, avec_temoin, **extra):
    m = res.metriques
    lev = _meilleur_levier(res, b, regles, leviers, n_departs, avec_temoin) \
        if (m["total_trades"] or 0) > 0 else None
    champs = dict(levier=None, p_reussite=None, temoin=None, gain=None)
    if lev is not None:
        champs.update(levier=lev[0], p_reussite=lev[1], temoin=lev[2], gain=lev[3])
    return LigneLabo(
        nom=nom, rendement_pct=m["total_return_pct"] or 0.0, dd_pct=m["max_drawdown_pct"] or 0.0,
        sharpe=m["sharpe_ratio"], profit_factor=m["profit_factor"], win_rate_pct=m["win_rate_pct"],
        n_trades=int(m["total_trades"] or 0), **champs, **extra)


def politiques(fees, slippage, capital):
    """{nom: config} : la référence (croisement seul), les stops ATR, les trailings.

    set_atr_stop() et set_trailing_stop() écrivent au MÊME endroit de la config :
    on ne peut pas combiner un stop ATR et un trailing, il faut choisir.
    """
    p = {"croisement seul": engine.nouvelle_config(fees, slippage, capital)}
    for mult in MULT_ATR:
        c = engine.nouvelle_config(fees, slippage, capital)
        c.set_atr_stop(multiplier=mult, period=14)
        p[f"stop ATR {mult:g}x"] = c
        c = engine.nouvelle_config(fees, slippage, capital)
        c.set_atr_stop(multiplier=mult, period=14)
        c.set_risk_reward_target(ratio=2.0)
        p[f"stop ATR {mult:g}x + target 1:2"] = c
    for pct in TRAILINGS:
        c = engine.nouvelle_config(fees, slippage, capital)
        c.set_trailing_stop(pct)
        p[f"trailing {pct * 100:.2f}%"] = c
    return p


def comparer_sorties(b: Bougies, entrees, sorties, couts: engine.Couts, regles, leviers,
                     n_departs=300, capital=10_000.0, annule=None):
    """Mêmes entrées partout, seule la façon de sortir change : l'écart entre deux
    lignes est attribuable à la sortie et à rien d'autre."""
    fees, slippage = couts.fractions(float(np.median(b.close)))
    lignes = []
    for nom, config in politiques(fees, slippage, capital).items():
        if annule is not None and annule():
            break
        res = engine.backtest(b, entrees, sorties, fees, slippage, capital, config=config)
        # total_trades compte les entrées, pas le mécanisme de sortie : seule la
        # RAISON de sortie dit si le stop/trailing a réellement coupé avant le signal.
        via_stop = sum(t.raison_sortie != "Signal" for t in res.trades)
        lignes.append(_ligne(nom, res, b, regles, leviers, n_departs, True, via_stop=via_stop))
    return lignes


class _StrategieLongShort(raptorbt.Strategy):
    """Long/short piloté par deux signaux déjà calculés.

    signal_haut ouvre un long (ou retourne un short en long), signal_bas ouvre un
    short (ou retourne un long en short). Le moteur interdit de fermer et rouvrir
    sur la même barre : on ferme, puis on entre à la barre suivante (`en_attente`).
    `haussiere` (booléen, True = tendance haussière) n'autorise le long qu'avec la
    tendance et le short qu'à contre : jamais de position contre la tendance de fond.
    """

    def __init__(self, signal_haut, signal_bas, autoriser_long, autoriser_short,
                 haussiere=None, taille=0.95):
        super().__init__()
        self.haut, self.bas = np.asarray(signal_haut, bool), np.asarray(signal_bas, bool)
        self.long_ok, self.short_ok = autoriser_long, autoriser_short
        self.haussiere = None if haussiere is None else np.asarray(haussiere, bool)
        self.taille = taille
        self.en_attente = None

    def _autorise(self, sens, i):
        if not (self.long_ok if sens == 1 else self.short_ok):
            return False
        if self.haussiere is None:
            return True
        return bool(self.haussiere[i]) == (sens == 1)

    def _entrer(self, sens):
        if sens == 1:
            self.enter_long(size_frac=self.taille)
        else:
            self.enter_short(size_frac=self.taille)

    def on_bar(self, ctx):
        if self.en_attente is not None and ctx.position is None:
            sens, self.en_attente = self.en_attente, None
            if self._autorise(sens, ctx.idx):
                self._entrer(sens)
            return
        sens = 1 if self.haut[ctx.idx] else -1 if self.bas[ctx.idx] else 0
        if not sens:
            return
        if ctx.position is not None:
            self.close_position()
            self.en_attente = sens
        elif self._autorise(sens, ctx.idx):
            self._entrer(sens)


def comparer_long_short(b: Bougies, entrees, sorties, couts: engine.Couts, regles, leviers,
                        n_departs=300, capital=10_000.0, haussiere=None, annule=None):
    """Long seul, short seul, long + short ; avec et sans filtre de tendance si `haussiere`.

    `entrees` / `sorties` sont les signaux BRUTS (non filtrés) : le filtre est appliqué
    à l'intérieur de la stratégie, direction par direction.
    Pas de témoin ici : la dérive de l'actif joue contre un short, un témoin à dérive
    long-only serait trompeur.
    """
    fees, slippage = couts.fractions(float(np.median(b.close)))
    variantes = [("long seul", True, False, None), ("short seul", False, True, None),
                 ("long + short", True, True, None)]
    if haussiere is not None:
        variantes += [("long seul (tendance)", True, False, haussiere),
                      ("short seul (tendance)", False, True, haussiere),
                      ("long + short (tendance)", True, True, haussiere)]
    lignes = []
    for nom, long_ok, short_ok, filtre in variantes:
        if annule is not None and annule():
            break
        brut = raptorbt.run_strategy_backtest(
            _StrategieLongShort(entrees, sorties, long_ok, short_ok, filtre),
            b.timestamps_ns(), b.open, b.high, b.low, b.close, b.volume,
            symbol=b.symbole, config=engine.nouvelle_config(fees, slippage, capital),
            account_type="margin",      # un short est refusé, en silence, sur un compte cash
            leverage=1.0)
        res = engine.resultat_de(brut)
        lignes.append(_ligne(
            nom, res, b, regles, leviers, n_departs, False,
            n_longs=sum(t.direction == 1 for t in res.trades),
            n_shorts=sum(t.direction == -1 for t in res.trades)))
    return lignes
