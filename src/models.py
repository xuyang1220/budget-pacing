import math
import random
from typing import Dict

# -----------------------------
# Helpers
# -----------------------------

def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

# -----------------------------
# Toy market models (producer-side).
# Replace with real feature sampling / trained models / replayed logs.
# -----------------------------

def sample_auction() -> Dict:
    """One incoming auction (impression opportunity)."""
    # toy: "quality" drives both pCTR and price competitiveness
    q = random.gauss(0.0, 1.0)
    return {"q": q}

def pctr_model(auction: Dict) -> float:
    """Toy pCTR. Replace with your model."""
    return clamp(sigmoid(0.8 * auction["q"]), 1e-4, 0.2)

def value_per_click() -> float:
    """Toy value per click (could be CPA target, revenue, etc.)."""
    return 1.0  # arbitrary units

def base_bid_from_value(pctr: float, v_click: float) -> float:
    """
    Base bid policy without pacing.
    For CPC: bid ~ pCTR * value_per_click (or something monotone).
    For CPA: bid ~ pCVR * value_per_conv * ...
    """
    return pctr * v_click

def win_prob(bid: float, auction: Dict) -> float:
    """
    Bid landscape / win rate model: P(win | bid, context).
    Toy: more competitive when q is high (harder to win).
    Replace with: logistic on log(bid) - log(market_price) etc.
    """
    competitiveness = 0.5 * auction["q"]  # higher q => tougher
    return clamp(sigmoid(3.0 * math.log(1.0 + bid) - competitiveness), 0.0, 1.0)

def clearing_price(bid: float, auction: Dict) -> float:
    """
    If you win, what do you pay? (2nd price proxy)
    Toy: pay some fraction of bid, plus context noise.
    Replace with sampled market price conditional on context.
    """
    noise = random.uniform(0.6, 0.95)
    return bid * noise
