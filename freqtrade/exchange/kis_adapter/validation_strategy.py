"""No-signal strategy for explicit paper validation commands."""

from pandas import DataFrame

from freqtrade.strategy import IStrategy


class KisValidationStrategy(IStrategy):
    timeframe = "1m"
    startup_candle_count = 0
    stoploss = -0.1
    minimal_roi = {"0": 10.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        return dataframe
