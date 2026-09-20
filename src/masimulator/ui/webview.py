"""Vue Plotly interactive dans QtWebEngine.

Une page « coquille » (plotly.js + QWebChannel) est chargée une fois ; chaque
figure est ensuite poussée avec ``Plotly.react`` sans recharger la page. Les
clics sur un point remontent en Python via le signal ``clic``.

On passe par un fichier local plutôt que par ``setHtml`` : plotly.js pèse ~4 Mo
et setHtml plafonne à 2 Mo.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import plotly
from PySide6.QtCore import QObject, QUrl, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView

_COQUILLE = """<!doctype html>
<html><head><meta charset="utf-8">
<style>html,body{margin:0;height:100%;background:#fff;overflow:hidden}
#g{width:100vw;height:100vh}</style>
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
<script src="plotly.min.js"></script>
</head><body><div id="g"></div>
<script>
let pont = null;
new QWebChannel(qt.webChannelTransport, c => { pont = c.objects.pont; });
const g = document.getElementById('g');
function dessiner(fig) {
  Plotly.react(g, fig.data, fig.layout,
               {responsive: true, displaylogo: false}).then(() => {
    g.removeAllListeners('plotly_click');
    g.on('plotly_click', d => {
      const p = d.points[0];
      if (pont) pont.recevoir(JSON.stringify({x: p.x, y: p.y, pointNumber: p.pointNumber}));
    });
  });
}
</script></body></html>
"""

_dossier: Path | None = None


def _dossier_page():
    """Dossier temporaire partagé : coquille HTML + plotly.min.js (copié une fois)."""
    global _dossier
    if _dossier is None:
        _dossier = Path(tempfile.mkdtemp(prefix="masimulator_"))
        src = Path(plotly.__file__).parent / "package_data" / "plotly.min.js"
        shutil.copy(src, _dossier / "plotly.min.js")
        (_dossier / "coquille.html").write_text(_COQUILLE, encoding="utf-8")
    return _dossier


class _Pont(QObject):
    clic = Signal(dict)

    @Slot(str)
    def recevoir(self, texte):
        self.clic.emit(json.loads(texte))


class PlotlyView(QWebEngineView):
    """Affiche une figure Plotly ; `clic` émet {x, y, pointNumber} du point cliqué."""

    clic = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pret = False
        self._en_attente = None
        self._pont = _Pont(self)
        self._pont.clic.connect(self.clic)
        self._canal = QWebChannel(self)
        self._canal.registerObject("pont", self._pont)
        self.page().setWebChannel(self._canal)
        self.page().setBackgroundColor(QColor("white"))
        self.loadFinished.connect(self._charge)
        self.setUrl(QUrl.fromLocalFile(str(_dossier_page() / "coquille.html")))

    def _charge(self, ok):
        self._pret = ok
        if ok and self._en_attente is not None:
            self._envoyer(self._en_attente)
            self._en_attente = None

    def _envoyer(self, figure_json):
        self.page().runJavaScript(f"dessiner({figure_json})")

    def afficher(self, figure):
        figure_json = figure.to_json()
        if self._pret:
            self._envoyer(figure_json)
        else:
            self._en_attente = figure_json
