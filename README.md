# MASimulator

Application de bureau **PySide6 + QtWebEngine + Plotly** pour **analyser**, sur une période récente, le comportement d'un bot de croisement de moyennes mobiles sur plusieurs indices CFD.

C'est un **outil d'analyse**. Il affiche des statistiques et des graphiques. Il ne paramètre rien automatiquement et ne recommande pas de choix : l'utilisateur lit les métriques et décide de ce qu'il met dans son bot.

## Le cadrage

On **ne cherche pas un edge sur 10 ans**. On cherche une **optimisation temporaire pour des challenges prop firm** : sur les dernières semaines ou mois, quelles configurations ont le mieux passé un challenge (+10 % avant -10 % de DD, en 30 jours), sur quels indices, avec quel levier.

Conséquences sur la conception :

- La **fenêtre d'analyse est récente et réglable**, pas un historique complet.
- Les résultats **périment vite**. L'outil sert à ré-analyser régulièrement.
- La métrique centrale est **P(réussite du challenge)**, affichée avec la référence du hasard pur `DD / (cible + DD)`.
- **Le choix du risque appartient à l'utilisateur.** Il peut privilégier la stabilité (une config qui tient sur plusieurs fenêtres et indices) ou le gain rapide et risqué (la meilleure config sur la fenêtre courante, même fragile). L'outil fournit les métriques pour les deux, sans arbitrer.

## Ce que fait l'outil

1. **Charge plusieurs indices CFD** (GER40, NAS100, US500, XAUUSD, USOIL, etc.) depuis les données **cTrader**.
2. **Crible** les combinaisons de moyennes (type d'entrée, type de sortie, périodes rapide/lente, filtres, stop/trailing) sur la fenêtre choisie.
3. **Simule des challenges prop firm** partant de nombreuses dates différentes pour estimer P(réussite).
4. **Affiche** les résultats de façon interactive : comparaison entre configs, entre indices, entre fenêtres.

## Ce que ça change par rapport à `AdvancedEMA/` et `CroisementMA/`

Le principe reste celui des deux scripts de [RaptorBT](../RaptorBT/) : moteur de backtest [RaptorBT](https://pypi.org/project/raptorbt/), simulateur de challenge, criblage de moyennes. La différence est l'usage.

| | `AdvancedEMA` / `CroisementMA` | MASimulator |
|---|---|---|
| Interface | Script Python, tout est configuré en constantes en haut du fichier, sortie texte dans le terminal | App Qt, paramètres modifiés dans l'interface |
| Résultats | Rapports texte à relire | Graphiques Plotly interactifs (zoom, survol, sélection) |
| Périmètre | Un symbole par lancement | Plusieurs indices côte à côte |
| Vues | Une sortie linéaire | Plusieurs vues spécialisées (voir ci-dessous) |
| Horizon | Un an et plus | Fenêtre récente, réglable |

## Vues

- **Vue d'ensemble** : un indice par ligne, avec la meilleure config sur la fenêtre courante, sa P(réussite), l'écart au hasard et la fraîcheur des données.
- **Criblage** : matrice entrée × sortie (EMA, WMA, ZLEMA, HMA, KAMA, SSMOOTH, VIDYA) en heatmap, avec les périodes en paramètres et une métrique au choix (P(réussite), rendement, drawdown, Sharpe, nombre de trades). Un clic sur une case ouvre le détail.
- **Détail d'une config** : courbe d'equity, trades sur le graphique de prix, drawdown, raisons de sortie, statistiques de trades.
- **Challenge prop firm** : distribution des issues (réussi / DD / perte jour / temps écoulé) selon la date de départ, avec le levier en paramètre. Règles éditables (cible, DD max, perte jour, durée, DD trailing ou fixe).
- **Comparaison de fenêtres et d'indices** : la même config évaluée sur plusieurs sous-fenêtres et sur d'autres indices, pour que l'utilisateur voie lui-même si elle tient ou si elle ne vaut que sur la fenêtre courante.

## Données

Bougies lues depuis le **cache local de cTrader** (`~/.config/Spotware/Cache/Spotware/BacktestingCache`, fichiers `.zbars` M1 reconstruits en M5/M15/H1). La variable d'environnement `MASIM_CTRADER` change de racine. Le contrôle sanitaire (`data.controle` : bougies incohérentes, sauts suspects, fenêtre trop courte) remonte ses alertes dans la vue d'ensemble, parce qu'un prix aberrant suffit à fabriquer un faux trade gagnant.

L'utilisateur choisit **la date de début et la date de fin**. L'app affiche la date de la dernière bougie par symbole, en rouge quand elle date de plus d'une semaine.

### Télécharger ce qui manque

cTrader n'a pas de commande « télécharger l'historique ». Le contournement est le bot fictif [DataFetcher](DataFetcher/DataFetcher/DataFetcher.cs) : il ne trade jamais, il sert à lancer un **backtest** que `ctrader-cli` fait tourner sur le symbole et la période voulus. Pour simuler, cTrader télécharge les bougies M1 manquantes et les écrit dans son cache.

À l'analyse, si un jour ouvré de la période n'a pas de fichier M1, l'app propose de lancer ce backtest fictif (uniquement sur la plage manquante). La progression et le journal de la CLI s'affichent, et l'opération est annulable. Un symbole absent du cache s'ajoute avec **+ Ajouter un symbole**, qui liste ceux du broker (`ctrader-cli symbols`).

Le panneau « cTrader » demande le cTID et le numéro de compte, mémorisés d'une session à l'autre (préremplis par `MASIM_CTID` et `MASIM_ACCOUNT`), et le chemin d'un fichier contenant le mot de passe. Le mot de passe n'est jamais saisi dans l'app.

Prérequis : `ctrader-cli` dans le PATH et le SDK .NET (le bot est compilé au premier téléchargement avec `dotnet build`, ce qui copie aussi `DataFetcher.algo` dans `~/Documents/cAlgo/Sources/Robots`).

## Stack

- **Python ≥ 3.12**, géré avec [uv](https://docs.astral.sh/uv/)
- **[PySide6](https://doc.qt.io/qtforpython-6/)** (Qt 6) pour l'interface. Les graphiques sont des pages **Plotly** affichées dans **QtWebEngine**, donc pleinement interactifs (zoom, survol, clic sur une case du criblage). Flet a été écarté : sur Linux desktop il ne peut rendre Plotly qu'en image statique.
- **[Plotly](https://plotly.com/python/)** pour les graphiques
- **[RaptorBT](https://pypi.org/project/raptorbt/)** (moteur de backtest en Rust), épinglé en `0.13.1`
- **Polars** et **PyArrow** pour les données, **DuckDB** pour les requêtes sur les trades

## Installation et lancement

```bash
uv sync
uv run masimulator
```

## Structure

```
src/masimulator/
├── __init__.py        point d'entrée (main)
├── data.py            lecture du cache cTrader, contrôle sanitaire
├── cli.py             pilote ctrader-cli : compilation du bot, téléchargement, symboles du broker
├── moyennes.py        catalogue de moyennes, signaux de croisement
├── engine.py          backtest RaptorBT, coûts broker, criblage entrée x sortie
├── propfirm.py        règles et simulateur de challenge
└── ui/
    ├── window.py      fenêtre principale, paramètres et vues
    ├── charts.py      figures Plotly (sans dépendance à Qt)
    └── webview.py     widget QtWebEngine qui affiche une figure et renvoie les clics
DataFetcher/           bot cTrader fictif (C#), lancé en backtest pour télécharger l'historique
```

Le calcul est **porté depuis [RaptorBT](../RaptorBT/)** ([moyennes.py](../RaptorBT/moyennes.py), [simulateur_propfirm.py](../RaptorBT/simulateur_propfirm.py), [couts.py](../RaptorBT/couts.py), [donnees.py](../RaptorBT/donnees.py)), débarrassé des `print` : les fonctions renvoient des structures que l'interface affiche. Le port a été vérifié sur XAUUSD : VIDYA → KAMA 9/21 donne 196 trades et 85 % de réussite à levier x5, comme dans le rapport de BotX.

## État actuel

Les cinq vues fonctionnent. Ce qui n'est pas encore là :

- **Long seul.** Pas de short ni de retournement (le moteur `Strategy` de RaptorBT est plus lent).
- **Filtre de tendance HTF** de [tendance_htf.py](../RaptorBT/tendance_htf.py) : non porté.
- **Comparaison de fenêtres.** Elle remonte dans le temps depuis la date de fin, mais ne télécharge pas l'historique qui manquerait avant la date de début : les fenêtres sans données s'affichent vides.
- **Courbes décimées.** Prix, moyennes et equity sont réduits à ~5 000 points (min/max par tranche, les pics sont conservés). Zoomer n'ajoute pas de détail.
- **Coûts par défaut** approximatifs (XAUUSD, EURUSD connus, 2 bps du prix sinon) : à remplacer par les tarifs du compte.

## Points à trancher

- **Métriques de stabilité à afficher** : lesquelles exposer pour que l'utilisateur juge la robustesse (sous-fenêtres, autres indices, écart au hasard).
- **Périmètre de criblage** : nombre de combinaisons par défaut, et temps de calcul acceptable dans une interface interactive.
