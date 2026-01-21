# #from dotenv import load_dotenv
# import os

# load_dotenv()
# API_KEY = os.getenv("PUBLIC_API_KEY")

# print("VS Code Python environment ready!")

# cash = 100
# symbol_price = 150
# quantity = cash / symbol_price
# print(f"Simulated buy {quantity} shares at ${symbol_price}")

import os
import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("PUBLIC_API_KEY")
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

res = requests.get("https://api.public.com/v1/accounts/me", headers=HEADERS)
print(res.json())
print("VS Code Python environment ready!")