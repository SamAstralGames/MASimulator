"""Pilotage de `ctrader-cli` : compilation du bot fictif, téléchargement de données.

cTrader n'a pas de commande « télécharger l'historique ». Le contournement :
lancer en backtest le bot fictif ``DataFetcher`` (il ne trade jamais) sur le
symbole et la période voulus. Pour simuler, cTrader télécharge les bougies M1
manquantes et les écrit dans son cache local, que ``data.py`` lit ensuite.
"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

RACINE_PROJET = Path(__file__).resolve().parents[2]
PROJET_BOT = RACINE_PROJET / "DataFetcher" / "DataFetcher"
ALGO = PROJET_BOT / "bin" / "Debug" / "net6.0" / "DataFetcher.algo"

_PROGRES = re.compile(r"Progress \| Backtesting \| ([\d.]+) %")
_BLOC_RAPPORT = re.compile(r'^\s*[{}"]')      # le résumé JSON que la CLI recopie en fin de run


class ErreurCli(RuntimeError):
    pass


@dataclass(frozen=True)
class Reglages:
    """Identifiants passés à ctrader-cli (le mot de passe reste dans un fichier)."""
    ctid: str
    pwd_file: str
    account: str

    def verifier(self):
        if not (self.ctid.strip() and self.account.strip()):
            raise ErreurCli("Renseigne le cTID et le numéro de compte (panneau cTrader).")
        if not Path(self.pwd_file).expanduser().is_file():
            raise ErreurCli(f"Fichier de mot de passe introuvable : {self.pwd_file}")

    def arguments(self):
        return [f"--ctid={self.ctid.strip()}",
                f"--pwd-file={Path(self.pwd_file).expanduser()}",
                f"--account={self.account.strip()}"]


def _executer(commande, journal=None, progres=None, annule=None):
    """Lance `commande`, relaie chaque ligne au journal et renvoie la sortie complète.

    La lecture se fait dans un thread pour pouvoir interrompre le processus même
    quand il ne produit aucune ligne (téléchargement long).
    """
    proc = subprocess.Popen(commande, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    lignes = queue.Queue()

    def lire():
        for ligne in proc.stdout:
            lignes.put(ligne.rstrip("\n"))
        lignes.put(None)

    threading.Thread(target=lire, daemon=True).start()

    sortie = []
    while True:
        if annule is not None and annule():
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise ErreurCli("Interrompu.")
        try:
            ligne = lignes.get(timeout=0.3)
        except queue.Empty:
            continue
        if ligne is None:
            break
        sortie.append(ligne)
        m = _PROGRES.search(ligne)
        if m:
            if progres is not None:
                progres(float(m.group(1)))
        elif journal is not None and ligne.strip() and not _BLOC_RAPPORT.match(ligne):
            journal(ligne)
    code = proc.wait()
    if code != 0:
        fin = "\n".join(sortie[-8:])
        raise ErreurCli(f"{Path(commande[0]).name} a échoué (code {code}).\n{fin}")
    return "\n".join(sortie)


def compiler_bot(journal=None, annule=None):
    """Compile DataFetcher si son .algo n'existe pas encore, et renvoie son chemin.

    On passe par `dotnet build` : `ctrader-cli build` cherche le pack .NET 6 dans
    des dossiers fixes et échoue si seul le cache NuGet le contient.
    """
    if ALGO.is_file():
        return ALGO
    if not PROJET_BOT.is_dir():
        raise ErreurCli(f"Projet du bot introuvable : {PROJET_BOT}")
    if journal:
        journal("Compilation de DataFetcher (dotnet build)...")
    _executer(["dotnet", "build", str(PROJET_BOT)], journal, None, annule)
    if not ALGO.is_file():
        raise ErreurCli(f"Compilation terminée mais {ALGO.name} est introuvable.")
    return ALGO


def telecharger(symbole, debut, fin, reglages: Reglages, journal=None, progres=None, annule=None):
    """Télécharge les M1 de `symbole` entre `debut` et `fin` (dates incluses).

    `progres(pct)` reçoit l'avancement du backtest fictif, 0 à 100. La commande
    n'écrit ses rapports que dans un dossier temporaire, jeté ensuite.
    """
    reglages.verifier()
    algo = compiler_bot(journal, annule)
    d, f = np.datetime_as_string(debut, unit="D"), np.datetime_as_string(fin, unit="D")
    fmt = lambda iso: f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}"      # dd/MM/yyyy attendu par la CLI
    if journal:
        journal(f"Téléchargement {symbole} : {fmt(d)} -> {fmt(f)}")
    with tempfile.TemporaryDirectory(prefix="masim_dl_") as tmp:
        _executer(
            ["ctrader-cli", "backtest", str(algo), *reglages.arguments(),
             f"--symbol={symbole}", "--period=m1", f"--start={fmt(d)}", f"--end={fmt(f)}",
             "--data-mode=m1", "--balance=10000",
             f"--report={tmp}/r.html", f"--report-json={tmp}/r.json",
             "--exit-on-stop"],       # sans lui, la CLI reste ouverte une fois le backtest fini
            journal, progres, annule)


def symboles_broker(reglages: Reglages):
    """Noms des symboles proposés par le broker (`ctrader-cli symbols`), triés."""
    reglages.verifier()
    sortie = _executer(["ctrader-cli", "symbols", *reglages.arguments()])
    debut = sortie.find("[")
    if debut < 0:
        raise ErreurCli("Réponse inattendue de ctrader-cli symbols.")
    return sorted({s["Name"] for s in json.loads(sortie[debut:])})
