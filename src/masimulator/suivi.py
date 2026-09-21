"""Suivi des configs sauvegardées : comment se comportent-elles sur le dernier mois ?

Une config sauvegardée garde un instantané de ses métriques au jour de la sauvegarde. Ici on
la rejoue sur les données les plus récentes du cache, avec ses propres réglages (moyennes,
filtre de tendance, politique de sortie, direction, coûts, règles du challenge, levier), pour
voir si elle tient.

Le backtest tourne UNE fois, en continu, sur la fenêtre de la config prolongée jusqu'à la
dernière bougie. Tout le reste s'en déduit :
- la courbe du dernier mois, au levier de la config (rendement et drawdown) ;
- la P(réussite) glissante : celle de la fenêtre de la config (sa longueur d'origine) arrêtée
  à chaque date du dernier mois. Le dernier point est la P(réussite) « d'aujourd'hui », à
  comparer à celle de la sauvegarde ;
- l'issue du challenge qu'on aurait lancé il y a un mois.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import data, engine, labo, propfirm
from . import tendance as tend
from .configs import ConfigSauvee

JOURS = 30                   # « le dernier mois »
PAS_P_JOURS = 3              # une P(réussite) glissante tous les 3 jours
FENETRE_MIN, FENETRE_MAX = 60, 270   # jours : bornes de la fenêtre de la P(réussite) glissante
MARGE_JOURS = 5              # amorçage des moyennes avant la première barre utile


class Interrompu(Exception):
    """L'utilisateur a arrêté le calcul."""


@dataclass(frozen=True)
class Suivi:
    id: int
    derniere_bougie: np.datetime64
    levier: float
    regles: propfirm.Regles
    temps: np.ndarray            # barres du dernier mois
    rendement: np.ndarray        # % depuis le début du mois, au levier
    drawdown: np.ndarray         # % (<= 0) au levier, selon la règle du challenge (suiveur ou fixe)
    rendement_pct: float
    dd_pct: float                # DD max du mois, positif
    n_trades: int                # trades ouverts pendant le mois
    issue: str                   # issue (propfirm.ISSUES) du challenge parti il y a un mois
    p_dates: np.ndarray          # fins de fenêtre de la P(réussite) glissante
    p_valeurs: np.ndarray        # fraction, NaN si indisponible (trop peu de trades ou de données)
    p_sauvee: float | None       # P(réussite) au moment de la sauvegarde
    fenetre_jours: int

    @property
    def p_actuelle(self):
        v = self.p_valeurs[-1] if len(self.p_valeurs) else np.nan
        return None if np.isnan(v) else float(v)

    @property
    def rend_dd(self):
        return self.rendement_pct / self.dd_pct if self.dd_pct else None


def levier_et_p_sauvee(c: ConfigSauvee):
    """(levier, P(réussite)) de la stratégie sauvegardée : celle de l'onglet de détail choisi,
    à défaut celle du criblage."""
    m = c.metriques
    d, cr = m.get("direction") or m.get("politique"), m.get("crible", {})
    if d and d.get("levier"):
        return float(d["levier"]), d.get("p_reussite")
    return float(cr.get("meilleur_levier") or 1.0), cr.get("meilleur_p")


def _regles(c):
    return propfirm.Regles(**c.parametres["regles"]) if c.parametres.get("regles") \
        else propfirm.Regles()


def _fenetre_jours(c):
    jours = int((c.fin - c.debut) / np.timedelta64(1, "D"))
    return min(max(jours, FENETRE_MIN), FENETRE_MAX)


def backtest_config(b, c: ConfigSauvee, haussiere):
    """Rejoue la stratégie sauvegardée sur `b` : engine.Resultat.

    Deux chemins, comme à la sauvegarde : une config de l'onglet « long / short » porte une
    direction (et son filtre de tendance seulement si la variante le dit) ; une config de
    l'onglet « croisement / stop / trailing » est long seul avec le filtre de tendance du criblage.
    """
    par = c.parametres
    prix = float(np.median(b.close))
    couts = engine.Couts(**par["couts"]) if par.get("couts") else engine.couts_par_defaut(c.symbole, prix)
    fees, slippage = couts.fractions(prix)
    capital = par.get("capital", 10_000.0)
    config = labo.politiques(fees, slippage, capital)[c.politique or "croisement seul"]
    if "direction" in c.metriques:
        signaux = engine.signaux_long_short(b.close, c.entree_type, c.sortie_type, c.rapide, c.lente)
        long_ok, short_ok, avec_tendance = labo.decoder_direction(c.direction)
        return labo.backtest_long_short(b, signaux, long_ok, short_ok,
                                        haussiere if avec_tendance else None, config)
    entrees, sorties = engine.signaux(b.close, c.entree_type, c.sortie_type, c.rapide, c.lente)
    if haussiere is not None:
        entrees = entrees & haussiere
    return engine.backtest(b, entrees, sorties, fees, slippage, capital, config=config)


def _courbe_mois(eq, levier, regles):
    """(rendement %, drawdown %) au levier, à partir de l'equity du mois (première valeur = base)."""
    rends = np.maximum(1.0 + levier * np.diff(eq) / eq[:-1], 0.0)
    e = np.concatenate([[1.0], np.cumprod(rends)])
    if regles.dd_trailing:
        sommet = np.maximum.accumulate(np.maximum(e, 1.0))
        dd = e / sommet - 1.0
    else:
        dd = np.minimum(e - 1.0, 0.0)
    return (e - 1.0) * 100, dd * 100


def _suivre_un(b, c, haussiere, annule):
    regles = _regles(c)
    levier, p_sauvee = levier_et_p_sauvee(c)
    fenetre = _fenetre_jours(c)
    par = c.parametres
    min_trades = par.get("min_trades", 20)
    n_departs = par.get("n_departs", 300)

    res = backtest_config(b, c, haussiere)
    if annule():
        raise Interrompu
    eq = res.equity[:len(b)]
    fin = b.temps[-1]
    i0 = int(np.searchsorted(b.temps, fin - np.timedelta64(JOURS, "D")))
    k0 = max(i0 - 1, 0)                        # la position déjà ouverte au début du mois compte
    temps_m, eq_m = b.temps[k0:], eq[k0:]
    rendement, drawdown = _courbe_mois(eq_m, levier, regles)
    entrees = np.array([t.entree_idx for t in res.trades], int)
    try:
        issue = propfirm.issue_depuis_le_debut(eq_m, temps_m, regles, levier)
    except (ValueError, IndexError):
        issue = "TEMPS"

    dates, valeurs = [], []
    for k in range(JOURS // PAS_P_JOURS, -1, -1):
        if annule():
            raise Interrompu
        fin_k = fin - np.timedelta64(k * PAS_P_JOURS, "D")
        j = int(np.searchsorted(b.temps, fin_k, side="right"))
        i = int(np.searchsorted(b.temps, fin_k - np.timedelta64(fenetre, "D")))
        p = np.nan
        if np.count_nonzero((entrees >= i) & (entrees < j)) >= min_trades:
            try:
                p = propfirm.simuler(eq[i:j], b.temps[i:j], regles, levier, n_departs).taux
            except ValueError:                  # fenêtre ou historique trop court
                pass
        dates.append(fin_k)
        valeurs.append(p)

    return Suivi(
        id=c.id, derniere_bougie=fin, levier=levier, regles=regles, temps=temps_m,
        rendement=rendement, drawdown=drawdown, rendement_pct=float(rendement[-1]),
        dd_pct=float(-drawdown.min()), n_trades=int(np.count_nonzero(entrees >= i0)), issue=issue,
        p_dates=np.array(dates, "datetime64[ns]"), p_valeurs=np.array(valeurs, float),
        p_sauvee=p_sauvee, fenetre_jours=fenetre)


def suivre(a_suivre, annule=lambda: False, racine=None):
    """Générateur : (config, Suivi ou message d'erreur) au fil du calcul.

    Les configs sont regroupées par (symbole, unité) pour ne lire les bougies qu'une fois.
    Le générateur s'arrête dès que `annule()` devient vrai : ce qui a été produit reste valable.
    """
    groupes = {}
    for c in a_suivre:
        groupes.setdefault((c.symbole, c.unite), []).append(c)
    for (symbole, unite), configs in groupes.items():
        if annule():
            return
        try:
            debut = np.datetime64("today") - np.timedelta64(
                JOURS + max(_fenetre_jours(c) for c in configs) + MARGE_JOURS + 30, "D")
            # `debut` part d'aujourd'hui, pas de la dernière bougie : un cache en retard de
            # quelques semaines reste couvert grâce aux 30 jours de plus.
            tout = data.charger(symbole, unite, debut)
        except Exception as e:
            for c in configs:
                yield c, f"Données indisponibles : {type(e).__name__} : {e}"
            continue
        fin = tout.temps[-1]
        cache_tendance = {}
        for c in configs:
            if annule():
                return
            try:
                depart = fin - np.timedelta64(JOURS + _fenetre_jours(c) + MARGE_JOURS, "D")
                i = int(np.searchsorted(tout.temps, depart))
                b = tout.tranche(i, len(tout))
                cle = (c.tendance, i)
                if cle not in cache_tendance:
                    cache_tendance[cle] = tend.calculer(b, c.tendance, racine)
                yield c, _suivre_un(b, c, cache_tendance[cle], annule)
            except Interrompu:
                return
            except Exception as e:
                yield c, f"{type(e).__name__} : {e}"
