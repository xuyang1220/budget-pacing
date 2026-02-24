import math
import random
from dataclasses import dataclass
from src.plotting import plot
from typing import Callable, Dict, List, Tuple

# -----------------------------
# Helpers / toy distributions
# -----------------------------

def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

# -----------------------------
# Models you can swap
# -----------------------------

def sample_auction() -> Dict:
    """
    One incoming auction (impression opportunity).
    Replace with your own feature sampling / replay logs.
    """
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

# -----------------------------
# Pacing controller
# -----------------------------

@dataclass
class PacingConfig:
    # pacing multiplier bounds
    mult_min: float = 0.1
    mult_max: float = 5.0

    # update rule parameters (simple PI-like)
    kp: float = 0.5
    ki: float = 0.05

    # anti-windup clamp on integral term
    integ_min: float = -5.0
    integ_max: float = 5.0

    # smoothing on multiplier updates
    smooth: float = 0.2  # 0..1, higher = faster changes

@dataclass
class PacingState:
    multiplier: float = 1.0
    integ_err: float = 0.0

def update_pacing_multiplier(
    cfg: PacingConfig,
    st: PacingState,
    target_spend_so_far: float,
    actual_spend_so_far: float,
) -> float:
    """
    Control objective: track cumulative spend curve.
    error > 0 => behind => increase multiplier.
    """
    # normalize error to avoid scaling headaches
    denom = max(1e-6, target_spend_so_far)
    err = (target_spend_so_far - actual_spend_so_far) / denom  # + means behind

    st.integ_err = clamp(st.integ_err + err, cfg.integ_min, cfg.integ_max)

    raw = 1.0 + cfg.kp * err + cfg.ki * st.integ_err
    raw = clamp(raw, cfg.mult_min, cfg.mult_max)

    # smooth multiplicative changes (EMA in multiplier space)
    st.multiplier = (1.0 - cfg.smooth) * st.multiplier + cfg.smooth * raw
    st.multiplier = clamp(st.multiplier, cfg.mult_min, cfg.mult_max)
    return st.multiplier

# -----------------------------
# Spend curve / budget pacing plan
# -----------------------------

def linear_spend_curve(total_budget: float, t: int, T: int) -> float:
    """
    Target cumulative spend by time t (0..T).
    Swap with: intraday curve, learned curve, front-load/back-load.
    """
    frac = t / max(1, T)
    return total_budget * frac

# -----------------------------
# Simulation core
# -----------------------------

@dataclass
class SimConfig:
    seed: int = 7

    # timeline
    horizon_minutes: int = 24 * 60
    auctions_per_minute: int = 200  # average supply rate

    # money
    total_budget: float = 200.0  # total spend cap
    stop_when_budget_exhausted: bool = True

    # bid clamps
    bid_min: float = 0.0
    bid_max: float = 5.0

def run_sim(
    sim: SimConfig,
    pacing_cfg: PacingConfig,
    spend_curve: Callable[[float, int, int], float] = lambda B, t, T: linear_spend_curve(B, t, T),
) -> Dict:
    random.seed(sim.seed)

    st = PacingState(multiplier=1.0, integ_err=0.0)

    spent = 0.0
    wins = 0
    auctions = 0
    clicks = 0

    # time series for plotting later
    ts: List[Dict] = []

    T = sim.horizon_minutes

    for t in range(1, T + 1):
        target_cum = spend_curve(sim.total_budget, t, T)

        # controller updates once per minute (common in practice)
        mult = update_pacing_multiplier(pacing_cfg, st, target_cum, spent)

        minute_spend = 0.0
        minute_wins = 0
        minute_clicks = 0

        for _ in range(sim.auctions_per_minute):
            if sim.stop_when_budget_exhausted and spent >= sim.total_budget:
                break

            auction = sample_auction()
            pctr = pctr_model(auction)

            base_bid = base_bid_from_value(pctr, value_per_click())
            bid = clamp(base_bid * mult, sim.bid_min, sim.bid_max)

            auctions += 1

            # decide win
            if random.random() < win_prob(bid, auction):
                price = clearing_price(bid, auction)

                # budget cap enforcement at impression-level
                if spent + price > sim.total_budget and sim.stop_when_budget_exhausted:
                    continue

                spent += price
                minute_spend += price
                wins += 1
                minute_wins += 1

                # click realization
                if random.random() < pctr:
                    clicks += 1
                    minute_clicks += 1

        ts.append(
            {
                "t_min": t,
                "target_cum_spend": target_cum,
                "actual_cum_spend": spent,
                "minute_spend": minute_spend,
                "pacing_mult": mult,
                "minute_wins": minute_wins,
                "minute_clicks": minute_clicks,
            }
        )

        if sim.stop_when_budget_exhausted and spent >= sim.total_budget:
            # still record that we're exhausted and stop
            break

    return {
        "summary": {
            "spent": spent,
            "budget": sim.total_budget,
            "auctions": auctions,
            "wins": wins,
            "clicks": clicks,
            "win_rate": wins / max(1, auctions),
            "ctr_on_wins": clicks / max(1, wins),
            "minutes_simulated": ts[-1]["t_min"] if ts else 0,
        },
        "timeseries": ts,
    }

# -----------------------------
# Example run
# -----------------------------

if __name__ == "__main__":
    sim_cfg = SimConfig(
        seed=42,
        horizon_minutes=6 * 60,
        auctions_per_minute=300,
        total_budget=100.0,
    )
    pacing_cfg = PacingConfig(kp=0.8, ki=0.05, smooth=0.3, mult_min=0.2, mult_max=3.0)

    out = run_sim(sim_cfg, pacing_cfg)

    print("Summary:", out["summary"])
    # You can later plot out["timeseries"] using matplotlib/pandas.

    plot(out)