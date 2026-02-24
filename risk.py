import os
import json
from datetime import datetime, timedelta

STATE_FILE = "state.json"

BASE_POSITION_SIZE = float(os.getenv("BASE_POSITION_SIZE", 0.1))
MAX_POSITION_SCALE = float(os.getenv("MAX_POSITION_SCALE", 2.0))
TREND_STRONG_THRESHOLD = float(os.getenv("TREND_STRONG_THRESHOLD", 0.02))
LOSS_COOLDOWN_DAYS = int(os.getenv("LOSS_COOLDOWN_DAYS", 3))


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def in_cooldown(symbol):
    state = load_state()
    if symbol not in state:
        return False

    halt_until = datetime.fromisoformat(state[symbol])
    return datetime.utcnow() < halt_until


def trigger_cooldown(symbol):
    state = load_state()
    halt_until = datetime.utcnow() + timedelta(days=LOSS_COOLDOWN_DAYS)
    state[symbol] = halt_until.isoformat()
    save_state(state)


def position_scale(trend_strength):
    scale = 1 + (trend_strength / TREND_STRONG_THRESHOLD)
    return min(scale, MAX_POSITION_SCALE)


def dynamic_take_profit(base_tp, trend_strength):
    return base_tp * position_scale(trend_strength)
