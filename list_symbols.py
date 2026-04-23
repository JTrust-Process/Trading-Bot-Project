import os
import requests
from typing import Any
from dotenv import load_dotenv

AUTH_URL = "https://api.public.com/userapiauthservice/personal/access-tokens"
ACCOUNT_URL = "https://api.public.com/userapigateway/trading/account"
PORTFOLIO_V2_URL_TMPL = "https://api.public.com/userapigateway/trading/{accountId}/portfolio/v2"

def get_token(secret: str, minutes: int = 15) -> str:
    r = requests.post(
        AUTH_URL,
        headers={"Content-Type": "application/json"},
        json={"secret": secret, "validityInMinutes": minutes},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["accessToken"]

def main() -> None:
    load_dotenv()
    secret = os.getenv("PUBLIC_SECRET")
    if not secret:
        raise SystemExit("Missing PUBLIC_SECRET in .env")

    token = get_token(secret)
    headers = {"Authorization": f"Bearer {token}"}

    acct = requests.get(ACCOUNT_URL, headers=headers, timeout=30)
    acct.raise_for_status()
    accounts: list[dict[str, Any]] = acct.json().get("accounts") or []
    account_id: str = str(accounts[0]["accountId"])

    port = requests.get(PORTFOLIO_V2_URL_TMPL.format(accountId=account_id), headers=headers, timeout=30)
    port.raise_for_status()
    portfolio = port.json()

    symbols: set[str] = set()
    positions: list[dict[str, Any]] = portfolio.get("positions") or []
    for p in positions:
        p_d: dict[str, Any] = p
        inst: dict[str, Any] = p_d.get("instrument") or {}
        if inst.get("type") == "EQUITY":
            sym = (inst.get("symbol") or "").upper().strip()
            qty = p_d.get("quantity") or p_d.get("qty") or p_d.get("shares")
            try:
                qty = float(qty or 0)
            except Exception:
                qty = 0.0
            if sym and qty > 0:
                symbols.add(sym)

    symbols_list: list[str] = sorted(symbols)
    print("\nYour held EQUITY symbols:")
    for s in symbols_list:
        print(" -", s)

    if symbols_list:
        print("\nPut this in your .env:")
        print("SYMBOLS=" + ",".join(symbols_list))

if __name__ == "__main__":
    main()