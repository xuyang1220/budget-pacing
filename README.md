# Ads Bidding & Calibration Simulator

This project implements a simplified but realistic simulation of an ads ranking + bidding system with:

- Budget constraint + pacing
- tCPA (target CPA) bidding
- Auction win modeling
- Conversion delay
- Online calibration
- Bayesian shrinkage
- Survival (delay) correction

The goal is to understand how real-world ads systems behave under delayed feedback, model bias, and control loops.

---

# 1. Auction & Bidding Model

Each minute:
- Generate auctions
- Predict pCTR and pCVR
- Compute bid: bid = BID_SCALE * pCTR * pCVR_calibrated * target_cpa
- Win probability modeled as: P(win) = sigmoid( log(bid) - competitiveness )
- Clearing price sampled from a lognormal distribution.

---

# 2. Budget & Pacing

We simulate:
- Total campaign budget
- Spend curve over time
- Pacing multiplier

We explored:

- Deterministic pacing multiplier
- Probabilistic throttling
- Oscillation control via smoothing
- Exponent split (`pacing_mult**0.3`) for stability

Key insight:
- Spend and CPA constraints can be **structurally infeasible**
- CPA vs cumulative spend frontier reveals supply limits

---

# 3. tCPA Control

We implemented:

- 2-sided CPA multiplier
- EMA-smoothed updates
- Guardrails to prevent instability

Observed behavior:
- If full budget requires high marginal CPA, system converges to supply frontier.
- Increasing budget or lowering target CPA may be infeasible.

---

# 4. Conversion Delay Modeling

True conversions are sampled as:
P(conv) = pCTR * pCVR_true

Where: pCVR_true = 0.8 * pCVR_pred

Conversions arrive after delay: delay ~ Exponential(mean=120 min)

This creates:
- Censoring bias
- Early observed CPA inflation
- Calibration instability

---

# 5. Calibration System

We implemented a realistic online calibration loop.

## 5.1 Cohort Alignment

We track conversions by win minute to align:

- Expected conversions
- Observed conversions

This removes numerator/denominator mismatch.

---

## 5.2 Hard Maturity (Lag L)

Only use impressions older than `L`: L = 240 minutes


Tradeoff:
- Larger L → less bias, more variance
- Smaller L → more bias, faster adaptation

---

## 5.3 Survival (Soft) Correction

Instead of hard cutoff, we compute expected arrived mass using exponential survival: F(age) = 1 - exp(-age / mu)


Maintain:

- Expected pending mass
- Expected arrived mass (soft matured)

This removes residual censoring bias.

---

## 5.4 Bayesian Shrinkage

Calibration ratio: ratio = (observed + prior_strength * prior_ratio) / (expected + prior_strength)

Where:
prior_strength = 50 
prior_ratio = 1.0

Prevents small-sample instability.

---

## 5.5 EMA Smoothing

calib_ema = (1 - alpha) * calib_ema + alpha * ratio  

With: alpha = 0.003 (per-minute updates)

Effective memory ≈ 1 / alpha ≈ 333 minutes.

---

## 5.6 Partial Application

To avoid overreaction: pCVR_calibrated = pCVR_pred * (calib_ema ** 0.5)


---

# 6. Observations

- Calibration converges near true bias (~0.8)
- Observed CPA > expected CPA due to pending conversions
- Soft survival correction reduces residual bias
- Policy feedback creates endogeneity
- Budget + CPA form a supply frontier

---

# 7. Key System Insights

This simulation demonstrates:

- Delayed feedback causes censoring bias
- Naive calibration collapses early
- Cohort alignment is critical
- Survival correction debiases delay
- Bayesian shrinkage stabilizes small segments
- EMA stabilizes dynamic updates
- Policy changes distribution (endogeneity)
- CPA targets can be structurally infeasible

---

# 8. Extensions

Possible next steps:

- Bid landscape estimation
- Reinforcement learning ranking
- Segment-specific delay distributions
- Multi-slot GSP auction

---

# Takeaway

This simulator reconstructs core mechanics of real-world tCPA bidding systems:

- Auction competition
- Budget pacing
- Delayed conversions
- Online calibration
- Control loop interactions

It provides an end-to-end experimental environment for reasoning about ads ranking and bidding dynamics at production scale.





