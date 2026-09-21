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


def figure_suivi(suivis, libelles, selection, regles: Regles):
    """Le dernier mois de plusieurs configs : rendement, drawdown et P(réussite) glissante.

    Trois panneaux sur le même axe temps (jamais deux échelles sur un même axe). Une seule
    config est en couleur, celle qu'on regarde ; les autres restent en gris, lisibles au
    survol : avec vingt courbes, vingt couleurs ne diraient plus rien. Chaque trace porte l'id
    de sa config dans `meta`, pour qu'un clic sur la courbe la sélectionne dans le tableau.
    """
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.06,
                        row_heights=[0.4, 0.3, 0.3])
    if not suivis:
        fig.add_annotation(text="Aucune config calculée", showarrow=False, font=dict(size=14, color=GRIS),
                           xref="paper", yref="paper", x=0.5, y=0.5)
        fig.update_xaxes(visible=False)
        fig.update_yaxes(visible=False)
        return _base(fig)

    # la sélection en dernier : elle se dessine par-dessus
    for s in sorted(suivis, key=lambda s: s.id == selection):
        choisie = s.id == selection
        ligne = dict(color=BLEU if choisie else "#B5B5B5", width=2.5 if choisie else 1)
        nom = libelles.get(s.id, f"#{s.id}")
        groupe = str(s.id)
        entete = "<b>%{fullData.name}</b><br>%{x|%d %b %H:%M}<br>"

        ir = _decimer(s.rendement, 600)
        fig.add_trace(go.Scatter(
            x=s.temps[ir], y=s.rendement[ir], mode="lines", name=nom, meta=s.id, line=ligne,
            legendgroup=groupe, showlegend=choisie,
            hovertemplate=entete + "rendement %{y:+.1f} %<extra></extra>"), row=1, col=1)
        idd = _decimer(s.drawdown, 600)
        fig.add_trace(go.Scatter(
            x=s.temps[idd], y=s.drawdown[idd], mode="lines", name=nom, meta=s.id, line=ligne,
            legendgroup=groupe, showlegend=False,
            hovertemplate=entete + "drawdown %{y:.1f} %<extra></extra>"), row=2, col=1)
        fig.add_trace(go.Scatter(
            x=s.p_dates, y=s.p_valeurs * 100, mode="lines+markers", name=nom, meta=s.id,
            line=ligne, marker=dict(size=7 if choisie else 4, color=ligne["color"]),
            legendgroup=groupe, showlegend=False, connectgaps=False,
            hovertemplate=entete + f"P(réussite) %{{y:.0f}} % (fenêtre {s.fenetre_jours} j)"
                                   "<extra></extra>"), row=3, col=1)

    ref = dict(width=1, dash="dash")
    fig.add_hline(y=regles.cible_pct, line=dict(color=COULEURS_ISSUE["REUSSI"], **ref), row=1, col=1,
                  annotation_text=f"cible +{regles.cible_pct:g} %", annotation_position="top left")
    fig.add_hline(y=0, line=dict(color="#999", width=1), row=1, col=1)
    fig.add_hline(y=-regles.dd_max_pct, line=dict(color=COULEURS_ISSUE["ECHEC_DD"], **ref), row=2, col=1,
                  annotation_text=f"DD max -{regles.dd_max_pct:g} %", annotation_position="bottom left")
    fig.add_hline(y=regles.hasard_pur() * 100, line=dict(color=GRIS, **ref), row=3, col=1,
                  annotation_text=f"hasard pur {regles.hasard_pur() * 100:.0f} %",
                  annotation_position="bottom left")
    fig.update_yaxes(title="Rendement %", row=1, col=1)
    fig.update_yaxes(title="DD %", row=2, col=1)
    fig.update_yaxes(title="P(réussite) %", range=[0, 100], row=3, col=1)
    fig.update_layout(hovermode="closest", legend=dict(orientation="h", y=1.08, x=0))
    return _base(fig).update_layout(margin=dict(l=60, r=24, t=36, b=36))
