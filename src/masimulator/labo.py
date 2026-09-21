"""Détails d'une paire du criblage : politiques de sortie et long/short.

Port de ``exit_policy.comparer_sorties`` et ``long_short.comparer_tendance_long_short``
(RaptorBT), avec la colonne « gain » de ``labo_propfirm`` : l'écart entre la
P(réussite) de la stratégie et celle d'un témoin sans edge. Rien n'est affiché,
tout est renvoyé sous forme de lignes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
import raptorbt

from . import engine, propfirm
from .data import Bougies

TRAILINGS = (0.001, 0.002, 0.003, 0.005, 0.0075, 0.01, 0.015, 0.02, 0.025)
MULT_ATR = (1.5, 2.0, 3.0)
ATR_PERIODE = 14


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
    politique: str | None = None       # politique de sortie de la ligne, clé de `politiques()`
    distance_stop_pct: float | None = None   # stop ATR : distance médiane à l'entrée, en % du prix

    @property
    def rend_dd(self):
        return self.rendement_pct / self.dd_pct if self.dd_pct else None

    @property
    def parametres_botx(self):
        """Réglages BotX qui reproduisent la politique de la ligne, ou None si non transposable."""
        return parametres_botx(self.politique) if self.politique else None

    @property
    def stop_botx(self):
        """Libellé court du stop BotX (« Percent 0.5 % », « AtrFixed 2x », « aucun »)."""
        p = self.parametres_botx
        if p is None:
            return None
        if p["StopMode"] == "AtrFixed":
            return f"AtrFixed {p['AtrMultiplier']:g}x ATR{p['AtrPeriod']}"
        return f"Percent {p['TrailingStopPct']:g} %" if p["TrailingStopPct"] else "aucun"

    @property
    def risque_botx(self):
        """RiskPerTradePct de BotX qui reproduit le levier de la ligne, None si non transposable.

        Le levier de l'outil est une EXPOSITION (x1 = 100 % de l'equity en notionnel), alors que
        BotX dimensionne par le RISQUE : volume = R % d'equity / distance du stop. Donc
        - trailing de T % : R = levier x T (exact, la distance est T % du prix) ;
        - sans stop : R est pris comme exposition brute en %, donc R = 100 x levier ;
        - AtrFixed : la distance vaut mult x ATR / prix, qui change à chaque trade. R = levier x
          distance médiane à l'entrée est une APPROXIMATION : BotX garde le risque constant et
          laisse le notionnel varier, le simulateur fait l'inverse.
        """
        p = self.parametres_botx
        if p is None or self.levier is None:
            return None
        if p["StopMode"] == "AtrFixed":
            return None if self.distance_stop_pct is None else self.levier * self.distance_stop_pct
        t = p["TrailingStopPct"]
        return self.levier * t if t > 0 else self.levier * 100


def parametres_botx(politique):
    """Réglages BotX équivalents à une politique de sortie, ou None si aucun n'existe.

    - croisement seul : StopMode Percent avec TrailingStopPct 0 (stop désactivé) ;
    - trailing X % : StopMode Percent, TrailingStopPct X ;
    - stop ATR Mx : StopMode AtrFixed (stop statique posé à l'entrée, jamais déplacé), avec le même
      ATR de Wilder sur 14 périodes des deux côtés ;
    - « + target 1:2 » : BotX n'a pas de take profit, pas de correspondance.
    Le mode ATR de BotX (StopMode Atr) est un trailing : il ne correspond à aucune politique ici.
    """
    if politique == "croisement seul":
        return {"StopMode": "Percent", "TrailingStopPct": 0.0}
    m = re.fullmatch(r"trailing ([\d.]+)%", politique or "")
    if m:
        return {"StopMode": "Percent", "TrailingStopPct": float(m.group(1))}
    m = re.fullmatch(r"stop ATR ([\d.]+)x", politique or "")
    if m:
        return {"StopMode": "AtrFixed", "AtrMultiplier": float(m.group(1)), "AtrPeriod": ATR_PERIODE}
    return None


def _distance_atr_pct(b, res, mult, atr):
    """Distance médiane du stop ATR à l'entrée, en % du prix (mult x ATR / prix)."""
    idx = [max(t.entree_idx - 1, 0) for t in res.trades]
    if not idx:
        return None
    distances = mult * atr[idx] / b.close[idx] * 100
    distances = distances[np.isfinite(distances)]         # l'ATR est vide sur ses premières barres
    return float(np.median(distances)) if len(distances) else None


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


def _via_stop(res):
    """Trades sortis par un stop, un trailing ou une target plutôt que par le croisement.

    total_trades compte les entrées, pas le mécanisme de sortie : seule la RAISON de
    sortie dit si le stop a réellement coupé avant le signal.
    """
    return sum(t.raison_sortie in ("StopLoss", "TakeProfit") for t in res.trades)


def _distance_pour(politique, b, res, atr):
    p = parametres_botx(politique)
    if p is None or p["StopMode"] != "AtrFixed":
        return None
    return _distance_atr_pct(b, res, p["AtrMultiplier"], atr)


def politiques(fees, slippage, capital):
    """{nom: config} : la référence (croisement seul), les stops ATR, les trailings.

    set_atr_stop() et set_trailing_stop() écrivent au MÊME endroit de la config :
    on ne peut pas combiner un stop ATR et un trailing, il faut choisir.
    """
    p = {"croisement seul": engine.nouvelle_config(fees, slippage, capital)}
    for mult in MULT_ATR:
        c = engine.nouvelle_config(fees, slippage, capital)
        c.set_atr_stop(multiplier=mult, period=ATR_PERIODE)
        p[f"stop ATR {mult:g}x"] = c
        c = engine.nouvelle_config(fees, slippage, capital)
        c.set_atr_stop(multiplier=mult, period=ATR_PERIODE)
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
    atr = raptorbt.atr(b.high, b.low, b.close, period=ATR_PERIODE)
    lignes = []
    for nom, config in politiques(fees, slippage, capital).items():
        if annule is not None and annule():
            break
        res = engine.backtest(b, entrees, sorties, fees, slippage, capital, config=config)
        lignes.append(_ligne(nom, res, b, regles, leviers, n_departs, True,
                             via_stop=_via_stop(res), politique=nom,
                             distance_stop_pct=_distance_pour(nom, b, res, atr)))
    return lignes


class _StrategieLongShort(raptorbt.Strategy):
    """Long/short tel que BotX le pratique, pour que ce qu'on mesure soit ce que le bot fera.

    - long : ouvert sur le croisement haussier de la moyenne d'ENTRÉE, fermé sur le
      croisement baissier de la moyenne de SORTIE ;
    - short : ouvert sur ce même croisement baissier de sortie (depuis un long, c'est le
      retournement ; depuis le plat aussi), fermé sur le croisement HAUSSIER de la moyenne
      de sortie, jamais celle d'entrée ;
    - un croisement d'entrée pendant un long est ignoré ;
    - le filtre de tendance ne bloque que les ouvertures (long avec la tendance, short à
      contre) ; les sorties marchent toujours ;
    - stops, trailing et target viennent de la config du backtest, appliqués par le noyau
      aux deux directions.

    Écart connu avec BotX : le moteur interdit de fermer et d'ouvrir sur la même barre.
    Un retournement ferme à la barre suivante et rouvre une barre plus tard, alors que BotX
    ferme et ouvre sur la même barre.
    """

    def __init__(self, entree_haussiere, sortie_baissiere, sortie_haussiere, autoriser_long,
                 autoriser_short, haussiere=None, taille=1.0):
        super().__init__()
        self.entree_haut = np.asarray(entree_haussiere, bool)
        self.sortie_bas = np.asarray(sortie_baissiere, bool)
        self.sortie_haut = np.asarray(sortie_haussiere, bool)
        self.long_ok, self.short_ok = autoriser_long, autoriser_short
        self.haussiere = None if haussiere is None else np.asarray(haussiere, bool)
        self.taille = taille
        self.en_attente = None       # sens à ouvrir dès que la position en cours est fermée

    def _tendance_ok(self, sens, i):
        return self.haussiere is None or bool(self.haussiere[i]) == (sens == 1)

    def _decider_ouverture(self, i):
        """Sens à ouvrir à la barre i depuis le plat, ou 0."""
        if self.entree_haut[i] and self.long_ok and self._tendance_ok(1, i):
            return 1
        if self.sortie_bas[i] and self.short_ok and self._tendance_ok(-1, i):
            return -1
        return 0

    def _ouvrir(self, sens):
        if sens == 1:
            self.enter_long(size_frac=self.taille)
        else:
            self.enter_short(size_frac=self.taille)

    def on_bar(self, ctx):
        i, pos = ctx.idx, ctx.position
        if self.en_attente is not None:
            if pos is None:
                sens, self.en_attente = self.en_attente, None
                self._ouvrir(sens)
            return                      # sinon la fermeture demandée n'est pas encore passée

        if pos is None:
            sens = self._decider_ouverture(i)
            if sens:
                self._ouvrir(sens)
            return

        if (pos.direction == 1 and self.sortie_bas[i]) or (pos.direction == -1 and self.sortie_haut[i]):
            self.close_position()
            self.en_attente = self._decider_ouverture(i) or None


def noms_politiques():
    """Les noms des politiques de sortie, dans l'ordre d'affichage."""
    return list(politiques(0.0, 0.0, 10_000.0))


# variante -> (long autorisé, short autorisé) ; le suffixe " (tendance)" ajoute le filtre de tendance
DIRECTIONS = {"long seul": (True, False), "short seul": (False, True), "long + short": (True, True)}
SUFFIXE_TENDANCE = " (tendance)"


def decoder_direction(nom):
    """(long autorisé, short autorisé, avec filtre de tendance) d'un nom de variante."""
    base = nom.removesuffix(SUFFIXE_TENDANCE)
    long_ok, short_ok = DIRECTIONS[base]
    return long_ok, short_ok, nom.endswith(SUFFIXE_TENDANCE)


def backtest_long_short(b: Bougies, signaux, long_ok, short_ok, filtre, config):
    """Un backtest long/short façon BotX ; `filtre` : tendance haussière, ou None."""
    brut = raptorbt.run_strategy_backtest(
        _StrategieLongShort(*signaux, long_ok, short_ok, filtre),
        b.timestamps_ns(), b.open, b.high, b.low, b.close, b.volume,
        symbol=b.symbole, config=config,
        account_type="margin",      # un short est refusé, en silence, sur un compte cash
        leverage=1.0)
    return engine.resultat_de(brut)


def comparer_long_short(b: Bougies, signaux, couts: engine.Couts, regles, leviers,
                        n_departs=300, capital=10_000.0, haussiere=None,
                        politique="croisement seul", annule=None):
    """Long seul, short seul, long + short ; avec et sans filtre de tendance si `haussiere`.

    `signaux` = engine.signaux_long_short(...) (bruts, non filtrés : le filtre s'applique
    dans la stratégie, direction par direction). `politique` : une clé de `politiques()`
    (croisement seul, stop ATR, trailing...), appliquée aux deux directions.
    Pas de témoin ici : la dérive de l'actif joue contre un short, un témoin à dérive
    long-only serait trompeur.
    """
    fees, slippage = couts.fractions(float(np.median(b.close)))
    atr = raptorbt.atr(b.high, b.low, b.close, period=ATR_PERIODE)
    config_de = lambda: politiques(fees, slippage, capital)[politique]
    noms = list(DIRECTIONS)
    if haussiere is not None:
        noms += [n + SUFFIXE_TENDANCE for n in DIRECTIONS]
    lignes = []
    for nom in noms:
        if annule is not None and annule():
            break
        long_ok, short_ok, avec_tendance = decoder_direction(nom)
        res = backtest_long_short(b, signaux, long_ok, short_ok,
                                  haussiere if avec_tendance else None, config_de())
        lignes.append(_ligne(
            nom, res, b, regles, leviers, n_departs, False,
            n_longs=sum(t.direction == 1 for t in res.trades),
            n_shorts=sum(t.direction == -1 for t in res.trades),
            via_stop=_via_stop(res), politique=politique,
            distance_stop_pct=_distance_pour(politique, b, res, atr)))
    return lignes
