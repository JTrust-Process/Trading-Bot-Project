import os
import requests
import pandas as pd

API_KEY = os.getenv("TWELVEDATA_API_KEY")

BASE_URL = "https://api.twelvedata.com/time_series"


def get_daily_bars(symbol, outputsize=50):
    params = {
        "symbol": symbol,
        "interval": "1day",
        "outputsize": outputsize,
        "apikey": API_KEY,
    }

    r = requests.get(BASE_URL, params=params)
    data = r.json()

    if "values" not in data:
        return None

    df = pd.DataFrame(data["values"])
    df = df.astype({
        "open": float,
        "high": float,
        "low": float,
        "close": float,
    })
    df = df.sort_values("datetime")
    df.reset_index(drop=True, inplace=True)
    return df
