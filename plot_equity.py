import pandas as pd
import matplotlib.pyplot as plt
from pathlib import Path

EQUITY_FILE = Path("logs/paper_equity.csv")   # created by your bot
OUTPUT_FILE = Path("logs/equity_curve.png")   # chart output

def main():
    if not EQUITY_FILE.exists():
        raise SystemExit(f"Couldn't find {EQUITY_FILE}. Run the bot in paper mode first.")

    df = pd.read_csv(EQUITY_FILE)

    # Parse + clean
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True, errors="coerce")
    df["total_value"] = pd.to_numeric(df["total_value"], errors="coerce")
    df = df.dropna(subset=["timestamp_utc", "total_value"]).sort_values("timestamp_utc")

    if df.empty:
        raise SystemExit("No valid rows found in paper_equity.csv yet.")

    # Plot
    plt.figure()
    plt.plot(df["timestamp_utc"], df["total_value"])
    plt.xlabel("Time (UTC)")
    plt.ylabel("Portfolio Value ($)")
    plt.title("Paper Trading Equity Curve")
    plt.xticks(rotation=30, ha="right")
    plt.tight_layout()

    plt.savefig(OUTPUT_FILE, dpi=200)
    plt.show()

    print(f"Saved equity curve to: {OUTPUT_FILE.resolve()}")

if __name__ == "__main__":
    main()
