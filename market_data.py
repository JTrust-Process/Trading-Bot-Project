import yfinance as yf
import pandas as pd


def get_daily_bars(symbol: str, outputsize: int = 50) -> pd.DataFrame | None:
    """
    Fetch daily OHLCV bars using yfinance.
    Completely free, no API key, no rate limits for daily data.
    Returns a DataFrame with columns: open, high, low, close, volume
    sorted oldest → newest, or None if data unavailable.
    """
    try:
        ticker = yf.Ticker(symbol)
        # Request enough history to cover outputsize bars
        df = ticker.history(period="3mo", interval="1d")

        if df is None or df.empty:
            return None

        # Normalize column names to lowercase to match existing bot expectations
        df = df.rename(columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        })

        df = df[["open", "high", "low", "close", "volume"]].copy()
        df = df.dropna(subset=["close"])

        # Sort oldest → newest and return last outputsize bars
        df = df.sort_index()
        df = df.tail(outputsize).reset_index(drop=True)

        return df if len(df) > 0 else None

    except Exception:
        return None