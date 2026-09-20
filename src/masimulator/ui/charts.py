"""Construction des figures Plotly. Aucune dépendance à Qt."""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .. import moyennes
from ..propfirm import ISSUES, Regles

# Palette Okabe-Ito : lisible aussi en daltonisme.
COULEURS_ISSUE = {
    "REUSSI": "#009E73",
    "ECHEC_DD": "#D55E00",
    "ECHEC_JOUR": "#E69F00",
    "TEMPS": "#999999",
}
LIBELLES_ISSUE = {
    "REUSSI": "Réussi",
    "ECHEC_DD": "Échec drawdown",
    "ECHEC_JOUR": "Échec perte jour",
    "TEMPS": "Temps écoulé",
}
BLEU, ORANGE, GRIS = "#0072B2", "#E69F00", "#7f7f7f"

# Métrique -> (libellé, extracteur, format, échelle de couleur, inverser l'échelle)
METRIQUES = {
    "p_best": "P(réussite), meilleur levier",
    "p_levier": "P(réussite) au levier choisi",
    "rendement": "Rendement (%)",
    "dd": "Drawdown max (%)",
    "sharpe": "Sharpe",
    "trades": "Nombre de trades",
}


def _base(fig, titre=None):
    fig.update_layout(
        template="plotly_white", title=dict(text=titre, x=0.01) if titre else None,
        margin=dict(l=60, r=24, t=48 if titre else 24, b=44),
        font=dict(size=12), hoverlabel=dict(font_size=12),
    )
    return fig


def _decimer(y, n=5000):
    """Indices à tracer : min et max de chaque tranche, pour garder les pics.

    On évite Scattergl (WebGL n'est pas garanti sous QtWebEngine) ; le SVG reste
    fluide jusqu'à quelques milliers de points par courbe.
    """
    y = np.asarray(y, float)
    k = max(len(y) * 2 // n, 1)
    if k <= 2:
        return np.arange(len(y))
    m = len(y) // k
    blocs = y[:m * k].reshape(m, k)
    base = np.arange(m) * k
    idx = np.concatenate([base + np.nanargmin(blocs, axis=1), base + np.nanargmax(blocs, axis=1),
                          [0, len(y) - 1]])
    return np.unique(idx)


def _valeur(ligne, metrique, levier):
    if metrique == "p_best":
        return ligne.meilleur_levier()[1] * 100 if ligne.p_reussite else np.nan
    if metrique == "p_levier":
        p = ligne.p_reussite.get(levier)
        return p * 100 if p is not None else np.nan
    if metrique == "rendement":
        return ligne.rendement_pct
    if metrique == "dd":
        return ligne.dd_pct
    if metrique == "sharpe":
        return ligne.sharpe if ligne.sharpe is not None else np.nan
    return float(ligne.n_trades)


def heatmap_crible(lignes, metrique="p_best", levier=1.0, regles=None):
    """Matrice entrée (lignes) x sortie (colonnes) ; un clic donne (y=entrée, x=sortie)."""
    types = moyennes.TYPES
    z = np.full((len(types), len(types)), np.nan)
    survol = [[""] * len(types) for _ in types]
    idx = {t: i for i, t in enumerate(types)}
    for l in lignes:
        i, j = idx[l.entree_type], idx[l.sortie_type]
        z[i, j] = _valeur(l, metrique, levier)
        lev, p = l.meilleur_levier()
        p_txt = f"{p * 100:.0f} % (levier {lev:g})" if l.p_reussite else "n/a (trop peu de trades)"
        sharpe = f"{l.sharpe:.2f}" if l.sharpe is not None else "n/a"
        survol[i][j] = (
            f"<b>{l.entree_type} → {l.sortie_type}</b><br>"
            f"trades : {l.n_trades}<br>rendement : {l.rendement_pct:+.1f} %<br>"
            f"DD max : {l.dd_pct:.1f} %<br>Sharpe : {sharpe}<br>"
            f"P(réussite) : {p_txt}")

    est_p = metrique in ("p_best", "p_levier")
    inverse = metrique == "dd"
    fig = go.Figure(go.Heatmap(
        z=z, x=list(types), y=list(types), customdata=survol,
        hovertemplate="%{customdata}<extra></extra>",
        colorscale="Blues_r" if inverse else "Blues",
        zmin=0 if est_p else None, zmax=100 if est_p else None,
        text=[[("" if np.isnan(v) else f"{v:.0f}" if est_p or metrique == "trades" else f"{v:.1f}")
               for v in row] for row in z],
        texttemplate="%{text}", xgap=2, ygap=2,
        colorbar=dict(thickness=12, title=dict(text="%" if est_p else "")),
    ))
    fig.update_yaxes(title="Entrée (croisement haussier)", autorange="reversed")
    fig.update_xaxes(title="Sortie (croisement baissier)", side="top")
    titre = METRIQUES[metrique] + (f" (levier {levier:g})" if metrique == "p_levier" else "")
    if est_p and regles is not None:
        titre += f"   ·   hasard pur : {regles.hasard_pur() * 100:.0f} %"
    return _base(fig, titre).update_layout(margin=dict(l=90, r=24, t=110, b=24),
                                           title=dict(y=0.97, x=0.01))


def figure_detail(b, res, entree_type, sortie_type, rapide, lente):
    """Prix + moyennes + trades, equity, drawdown, sur un axe temps partagé."""
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.03,
                        row_heights=[0.5, 0.3, 0.2])
    ip = _decimer(b.close)
    fig.add_trace(go.Scatter(x=b.temps[ip], y=b.close[ip], mode="lines", name="Clôture",
                             line=dict(color="#555", width=1)), row=1, col=1)
    for nom, dash, tag in ((entree_type, "solid", "entrée"), (sortie_type, "dot", "sortie")):
        if dash == "dot" and sortie_type == entree_type:
            continue
        for periode, couleur in ((rapide, BLEU), (lente, ORANGE)):
            fig.add_trace(go.Scatter(
                x=b.temps[ip], y=moyennes.CATALOGUE[nom](b.close, periode)[ip], mode="lines",
                name=f"{nom} {periode} ({tag})", line=dict(color=couleur, width=1, dash=dash),
            ), row=1, col=1)

    ok = [t for t in res.trades if t.sortie_idx is not None]
    if ok:
        fig.add_trace(go.Scatter(
            x=b.temps[[t.entree_idx for t in ok]], y=[t.prix_entree for t in ok],
            mode="markers", name="Entrée",
            marker=dict(symbol="triangle-up", size=8, color="#009E73"),
            hovertemplate="Entrée %{y:.5g}<extra></extra>"), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=b.temps[[t.sortie_idx for t in ok]], y=[t.prix_sortie for t in ok],
            mode="markers", name="Sortie",
            marker=dict(symbol="triangle-down", size=8,
                        color=["#009E73" if t.pnl > 0 else "#D55E00" for t in ok]),
            text=[f"{t.pnl:+.2f} ({t.raison_sortie})" for t in ok],
            hovertemplate="Sortie %{y:.5g}<br>PnL %{text}<extra></extra>"), row=1, col=1)

    eq = res.equity[:len(b)]
    ie = _decimer(eq)
    fig.add_trace(go.Scatter(x=b.temps[ie], y=eq[ie], mode="lines", name="Equity",
                             line=dict(color=BLEU, width=1.5), showlegend=False), row=2, col=1)
    dd = (eq / np.maximum.accumulate(eq) - 1) * 100
    idd = _decimer(dd)
    fig.add_trace(go.Scatter(x=b.temps[idd], y=dd[idd], mode="lines", name="Drawdown",
                             fill="tozeroy", line=dict(color="#D55E00", width=1),
                             showlegend=False), row=3, col=1)
    fig.update_yaxes(title="Prix", row=1, col=1)
    fig.update_yaxes(title="Equity", row=2, col=1)
    fig.update_yaxes(title="DD %", row=3, col=1)
    fig.update_layout(hovermode="x unified", legend=dict(orientation="h", y=1.04, x=0))
    return _base(fig).update_layout(margin=dict(l=60, r=24, t=36, b=36))


def figure_leviers(sims, temoins, regles: Regles):
    """Issues des challenges par levier (barres empilées) + témoin sans edge."""
    leviers = [f"x{s.levier:g}" for s in sims]
    fig = go.Figure()
    for issue in ISSUES:
        fig.add_trace(go.Bar(
            x=leviers, y=[s.part(issue) * 100 for s in sims],
            name=LIBELLES_ISSUE[issue], marker_color=COULEURS_ISSUE[issue],
            hovertemplate="%{y:.0f} %<extra>" + LIBELLES_ISSUE[issue] + "</extra>"))
    fig.add_trace(go.Scatter(
        x=leviers, y=[t * 100 for t in temoins], mode="markers+lines", name="Témoin sans edge",
        line=dict(color="black", dash="dot", width=1), marker=dict(symbol="diamond", size=9),
        hovertemplate="%{y:.0f} %<extra>Témoin</extra>"))
    fig.add_hline(y=regles.hasard_pur() * 100, line=dict(color=GRIS, dash="dash", width=1),
                  annotation_text=f"hasard pur {regles.hasard_pur() * 100:.0f} %",
                  annotation_position="top left")
    fig.update_layout(barmode="stack", legend=dict(orientation="h", y=-0.12, x=0))
    fig.update_yaxes(title="% des challenges simulés", range=[0, 100])
    return _base(fig, f"Issues du challenge selon le levier  ({regles})")


def figure_departs(temps, sim):
    """Issue de chaque challenge selon sa date de départ."""
    debuts = temps[list(sim.departs)]
    fig = go.Figure()
    for issue in ISSUES:
        masque = np.array([r == issue for r in sim.resultats])
        if not masque.any():
            continue
        fig.add_trace(go.Scatter(
            x=debuts[masque], y=[LIBELLES_ISSUE[issue]] * int(masque.sum()), mode="markers",
            name=LIBELLES_ISSUE[issue], marker=dict(color=COULEURS_ISSUE[issue], size=7,
                                                    opacity=0.65),
            hovertemplate="départ %{x|%Y-%m-%d %H:%M}<extra></extra>"))
    fig.update_yaxes(categoryorder="array",
                     categoryarray=[LIBELLES_ISSUE[i] for i in reversed(ISSUES)])
    fig.update_layout(showlegend=False)
    return _base(fig, f"Issue selon la date de départ  (levier x{sim.levier:g}, "
                      f"{sim.n} départs, réussite {sim.taux * 100:.0f} %)")


def heatmap_fenetres(z, colonnes, lignes, hover, titre, regles=None):
    """P(réussite) par symbole (lignes) et par fenêtre (colonnes)."""
    fig = go.Figure(go.Heatmap(
        z=np.array(z, float) * 100, x=colonnes, y=lignes, customdata=hover,
        hovertemplate="%{customdata}<extra></extra>", colorscale="Blues", zmin=0, zmax=100,
        text=[[("" if np.isnan(v) else f"{v * 100:.0f}") for v in row] for row in z],
        texttemplate="%{text}", xgap=2, ygap=2, colorbar=dict(thickness=12, title=dict(text="%")),
    ))
    fig.update_yaxes(autorange="reversed")
    if regles is not None:
        titre += f"   ·   hasard pur : {regles.hasard_pur() * 100:.0f} %"
    return _base(fig, titre)
