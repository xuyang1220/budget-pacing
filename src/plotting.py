import matplotlib.pyplot as plt
from typing import Dict

def plot(out: Dict) -> None:
    ts = out["timeseries"]

    t = [x["t_min"] for x in ts]
    target = [x["target_cum_spend"] for x in ts]
    actual = [x["actual_cum_spend"] for x in ts]
    mult = [x["pacing_mult"] for x in ts]
    minute_spend = [x["minute_spend"] for x in ts]

    # -----------------------------
    # 1. Target vs Actual Spend
    # -----------------------------
    plt.figure()
    plt.plot(t, target, label="Target Cumulative Spend")
    plt.plot(t, actual, label="Actual Cumulative Spend")
    plt.xlabel("Minute")
    plt.ylabel("Spend")
    plt.title("Budget Pacing: Target vs Actual Spend")
    plt.legend()
    plt.grid(True)
    plt.show()

    # -----------------------------
    # 2. Pacing Multiplier
    # -----------------------------
    plt.figure()
    plt.plot(t, mult)
    plt.xlabel("Minute")
    plt.ylabel("Pacing Multiplier")
    plt.title("Pacing Multiplier Over Time")
    plt.grid(True)
    plt.show()

    # -----------------------------
    # 3. Minute-Level Spend
    # -----------------------------
    plt.figure()
    plt.plot(t, minute_spend)
    plt.xlabel("Minute")
    plt.ylabel("Spend per Minute")
    plt.title("Minute-Level Spend (Oscillation View)")
    plt.grid(True)
    plt.show()