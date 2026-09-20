"""Fenêtre principale : panneau de paramètres à gauche, vues en onglets à droite."""

from __future__ import annotations

from dataclasses import dataclass, field

import os

import numpy as np
from PySide6.QtCore import QDate, QSettings, Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateEdit, QDialog,
    QDialogButtonBox, QDockWidget, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from .. import cli, data, engine, moyennes, propfirm
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


@dataclass
class ResultatSymbole:
    bougies: data.Bougies | None = None
    alertes: list = field(default_factory=list)
    couts: engine.Couts | None = None
    lignes: list = field(default_factory=list)
    erreur: str | None = None

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

    def run(self):
        try:
            self.fini.emit(self._fonction(
                lambda fait, total, msg="": self.progres.emit(fait, total, msg),
                lambda: self._annule,
                self.journal.emit))
        except Exception as e:                       # remonté à l'utilisateur
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


def _tableau(colonnes):
    t = QTableWidget(0, len(colonnes))
    t.setHorizontalHeaderLabels(colonnes)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
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

        self._construire_panneau()
        self._construire_vues()

        corps = QSplitter()
        corps.addWidget(self._panneau)
        corps.addWidget(self.onglets)
        corps.setStretchFactor(1, 1)
        corps.setSizes([360, 1140])
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
        self.combo_unite.addItems([u for u in data.MINUTES if u != "m1"])
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

        self._panneau = QWidget()
        v = QVBoxLayout(self._panneau)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(scroll, 1)
        v.addWidget(self.bouton_analyser)

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
            telecharger=self.check_telecharger.isChecked())

    # ------------------------------------------------------------------ vues

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
        v.addWidget(self.table_apercu, 1)
        v.addWidget(self.texte_alertes)
        self.onglets.addTab(w, ONGLETS[T_APERCU])

        # Criblage
        w = QWidget()
        v = QVBoxLayout(w)
        h = QHBoxLayout()
        self.combo_crible_sym = QComboBox()
        self.combo_metrique = QComboBox()
        for cle, libelle in charts.METRIQUES.items():
            self.combo_metrique.addItem(libelle, cle)
        self.combo_crible_levier = QComboBox()
        for widget in (self.combo_crible_sym, self.combo_metrique, self.combo_crible_levier):
            widget.currentIndexChanged.connect(self._maj_crible)
        h.addWidget(QLabel("Symbole"))
        h.addWidget(self.combo_crible_sym)
        h.addWidget(QLabel("Métrique"))
        h.addWidget(self.combo_metrique)
        h.addWidget(QLabel("Levier"))
        h.addWidget(self.combo_crible_levier)
        h.addStretch(1)
        h.addWidget(QLabel("Clique sur une case pour ouvrir son détail"))
        self.vue_crible = PlotlyView()
        self.vue_crible.clic.connect(self._clic_crible)
        v.addLayout(h)
        v.addWidget(self.vue_crible, 1)
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
        v.addWidget(self.vue_detail, 1)
        v.addWidget(self.table_sorties)
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
                        annule=annule)
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
        for combo in (self.combo_crible_levier, self.combo_chal_levier, self.combo_cmp_levier):
            combo.blockSignals(True)
            combo.clear()
            for lev in self.params.leviers:
                combo.addItem(f"x{lev:g}", lev)
            combo.blockSignals(False)
        self.spin_det_rapide.setValue(self.params.rapide)
        self.spin_det_lente.setValue(self.params.lente)
        self.detail = None
        self.onglets.setCurrentIndex(T_APERCU)
        if analyses:
            # On ouvre le symbole le mieux classé de la vue d'ensemble.
            sym = max(analyses, key=lambda s: (self.resultats[s].meilleure() or _AUCUNE).meilleur_levier()[1])
            self.combo_crible_sym.setCurrentText(sym)
            self._maj_crible()
            meilleure = self.resultats[sym].meilleure()
            if meilleure is not None:
                self._selectionner_config(sym, meilleure.entree_type, meilleure.sortie_type)

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

    def _maj_crible(self):
        sym = self.combo_crible_sym.currentText()
        r = self.resultats.get(sym)
        if r is None or not r.lignes:
            return
        metrique = self.combo_metrique.currentData()
        self.combo_crible_levier.setEnabled(metrique == "p_levier")
        levier = self.combo_crible_levier.currentData() or 1.0
        self.vue_crible.afficher(
            charts.heatmap_crible(r.lignes, metrique, levier, self.params.regles))

    def _clic_crible(self, point):
        entree, sortie = point.get("y"), point.get("x")
        if entree in moyennes.CATALOGUE and sortie in moyennes.CATALOGUE:
            self._selectionner_config(self.combo_crible_sym.currentText(), entree, sortie)
            self.onglets.setCurrentIndex(T_DETAIL)

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
        self.detail = Detail(sym, b, res, entree_type, sortie_type, rapide, lente, sims, temoins)

        self._afficher_kpi(res)
        self._remplir_sorties(res)
        self.vue_detail.afficher(charts.figure_detail(b, res, entree_type, sortie_type, rapide, lente))
        self._maj_challenge()

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
