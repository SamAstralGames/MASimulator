"""Backtest RaptorBT et criblage entrée x sortie.

Contrat avec RaptorBT : des tableaux de prix (numpy) et deux tableaux de
booleens (où entrer, où sortir). Exécution à l'ouverture de la barre suivante
(``next_bar_open``), le seul contrat honnête : on ne peut pas acheter à un prix
déjà passé.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import raptorbt

from . import moyennes, propfirm
from .data import Bougies

LEVIERS_DEFAUT = (0.5, 1.0, 1.5, 2.0, 3.0, 5.0)


@dataclass(frozen=True)
class Couts:
    """Tarifs du broker, dans les unités du broker."""
    spread: float = 0.0                 # dans l'unité du prix
    commission_par_lot_par_cote: float = 0.0
    taille_contrat: float = 1.0

    def fractions(self, prix_typique):
        """(fees, slippage) : fractions de la valeur échangée, par côté."""
        slippage = (self.spread / 2.0) / prix_typique
        fees = self.commission_par_lot_par_cote / (self.taille_contrat * prix_typique)
        return fees, slippage

    def aller_retour_bps(self, prix_typique):
        fees, slippage = self.fractions(prix_typique)
        return (fees + slippage) * 2 * 10_000


# Tarifs par défaut, MODIFIABLES dans l'interface : ce sont des ordres de
# grandeur, pas les tarifs de ton compte.
COUTS_PAR_SYMBOLE = {
    "XAUUSD": Couts(spread=0.20, commission_par_lot_par_cote=3.0, taille_contrat=100.0),
    "EURUSD": Couts(spread=0.00008, commission_par_lot_par_cote=3.0, taille_contrat=100_000.0),
}


def couts_par_defaut(symbole, prix_typique):
    """Tarifs connus du symbole, sinon un spread de 2 bps sans commission."""
    return COUTS_PAR_SYMBOLE.get(symbole.upper(), Couts(spread=prix_typique * 2e-4))


@dataclass(frozen=True)
class Trade:
    entree_idx: int
    sortie_idx: int | None
    prix_entree: float
    prix_sortie: float | None
    taille: float
    pnl: float
    rendement_pct: float
    frais: float
    raison_sortie: str
    direction: int = 1


@dataclass(frozen=True)
class Resultat:
    """Sortie d'un backtest : métriques scalaires, equity et trades."""
    metriques: dict
    equity: np.ndarray
    trades: list[Trade]
    exposition: float | None     # part du temps en position, 0..1


_METRIQUES = (
    "start_value", "end_value", "total_return_pct", "sharpe_ratio",
    "max_drawdown_pct", "total_trades", "win_rate_pct", "profit_factor",
    "total_fees_paid", "exposure_pct", "cost_to_gross_profit_pct",
)


def signaux(close, entree_type, sortie_type, rapide, lente, cache=None):
    """(entrées, sorties) : croisement haussier de l'entrée, baissier de la sortie.

    `cache` (dict) évite de recalculer une moyenne déjà vue quand on crible.
    """
    cache = cache if cache is not None else {}

    def position(nom):
        cle = (nom, rapide, lente)
        if cle not in cache:
            cache[cle] = moyennes.rapide_au_dessus(close, nom, rapide, lente)
        return cache[cle]

    entrees = moyennes.croisement_haussier(*position(entree_type))
    sorties = moyennes.croisement_baissier(*position(sortie_type))
    return entrees, sorties


def signaux_long_short(close, entree_type, sortie_type, rapide, lente, cache=None):
    """(entrée haussière, sortie baissière, sortie haussière), comme les lit BotX :
    l'entrée d'un long vient de la moyenne d'entrée, tout le reste (fermer un long,
    ouvrir un short, fermer un short) de la moyenne de SORTIE."""
    cache = cache if cache is not None else {}

    def position(nom):
        cle = (nom, rapide, lente)
        if cle not in cache:
            cache[cle] = moyennes.rapide_au_dessus(close, nom, rapide, lente)
        return cache[cle]

    return (moyennes.croisement_haussier(*position(entree_type)),
            moyennes.croisement_baissier(*position(sortie_type)),
            moyennes.croisement_haussier(*position(sortie_type)))


def nouvelle_config(fees, slippage, capital=10_000.0):
    return raptorbt.BacktestConfig(
        initial_capital=capital, fees=fees, slippage=slippage,
        fill_timing="next_bar_open",
    )


def resultat_de(res, avec_trades=True):
    """Convertit un résultat brut RaptorBT (array ou stratégie) en `Resultat`."""
    m = res.metrics
    metriques = {k: getattr(m, k, None) for k in _METRIQUES}
    trades = []
    if avec_trades:
        trades = [
            Trade(t.entry_idx, t.exit_idx, t.entry_price, t.exit_price, t.size,
                  t.pnl, t.return_pct, t.fees, str(t.exit_reason), getattr(t, "direction", 1))
            for t in res.trades()
        ]
    exposition = m.exposure_pct / 100 if m.exposure_pct is not None else None
    return Resultat(metriques, np.asarray(res.equity_curve(), float), trades, exposition)


def backtest(b: Bougies, entrees, sorties, fees, slippage, capital=10_000.0,
             stop_pct=None, target_pct=None, avec_trades=True, config=None):
    """Backtest long seul. `config` (optionnel) remplace stop_pct/target_pct :
    c'est par là que passent les politiques de sortie de `labo`."""
    if config is None:
        config = nouvelle_config(fees, slippage, capital)
        if stop_pct is not None:
            config.set_fixed_stop(stop_pct)
        if target_pct is not None:
            config.set_fixed_target(target_pct)

    res = raptorbt.run_single_backtest(
        timestamps=b.timestamps_ns(),
        open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume,
        entries=entrees, exits=sorties, direction=1, weight=1.0,
        symbol=b.symbole, config=config,
    )
    return resultat_de(res, avec_trades)


@dataclass(frozen=True)
class LigneCrible:
    """Une case du criblage : une paire (entrée, sortie) et ses métriques."""
    entree_type: str
    sortie_type: str
    rapide: int
    lente: int
    n_trades: int
    rendement_pct: float
    dd_pct: float
    sharpe: float | None
    win_rate_pct: float | None
    profit_factor: float | None
    p_reussite: dict = field(default_factory=dict)    # levier -> P(réussite)

    def meilleur_levier(self):
        if not self.p_reussite:
            return None, 0.0
        return max(self.p_reussite.items(), key=lambda kv: kv[1])


def cribler(b: Bougies, entree_types, sortie_types, rapide, lente, couts: Couts,
            regles: propfirm.Regles, leviers=LEVIERS_DEFAUT, n_departs=300,
            min_trades=20, capital=10_000.0, progres=None, annule=None, haussiere=None):
    """Teste toutes les paires entrée x sortie et renvoie une LigneCrible par paire.

    `haussiere` (tableau booléen, True = tendance de fond haussière) restreint les
    entrées aux barres où elle est vraie.

    `progres(fait, total)` est appelé après chaque paire ; `annule()` renvoie
    True pour interrompre. Une paire sous `min_trades` garde ses métriques mais
    n'a pas de P(réussite) : trop peu de trades pour que ça veuille dire quelque
    chose.
    """
    prix_typique = float(np.median(b.close))
    fees, slippage = couts.fractions(prix_typique)
    cache = {}
    paires = [(e, s) for e in entree_types for s in sortie_types]
    lignes = []
    for i, (e, s) in enumerate(paires):
        if annule is not None and annule():
            break
        entrees, sorties = signaux(b.close, e, s, rapide, lente, cache)
        if haussiere is not None:
            entrees = entrees & haussiere
        r = backtest(b, entrees, sorties, fees, slippage, capital, avec_trades=False)
        m = r.metriques
        n_trades = int(m["total_trades"] or 0)
        p = {}
        if n_trades >= min_trades:
            for lev in leviers:
                p[lev] = propfirm.simuler(r.equity, b.temps, regles, lev, n_departs).taux
        lignes.append(LigneCrible(
            e, s, rapide, lente, n_trades,
            m["total_return_pct"] or 0.0, m["max_drawdown_pct"] or 0.0,
            m["sharpe_ratio"], m["win_rate_pct"], m["profit_factor"], p))
        if progres is not None:
            progres(i + 1, len(paires))
    return lignes
