"""Fenêtre principale : panneau de paramètres à gauche, vues en onglets à droite."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace

import os

import numpy as np
from PySide6.QtCore import QDate, QItemSelectionModel, QSettings, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateEdit, QDialog,
    QDialogButtonBox, QDockWidget, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox,
    QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from .. import cli, configs, data, engine, horaire, labo, moyennes, propfirm, suivi
from .. import tendance as tend
from . import charts
from .webview import PlotlyView

ONGLETS = ("Vue d'ensemble", "Criblage", "Détail", "Challenge", "Comparaison")
T_APERCU, T_CRIBLE, T_DETAIL, T_CHALLENGE, T_COMPARAISON = range(5)
FRAICHEUR_MAX_JOURS = 7
ROUGE = QBrush(QColor("#D55E00"))
_AUCUNE = engine.LigneCrible("", "", 0, 0, 0, 0.0, 0.0, None, None, None)


# --------------------------------------------------------------------------
# Structures
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Params:
    symboles: tuple
    unite: str
    debut: np.datetime64
    fin: np.datetime64
    rapide: int
    lente: int
    capital: float
    regles: propfirm.Regles
    leviers: tuple
    n_departs: int
    min_trades: int
    couts: dict          # symbole -> (spread | None, commission, contrat)
    reglages: cli.Reglages
    telecharger: bool    # combler les trous du cache avec ctrader-cli
    tendance: tend.Tendance


@dataclass
class ResultatSymbole:
    bougies: data.Bougies | None = None
    alertes: list = field(default_factory=list)
    couts: engine.Couts | None = None
    lignes: list = field(default_factory=list)
    erreur: str | None = None
    tendance: tend.Tendance = tend.Tendance()
    haussiere: np.ndarray | None = None       # True = tendance de fond haussière

    def meilleure(self):
        """Ligne au meilleur P(réussite) ; départage au rendement."""
        avec_p = [l for l in self.lignes if l.p_reussite]
        if not avec_p:
            return None
        return max(avec_p, key=lambda l: (l.meilleur_levier()[1], l.rendement_pct))


@dataclass
class Detail:
    symbole: str
    bougies: data.Bougies
    resultat: engine.Resultat
    entree_type: str
    sortie_type: str
    rapide: int
    lente: int
    simulations: list      # une propfirm.Simulation par levier
    temoins: list
    tendance: tend.Tendance


class Tache(QThread):
    """Exécute `fonction(progres, annule, journal)` hors du thread d'interface."""

    progres = Signal(int, int, str)
    fini = Signal(object)
    echec = Signal(str)
    journal = Signal(str)

    def __init__(self, fonction, parent=None):
        super().__init__(parent)
        self._fonction = fonction
        self._annule = False

    def annuler(self):
        self._annule = True

    @property
    def annulee(self):
        return self._annule

    def run(self):
        try:
            self.fini.emit(self._fonction(
                lambda fait, total, msg="": self.progres.emit(fait, total, msg),
                lambda: self._annule,
                self.journal.emit))
        except Exception as e:                       # remonté à l'utilisateur
            self.echec.emit(f"{type(e).__name__} : {e}")


class TacheSuivi(QThread):
    """Rejoue des configs sauvegardées sur le dernier mois, en émettant chaque résultat dès qu'il
    est prêt : arrêter la tâche laisse ce qui est déjà calculé."""

    resultat = Signal(int, object)         # id de la config, suivi.Suivi ou message d'erreur
    progres = Signal(int, int)
    echec = Signal(str)

    def __init__(self, a_suivre, parent=None):
        super().__init__(parent)
        self._a_suivre = list(a_suivre)
        self._annule = False

    def annuler(self):
        self._annule = True

    def run(self):
        try:
            for fait, (c, r) in enumerate(suivi.suivre(self._a_suivre, lambda: self._annule), 1):
                self.resultat.emit(c.id, r)
                self.progres.emit(fait, len(self._a_suivre))
        except Exception as e:
            self.echec.emit(f"{type(e).__name__} : {e}")


def _spin(minimum, maximum, valeur, pas=1):
    s = QSpinBox()
    s.setRange(minimum, maximum)
    s.setValue(valeur)
    s.setSingleStep(pas)
    return s


def _dspin(minimum, maximum, valeur, decimales=1, pas=1.0):
    s = QDoubleSpinBox()
    s.setRange(minimum, maximum)
    s.setDecimals(decimales)
    s.setSingleStep(pas)
    s.setValue(valeur)
    return s


def _cellule(texte, droite=False, brush=None):
    item = QTableWidgetItem(str(texte))
    if droite:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if brush is not None:
        item.setForeground(brush)
    return item


class _Num(QTableWidgetItem):
    """Cellule numérique : s'affiche comme `texte`, se trie sur `valeur` (None = le plus petit)."""

    def __init__(self, texte, valeur=None, brush=None, fond=None):
        super().__init__(texte)
        self._valeur = valeur
        self.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if brush is not None:
            self.setForeground(brush)
        if fond is not None:
            self.setBackground(fond)

    def __lt__(self, autre):
        a, b = self._valeur, getattr(autre, "_valeur", None)
        if a is None or b is None:
            return a is None and b is not None
        return a < b


def _cellules_botx(l):
    """Cellules « Stop BotX » et « Risque BotX % » d'une ligne de détail."""
    gris = QBrush(QColor("#999999"))
    if l.politique is None or l.levier is None:
        return [_Num("-"), _Num("-")]
    stop, r = l.stop_botx, l.risque_botx
    if stop is None:
        return [_Num("non transp.", brush=gris), _Num("non transp.", brush=gris)]
    approx = l.parametres_botx["StopMode"] == "AtrFixed"
    return [_Num(stop), _Num("-" if r is None else f"{'~' if approx else ''}{r:.3g}", r)]


def _fond_p(p):
    """Teinte bleue proportionnelle à une probabilité de réussite (0..1)."""
    return QBrush(QColor(0, 114, 178, int(min(max(p, 0.0), 1.0) * 150)))


def _signe(valeur):
    return ROUGE if valeur is not None and valeur < 0 else None


def _n(v, fmt, defaut="n/a"):
    return defaut if v is None or (isinstance(v, float) and np.isnan(v)) else format(v, fmt)


def _tableau(colonnes, etirer=True):
    t = QTableWidget(0, len(colonnes))
    t.setHorizontalHeaderLabels(colonnes)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(etirer)
    return t


def _jour(t):
    return str(np.datetime_as_string(t, unit="D"))


def _retard_jours(date):
    return int((np.datetime64("today") - date.astype("datetime64[D]")) / np.timedelta64(1, "D"))


class DialogueSymbole(QDialog):
    """Choix d'un symbole parmi ceux du broker, avec filtre ; la saisie libre reste possible."""

    def __init__(self, symboles, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ajouter un symbole")
        self.resize(360, 460)
        self._filtre = QLineEdit()
        self._filtre.setPlaceholderText("Filtrer, ou saisir un nom exact (ex. GERMANY 40)")
        self._liste = QListWidget()
        self._liste.addItems(symboles)
        boutons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        v = QVBoxLayout(self)
        v.addWidget(self._filtre)
        v.addWidget(self._liste, 1)
        v.addWidget(boutons)
        self._filtre.textChanged.connect(self._filtrer)
        self._liste.itemDoubleClicked.connect(lambda _: self.accept())
        boutons.accepted.connect(self.accept)
        boutons.rejected.connect(self.reject)

    def _filtrer(self, texte):
        for i in range(self._liste.count()):
            self._liste.item(i).setHidden(texte.lower() not in self._liste.item(i).text().lower())

    def choix(self):
        courant = self._liste.currentItem()
        if courant is not None and not courant.isHidden():
            return courant.text()
        return self._filtre.text().strip()


# --------------------------------------------------------------------------
# Fenêtre
# --------------------------------------------------------------------------

class FenetrePrincipale(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MASimulator")
        self.resize(1500, 900)

        self.infos = {}                # symbole -> data.InfoSymbole
        self.params: Params | None = None
        self.resultats: dict[str, ResultatSymbole] = {}
        self.detail: Detail | None = None
        self._tache: Tache | None = None
        self._symboles_broker: list[str] | None = None
        self._tentatives = set()       # trous déjà tentés : on ne re-propose pas le même
        self._labos_en_cours: list[Tache] = []
        self._labo_cle = None
        self._labo_lignes = ([], [])       # (politiques, long/short) de la paire affichée
        self._a_restaurer = None           # paire à resélectionner après une analyse
        self._configs = {}                 # id -> configs.ConfigSauvee, onglet Configs
        self._groupes = {}                 # id -> configs.Groupe
        self._suivis = {}                  # id -> suivi.Suivi ou message d'erreur, vue d'ensemble
        self._tache_suivi: TacheSuivi | None = None
        self._suivi_ids = []               # configs sélectionnées dans le suivi (une : en couleur ; plusieurs : agrégées)
        self._suivi_reconstruction = False
        self._timer_suivi = QTimer(self)
        self._timer_suivi.setSingleShot(True)
        self._timer_suivi.timeout.connect(self._remplir_suivi)
        self._timer_labo = QTimer(self)
        self._timer_labo.setSingleShot(True)
        self._timer_labo.timeout.connect(self._lancer_labo)

        self._construire_panneau()
        self._construire_vues()

        corps = QSplitter()
        corps.addWidget(self._panneau)
        corps.addWidget(self.onglets)
        corps.setStretchFactor(1, 1)
        corps.setSizes([440, 1060])
        self.setCentralWidget(corps)

        self.journal = QPlainTextEdit()
        self.journal.setReadOnly(True)
        self.journal.setMaximumBlockCount(2000)
        self.dock_journal = QDockWidget("Journal cTrader")
        self.dock_journal.setWidget(self.journal)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_journal)
        self.dock_journal.hide()

        self.barre_progres = QProgressBar()
        self.barre_progres.setMaximumWidth(260)
        self.barre_progres.setVisible(False)
        self.statusBar().addPermanentWidget(self.barre_progres)

        self._charger_symboles()
        self._rafraichir_configs()
        QTimer.singleShot(0, self._lancer_suivi)      # le calcul démarre en fond dès l'ouverture

    def closeEvent(self, event):
        """Interrompt les calculs en cours et attend leurs threads avant de quitter."""
        self._enregistrer_note()
        taches = list(self._labos_en_cours) + [t for t in (self._tache, self._tache_suivi)
                                                if t is not None]
        for tache in taches:
            tache.annuler()
        for tache in taches:
            tache.wait(10_000)
        super().closeEvent(event)

    # ---------------------------------------------------------------- panneau

    def _construire_panneau(self):
        contenu = QWidget()
        col = QVBoxLayout(contenu)

        g = QGroupBox("Données")
        f = QFormLayout(g)
        self.liste_symboles = QListWidget()
        self.liste_symboles.setMaximumHeight(130)
        f.addRow(self.liste_symboles)
        bouton_ajout = QPushButton("+ Ajouter un symbole...")
        bouton_ajout.setToolTip("Symboles du broker (ctrader-cli symbols). Un symbole absent du "
                                "cache sera téléchargé à l'analyse.")
        bouton_ajout.clicked.connect(self._ajouter_symbole_broker)
        f.addRow(bouton_ajout)
        self.combo_unite = QComboBox()
        self.combo_unite.addItems(list(data.MINUTES))
        self.combo_unite.setCurrentText("m5")
        f.addRow("Unité de temps", self.combo_unite)
        self.date_debut = QDateEdit()
        self.date_fin = QDateEdit()
        for d in (self.date_debut, self.date_fin):
            d.setCalendarPopup(True)
            d.setDisplayFormat("dd/MM/yyyy")
        f.addRow("Début", self.date_debut)
        f.addRow("Fin", self.date_fin)
        self.check_telecharger = QCheckBox("Télécharger les données manquantes")
        self.check_telecharger.setChecked(True)
        self.check_telecharger.setToolTip(
            "Si le cache cTrader a des trous sur la période, lance le bot fictif DataFetcher "
            "en backtest via ctrader-cli : cTrader télécharge alors les bougies manquantes.")
        f.addRow(self.check_telecharger)
        col.addWidget(g)

        g = QGroupBox("cTrader (téléchargement)")
        f = QFormLayout(g)
        reg = QSettings("MASimulator", "MASimulator")
        self.edit_ctid = QLineEdit(reg.value("ctid", os.environ.get("MASIM_CTID", "")))
        self.edit_pwd = QLineEdit(reg.value("pwd_file", "~/.config/ctrader-cli/password.pwd"))
        self.edit_account = QLineEdit(reg.value("account", os.environ.get("MASIM_ACCOUNT", "")))
        self.edit_ctid.setPlaceholderText("cTID (email)")
        self.edit_account.setPlaceholderText("numéro de compte")
        self.edit_pwd.setToolTip("Fichier contenant le mot de passe : jamais saisi dans l'app.")
        f.addRow("cTID", self.edit_ctid)
        f.addRow("Mot de passe (fichier)", self.edit_pwd)
        f.addRow("Compte", self.edit_account)
        col.addWidget(g)

        g = QGroupBox("Signal")
        f = QFormLayout(g)
        self.spin_rapide = _spin(2, 500, 9)
        self.spin_lente = _spin(3, 1000, 21)
        f.addRow("Période rapide", self.spin_rapide)
        f.addRow("Période lente", self.spin_lente)
        col.addWidget(g)

        g = QGroupBox("Challenge prop firm")
        f = QFormLayout(g)
        self.spin_capital = _dspin(1000, 10_000_000, 10_000, 0, 1000)
        self.spin_cible = _dspin(0.1, 100, 10.0)
        self.spin_dd = _dspin(0.1, 100, 10.0)
        self.spin_jour = _dspin(0.1, 100, 5.0)
        self.spin_duree = _spin(1, 365, 30)
        self.check_trailing = QCheckBox("Drawdown trailing")
        self.edit_leviers = QLineEdit("0.5, 1, 1.5, 2, 3, 5")
        self.spin_departs = _spin(50, 5000, 300, 50)
        self.spin_min_trades = _spin(1, 1000, 20)
        f.addRow("Capital", self.spin_capital)
        f.addRow("Cible (%)", self.spin_cible)
        f.addRow("DD max (%)", self.spin_dd)
        f.addRow("Perte jour max (%)", self.spin_jour)
        f.addRow("Durée (jours)", self.spin_duree)
        f.addRow(self.check_trailing)
        f.addRow("Leviers", self.edit_leviers)
        f.addRow("Départs simulés", self.spin_departs)
        f.addRow("Trades min.", self.spin_min_trades)
        self.spin_min_trades.setToolTip("Sous ce nombre de trades, une paire n'a pas de "
                                        "P(réussite) : l'échantillon est trop mince.")
        col.addWidget(g)

        g = QGroupBox("Coûts par symbole")
        v = QVBoxLayout(g)
        self.table_couts = QTableWidget(0, 3)
        self.table_couts.setHorizontalHeaderLabels(["Spread", "Comm./lot", "Contrat"])
        self.table_couts.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table_couts.setMaximumHeight(170)
        self.table_couts.setToolTip("Spread (dans l'unité du prix) ; commission par lot et par côté ; "
                                    "taille du contrat. Spread vide = 2 bps du prix. Ce sont des "
                                    "ordres de grandeur : mets les tarifs de ton compte.")
        v.addWidget(self.table_couts)
        col.addWidget(g)

        col.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(contenu)
        scroll.setMinimumWidth(320)

        self.bouton_analyser = QPushButton("Analyser")
        self.bouton_analyser.setStyleSheet("font-weight: bold; padding: 8px;")
        self.bouton_analyser.clicked.connect(self._analyser)

        self.onglets_gauche = QTabWidget()
        self.onglets_gauche.addTab(scroll, "Paramètres")
        self.onglets_gauche.addTab(self._construire_panneau_configs(), "Configs")
        self.onglets_gauche.currentChanged.connect(
            lambda i: self._rafraichir_configs() if i == 1 else None)

        self._panneau = QWidget()
        v = QVBoxLayout(self._panneau)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self.onglets_gauche, 1)
        v.addWidget(self.bouton_analyser)

    def _construire_panneau_configs(self):
        """Configs sauvegardées, dans la sidebar : on prend ses notes sans quitter les tableaux."""
        w = QWidget()
        v = QVBoxLayout(w)
        boutons = QHBoxLayout()
        for libelle, aide, action in (
                ("Recharger", "Remet les paramètres de la config dans l'onglet Paramètres, sans "
                              "lancer d'analyse.", self._recharger_config),
                ("Copier", "Copie le résumé (avec la note) dans le presse-papiers.", self._copier_resume),
                ("Supprimer", "Supprime la config de la base.", self._supprimer_config)):
            bouton = QPushButton(libelle)
            bouton.setToolTip(aide)
            bouton.clicked.connect(action)
            boutons.addWidget(bouton)
        v.addLayout(boutons)

        self.liste_configs = QListWidget()
        self.liste_configs.setWordWrap(True)
        self.liste_configs.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.liste_configs.setUniformItemSizes(False)
        self.liste_configs.currentItemChanged.connect(self._config_choisie)
        self.texte_config = QPlainTextEdit()
        self.texte_config.setReadOnly(True)
        self.texte_config.setPlaceholderText(
            "Sauvegarde une config depuis l'onglet Criblage (bouton en bas à droite) : elle "
            "apparaît ici, prête à recevoir une note.")
        self.note_config = QPlainTextEdit()
        self.note_config.setPlaceholderText("Note : pourquoi cette config, ce qu'il faut revérifier... "
                                            "Enregistrée automatiquement.")
        self.note_config.textChanged.connect(self._note_modifiee)
        self._note_id = None               # config dont la note est dans l'éditeur
        self._note_sale = False
        self._timer_note = QTimer(self)
        self._timer_note.setSingleShot(True)
        self._timer_note.timeout.connect(self._enregistrer_note)

        def bloc(titre, widget):
            b = QWidget()
            vb = QVBoxLayout(b)
            vb.setContentsMargins(0, 0, 0, 0)
            vb.addWidget(QLabel(titre))
            vb.addWidget(widget, 1)
            return b

        separateur = QSplitter(Qt.Orientation.Vertical)
        separateur.addWidget(bloc("Configs sauvegardées", self.liste_configs))
        separateur.addWidget(bloc("Résumé", self.texte_config))
        separateur.addWidget(bloc("Note", self.note_config))
        separateur.setSizes([260, 260, 200])
        v.addWidget(separateur, 1)
        base = QLabel(f"Base : {configs.chemin_base()}")
        base.setWordWrap(True)
        base.setStyleSheet("color: #888; font-size: 10px;")
        v.addWidget(base)
        return w

    def _charger_symboles(self):
        self.infos = data.lister_symboles()
        for sym, info in self.infos.items():
            couverture = 0
            if info.premiere_m1 is not None:
                couverture = (info.derniere_m1 - info.premiere_m1) / np.timedelta64(1, "D")
            self._ajouter_symbole(sym, couverture >= 180)
        self._dates_par_defaut()

    def _texte_symbole(self, sym):
        info = self.infos.get(sym)
        if info is None or info.derniere_m1 is None:
            return f"{sym}   ·   pas de données (à télécharger)"
        texte = f"{sym}   ·   {info.derniere_m1}"
        retard = _retard_jours(info.derniere_m1)
        if retard > FRAICHEUR_MAX_JOURS:
            texte += f"   (il y a {retard} j)"
        return texte

    def _ajouter_symbole(self, sym, coche):
        """Ajoute `sym` à la liste et à la table des coûts (ou le coche s'il existe déjà)."""
        for i in range(self.liste_symboles.count()):
            item = self.liste_symboles.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == sym:
                item.setCheckState(Qt.CheckState.Checked if coche else item.checkState())
                return
        item = QListWidgetItem(self._texte_symbole(sym))
        item.setData(Qt.ItemDataRole.UserRole, sym)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if coche else Qt.CheckState.Unchecked)
        self.liste_symboles.addItem(item)

        connu = engine.COUTS_PAR_SYMBOLE.get(sym)
        ligne = self.table_couts.rowCount()
        self.table_couts.insertRow(ligne)
        self.table_couts.setVerticalHeaderItem(ligne, QTableWidgetItem(sym))
        self.table_couts.setItem(ligne, 0, QTableWidgetItem(f"{connu.spread:g}" if connu else ""))
        self.table_couts.setItem(ligne, 1, QTableWidgetItem(
            f"{connu.commission_par_lot_par_cote:g}" if connu else "0"))
        self.table_couts.setItem(ligne, 2, QTableWidgetItem(
            f"{connu.taille_contrat:g}" if connu else "1"))

    def _maj_textes_symboles(self):
        for i in range(self.liste_symboles.count()):
            item = self.liste_symboles.item(i)
            item.setText(self._texte_symbole(item.data(Qt.ItemDataRole.UserRole)))

    def _symboles_coches(self):
        return tuple(
            self.liste_symboles.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.liste_symboles.count())
            if self.liste_symboles.item(i).checkState() == Qt.CheckState.Checked)

    def _dates_par_defaut(self):
        """Fin = dernière bougie connue des symboles cochés, début = 120 jours avant."""
        fins = [self.infos[s].derniere_m1 for s in self._symboles_coches()
                if s in self.infos and self.infos[s].derniere_m1 is not None]
        fin = max(fins) if fins else np.datetime64("today") - np.timedelta64(1, "D")
        for widget, date in ((self.date_fin, fin), (self.date_debut, fin - np.timedelta64(120, "D"))):
            widget.setDate(QDate.fromString(str(date), "yyyy-MM-dd"))

    def _lire_tendance(self):
        return tend.Tendance(
            mode=self.combo_tend_mode.currentData(), ma_type=self.combo_tend_ma.currentText(),
            unite=self.combo_tend_unite.currentText(), periode=self.spin_tend_periode.value(),
            rapide=self.spin_tend_rapide.value(), lente=self.spin_tend_lente.value())

    def _ecrire_tendance(self, t):
        for widget in (self.combo_tend_mode, self.combo_tend_ma, self.combo_tend_unite):
            widget.blockSignals(True)
        self.combo_tend_mode.setCurrentIndex(max(self.combo_tend_mode.findData(t.mode), 0))
        self.combo_tend_ma.setCurrentText(t.ma_type)
        self.combo_tend_unite.setCurrentText(t.unite)
        for widget in (self.combo_tend_mode, self.combo_tend_ma, self.combo_tend_unite):
            widget.blockSignals(False)
        self.spin_tend_periode.setValue(t.periode)
        self.spin_tend_rapide.setValue(t.rapide)
        self.spin_tend_lente.setValue(t.lente)
        self._maj_champs_tendance()

    def _maj_champs_tendance(self):
        mode = self.combo_tend_mode.currentData()
        visibles = {"ma": mode != "aucune", "unite": mode != "aucune",
                    "periode": mode == "niveau", "rapide": mode == "croisement",
                    "lente": mode == "croisement"}
        for nom, (etiquette, widget) in self._champs_tend.items():
            etiquette.setVisible(visibles[nom])
            widget.setVisible(visibles[nom])

    def _lire_reglages(self):
        return cli.Reglages(self.edit_ctid.text(), self.edit_pwd.text(), self.edit_account.text())

    def _ajouter_symbole_broker(self):
        if self._symboles_broker is None:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                self._symboles_broker = cli.symboles_broker(self._lire_reglages())
            except cli.ErreurCli as e:
                QMessageBox.warning(self, "Symboles du broker",
                                    f"{e}\n\nTu peux quand même saisir un nom exact.")
            finally:
                QApplication.restoreOverrideCursor()
        dialogue = DialogueSymbole(self._symboles_broker or [], self)
        if dialogue.exec() and dialogue.choix():
            self._ajouter_symbole(dialogue.choix().upper(), True)

    def _lire_params(self):
        symboles = self._symboles_coches()
        if not symboles:
            raise ValueError("Coche au moins un symbole.")
        debut = np.datetime64(self.date_debut.date().toString("yyyy-MM-dd"))
        fin = np.datetime64(self.date_fin.date().toString("yyyy-MM-dd"))
        if (fin - debut) / np.timedelta64(1, "D") < self.spin_duree.value() + 5:
            raise ValueError(f"La période doit dépasser de quelques jours la durée du challenge "
                             f"({self.spin_duree.value()} j).")
        if self.spin_rapide.value() >= self.spin_lente.value():
            raise ValueError("La période rapide doit être inférieure à la lente.")
        try:
            leviers = tuple(float(x) for x in self.edit_leviers.text().replace(";", ",").split(",")
                            if x.strip())
        except ValueError:
            raise ValueError("Leviers : liste de nombres séparés par des virgules.") from None
        if not leviers or min(leviers) <= 0:
            raise ValueError("Leviers : au moins une valeur strictement positive.")

        def nombre(ligne, col, defaut):
            item = self.table_couts.item(ligne, col)
            texte = item.text().strip().replace(",", ".") if item else ""
            return float(texte) if texte else defaut

        couts = {}
        for i in range(self.table_couts.rowCount()):
            sym = self.table_couts.verticalHeaderItem(i).text()
            try:
                couts[sym] = (nombre(i, 0, None), nombre(i, 1, 0.0), nombre(i, 2, 1.0))
            except ValueError:
                raise ValueError(f"Coûts de {sym} : nombres attendus.") from None

        reglages = self._lire_reglages()
        reg = QSettings("MASimulator", "MASimulator")
        reg.setValue("ctid", reglages.ctid)
        reg.setValue("pwd_file", reglages.pwd_file)
        reg.setValue("account", reglages.account)

        return Params(
            symboles=symboles, unite=self.combo_unite.currentText(), debut=debut, fin=fin,
            rapide=self.spin_rapide.value(), lente=self.spin_lente.value(),
            capital=self.spin_capital.value(),
            regles=propfirm.Regles(self.spin_cible.value(), self.spin_dd.value(),
                                   self.spin_jour.value(), self.spin_duree.value(),
                                   self.check_trailing.isChecked()),
            leviers=leviers, n_departs=self.spin_departs.value(),
            min_trades=self.spin_min_trades.value(), couts=couts, reglages=reglages,
            telecharger=self.check_telecharger.isChecked(), tendance=self._lire_tendance())

    # ------------------------------------------------------------------ vues

    @staticmethod
    def _panneau_labo(table, note):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 4, 0, 0)
        v.addWidget(table, 1)
        etiquette = QLabel(note)
        etiquette.setWordWrap(True)
        etiquette.setStyleSheet("color: #666;")
        v.addWidget(etiquette)
        return w

    def _construire_vues(self):
        self.onglets = QTabWidget()

        # Vue d'ensemble
        w = QWidget()
        v = QVBoxLayout(w)
        self.table_apercu = _tableau([
            "Symbole", "Dernière bougie", "Barres", "Alertes", "Meilleure config", "Levier",
            "P(réussite)", "Hasard", "Écart", "Trades", "Rendement %", "DD max %"])
        self.table_apercu.itemSelectionChanged.connect(self._afficher_alertes)
        self.table_apercu.itemDoubleClicked.connect(self._ouvrir_depuis_apercu)
        self.texte_alertes = QPlainTextEdit()
        self.texte_alertes.setReadOnly(True)
        self.texte_alertes.setMaximumHeight(120)
        self.texte_alertes.setPlaceholderText(
            "Lance une analyse, puis sélectionne un symbole pour voir ses alertes de données. "
            "Double-clic : ouvre son criblage.")
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self.table_apercu, 1)
        v.addWidget(self.texte_alertes)
        # La vue d'ensemble est divisée : les symboles analysés en haut, le suivi des configs
        # sauvegardées (leur dernier mois) en bas.
        division = QSplitter(Qt.Orientation.Vertical)
        division.addWidget(w)
        division.addWidget(self._construire_suivi())
        division.setStretchFactor(1, 1)
        division.setSizes([190, 660])
        self.onglets.addTab(division, ONGLETS[T_APERCU])

        # Criblage
        w = QWidget()
        v = QVBoxLayout(w)
        h = QHBoxLayout()
        self.combo_crible_sym = QComboBox()
        self.combo_crible_sym.currentIndexChanged.connect(self._changer_symbole_crible)
        h.addWidget(QLabel("Symbole"))
        h.addWidget(self.combo_crible_sym)
        h.addSpacing(20)

        # Filtre de tendance de fond : les longs ne partent qu'avec la tendance.
        self.combo_tend_mode = QComboBox()
        for cle, libelle in tend.MODES.items():
            self.combo_tend_mode.addItem(libelle, cle)
        self.combo_tend_ma = QComboBox()
        self.combo_tend_ma.addItems(moyennes.TYPES)
        self.combo_tend_unite = QComboBox()
        self.combo_tend_unite.addItems(list(data.MINUTES))
        self.combo_tend_unite.setCurrentText("h1")
        self.spin_tend_periode = _spin(2, 1000, 200)
        self.spin_tend_rapide = _spin(2, 500, 20)
        self.spin_tend_lente = _spin(3, 1000, 50)
        self.bouton_tendance = QPushButton("Appliquer")
        self.bouton_tendance.setToolTip("Recalcule le criblage de ce symbole avec ce filtre. "
                                        "\"Analyser\" l'applique à tous les symboles.")
        self.bouton_tendance.clicked.connect(self._appliquer_tendance)
        self.combo_tend_mode.currentIndexChanged.connect(self._maj_champs_tendance)
        h.addWidget(QLabel("Tendance"))
        h.addWidget(self.combo_tend_mode)
        self._champs_tend = {
            "ma": (QLabel("Moyenne"), self.combo_tend_ma),
            "unite": (QLabel("TF"), self.combo_tend_unite),
            "periode": (QLabel("Période"), self.spin_tend_periode),
            "rapide": (QLabel("Rapide"), self.spin_tend_rapide),
            "lente": (QLabel("Lente"), self.spin_tend_lente),
        }
        for etiquette, widget in self._champs_tend.values():
            h.addWidget(etiquette)
            h.addWidget(widget)
        h.addWidget(self.bouton_tendance)
        h.addStretch(1)
        self.check_propre = QCheckBox("Sans collision")
        self.check_propre.setToolTip(
            "Masque les paires où un signal d'entrée et un signal de sortie tombent sur la même "
            "barre pendant une position. Ces collisions sont corrigées dans la simulation (sortie "
            "prioritaire, entrée reportée d'une barre) mais la réouverture a une barre de retard "
            "sur BotX : ces paires sont les plus sensibles à ce détail de timing.")
        self.check_propre.toggled.connect(lambda _: self._maj_crible())
        h.addWidget(self.check_propre)
        self._maj_champs_tendance()

        self.table_crible = _tableau([], etirer=False)
        self.table_crible.setSortingEnabled(True)
        self.table_crible.setAlternatingRowColors(True)
        self.table_crible.itemSelectionChanged.connect(self._planifier_labo)
        self.table_crible.itemDoubleClicked.connect(self._ouvrir_detail_depuis_crible)

        self.label_labo = QLabel("Lance une analyse, puis sélectionne une ligne.")
        self.label_labo.setStyleSheet("font-weight: bold;")
        self.table_pol = _tableau([
            "Politique de sortie", "Rend. %", "DD %", "Sharpe", "PF", "Réussite %", "Trades",
            "Rend/DD", "Via stop", "Levier", "P(réussite)", "Témoin", "Gain (pts)",
            "Stop BotX", "Risque BotX %"], etirer=False)
        self.table_ls = _tableau([
            "Variante", "Rend. %", "DD %", "Sharpe", "PF", "Longs", "Shorts", "Trades",
            "Rend/DD", "Via stop", "Levier", "P(réussite)", "Stop BotX", "Risque BotX %"],
            etirer=False)
        for table in (self.table_pol, self.table_ls):
            n = table.columnCount()
            table.horizontalHeaderItem(n - 2).setToolTip(
                "Réglage BotX équivalent : Percent (trailing en %, 0 = sans stop) ou AtrFixed "
                "(stop ATR statique). « non transp. » : la target 1:2 n'a pas de take profit dans "
                "BotX.")
            table.horizontalHeaderItem(n - 1).setToolTip(
                "RiskPerTradePct à saisir dans BotX pour retrouver le levier de la ligne. Trailing "
                "de T % : levier x T. Sans stop : 100 x levier. AtrFixed (~) : levier x distance "
                "médiane du stop à l'entrée, approximatif car BotX garde le risque constant et "
                "laisse le notionnel varier.")
        self.combo_ls_politique = QComboBox()
        self.combo_ls_politique.addItems(labo.noms_politiques())
        self.combo_ls_politique.setToolTip("Politique de sortie appliquée aux deux directions.")
        self.combo_ls_politique.currentIndexChanged.connect(lambda _: self._lancer_labo(True))
        self.onglets_labo = QTabWidget()
        self.onglets_labo.addTab(self._panneau_labo(self.table_pol, (
            "Mêmes entrées partout, seule la sortie change. Via stop = trades sortis par le stop "
            "ou le trailing plutôt que par le croisement (0 = il ne s'est jamais déclenché, la "
            "ligne est alors identique au croisement seul). Gain = P(réussite) moins celle d'un "
            "témoin sans edge, de même volatilité et de même dérive que l'actif, au levier qui "
            "maximise ce gain. Les deux dernières colonnes donnent les réglages BotX qui reproduisent "
            "la ligne : le stop (Percent ou AtrFixed) et RiskPerTradePct (levier x trailing ; sans "
            "stop 100 x levier ; en AtrFixed levier x distance médiane du stop, approximatif). Les "
            "lignes avec target 1:2 ne sont pas transposables : BotX n'a pas de take profit. "
            "Le mode Atr de BotX (trailing) ne correspond à aucune ligne.")), "Croisement / stop / trailing")
        panneau_ls = self._panneau_labo(self.table_ls, (
            "Reproduit le comportement de BotX : le long s'ouvre sur le croisement haussier de la "
            "moyenne d'entrée ; le croisement baissier de la moyenne de sortie ferme le long et "
            "ouvre le short (depuis le plat aussi) ; le short se ferme sur le croisement haussier "
            "de la moyenne de sortie. Les lignes « tendance » ne prennent un long qu'avec la "
            "tendance de fond et un short qu'à contre. Le retournement ferme puis rouvre une barre "
            "plus tard (le moteur interdit les deux sur la même barre), BotX les fait sur la même. "
            "Levier = celui qui maximise la P(réussite), et les deux dernières colonnes le traduisent "
            "en paramètres BotX (voir l'onglet précédent). Pas de témoin : la dérive de l'actif "
            "joue contre un short."))
        choix = QHBoxLayout()
        choix.addWidget(QLabel("Politique de sortie"))
        choix.addWidget(self.combo_ls_politique)
        choix.addStretch(1)
        panneau_ls.layout().insertLayout(0, choix)
        self.onglets_labo.addTab(panneau_ls, "Long / short")
        self.bouton_sauver = QPushButton("Sauvegarder cette config")
        self.bouton_sauver.setToolTip(
            "Enregistre dans la base DuckDB (la note se prend ensuite dans la sidebar, onglet Configs) : la paire sélectionnée, le filtre de "
            "tendance, et l'onglet de détail affiché : politique de sortie (long seul) ou "
            "variante long/short avec sa politique. Sans ligne sélectionnée, la première.")
        self.bouton_sauver.clicked.connect(self._sauvegarder_config)
        bas = QWidget()
        vb = QVBoxLayout(bas)
        vb.setContentsMargins(0, 0, 0, 0)
        entete = QHBoxLayout()
        entete.addWidget(self.label_labo, 1)
        entete.addWidget(self.bouton_sauver)
        vb.addLayout(entete)
        vb.addWidget(self.onglets_labo, 1)
        separateur = QSplitter(Qt.Orientation.Vertical)
        separateur.addWidget(self.table_crible)
        separateur.addWidget(bas)
        separateur.setSizes([380, 320])
        v.addLayout(h)
        v.addWidget(separateur, 1)
        self.onglets.addTab(w, ONGLETS[T_CRIBLE])

        # Détail
        w = QWidget()
        v = QVBoxLayout(w)
        h = QHBoxLayout()
        self.combo_det_sym = QComboBox()
        self.combo_det_entree = QComboBox()
        self.combo_det_sortie = QComboBox()
        self.combo_det_entree.addItems(moyennes.TYPES)
        self.combo_det_sortie.addItems(moyennes.TYPES)
        self.spin_det_rapide = _spin(2, 500, 9)
        self.spin_det_lente = _spin(3, 1000, 21)
        self.spin_stop = _dspin(0, 50, 0.0, 2, 0.5)
        self.spin_target = _dspin(0, 100, 0.0, 2, 0.5)
        self.spin_stop.setSpecialValueText("aucun")
        self.spin_target.setSpecialValueText("aucun")
        self.bouton_detail = QPushButton("Calculer")
        self.bouton_detail.clicked.connect(self._calculer_detail)
        for libelle, widget in (
                ("Symbole", self.combo_det_sym), ("Entrée", self.combo_det_entree),
                ("Sortie", self.combo_det_sortie), ("Rapide", self.spin_det_rapide),
                ("Lente", self.spin_det_lente), ("Stop %", self.spin_stop),
                ("Target %", self.spin_target)):
            h.addWidget(QLabel(libelle))
            h.addWidget(widget)
        h.addWidget(self.bouton_detail)
        h.addStretch(1)
        self.label_kpi = QLabel()
        self.label_kpi.setTextFormat(Qt.TextFormat.RichText)
        self.label_kpi.setWordWrap(True)
        self.vue_detail = PlotlyView()
        self.table_sorties = _tableau(["Raison de sortie", "Trades", "PnL moyen", "PnL total"])
        self.table_sorties.setMaximumHeight(120)
        v.addLayout(h)
        v.addWidget(self.label_kpi)
        self.onglets_detail = QTabWidget()
        prix = QWidget()
        vp = QVBoxLayout(prix)
        vp.setContentsMargins(0, 4, 0, 0)
        vp.addWidget(self.vue_detail, 1)
        vp.addWidget(self.table_sorties)
        self.onglets_detail.addTab(prix, "Prix, equity, drawdown")
        self.onglets_detail.addTab(self._construire_horaire(), "Par tranche horaire")
        v.addWidget(self.onglets_detail, 1)
        self.onglets.addTab(w, ONGLETS[T_DETAIL])

        # Challenge
        w = QWidget()
        v = QVBoxLayout(w)
        h = QHBoxLayout()
        self.combo_chal_levier = QComboBox()
        self.combo_chal_levier.currentIndexChanged.connect(self._maj_departs)
        self.label_chal = QLabel("Calcule d'abord une config dans l'onglet Détail.")
        h.addWidget(QLabel("Levier de la frise"))
        h.addWidget(self.combo_chal_levier)
        h.addWidget(self.label_chal, 1)
        self.vue_leviers = PlotlyView()
        self.vue_departs = PlotlyView()
        v.addLayout(h)
        v.addWidget(self.vue_leviers, 3)
        v.addWidget(self.vue_departs, 2)
        self.onglets.addTab(w, ONGLETS[T_CHALLENGE])

        # Comparaison
        w = QWidget()
        v = QVBoxLayout(w)
        h = QHBoxLayout()
        self.spin_cmp_longueur = _spin(35, 365, 60)
        self.spin_cmp_longueur.setSuffix(" j")
        self.spin_cmp_nombre = _spin(2, 12, 4)
        self.combo_cmp_levier = QComboBox()
        self.bouton_cmp = QPushButton("Comparer")
        self.bouton_cmp.clicked.connect(self._comparer)
        h.addWidget(QLabel("Longueur d'une fenêtre"))
        h.addWidget(self.spin_cmp_longueur)
        h.addWidget(QLabel("Nombre de fenêtres"))
        h.addWidget(self.spin_cmp_nombre)
        h.addWidget(QLabel("Levier"))
        h.addWidget(self.combo_cmp_levier)
        h.addWidget(self.bouton_cmp)
        h.addStretch(1)
        self.label_cmp = QLabel(
            "La config du dernier Détail est évaluée sur des fenêtres successives, pour chaque "
            "symbole analysé : tient-elle dans le temps et d'un indice à l'autre ?")
        self.label_cmp.setWordWrap(True)
        self.vue_cmp = PlotlyView()
        v.addLayout(h)
        v.addWidget(self.label_cmp)
        v.addWidget(self.vue_cmp, 1)
        self.onglets.addTab(w, ONGLETS[T_COMPARAISON])

    # ------------------------------------------- suivi des configs sauvegardées

    # Les colonnes qui servent au classement d'abord : elles doivent tenir à l'écran sans défiler.
    COLONNES_SUIVI = ("#", "Config", "P(réussite)", "Sauvegarde", "Écart", "Levier", "Rend. 30j %",
                      "DD max %", "Rend/DD", "Trades", "Challenge -30j", "Sortie / direction",
                      "Données", "Groupes", "Note")
    C_SUIVI_P = 2                        # colonne P(réussite) : le tri par défaut

    def _construire_suivi(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 4, 0, 0)
        h = QHBoxLayout()
        titre = QLabel("Configs sauvegardées : le dernier mois")
        titre.setStyleSheet("font-weight: bold;")
        self.combo_suivi_sym = QComboBox()
        self.combo_suivi_tf = QComboBox()
        for combo in (self.combo_suivi_sym, self.combo_suivi_tf):
            combo.currentIndexChanged.connect(lambda _: self._remplir_suivi())
        self.combo_suivi_groupe = QComboBox()
        self.combo_suivi_groupe.setToolTip(
            "Un groupe est un ensemble de configs à suivre ensemble. Le choisir n'affiche que ses "
            "configs, toutes sélectionnées : le graphique montre aussitôt leur portefeuille.")
        self.combo_suivi_groupe.currentIndexChanged.connect(self._groupe_choisi)
        self.bouton_groupe = QToolButton()
        self.bouton_groupe.setText("Groupe ▾")
        self.bouton_groupe.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu_groupe = QMenu(self.bouton_groupe)
        self.menu_groupe.aboutToShow.connect(self._construire_menu_groupe)
        self.bouton_groupe.setMenu(self.menu_groupe)
        self.bouton_suivi = QPushButton("Actualiser")
        self.bouton_suivi.setToolTip(
            "Rejoue les configs affichées sur les 30 derniers jours des données du cache, en "
            "tâche de fond. Stop interrompt le calcul et garde ce qui est déjà affiché.")
        self.bouton_suivi.clicked.connect(self._lancer_suivi)
        self.barre_suivi = QProgressBar()
        self.barre_suivi.setMaximumWidth(160)
        self.barre_suivi.setVisible(False)
        self.label_suivi = QLabel()
        self.label_suivi.setStyleSheet("color: #666;")
        h.addWidget(titre)
        h.addSpacing(16)
        h.addWidget(QLabel("Symbole"))
        h.addWidget(self.combo_suivi_sym)
        h.addWidget(QLabel("Timeframe"))
        h.addWidget(self.combo_suivi_tf)
        h.addWidget(QLabel("Groupe"))
        h.addWidget(self.combo_suivi_groupe)
        h.addWidget(self.bouton_groupe)
        h.addWidget(self.bouton_suivi)
        h.addWidget(self.barre_suivi)
        h.addWidget(self.label_suivi, 1)

        self.table_suivi = _tableau(list(self.COLONNES_SUIVI))
        self.table_suivi.setAlternatingRowColors(True)
        entete = self.table_suivi.horizontalHeader()
        entete.setSortIndicator(self.C_SUIVI_P, Qt.SortOrder.DescendingOrder)
        self.table_suivi.setSortingEnabled(True)
        self.table_suivi.horizontalHeader().setSectionResizeMode(
            len(self.COLONNES_SUIVI) - 1, QHeaderView.ResizeMode.Interactive)
        self.table_suivi.setColumnWidth(len(self.COLONNES_SUIVI) - 1, 200)
        for col, aide in (
                (2, "P(réussite) d'un challenge sur la fenêtre de la config (sa longueur "
                    "d'origine), arrêtée à la dernière bougie."),
                (3, "P(réussite) enregistrée au moment de la sauvegarde."),
                (4, "P(réussite) d'aujourd'hui moins celle de la sauvegarde : négatif = la config "
                    "s'est dégradée."),
                (6, "Rendement des 30 derniers jours, au levier de la config (celui de la "
                    "sauvegarde), en repartant de 0 au début du mois."),
                (7, "Pire drawdown du mois au même levier, selon la règle du challenge (suiveur "
                    "ou fixe). Rouge : la limite du challenge est atteinte."),
                (10, "Issue d'un challenge qui aurait démarré au début du mois.")):
            self.table_suivi.horizontalHeaderItem(col).setToolTip(aide)
        self.table_suivi.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table_suivi.setToolTip(
            "Ctrl+clic ou Maj+clic : sélection multiple. Avec deux configs ou plus, le graphique "
            "montre le portefeuille qu'elles forment.")
        self.table_suivi.itemSelectionChanged.connect(self._suivi_selectionne)
        self.vue_suivi = PlotlyView()
        self.vue_suivi.clic.connect(self._clic_suivi)

        self.combo_agregat = QComboBox()
        for cle, texte in suivi.MODES_AGREGAT.items():
            self.combo_agregat.addItem(texte, cle)
        self.combo_agregat.setToolTip(
            "Capital partagé : le capital est réparti à parts égales entre les configs choisies "
            "(equity du portefeuille = moyenne des equities).\nCumul : chaque config tourne sur "
            "tout le capital à son levier, gains et pertes s'additionnent (exposition = somme des "
            "leviers).")
        self.combo_agregat.currentIndexChanged.connect(lambda _: self._maj_graphique_suivi())
        self.label_agregat = QLabel()
        self.label_agregat.setWordWrap(True)
        self.label_agregat.setTextFormat(Qt.TextFormat.RichText)
        self.bandeau_agregat = QWidget()
        b = QVBoxLayout(self.bandeau_agregat)
        b.setContentsMargins(0, 0, 0, 0)
        ligne = QHBoxLayout()
        ligne.addWidget(QLabel("Portefeuille"))
        ligne.addWidget(self.combo_agregat, 1)
        b.addLayout(ligne)
        b.addWidget(self.label_agregat)
        self.bandeau_agregat.setVisible(False)
        droite = QWidget()
        d = QVBoxLayout(droite)
        d.setContentsMargins(0, 0, 0, 0)
        d.addWidget(self.bandeau_agregat)
        d.addWidget(self.vue_suivi, 1)

        corps = QSplitter(Qt.Orientation.Horizontal)
        corps.addWidget(self.table_suivi)
        corps.addWidget(droite)
        corps.setStretchFactor(0, 5)
        corps.setStretchFactor(1, 3)
        corps.setSizes([720, 430])
        v.addLayout(h)
        v.addWidget(corps, 1)
        return w

    def _configs_affichees(self):
        """Configs qui passent les filtres symbole et timeframe, la plus récente d'abord."""
        sym, tf = self.combo_suivi_sym.currentData(), self.combo_suivi_tf.currentData()
        groupe = self._groupes.get(self.combo_suivi_groupe.currentData())
        return [c for c in self._configs.values()
                if (sym is None or c.symbole == sym) and (tf is None or c.unite == tf)
                and (groupe is None or c.id in groupe.ids)]

    def _maj_filtres_suivi(self):
        """Les valeurs des filtres viennent des configs sauvegardées ; on garde le choix en cours."""
        for combo, valeurs in ((self.combo_suivi_sym, sorted({c.symbole for c in self._configs.values()})),
                               (self.combo_suivi_tf, sorted({c.unite for c in self._configs.values()},
                                                            key=lambda u: data.MINUTES[u]))):
            choix = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("Tous", None)
            for x in valeurs:
                combo.addItem(x.upper() if combo is self.combo_suivi_tf else x, x)
            i = combo.findData(choix)
            combo.setCurrentIndex(max(i, 0))
            combo.blockSignals(False)
        choix = self.combo_suivi_groupe.currentData()
        self.combo_suivi_groupe.blockSignals(True)
        self.combo_suivi_groupe.clear()
        self.combo_suivi_groupe.addItem("Tous", None)
        for g in self._groupes.values():
            self.combo_suivi_groupe.addItem(f"{g.nom} ({len(g.ids)})", g.id)
        self.combo_suivi_groupe.setCurrentIndex(max(self.combo_suivi_groupe.findData(choix), 0))
        self.combo_suivi_groupe.blockSignals(False)

    # ---- groupes

    def _groupe_courant(self):
        return self._groupes.get(self.combo_suivi_groupe.currentData())

    def _groupe_choisi(self, *_):
        """Choisir un groupe affiche ses configs et les sélectionne toutes : leur portefeuille
        apparaît au graphique. Les filtres symbole et timeframe sont remis à « Tous », sinon
        ils masqueraient des membres du groupe."""
        for combo in (self.combo_suivi_sym, self.combo_suivi_tf):
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
        groupe = self._groupe_courant()
        if groupe is not None:
            self._suivi_ids = list(groupe.ids)
        self._remplir_suivi()

    def _recharger_groupes(self, choisir=None):
        try:
            self._groupes = {g.id: g for g in configs.lister_groupes()}
        except Exception as e:
            self._groupes = {}
            self.statusBar().showMessage(f"Groupes illisibles : {e}", 8000)
        self._maj_filtres_suivi()
        if choisir is None:
            self._remplir_suivi()
            return
        self.combo_suivi_groupe.blockSignals(True)
        self.combo_suivi_groupe.setCurrentIndex(max(self.combo_suivi_groupe.findData(choisir), 0))
        self.combo_suivi_groupe.blockSignals(False)
        self._groupe_choisi()

    def _construire_menu_groupe(self):
        m = self.menu_groupe
        m.clear()
        groupe = self._groupe_courant()
        selection = list(self._suivi_ids)
        n = len(selection)
        m.addAction(f"Nouveau groupe avec la sélection ({n})…", self._nouveau_groupe).setEnabled(n > 0)
        ajouter = m.addMenu("Ajouter la sélection à")
        ajouter.setEnabled(n > 0 and bool(self._groupes))
        for g in self._groupes.values():
            ajouter.addAction(g.nom, lambda g=g: self._groupe_ajouter(g))
        m.addSeparator()
        m.addAction("Retirer la sélection de ce groupe", self._groupe_retirer).setEnabled(
            groupe is not None and n > 0)
        m.addAction("Renommer le groupe…", self._groupe_renommer).setEnabled(groupe is not None)
        m.addAction("Supprimer le groupe", self._groupe_supprimer).setEnabled(groupe is not None)

    def _demander_nom(self, titre, defaut=""):
        nom, ok = QInputDialog.getText(self, titre, "Nom du groupe :", text=defaut)
        return nom.strip() if ok and nom.strip() else None

    def _groupe_action(self, fonction, *args, choisir=None):
        try:
            fonction(*args)
        except Exception as e:
            QMessageBox.warning(self, "Groupe", str(e))
            return False
        self._recharger_groupes(choisir)
        return True

    def _nouveau_groupe(self):
        nom = self._demander_nom("Nouveau groupe")
        if nom is None:
            return
        try:
            id_groupe = configs.creer_groupe(nom, self._suivi_ids)
        except Exception as e:
            QMessageBox.warning(self, "Groupe", str(e))
            return
        self._recharger_groupes(choisir=id_groupe)

    def _groupe_ajouter(self, groupe):
        if self._groupe_action(configs.ajouter_au_groupe, groupe.id, self._suivi_ids):
            self.statusBar().showMessage(f"Ajouté au groupe « {groupe.nom} ».", 4000)

    def _groupe_retirer(self):
        g = self._groupe_courant()
        if g is not None:
            self._groupe_action(configs.retirer_du_groupe, g.id, self._suivi_ids, choisir=g.id)

    def _groupe_renommer(self):
        g = self._groupe_courant()
        nom = self._demander_nom("Renommer le groupe", g.nom) if g is not None else None
        if nom is not None:
            self._groupe_action(configs.renommer_groupe, g.id, nom, choisir=g.id)

    def _groupe_supprimer(self):
        g = self._groupe_courant()
        if g is None:
            return
        Bouton = QMessageBox.StandardButton
        if QMessageBox.question(self, "Supprimer", f"Supprimer le groupe « {g.nom} » ? Ses configs "
                                "restent sauvegardées.", Bouton.Yes | Bouton.No,
                                Bouton.No) == Bouton.Yes:
            self._groupe_action(configs.supprimer_groupe, g.id)

    def _lancer_suivi(self):
        """Actualiser / Stop : rejoue les configs affichées en tâche de fond."""
        if self._tache_suivi is not None and self._tache_suivi.isRunning():
            self._tache_suivi.annuler()
            self.bouton_suivi.setEnabled(False)
            return
        a_suivre = self._configs_affichees()
        if not a_suivre:
            return
        tache = TacheSuivi(a_suivre, self)
        self._tache_suivi = tache
        self.bouton_suivi.setText("Stop")
        self.barre_suivi.setRange(0, len(a_suivre))
        self.barre_suivi.setValue(0)
        self.barre_suivi.setVisible(True)

        def resultat(id_config, r):
            self._suivis[id_config] = r
            self._timer_suivi.start(250)          # regroupe les résultats qui arrivent en rafale

        def fini():
            self.barre_suivi.setVisible(False)
            self.bouton_suivi.setText("Actualiser")
            self.bouton_suivi.setEnabled(True)
            if self._tache_suivi is tache:
                self._tache_suivi = None
            tache.deleteLater()
            self._remplir_suivi()

        tache.resultat.connect(resultat)
        tache.progres.connect(lambda fait, total: self.barre_suivi.setValue(fait))
        tache.echec.connect(lambda m: self.label_suivi.setText(f"Suivi interrompu : {m}"))
        tache.finished.connect(fini)
        tache.start()
        self._remplir_suivi()

    @staticmethod
    def _strategie_suivi(c):
        """Sortie et direction de la config ; le filtre de tendance du criblage s'ajoute au long seul."""
        direction = c.direction or "long seul"
        if "direction" not in c.metriques and c.tendance.active:
            direction += " (tendance)"
        return f"{c.politique} · {direction}"

    def _remplir_suivi(self):
        """Reconstruit le tableau et le graphique. Les configs pas encore calculées y figurent
        déjà, en attente ; le tri en cours (colonne et sens) est conservé."""
        self._timer_suivi.stop()
        t = self.table_suivi
        affichees = self._configs_affichees()
        entete = t.horizontalHeader()
        colonne, sens = entete.sortIndicatorSection(), entete.sortIndicatorOrder()
        self._suivi_reconstruction = True
        t.setSortingEnabled(False)
        t.setRowCount(0)
        gris = QBrush(QColor("#999999"))
        for c in affichees:
            i = t.rowCount()
            t.insertRow(i)
            r = self._suivis.get(c.id)
            note = c.note.strip().splitlines()[0] if c.note.strip() else ""
            debut = [_Num(f"#{c.id}", c.id),
                     _cellule(f"{c.symbole} {c.unite.upper()}  {c.entree_type}→{c.sortie_type} "
                              f"{c.rapide}/{c.lente}")]
            fin = [_cellule(self._strategie_suivi(c))]
            if not isinstance(r, suivi.Suivi):
                milieu = [_Num("erreur" if r else "…", brush=gris) for _ in range(9)]
                donnees = _Num("…", brush=gris)
                if r:
                    milieu[0].setToolTip(r)
                    milieu[0].setForeground(ROUGE)
            else:
                retard = _retard_jours(r.derniere_bougie)
                p, ps = r.p_actuelle, r.p_sauvee
                ecart = None if p is None or ps is None else p - ps
                milieu = [
                    _Num("n/a" if p is None else f"{p * 100:.0f} %", p,
                         fond=None if p is None else _fond_p(p)),
                    _Num("n/a" if ps is None else f"{ps * 100:.0f} %", ps),
                    _Num("n/a" if ecart is None else f"{ecart * 100:+.0f} pts", ecart,
                         brush=_signe(ecart)),
                    _Num(f"x{r.levier:g}", r.levier),
                    _Num(f"{r.rendement_pct:+.1f}", r.rendement_pct, brush=_signe(r.rendement_pct)),
                    _Num(f"{r.dd_pct:.1f}", r.dd_pct,
                         brush=ROUGE if r.dd_pct >= r.regles.dd_max_pct else None),
                    _Num(_n(r.rend_dd, ".1f"), r.rend_dd),
                    _Num(str(r.n_trades), r.n_trades),
                    _cellule(charts.LIBELLES_ISSUE[r.issue],
                             brush=ROUGE if r.issue.startswith("ECHEC") else None),
                ]
                donnees = _Num(f"{_jour(r.derniere_bougie)} ({retard} j)", -retard,
                               brush=ROUGE if retard > FRAICHEUR_MAX_JOURS else None)
            groupes = ", ".join(g.nom for g in self._groupes.values() if c.id in g.ids)
            for j, item in enumerate(debut + milieu + fin + [donnees, _cellule(groupes), _cellule(note)]):
                t.setItem(i, j, item)
            t.item(i, 0).setData(Qt.ItemDataRole.UserRole, c.id)
            t.item(i, 0).setToolTip(configs.resume(c))
        t.setSortingEnabled(True)
        t.sortItems(colonne, sens)

        ids = [t.item(i, 0).data(Qt.ItemDataRole.UserRole) for i in range(t.rowCount())]
        self._suivi_ids = [i for i in self._suivi_ids if i in ids] or ids[:1]
        for id_config in self._suivi_ids:
            t.selectionModel().select(
                t.model().index(ids.index(id_config), 0),
                QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        self._suivi_reconstruction = False
        self._maj_etat_suivi(affichees)
        self._maj_graphique_suivi()

    def _maj_etat_suivi(self, affichees):
        if not self._configs:
            self.label_suivi.setText("Aucune config sauvegardée : sauvegarde-en depuis le Criblage.")
            return
        calculees = [self._suivis[c.id] for c in affichees if isinstance(self._suivis.get(c.id), suivi.Suivi)]
        texte = f"{len(calculees)} sur {len(affichees)} calculées"
        if calculees:
            derniere = max(s.derniere_bougie for s in calculees)
            texte += f"  ·  données jusqu'au {_jour(derniere)}"
        self.label_suivi.setText(texte)

    def _suivi_selectionne(self):
        if self._suivi_reconstruction:
            return
        lignes = self.table_suivi.selectionModel().selectedRows()
        if not lignes:
            return
        self._suivi_ids = [self.table_suivi.item(l.row(), 0).data(Qt.ItemDataRole.UserRole)
                           for l in sorted(lignes, key=lambda l: l.row())]
        self._maj_graphique_suivi()

    def _clic_suivi(self, point):
        """Un clic sur une courbe la sélectionne dans le tableau."""
        id_config = point.get("meta")
        for i in range(self.table_suivi.rowCount()):
            if self.table_suivi.item(i, 0).data(Qt.ItemDataRole.UserRole) == id_config:
                self.table_suivi.selectRow(i)
                break

    def _maj_graphique_suivi(self):
        calcules = [(c, self._suivis[c.id]) for c in self._configs_affichees()
                    if isinstance(self._suivis.get(c.id), suivi.Suivi)]
        libelles = {c.id: f"#{c.id} {c.symbole} {c.unite.upper()} {c.entree_type}→{c.sortie_type} "
                          f"{c.rapide}/{c.lente} x{s.levier:g}" for c, s in calcules}
        suivis = [s for _, s in calcules]
        choisis = [s for s in suivis if s.id in self._suivi_ids]
        agregat = None
        if len(choisis) >= 2:
            agregat = suivi.agreger(choisis, self.combo_agregat.currentData())
            suivis = choisis                    # le reste n'est que du bruit à côté du portefeuille
            regles = agregat.regles
        else:
            regles = choisis[0].regles if choisis else (
                suivis[0].regles if suivis else propfirm.Regles())
        self.bandeau_agregat.setVisible(agregat is not None)
        if agregat is not None:
            self.label_agregat.setText(self._texte_agregat(agregat))
        self.vue_suivi.afficher(charts.figure_suivi(
            suivis, libelles, self._suivi_ids[0] if self._suivi_ids else None, regles, agregat))

    @staticmethod
    def _texte_agregat(a):
        """Statistiques du portefeuille, en une courte fiche sous le sélecteur de mode."""
        def couleur(v):
            return "#1a7f37" if v > 0 else "#cf222e" if v < 0 else "#666"
        n = len(a.ids)
        rd = "n/a" if a.rend_dd is None else f"{a.rend_dd:.1f}"
        limite = ' style="color:#cf222e"' if a.dd_pct >= a.regles.dd_max_pct else ""
        texte = (
            f"<b>{n} configs</b> · rendement <b style='color:{couleur(a.rendement_pct)}'>"
            f"{a.rendement_pct:+.1f} %</b> · DD max <b{limite}>{a.dd_pct:.1f} %</b> · "
            f"Rend/DD <b>{rd}</b> · {a.n_trades} trades<br>"
            f"Challenge -30j : <b>{charts.LIBELLES_ISSUE[a.issue]}</b> · "
            f"{a.n_positives}/{n} configs positives · "
            f"DD moyen des configs seules : {a.dd_moyen_pct:.1f} %")
        if a.regles_differentes:
            texte += "<br><i>Les configs n'ont pas les mêmes règles de challenge : celles de la première s'appliquent.</i>"
        return texte

    # -------------------------------------------------------------- analyse

    def _analyser(self):
        if self._tache is not None and self._tache.isRunning():
            self._tache.annuler()
            self.bouton_analyser.setEnabled(False)
            return
        try:
            params = self._lire_params()
        except ValueError as e:
            QMessageBox.warning(self, "Paramètres", str(e))
            return

        manques = self._trous_a_combler(params)
        if manques is None:
            return

        def travail(progres, annule, journal):
            sortie = {}
            n = len(params.symboles)
            for i, sym in enumerate(params.symboles):
                if annule():
                    break
                res = ResultatSymbole()
                sortie[sym] = res
                base = i * 100                 # chaque symbole pèse 100 unités de progression
                part_crible = 100
                try:
                    if sym in manques:
                        part_crible = 50       # la moitié de la barre pour le téléchargement
                        c = manques[sym]
                        try:
                            cli.telecharger(
                                sym, c.premier_manquant, c.dernier_manquant, params.reglages,
                                journal,
                                lambda pct, base=base, sym=sym: progres(
                                    int(base + pct / 2), n * 100, f"{sym} : téléchargement"),
                                annule)
                        except cli.ErreurCli as e:
                            if annule():
                                break
                            res.alertes.append(data.Alerte("avertissement", f"Téléchargement échoué : {e}"))
                    b = data.charger(sym, params.unite, params.debut, params.fin)
                    res.alertes += data.controle(b)
                    trou = data.couverture(sym, params.debut, params.fin)
                    if not trou.complete:
                        res.alertes.append(data.Alerte(
                            "avertissement",
                            f"{trou.manquants} jours ouvrés sans données sur la période "
                            f"({trou.premier_manquant} -> {trou.dernier_manquant})."))
                    res.bougies = b
                    try:
                        res.haussiere = tend.calculer(b, params.tendance)
                        res.tendance = params.tendance
                    except Exception as e:
                        res.alertes.append(data.Alerte(
                            "avertissement", f"Filtre de tendance ignoré : {e}"))
                    prix = float(np.median(b.close))
                    spread, comm, contrat = params.couts[sym]
                    res.couts = engine.Couts(spread if spread is not None else prix * 2e-4,
                                             comm, contrat)
                    res.lignes = engine.cribler(
                        b, moyennes.TYPES, moyennes.TYPES, params.rapide, params.lente, res.couts,
                        params.regles, params.leviers, params.n_departs, params.min_trades,
                        params.capital,
                        progres=lambda f, t, base=base, sym=sym: progres(
                            int(base + 100 - part_crible + f / t * part_crible), n * 100,
                            f"{sym} : criblage"),
                        annule=annule, haussiere=res.haussiere)
                except Exception as e:
                    res.erreur = f"{type(e).__name__} : {e}"
            return sortie

        self._demarrer(travail, self._analyse_finie, self.bouton_analyser, "Annuler",
                       "Analyser", params)

    def _trous_a_combler(self, params):
        """{symbole: Couverture} à télécharger, {} si rien à faire, None si l'utilisateur annule."""
        if not params.telecharger:
            return {}
        manques = {}
        for sym in params.symboles:
            c = data.couverture(sym, params.debut, params.fin)
            if not c.complete and (sym, c.premier_manquant, c.dernier_manquant) not in self._tentatives:
                manques[sym] = c
        if not manques:
            return {}
        detail = "\n".join(
            f"  {s} : {c.manquants} jours ouvrés manquants ({c.premier_manquant} -> {c.dernier_manquant})"
            for s, c in manques.items())
        Bouton = QMessageBox.StandardButton
        reponse = QMessageBox.question(
            self, "Données manquantes",
            f"Le cache cTrader a des trous sur la période :\n\n{detail}\n\n"
            "Les télécharger avec ctrader-cli (bot fictif DataFetcher en backtest) ? "
            "Compte quelques secondes par symbole et par mois.",
            Bouton.Yes | Bouton.No | Bouton.Cancel, Bouton.Yes)
        if reponse == Bouton.Cancel:
            return None
        if reponse == Bouton.No:
            return {}
        try:
            params.reglages.verifier()
        except cli.ErreurCli as e:
            QMessageBox.warning(self, "cTrader", str(e))
            return None
        for s, c in manques.items():
            self._tentatives.add((s, c.premier_manquant, c.dernier_manquant))
        return manques

    def _demarrer(self, fonction, a_la_fin, bouton, libelle_actif, libelle_repos, contexte=None):
        self._tache = Tache(fonction, self)
        self._contexte = contexte
        bouton.setText(libelle_actif)
        self.barre_progres.setVisible(True)
        self.barre_progres.setValue(0)

        def fin():
            self.barre_progres.setVisible(False)
            bouton.setText(libelle_repos)
            bouton.setEnabled(True)
            self.statusBar().clearMessage()

        def progres(fait, total, msg):
            self.barre_progres.setMaximum(total)      # 0 = progression indéterminée
            self.barre_progres.setValue(fait)
            self.statusBar().showMessage(msg)

        def journaliser(ligne):
            self.journal.appendPlainText(ligne)
            self.dock_journal.show()

        def termine(resultat):
            fin()
            a_la_fin(resultat)

        def echec(message):
            fin()
            QMessageBox.critical(self, "Erreur", message)

        self._tache.progres.connect(progres)
        self._tache.journal.connect(journaliser)
        self._tache.fini.connect(termine)
        self._tache.echec.connect(echec)
        self._tache.start()

    def _analyse_finie(self, resultats):
        self.params = self._contexte
        self.resultats = resultats
        self.infos = data.lister_symboles()        # de nouvelles données ont pu arriver
        self._maj_textes_symboles()
        self._remplir_apercu()
        analyses = [s for s, r in resultats.items() if r.bougies is not None and r.lignes]

        for combo in (self.combo_crible_sym, self.combo_det_sym):
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(analyses)
            combo.blockSignals(False)
        for combo in (self.combo_chal_levier, self.combo_cmp_levier):
            combo.blockSignals(True)
            combo.clear()
            for lev in self.params.leviers:
                combo.addItem(f"x{lev:g}", lev)
            combo.blockSignals(False)
        self.spin_det_rapide.setValue(self.params.rapide)
        self.spin_det_lente.setValue(self.params.lente)
        self.detail = None
        self.onglets.setCurrentIndex(T_APERCU)
        restaurer, self._a_restaurer = self._a_restaurer, None
        if analyses:
            if restaurer is not None and restaurer[0] in analyses:
                sym, e, s = restaurer          # la config qu'on vient de recharger
            else:
                # On ouvre le symbole le mieux classé de la vue d'ensemble.
                sym = max(analyses, key=lambda x: (self.resultats[x].meilleure() or _AUCUNE).meilleur_levier()[1])
                m = self.resultats[sym].meilleure()
                e, s = (m.entree_type, m.sortie_type) if m is not None else (None, None)
                restaurer = None
            self.combo_crible_sym.setCurrentText(sym)
            self._changer_symbole_crible()
            if e is not None:
                self._selectionner_paire_crible(e, s)
                self._selectionner_config(sym, e, s)
            if restaurer is not None:
                self.onglets.setCurrentIndex(T_CRIBLE)

    def _remplir_apercu(self):
        t = self.table_apercu
        t.setRowCount(0)
        hasard = self.params.regles.hasard_pur()

        def cle(item):
            m = item[1].meilleure()
            return -(m.meilleur_levier()[1] if m else -1)

        for sym, r in sorted(self.resultats.items(), key=cle):
            i = t.rowCount()
            t.insertRow(i)
            b = r.bougies
            derniere = b.temps[-1].astype("datetime64[m]") if b is not None else None
            retard = _retard_jours(derniere) if derniere is not None else None
            perime = retard is not None and retard > FRAICHEUR_MAX_JOURS
            m = r.meilleure()
            lev, p = m.meilleur_levier() if m else (None, None)
            cellules = [
                sym,
                f"{derniere}  (il y a {retard} j)" if derniere is not None else "n/a",
                len(b) if b is not None else "",
                r.erreur or (f"{len(r.alertes)}" if r.alertes else "0"),
                f"{m.entree_type} → {m.sortie_type}" if m else "aucune (trop peu de trades)",
                f"x{lev:g}" if lev else "",
                f"{p * 100:.0f} %" if m else "",
                f"{hasard * 100:.0f} %",
                f"{(p - hasard) * 100:+.0f} pts" if m else "",
                m.n_trades if m else "",
                f"{m.rendement_pct:+.1f}" if m else "",
                f"{m.dd_pct:.1f}" if m else "",
            ]
            for j, texte in enumerate(cellules):
                brush = ROUGE if (j == 1 and perime) or (j == 3 and (r.erreur or r.alertes)) else None
                t.setItem(i, j, _cellule(texte, droite=j >= 5, brush=brush))
            t.item(i, 0).setData(Qt.ItemDataRole.UserRole, sym)
        if t.rowCount():
            t.selectRow(0)

    def _symbole_de_ligne(self):
        lignes = self.table_apercu.selectionModel().selectedRows()
        return self.table_apercu.item(lignes[0].row(), 0).data(Qt.ItemDataRole.UserRole) \
            if lignes else None

    def _afficher_alertes(self):
        sym = self._symbole_de_ligne()
        r = self.resultats.get(sym)
        if r is None:
            return
        lignes = [f"[{a.niveau}] {a.message}" for a in r.alertes]
        if r.erreur:
            lignes.insert(0, f"[erreur] {r.erreur}")
        self.texte_alertes.setPlainText("\n".join(lignes) or "Aucune alerte sur les données.")

    def _ouvrir_depuis_apercu(self, item):
        sym = self.table_apercu.item(item.row(), 0).data(Qt.ItemDataRole.UserRole)
        if self.combo_crible_sym.findText(sym) >= 0:
            self.combo_crible_sym.setCurrentText(sym)
            self.onglets.setCurrentIndex(T_CRIBLE)

    # -------------------------------------------------------------- criblage

    def _changer_symbole_crible(self):
        r = self.resultats.get(self.combo_crible_sym.currentText())
        if r is not None:
            self._ecrire_tendance(r.tendance)
        self._maj_crible()

    def _maj_crible(self):
        sym = self.combo_crible_sym.currentText()
        r = self.resultats.get(sym)
        t = self.table_crible
        t.setSortingEnabled(False)
        t.setRowCount(0)
        if r is None or not r.lignes:
            return
        leviers, hasard = self.params.leviers, self.params.regles.hasard_pur()
        colonnes = (["Entrée", "Sortie", "Trades", "Rend. %", "DD %", "Sharpe", "PF", "Réussite %"]
                    + [f"P x{lev:g}" for lev in leviers]
                    + ["Meilleur P", "Levier", "Écart (pts)", "Collisions", "Marge min", "Ignorées"])
        t.setColumnCount(len(colonnes))
        t.setHorizontalHeaderLabels(colonnes)
        n = len(colonnes)
        for j, aide in ((n - 3, "Barres où un signal d'entrée et un signal de sortie coïncident pendant une "
                                "position. Corrigées : la sortie est prioritaire et l'entrée est reportée "
                                "d'une barre (BotX ferme et rouvre sur la même barre)."),
                        (n - 2, "Plus petit nombre de barres entre une sortie sur signal et le signal "
                                "d'entrée suivant (1 = la barre d'après)."),
                        (n - 1, "Signaux d'entrée reçus alors qu'une position était déjà ouverte, sans "
                                "effet (BotX les ignore aussi), sur le total des signaux d'entrée.")):
            t.horizontalHeaderItem(j).setToolTip(aide)
        lignes = [l for l in r.lignes
                  if not (self.check_propre.isChecked() and l.chevauchement and l.chevauchement.n_collisions)]
        for l in lignes:
            i = t.rowCount()
            t.insertRow(i)
            lev, p = l.meilleur_levier()
            cellules = [
                QTableWidgetItem(l.entree_type), QTableWidgetItem(l.sortie_type),
                _Num(str(l.n_trades), l.n_trades),
                _Num(f"{l.rendement_pct:+.1f}", l.rendement_pct, _signe(l.rendement_pct)),
                _Num(f"{l.dd_pct:.1f}", l.dd_pct),
                _Num(_n(l.sharpe, ".2f"), l.sharpe, _signe(l.sharpe)),
                _Num(_n(l.profit_factor, ".2f"), l.profit_factor),
                _Num(_n(l.win_rate_pct, ".0f"), l.win_rate_pct),
            ]
            for lv in leviers:
                pl = l.p_reussite.get(lv)
                cellules.append(_Num("-" if pl is None else f"{pl * 100:.0f}", pl,
                                     fond=None if pl is None else _fond_p(pl)))
            avec_p = bool(l.p_reussite)
            cellules += [
                _Num(f"{p * 100:.0f}" if avec_p else "-", p if avec_p else None,
                     fond=_fond_p(p) if avec_p else None),
                _Num(f"x{lev:g}" if avec_p else "-", lev if avec_p else None),
                _Num(f"{round((p - hasard) * 100):+d}" if avec_p else "-",
                     (p - hasard) * 100 if avec_p else None),
            ]
            ch = l.chevauchement
            cellules += [
                _Num(str(ch.n_collisions), ch.n_collisions, ROUGE if ch.n_collisions else None),
                _Num(_n(ch.marge_min, "d"), ch.marge_min),
                _Num(f"{ch.n_ignorees}/{ch.n_signaux_entree}", ch.n_ignorees),
            ] if ch else [_Num("-"), _Num("-"), _Num("-")]
            for j, cellule in enumerate(cellules):
                t.setItem(i, j, cellule)
            t.item(i, 0).setData(Qt.ItemDataRole.UserRole, (l.entree_type, l.sortie_type))
        t.setSortingEnabled(True)
        t.sortByColumn(len(leviers) + 8, Qt.SortOrder.DescendingOrder)     # "Meilleur P"
        t.selectRow(0)

    def _paire_selectionnee(self):
        lignes = self.table_crible.selectionModel().selectedRows()
        if not lignes:
            return None
        e, s = self.table_crible.item(lignes[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        return self.combo_crible_sym.currentText(), e, s

    def _ouvrir_detail_depuis_crible(self, _item):
        paire = self._paire_selectionnee()
        if paire is not None:
            self._selectionner_config(*paire)
            self.onglets.setCurrentIndex(T_DETAIL)

    def _appliquer_tendance(self):
        if self._tache is not None and self._tache.isRunning():
            self._tache.annuler()
            return
        sym = self.combo_crible_sym.currentText()
        r, p = self.resultats.get(sym), self.params
        if r is None or r.bougies is None:
            return
        t = self._lire_tendance()

        def travail(progres, annule, journal):
            haussiere = tend.calculer(r.bougies, t)
            lignes = engine.cribler(
                r.bougies, moyennes.TYPES, moyennes.TYPES, p.rapide, p.lente, r.couts, p.regles,
                p.leviers, p.n_departs, p.min_trades, p.capital,
                progres=lambda f, n: progres(f, n, f"{sym} : criblage"), annule=annule,
                haussiere=haussiere)
            return haussiere, lignes

        def fini(resultat):
            r.haussiere, r.lignes = resultat
            r.tendance = t
            self.detail = None
            self._remplir_apercu()
            self._maj_crible()

        self._demarrer(travail, fini, self.bouton_tendance, "Annuler", "Appliquer")

    def _selectionner_paire_crible(self, entree, sortie):
        t = self.table_crible
        for i in range(t.rowCount()):
            if t.item(i, 0).data(Qt.ItemDataRole.UserRole) == (entree, sortie):
                t.selectRow(i)
                t.scrollToItem(t.item(i, 0))
                return

    # -- sauvegarde des configs (DuckDB)

    @staticmethod
    def _ligne_labo_selectionnee(table, lignes):
        """La ligne sélectionnée du tableau de détail, sinon la première (la référence)."""
        rangs = table.selectionModel().selectedRows()
        if rangs and rangs[0].row() < len(lignes):
            return lignes[rangs[0].row()]
        return lignes[0] if lignes else None

    def _config_courante(self):
        paire = self._paire_selectionnee()
        if paire is None or self.params is None:
            return None
        sym, e, s = paire
        r, p = self.resultats[sym], self.params
        ligne = next(l for l in r.lignes if (l.entree_type, l.sortie_type) == (e, s))
        lev, meilleur_p = ligne.meilleur_levier()
        # Une config = une seule stratégie : l'onglet de détail affiché dit laquelle.
        en_long_short = self.onglets_labo.currentIndex() == 1
        pol = ls = None
        if en_long_short:
            ls = self._ligne_labo_selectionnee(self.table_ls, self._labo_lignes[1])
        else:
            pol = self._ligne_labo_selectionnee(self.table_pol, self._labo_lignes[0])
        metriques = {"crible": {
            "n_trades": ligne.n_trades, "rendement_pct": ligne.rendement_pct,
            "dd_pct": ligne.dd_pct, "sharpe": ligne.sharpe, "win_rate_pct": ligne.win_rate_pct,
            "profit_factor": ligne.profit_factor, "p_reussite": ligne.p_reussite,
            "meilleur_levier": lev, "meilleur_p": meilleur_p if ligne.p_reussite else None,
            "hasard": p.regles.hasard_pur(),
            "chevauchement": asdict(ligne.chevauchement) if ligne.chevauchement else None}}
        if pol is not None:
            metriques["politique"] = {**asdict(pol), "botx_params": pol.parametres_botx,
                                      "risque_botx": pol.risque_botx}
        if ls is not None:
            metriques["direction"] = {**asdict(ls), "politique": self.combo_ls_politique.currentText(),
                                      "botx_params": ls.parametres_botx, "risque_botx": ls.risque_botx}
        return configs.Config(
            note="", symbole=sym, unite=p.unite, debut=p.debut, fin=p.fin, entree_type=e,
            sortie_type=s, rapide=p.rapide, lente=p.lente, tendance=r.tendance,
            politique=self.combo_ls_politique.currentText() if en_long_short
            else (pol.nom if pol else "croisement seul"),
            direction=ls.nom if ls else "long seul",
            parametres={"regles": asdict(p.regles), "capital": p.capital,
                        "leviers": list(p.leviers), "n_departs": p.n_departs,
                        "min_trades": p.min_trades, "couts": asdict(r.couts)},
            metriques=metriques)

    def _sauvegarder_config(self):
        """Enregistre tout de suite (sans boîte modale qui cacherait les tableaux) et ouvre la
        config dans la sidebar, curseur dans la note."""
        config = self._config_courante()
        if config is None:
            QMessageBox.information(self, "Configs", "Sélectionne d'abord une ligne du criblage.")
            return
        try:
            id_config = configs.sauvegarder(config)
        except Exception as e:
            QMessageBox.warning(self, "Configs", f"Sauvegarde impossible : {e}")
            return
        self._rafraichir_configs(selectionner=id_config)
        self.onglets_gauche.setCurrentIndex(1)
        self.note_config.setFocus()
        self.statusBar().showMessage(f"Config #{id_config} sauvegardée : ajoute une note dans la sidebar.", 8000)

    @staticmethod
    def _libelle_config(c):
        cr = c.metriques.get("crible", {})
        p = (f"{cr['meilleur_p'] * 100:.0f} % x{cr['meilleur_levier']:g}"
             if cr.get("meilleur_p") is not None else "-")
        note = c.note.strip().splitlines()[0] if c.note.strip() else "(sans note)"
        return (f"#{c.id} · {c.symbole} {c.unite.upper()} · {p}\n"
                f"{c.entree_type}→{c.sortie_type} {c.rapide}/{c.lente}\n{note}")

    def _rafraichir_configs(self, selectionner=None):
        self._enregistrer_note()
        courante = self._config_selectionnee()
        cible = selectionner if selectionner is not None else (courante.id if courante else None)
        self.liste_configs.blockSignals(True)
        self.liste_configs.clear()
        try:
            self._configs = {c.id: c for c in configs.lister()}
        except Exception as e:
            self._configs = {}
            self.liste_configs.blockSignals(False)
            self.texte_config.setPlainText(f"Base illisible : {e}")
            self._charger_note(None)
            return
        for c in self._configs.values():
            item = QListWidgetItem(self._libelle_config(c))
            item.setSizeHint(QSize(0, 3 * self.liste_configs.fontMetrics().lineSpacing() + 12))
            item.setData(Qt.ItemDataRole.UserRole, c.id)
            item.setToolTip(c.note)
            self.liste_configs.addItem(item)
            if c.id == cible:
                self.liste_configs.setCurrentItem(item)
        if self.liste_configs.currentItem() is None and self.liste_configs.count():
            self.liste_configs.setCurrentRow(0)
        self.liste_configs.blockSignals(False)
        self._config_choisie()
        self._suivis = {i: r for i, r in self._suivis.items() if i in self._configs}
        try:
            self._groupes = {g.id: g for g in configs.lister_groupes()}
        except Exception:
            self._groupes = {}
        self._maj_filtres_suivi()
        self._remplir_suivi()

    def _config_choisie(self, *_):
        self._enregistrer_note()              # la note de la config qu'on quitte
        c = self._config_selectionnee()
        self.texte_config.setPlainText(configs.resume(c) if c else "")
        self._charger_note(c)

    def _charger_note(self, c):
        self.note_config.blockSignals(True)
        self.note_config.setPlainText(c.note if c else "")
        self.note_config.blockSignals(False)
        self.note_config.setEnabled(c is not None)
        self._note_id = c.id if c else None
        self._note_sale = False

    def _note_modifiee(self):
        self._note_sale = True
        self._timer_note.start(700)          # enregistre quand la frappe se calme

    def _enregistrer_note(self):
        self._timer_note.stop()
        if not self._note_sale or self._note_id is None:
            return
        self._note_sale = False
        texte = self.note_config.toPlainText().strip()
        c = self._configs.get(self._note_id)
        try:
            configs.modifier_note(self._note_id, texte)
        except Exception as e:
            self.statusBar().showMessage(f"Note non enregistrée : {e}", 8000)
            self._note_sale = True
            return
        if c is not None:
            c = replace(c, note=texte)
            self._configs[c.id] = c
            for i in range(self.liste_configs.count()):
                item = self.liste_configs.item(i)
                if item.data(Qt.ItemDataRole.UserRole) == c.id:
                    item.setText(self._libelle_config(c))
                    item.setToolTip(c.note)
            actuelle = self._config_selectionnee()
            if actuelle is not None and actuelle.id == c.id:
                self.texte_config.setPlainText(configs.resume(c))

    def _config_selectionnee(self):
        item = self.liste_configs.currentItem()
        return self._configs.get(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None

    def _copier_resume(self):
        self._enregistrer_note()
        c = self._config_selectionnee()
        if c is not None:
            QApplication.clipboard().setText(configs.resume(c))
            self.statusBar().showMessage("Résumé copié.", 4000)

    def _supprimer_config(self):
        c = self._config_selectionnee()
        if c is None:
            return
        Bouton = QMessageBox.StandardButton
        if QMessageBox.question(self, "Supprimer", f"Supprimer la config #{c.id} ({c.symbole}) ?",
                                Bouton.Yes | Bouton.No, Bouton.No) == Bouton.Yes:
            self._note_sale = False
            configs.supprimer(c.id)
            self._rafraichir_configs()

    def _recharger_config(self):
        """Remet les paramètres de la config dans le panneau de gauche (sans lancer d'analyse)."""
        c = self._config_selectionnee()
        if c is None:
            return
        for i in range(self.liste_symboles.count()):
            self.liste_symboles.item(i).setCheckState(Qt.CheckState.Unchecked)
        self._ajouter_symbole(c.symbole, True)
        self.combo_unite.setCurrentText(c.unite)
        self.date_debut.setDate(QDate.fromString(str(c.debut), "yyyy-MM-dd"))
        self.date_fin.setDate(QDate.fromString(str(c.fin), "yyyy-MM-dd"))
        self.spin_rapide.setValue(c.rapide)
        self.spin_lente.setValue(c.lente)
        self._ecrire_tendance(c.tendance)
        par = c.parametres
        r = par.get("regles", {})
        for spin, cle in ((self.spin_cible, "cible_pct"), (self.spin_dd, "dd_max_pct"),
                          (self.spin_jour, "perte_jour_pct"), (self.spin_duree, "jours_max")):
            if cle in r:
                spin.setValue(r[cle])
        self.check_trailing.setChecked(bool(r.get("dd_trailing", False)))
        if "capital" in par:
            self.spin_capital.setValue(par["capital"])
        if par.get("leviers"):
            self.edit_leviers.setText(", ".join(f"{x:g}" for x in par["leviers"]))
        if "n_departs" in par:
            self.spin_departs.setValue(par["n_departs"])
        if "min_trades" in par:
            self.spin_min_trades.setValue(par["min_trades"])
        cts = par.get("couts")
        for i in range(self.table_couts.rowCount()):
            if cts and self.table_couts.verticalHeaderItem(i).text() == c.symbole:
                self.table_couts.item(i, 0).setText(f"{cts['spread']:g}")
                self.table_couts.item(i, 1).setText(f"{cts['commission_par_lot_par_cote']:g}")
                self.table_couts.item(i, 2).setText(f"{cts['taille_contrat']:g}")
        self._a_restaurer = (c.symbole, c.entree_type, c.sortie_type)
        self.onglets_gauche.setCurrentIndex(0)          # les paramètres rechargés, à ajuster
        self.statusBar().showMessage(
            "Paramètres rechargés : clique sur Analyser pour retrouver cette config.", 10000)

    # -- détails de la paire sélectionnée : politiques de sortie et long/short

    def _planifier_labo(self):
        self._timer_labo.start(250)       # évite de recalculer à chaque flèche du clavier

    def _lancer_labo(self, partiel=False):
        """Calcule les détails de la paire sélectionnée. `partiel` : seul l'onglet long / short
        (le sélecteur de politique a changé), les sorties long seul restent affichées."""
        paire = self._paire_selectionnee()
        if paire is None or self.params is None:
            return
        sym, e, s = paire
        r, p = self.resultats[sym], self.params
        politique = self.combo_ls_politique.currentText()
        cle = (sym, e, s, id(r.lignes), r.tendance, politique)
        if partiel and cle[:5] != (self._labo_cle or (None,) * 6)[:5]:
            partiel = False                # autre paire depuis : tout recalculer
        self._labo_cle = cle
        for tache in self._labos_en_cours:
            tache.annuler()
        if not partiel:
            self._labo_lignes = ([], [])
            self.table_pol.setRowCount(0)
        self.table_ls.setRowCount(0)
        self.label_labo.setText(f"{sym} · {e} → {s} · {p.rapide}/{p.lente} · "
                                f"{r.tendance.libelle()}   (calcul en cours...)")
        b, couts, haussiere = r.bougies, r.couts, r.haussiere

        def travail(progres, annule, journal):
            entrees, sorties = engine.signaux(b.close, e, s, p.rapide, p.lente)
            pol = None
            if not partiel:
                filtrees = entrees & haussiere if haussiere is not None else entrees
                pol = labo.comparer_sorties(b, filtrees, sorties, couts, p.regles, p.leviers,
                                            p.n_departs, p.capital, annule)
            signaux = engine.signaux_long_short(b.close, e, s, p.rapide, p.lente)
            ls = labo.comparer_long_short(b, signaux, couts, p.regles, p.leviers, p.n_departs,
                                          p.capital, haussiere, politique, annule)
            return pol, ls

        tache = Tache(travail, self)
        self._labos_en_cours.append(tache)

        def termine(resultat):
            self._labos_en_cours.remove(tache)
            if cle == self._labo_cle and not tache.annulee:
                self._afficher_labo(cle, b, *resultat)

        def echec(message):
            self._labos_en_cours.remove(tache)
            if cle == self._labo_cle:
                self.label_labo.setText(f"Détails indisponibles : {message}")

        tache.fini.connect(termine)
        tache.echec.connect(echec)
        tache.start()

    def _afficher_labo(self, cle, b, pol, ls):
        sym, e, s = cle[:3]
        p = self.params
        if pol is None:                    # mise à jour partielle : on garde les sorties long seul
            pol = self._labo_lignes[0]
        self._labo_lignes = (pol, ls)
        self.label_labo.setText(f"{sym} · {e} → {s} · {p.rapide}/{p.lente} · {cle[4].libelle()}"
                                f" · long/short : {cle[5]}")
        bh_rend, bh_dd = labo.buy_and_hold(b.close)

        def remplir(table, lignes, colonnes_fn, col_rend_dd):
            table.setRowCount(0)
            for l in lignes:
                i = table.rowCount()
                table.insertRow(i)
                for j, c in enumerate(colonnes_fn(l)):
                    table.setItem(i, j, c)
            # La référence : l'actif seul, sans stratégie.
            bh = [""] * table.columnCount()
            bh[:3] = ["Buy & hold", f"{bh_rend:+.1f}", f"{bh_dd:.1f}"]
            bh[col_rend_dd] = f"{bh_rend / bh_dd:.2f}" if bh_dd else ""
            i = table.rowCount()
            table.insertRow(i)
            for j, texte in enumerate(bh):
                table.setItem(i, j, _cellule(texte, droite=j > 0))

        def commun(l):
            return [_Num(f"{l.rendement_pct:+.1f}", l.rendement_pct, _signe(l.rendement_pct)),
                    _Num(f"{l.dd_pct:.1f}", l.dd_pct),
                    _Num(_n(l.sharpe, ".2f"), l.sharpe, _signe(l.sharpe)),
                    _Num(_n(l.profit_factor, ".2f"), l.profit_factor)]

        def lev_p(l):
            avec = l.p_reussite is not None
            return [_Num(f"x{l.levier:g}" if avec else "-", l.levier),
                    _Num(f"{l.p_reussite * 100:.0f} %" if avec else "-", l.p_reussite,
                         fond=_fond_p(l.p_reussite) if avec else None)]

        def ligne_pol(l):
            gain = l.gain
            return [QTableWidgetItem(l.nom), *commun(l),
                    _Num(_n(l.win_rate_pct, ".0f"), l.win_rate_pct), _Num(str(l.n_trades), l.n_trades),
                    _Num(_n(l.rend_dd, ".2f"), l.rend_dd),
                    _Num(f"{l.via_stop}/{l.n_trades}", l.via_stop), *lev_p(l),
                    _Num(f"{l.temoin * 100:.0f} %" if l.temoin is not None else "-", l.temoin),
                    _Num(f"{round(gain * 100):+d}" if gain is not None else "-", gain,
                         brush=QBrush(QColor("#009E73")) if gain is not None and gain > 0.05 else None),
                    *_cellules_botx(l)]

        def ligne_ls(l):
            return [QTableWidgetItem(l.nom), *commun(l), _Num(str(l.n_longs), l.n_longs),
                    _Num(str(l.n_shorts), l.n_shorts), _Num(str(l.n_trades), l.n_trades),
                    _Num(_n(l.rend_dd, ".2f"), l.rend_dd),
                    _Num(f"{l.via_stop}/{l.n_trades}", l.via_stop), *lev_p(l), *_cellules_botx(l)]

        remplir(self.table_pol, pol, ligne_pol, 7)
        remplir(self.table_ls, ls, ligne_ls, 8)

    def _selectionner_config(self, sym, entree, sortie):
        self.combo_det_sym.setCurrentText(sym)
        self.combo_det_entree.setCurrentText(entree)
        self.combo_det_sortie.setCurrentText(sortie)
        self.spin_det_rapide.setValue(self.params.rapide)
        self.spin_det_lente.setValue(self.params.lente)
        self._calculer_detail()

    # ---------------------------------------------------------------- détail

    def _calculer_detail(self):
        sym = self.combo_det_sym.currentText()
        r = self.resultats.get(sym)
        if r is None or r.bougies is None:
            return
        b, p = r.bougies, self.params
        rapide, lente = self.spin_det_rapide.value(), self.spin_det_lente.value()
        if rapide >= lente:
            QMessageBox.warning(self, "Détail", "La période rapide doit être inférieure à la lente.")
            return
        entree_type, sortie_type = self.combo_det_entree.currentText(), self.combo_det_sortie.currentText()
        fees, slippage = r.couts.fractions(float(np.median(b.close)))
        entrees, sorties = engine.signaux(b.close, entree_type, sortie_type, rapide, lente)
        if r.haussiere is not None:
            entrees = entrees & r.haussiere
        stop, target = self.spin_stop.value() / 100 or None, self.spin_target.value() / 100 or None
        res = engine.backtest(b, entrees, sorties, fees, slippage, p.capital, stop, target)

        sims, temoins = [], []
        try:
            for lev in p.leviers:
                sims.append(propfirm.simuler(res.equity, b.temps, p.regles, lev, p.n_departs))
                temoins.append(propfirm.temoin_sans_edge(
                    res.equity, b.temps, p.regles, lev, p.n_departs,
                    close_actif=b.close, exposition=res.exposition))
        except ValueError as e:
            self.label_chal.setText(f"Simulation impossible : {e}")
            sims, temoins = [], []
        self.detail = Detail(sym, b, res, entree_type, sortie_type, rapide, lente, sims, temoins,
                             r.tendance)

        self._afficher_kpi(res)
        self._remplir_sorties(res)
        self._maj_horaire()
        self.vue_detail.afficher(charts.figure_detail(b, res, entree_type, sortie_type, rapide, lente))
        self._maj_challenge()

    # ------------------------------------------------ analyse par tranche horaire

    def _construire_horaire(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 4, 0, 0)
        h = QHBoxLayout()
        self.combo_tranche = QComboBox()
        for minutes, libelle in ((30, "30 min"), (60, "1 h"), (120, "2 h")):
            self.combo_tranche.addItem(libelle, minutes)
        self.spin_decalage = _spin(-12, 14, 0)
        self.spin_decalage.setPrefix("UTC ")
        self.spin_decalage.setSuffix(" h")
        self.spin_decalage.setSpecialValueText("UTC")
        self.spin_decalage.setToolTip(
            "Les bougies sont en UTC. +1 ou +2 pour l'heure de Paris (l'heure d'été n'est pas "
            "gérée : le décalage est le même toute l'année).")
        for widget in (self.combo_tranche, self.spin_decalage):
            (widget.currentIndexChanged if widget is self.combo_tranche
             else widget.valueChanged).connect(lambda _: self._maj_horaire())
        h.addWidget(QLabel("Tranche"))
        h.addWidget(self.combo_tranche)
        h.addWidget(QLabel("Heure affichée"))
        h.addWidget(self.spin_decalage)
        h.addStretch(1)
        self.label_horaire = QLabel("Calcule d'abord une config.")
        self.label_horaire.setWordWrap(True)
        self.label_horaire.setToolTip(
            "Contribution : le rendement de chaque barre (equity, position ouverte comprise) "
            "additionné par tranche : où l'equity monte et où elle se dégrade. Trades : classés "
            "par tranche d'ENTRÉE : à quelle heure la décision d'entrer est bonne ou mauvaise.")
        self.vue_horaire = PlotlyView()
        self.table_horaire = _tableau(["Tranche", "Barres", "Contribution %", "Trades entrés",
                                       "Gagnants %", "PnL total", "PnL moyen"])
        self.table_horaire.setSortingEnabled(True)
        self.table_horaire.setAlternatingRowColors(True)
        separateur = QSplitter(Qt.Orientation.Vertical)
        separateur.addWidget(self.vue_horaire)
        separateur.addWidget(self.table_horaire)
        separateur.setSizes([420, 200])
        v.addLayout(h)
        v.addWidget(self.label_horaire)
        v.addWidget(separateur, 1)
        return w

    def _maj_horaire(self):
        d = self.detail
        if d is None:
            return
        minutes = self.combo_tranche.currentData()
        pas = horaire.pas_effectif(d.bougies.unite, minutes)
        tranches = horaire.par_tranche(d.bougies, d.resultat, minutes, self.spin_decalage.value())
        note = f"  (barres {d.bougies.unite.upper()} : tranche portée à {pas} min)" if pas != minutes else ""
        self.label_horaire.setText(horaire.resume(tranches) + note)
        self.vue_horaire.afficher(charts.figure_horaire(tranches, pas))

        t = self.table_horaire
        t.setSortingEnabled(False)
        t.setRowCount(0)
        for tr in tranches:
            i = t.rowCount()
            t.insertRow(i)
            taux, moyen = tr.taux_gain_pct, tr.pnl_moyen
            cellules = [
                _Num(tr.libelle_plage, tr.debut_min),
                _Num(str(tr.n_barres), tr.n_barres),
                _Num(f"{tr.contribution_pct:+.2f}", tr.contribution_pct, brush=_signe(tr.contribution_pct)),
                _Num(str(tr.n_trades), tr.n_trades),
                _Num("n/a" if taux is None else f"{taux:.0f}", taux),
                _Num(f"{tr.pnl_total:+,.2f}", tr.pnl_total, brush=_signe(tr.pnl_total)),
                _Num("n/a" if moyen is None else f"{moyen:+,.2f}", moyen, brush=_signe(moyen)),
            ]
            for j, cellule in enumerate(cellules):
                t.setItem(i, j, cellule)
        t.setSortingEnabled(True)
        t.sortItems(0, Qt.SortOrder.AscendingOrder)

    def _afficher_kpi(self, res):
        m = res.metriques

        def f(cle, fmt, suffixe=""):
            v = m.get(cle)
            return "n/a" if v is None else format(v, fmt) + suffixe

        blocs = [
            ("Rendement", f("total_return_pct", "+.1f", " %")),
            ("DD max", f("max_drawdown_pct", ".1f", " %")),
            ("Sharpe", f("sharpe_ratio", ".2f")),
            ("Trades", f("total_trades", "d")),
            ("Réussite", f("win_rate_pct", ".0f", " %")),
            ("Profit factor", f("profit_factor", ".2f")),
            ("Frais", f("total_fees_paid", ",.0f")),
            ("Exposition", f("exposure_pct", ".0f", " %")),
        ]
        if res.collisions:
            blocs.append(("Collisions corrigées", str(res.collisions)))
        self.label_kpi.setText("&nbsp;&nbsp;·&nbsp;&nbsp;".join(
            f"<span style='color:#666'>{k}</span> <b>{v}</b>" for k, v in blocs))

    def _remplir_sorties(self, res):
        groupes = {}
        for t in res.trades:
            groupes.setdefault(t.raison_sortie, []).append(t.pnl)
        t = self.table_sorties
        t.setRowCount(0)
        for raison, pnls in sorted(groupes.items(), key=lambda kv: -len(kv[1])):
            i = t.rowCount()
            t.insertRow(i)
            for j, texte in enumerate([raison, len(pnls), f"{np.mean(pnls):+.2f}", f"{sum(pnls):+,.2f}"]):
                t.setItem(i, j, _cellule(texte, droite=j > 0))

    # ------------------------------------------------------------- challenge

    def _maj_challenge(self):
        d = self.detail
        if d is None or not d.simulations:
            return
        self.label_chal.setText(
            f"{d.symbole}  {d.entree_type} → {d.sortie_type}  {d.rapide}/{d.lente}"
            f"   ·   {d.simulations[0].n} départs, fenêtre de {self.params.regles.jours_max} j "
            f"({d.simulations[0].duree_barres} barres)")
        self.vue_leviers.afficher(charts.figure_leviers(d.simulations, d.temoins, self.params.regles))
        # Le levier le plus parlant par défaut : celui qui réussit le mieux ici.
        meilleur = max(d.simulations, key=lambda s: s.taux).levier
        for combo in (self.combo_chal_levier, self.combo_cmp_levier):
            combo.blockSignals(True)
            combo.setCurrentIndex(max(combo.findData(meilleur), 0))
            combo.blockSignals(False)
        self._maj_departs()

    def _maj_departs(self):
        d = self.detail
        if d is None or not d.simulations:
            return
        levier = self.combo_chal_levier.currentData()
        sim = next((s for s in d.simulations if s.levier == levier), d.simulations[0])
        self.vue_departs.afficher(charts.figure_departs(d.bougies.temps, sim))

    # ------------------------------------------------------------ comparaison

    def _comparer(self):
        d, p = self.detail, self.params
        if d is None:
            QMessageBox.information(self, "Comparaison", "Calcule d'abord une config dans l'onglet Détail.")
            return
        longueur, nombre = self.spin_cmp_longueur.value(), self.spin_cmp_nombre.value()
        if longueur <= p.regles.jours_max:
            QMessageBox.warning(self, "Comparaison", f"Une fenêtre doit dépasser la durée du "
                                f"challenge ({p.regles.jours_max} j).")
            return
        levier = self.combo_cmp_levier.currentData()
        symboles = [s for s, r in self.resultats.items() if r.bougies is not None and r.lignes]
        couts = {s: self.resultats[s].couts for s in symboles}
        cfg = (d.entree_type, d.sortie_type, d.rapide, d.lente)
        filtre = d.tendance

        def travail(progres, annule, journal):
            colonnes = [f"J-{(nombre - w) * longueur} → J-{(nombre - w - 1) * longueur}"
                        if w < nombre - 1 else f"J-{longueur} → fin" for w in range(nombre)]
            colonnes.append(f"Ensemble ({nombre * longueur} j)")
            z = np.full((len(symboles), nombre + 1), np.nan)
            hover = [[""] * (nombre + 1) for _ in symboles]
            for i, sym in enumerate(symboles):
                if annule():
                    break
                progres(i, len(symboles), f"{sym} : chargement et backtest")
                b = data.charger(sym, p.unite, p.fin - np.timedelta64(nombre * longueur, "D"), p.fin)
                fees, slip = couts[sym].fractions(float(np.median(b.close)))
                e, s = engine.signaux(b.close, *cfg[:2], *cfg[2:])
                if filtre.active:
                    e = e & tend.calculer(b, filtre)
                res = engine.backtest(b, e, s, fees, slip, p.capital, avec_trades=False)
                fin_t = b.temps[-1]
                bornes = [fin_t - np.timedelta64((nombre - w) * longueur, "D") for w in range(nombre)]
                bornes.append(fin_t + np.timedelta64(1, "ns"))
                idx = np.searchsorted(b.temps, bornes)
                tranches = [(idx[w], idx[w + 1]) for w in range(nombre)] + [(0, len(b))]
                for w, (i0, i1) in enumerate(tranches):
                    try:
                        sim = propfirm.simuler(res.equity[i0:i1], b.temps[i0:i1], p.regles,
                                               levier, p.n_departs)
                    except ValueError as err:
                        hover[i][w] = f"{sym}<br>{err}"
                        continue
                    eq = res.equity[i0:i1]
                    z[i, w] = sim.taux
                    hover[i][w] = (
                        f"<b>{sym}</b> · {colonnes[w]}<br>{_jour(b.temps[i0])} → "
                        f"{_jour(b.temps[i1 - 1])}<br>P(réussite) : {sim.taux * 100:.0f} %<br>"
                        f"rendement de la fenêtre : {(eq[-1] / eq[0] - 1) * 100:+.1f} %")
            return z, colonnes, symboles, hover

        def fini(resultat):
            z, colonnes, lignes, hover = resultat
            titre = (f"P(réussite) de {cfg[0]} → {cfg[1]} {cfg[2]}/{cfg[3]}, levier x{levier:g}, "
                     f"par fenêtre de {longueur} j")
            self.vue_cmp.afficher(charts.heatmap_fenetres(z, colonnes, lignes, hover, titre, p.regles))

        self._demarrer(travail, fini, self.bouton_cmp, "Annuler", "Comparer")
