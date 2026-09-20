"""Configs sauvegardées dans une base DuckDB, avec une note.

Une config est ce que l'utilisateur a trouvé et veut retrouver : le symbole, la
fenêtre, la paire de moyennes, le filtre de tendance, la politique de sortie et
la variante long/short retenues, plus un instantané des métriques au moment de la
sauvegarde. Les métriques sont un souvenir, pas un état : elles ne bougent plus
quand le marché avance.

La base est un fichier DuckDB ordinaire, requêtable à la main :
    duckdb ~/.local/share/masimulator/configs.duckdb "select id, note, symbole from configs"
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import duckdb
import numpy as np

from .tendance import Tendance

_SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS seq_configs START 1;
CREATE TABLE IF NOT EXISTS configs (
    id BIGINT PRIMARY KEY DEFAULT nextval('seq_configs'),
    cree_le TIMESTAMP,
    note VARCHAR,
    symbole VARCHAR NOT NULL,
    unite VARCHAR NOT NULL,
    debut DATE NOT NULL,
    fin DATE NOT NULL,
    entree_type VARCHAR NOT NULL,
    sortie_type VARCHAR NOT NULL,
    rapide INTEGER NOT NULL,
    lente INTEGER NOT NULL,
    tendance_mode VARCHAR NOT NULL,
    tendance_ma VARCHAR,
    tendance_unite VARCHAR,
    tendance_periode INTEGER,
    tendance_rapide INTEGER,
    tendance_lente INTEGER,
    politique VARCHAR,
    direction VARCHAR,
    parametres VARCHAR,
    metriques VARCHAR
);
"""


@dataclass(frozen=True)
class Config:
    note: str
    symbole: str
    unite: str
    debut: np.datetime64
    fin: np.datetime64
    entree_type: str
    sortie_type: str
    rapide: int
    lente: int
    tendance: Tendance
    politique: str = "croisement seul"
    direction: str = "long seul"
    parametres: dict = field(default_factory=dict)   # règles du challenge, capital, leviers, coûts
    metriques: dict = field(default_factory=dict)    # instantané : "crible", "politique", "direction"


@dataclass(frozen=True)
class ConfigSauvee(Config):
    id: int = 0
    cree_le: datetime | None = None


def chemin_base():
    """MASIM_DB, sinon ~/.local/share/masimulator/configs.duckdb."""
    if os.environ.get("MASIM_DB"):
        return Path(os.environ["MASIM_DB"]).expanduser()
    donnees = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
    return donnees / "masimulator" / "configs.duckdb"


def _connexion(chemin=None):
    chemin = Path(chemin) if chemin else chemin_base()
    chemin.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(chemin))
    con.execute(_SCHEMA)
    return con


def _json(objet):
    """JSON tolérant aux types numpy et aux clés flottantes (niveaux de levier)."""
    def defaut(o):
        if isinstance(o, np.generic):
            return o.item()
        if isinstance(o, np.ndarray):
            return o.tolist()
        return str(o)
    return json.dumps(objet, default=defaut, ensure_ascii=False)


def _jour(date):
    return str(np.datetime_as_string(np.datetime64(date, "D"), unit="D"))


def sauvegarder(config: Config, chemin=None):
    """Enregistre la config et renvoie son id."""
    t = config.tendance
    con = _connexion(chemin)
    try:
        return con.execute(
            """INSERT INTO configs (cree_le, note, symbole, unite, debut, fin, entree_type,
                   sortie_type, rapide, lente, tendance_mode, tendance_ma, tendance_unite,
                   tendance_periode, tendance_rapide, tendance_lente, politique, direction,
                   parametres, metriques)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            [datetime.now().replace(microsecond=0), config.note, config.symbole, config.unite,
             _jour(config.debut), _jour(config.fin), config.entree_type, config.sortie_type,
             config.rapide, config.lente, t.mode, t.ma_type, t.unite, t.periode, t.rapide,
             t.lente, config.politique, config.direction, _json(config.parametres),
             _json(config.metriques)]).fetchone()[0]
    finally:
        con.close()


def lister(chemin=None):
    """Toutes les configs sauvegardées, la plus récente d'abord."""
    con = _connexion(chemin)
    try:
        lignes = con.execute("SELECT * FROM configs ORDER BY cree_le DESC, id DESC").fetchall()
        colonnes = [c[0] for c in con.description]
    finally:
        con.close()
    configs = []
    for ligne in lignes:
        r = dict(zip(colonnes, ligne))
        configs.append(ConfigSauvee(
            id=r["id"], cree_le=r["cree_le"], note=r["note"] or "", symbole=r["symbole"],
            unite=r["unite"], debut=np.datetime64(r["debut"], "D"), fin=np.datetime64(r["fin"], "D"),
            entree_type=r["entree_type"], sortie_type=r["sortie_type"], rapide=r["rapide"],
            lente=r["lente"],
            tendance=Tendance(r["tendance_mode"], r["tendance_ma"] or "EMA",
                              r["tendance_unite"] or "h1", r["tendance_periode"] or 200,
                              r["tendance_rapide"] or 20, r["tendance_lente"] or 50),
            politique=r["politique"] or "", direction=r["direction"] or "",
            parametres=json.loads(r["parametres"] or "{}"),
            metriques=json.loads(r["metriques"] or "{}")))
    return configs


def modifier_note(id_config, note, chemin=None):
    con = _connexion(chemin)
    try:
        con.execute("UPDATE configs SET note = ? WHERE id = ?", [note, id_config])
    finally:
        con.close()


def supprimer(id_config, chemin=None):
    con = _connexion(chemin)
    try:
        con.execute("DELETE FROM configs WHERE id = ?", [id_config])
    finally:
        con.close()


def _pct(v):
    return "n/a" if v is None else f"{v * 100:.0f} %"


def resume(c: Config):
    """Texte lisible et copiable : de quoi retrouver et recréer la config à la main."""
    p = c.parametres.get("regles", {})
    lignes = [
        f"{c.symbole} {c.unite.upper()}  ·  {_jour(c.debut)} -> {_jour(c.fin)}",
        f"Entrée {c.entree_type} -> sortie {c.sortie_type}, moyennes {c.rapide}/{c.lente}",
        "Filtre de tendance : " + (c.tendance.libelle().removeprefix("tendance : ")
                                   if c.tendance.active else "aucun"),
        f"Politique de sortie : {c.politique}   ·   Direction : {c.direction}",
    ]
    if p:
        lignes.append(
            f"Challenge : cible +{p.get('cible_pct', 0):g} % / DD -{p.get('dd_max_pct', 0):g} % "
            f"{'trailing' if p.get('dd_trailing') else 'fixe'} / perte jour "
            f"-{p.get('perte_jour_pct', 0):g} % / {p.get('jours_max', 0)} j")
    couts = c.parametres.get("couts")
    if couts:
        lignes.append(f"Coûts : spread {couts.get('spread', 0):g}, commission "
                      f"{couts.get('commission_par_lot_par_cote', 0):g}/lot/côté, contrat "
                      f"{couts.get('taille_contrat', 1):g}")
    m = c.metriques
    cr = m.get("crible")
    if cr:
        meilleur = ""
        if cr.get("meilleur_p") is not None:
            meilleur = (f", P(réussite) {_pct(cr['meilleur_p'])} à x{cr['meilleur_levier']:g} "
                        f"(hasard {_pct(cr.get('hasard'))})")
        lignes.append(f"Au moment de la sauvegarde (criblage, long seul) : {cr['n_trades']} trades, "
                      f"{cr['rendement_pct']:+.1f} %, DD {cr['dd_pct']:.1f} %{meilleur}")
    for cle, titre in (("politique", "Sortie"), ("direction", "Direction")):
        d = m.get(cle)
        if d:
            levier = f", x{d['levier']:g}" if d.get("levier") is not None else ""
            titre += f" (sortie {d['politique']})" if d.get("politique") else ""
            lignes.append(f"  {titre} « {d['nom']} » : {d['n_trades']} trades, "
                          f"{d['rendement_pct']:+.1f} %, DD {d['dd_pct']:.1f} %, "
                          f"P(réussite) {_pct(d.get('p_reussite'))}{levier}")
            if d.get("risque_botx") is not None:
                t = d["trailing_botx"]
                lignes.append(f"    BotX : RiskPerTradePct {d['risque_botx']:.3g}, "
                              + (f"TrailingStopPct {t:g}" if t else "TrailingStopPct 0 (sans stop)"))
            elif d.get("levier") is not None:
                lignes.append("    BotX : non transposable (le stop ATR du simulateur est fixe, "
                              "le mode ATR de BotX est un trailing)")
    if c.note.strip():
        lignes.append(f"Note : {c.note.strip()}")
    return "\n".join(lignes)
