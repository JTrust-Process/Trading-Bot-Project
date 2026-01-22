import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict


def utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PaperPortfolio:
    cash: float
    positions: Dict[str, float]  # symbol -> shares

    @staticmethod
    def load(path: str, start_cash: float) -> "PaperPortfolio":
        if not os.path.exists(path):
            return PaperPortfolio(cash=start_cash, positions={})
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return PaperPortfolio(
                cash=float(d.get("cash", start_cash)),
                positions={k: float(v) for k, v in (d.get("positions") or {}).items()}
        )
        except Exception:
            # If the file is corrupted, start fresh rather than crashing the bot
            return PaperPortfolio(cash=start_cash, positions={})


    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)

    def buy_notional(self, symbol: str, dollars: float, price: float) -> str:
        if dollars <= 0:
            return "SKIP: dollars <= 0"
        if price <= 0:
            return "SKIP: invalid price"
        if dollars > self.cash:
            dollars = self.cash  # spend remaining cash
        if dollars < 1.0:
            return "SKIP: not enough cash to buy meaningful amount"

        shares = dollars / price
        self.cash -= dollars
        self.positions[symbol] = self.positions.get(symbol, 0.0) + shares
        return f"BOUGHT: ${dollars:.2f} of {symbol} at {price:.2f} ({shares:.6f} shares)"

    def total_value(self, prices: Dict[str, float]) -> float:
        total = self.cash
        for sym, sh in self.positions.items():
            total += sh * float(prices.get(sym, 0.0))
        return total
