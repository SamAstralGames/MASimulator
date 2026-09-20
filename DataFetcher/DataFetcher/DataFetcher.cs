using cAlgo.API;

namespace cAlgo.Robots;

/// <summary>
/// DataFetcher : bot fictif qui ne trade jamais.
///
/// Son seul but est d'etre lance en BACKTEST par `ctrader-cli backtest` : pour
/// simuler, cTrader telecharge l'historique du symbole et de la periode
/// demandes et l'ecrit dans son cache local
/// (~/.config/Spotware/Cache/Spotware/BacktestingCache), que MASimulator lit
/// ensuite. Aucun ordre n'est envoye, quels que soient les droits du compte.
/// </summary>
[Robot(TimeZone = TimeZones.UTC, AccessRights = AccessRights.None)]
public class DataFetcher : Robot
{
    protected override void OnStart()
    {
        Print($"DataFetcher : {SymbolName} {TimeFrame}, premiere barre {Bars.OpenTimes[0]:yyyy-MM-dd HH:mm}");
    }

    protected override void OnStop()
    {
        Print($"DataFetcher : termine, {Bars.Count} barres jusqu'au {Bars.LastBar.OpenTime:yyyy-MM-dd HH:mm}");
    }
}
