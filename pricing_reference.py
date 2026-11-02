"""
Stox - reference implementation for E0 (Pricing Core) and E2 (Probability Engine).

VERSION 2.0.0 - multi-class. See CHANGELOG at the bottom of this docstring.

This file is the ARBITER. Where the TypeScript implementation and this file disagree,
this file is right until someone proves otherwise in a PR that updates both.

Conventions (fixed; do not change without updating every consumer):
  - Time T is in YEARS, computed as calendar_days / 365.0
    v2: T runs to the SETTLEMENT instant, not to the last trading day. See year_fraction().
  - Volatility sigma is annualised, expressed as a decimal (0.25 == 25%)
  - Rate r and dividend yield q are annualised continuous decimals
  - Vega is returned PER 1.00 OF VOL (multiply by 0.01 for per-vol-point display)
  - Theta is returned PER YEAR (multiply by 1/365 for per-calendar-day display)
  - Rho is returned PER 1.00 OF RATE (multiply by 0.01 for per-percentage-point display)
  - Greeks are per one contract-unit of underlying, NOT per 100-share contract.
    The contract multiplier is applied at the E1 layer, never here.

Scaling is the single commonest source of divergence between two implementations.
Every fixture in ../fixtures asserts the raw convention above.

-----------------------------------------------------------------------------
v2.0.0 CHANGELOG - what changed and why (D1.1-A section 15 amendment)
-----------------------------------------------------------------------------
v1 assumed one instrument class: a physically-settled American option on a spot
equity that pays discrete cash dividends. V1 product scope is broader - broad-market
index options plus VIX - and futures are an open fork (IM-16). Four v1 assumptions
are false outside the equity class:

  1. THE FORWARD IS DERIVED FROM SPOT. v1 computes the forward implicitly as
     S*exp((r-q)T) everywhere. For a VIX option the forward is the matching-expiry
     VIX FUTURE, an observed price. Spot VIX is not holdable or borrowable, so no
     cost-of-carry relationship links it to the option's forward. Pricing VIX options
     off spot VIX produces wrong values in every Greek, every IV, every touch and POP
     figure - and the error is PLAUSIBLE, which is the dangerous kind.
     -> v2 adds an explicit forward-parameterised core (black76_*) and a routing
        function that REFUSES to price when the forward reference is not supplied.

  2. CARRY EQUALS r IN THE BINOMIAL. v1's CRR sets p = (exp(r*dt)-d)/(u-d), which
     hardcodes carry b = r: correct for a non-dividend-paying stock only. An American
     index option (at least one long-standing US index product is American-style) needs
     b = r - q. An American option on a future needs b = 0.
     -> v2 generalises CRR on a carry rate b. Default b = r reproduces every v1
        fixture value bit-identically.

  3. MULTIPLIER IS 100 AND THE DELIVERABLE IS 100 SHARES. False for adjusted
     contracts and for every futures product.
     -> v2 makes multiplier explicit at E1 and REFUSES non-standard deliverables
        rather than pricing them wrongly.

  4. A SHORT PUT IS CASH-SECURED AT K*multiplier. False for options on futures,
     which are SPAN-margined; there is no collateral figure to divide by.
     -> v2 raises CollateralUndefined rather than returning a fabricated denominator.

Nothing in v1's equity path changed. Every v1 fixture value is reproduced exactly;
the regression is asserted in the fixture generator.
"""

from datetime import datetime, timezone

import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq

ENGINE_VERSION = "2.0.0"
SQRT_EPS = 1e-12
SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0


# ---------------------------------------------------------------------------
# v2  Typed refusals
#
# These are not error handling. They are the instrument-class posture expressed in
# the type system, exactly as AnnualizationRefused is the compliance posture. Each
# maps to an EngineError kind in SPEC.md section 4 and each must be RENDERED by the
# caller, never swallowed. A refusal that a UI hides teaches the reader nothing.
# ---------------------------------------------------------------------------

class ForwardReferenceMissing(Exception):
    """The contract's forward_reference_instrument was not supplied, or was supplied
    as a spot level for a product whose forward is a futures contract. Refusing is
    the entire point: the alternative is a plausible wrong number."""


class ModelRoutingError(Exception):
    """exercise_style / settlement_method / forward basis were absent or mutually
    inconsistent. The engine never infers these from an asset-class heuristic."""


class NonStandardDeliverable(Exception):
    """Adjusted contract: the deliverable is not `multiplier` shares of the
    underlying. Wave 1 refuses rather than pricing against the wrong deliverable."""


class CollateralUndefined(Exception):
    """Return-on-collateral requested for a product with no collateral concept
    (options on futures are SPAN-margined). Caller must supply an explicit margin
    basis and label the figure as return-on-margin, which is a different number."""


class AnnualizationRefused(Exception):
    """Typed refusal. Callers must render the reason, never swallow it and never
    substitute a null. See brief document section 0.3."""


# ---------------------------------------------------------------------------
# v2  Time to expiry - to SETTLEMENT, not to last trade
#
# v1's calendar_days/365 is right, but the endpoint was unstated and the naive
# endpoint is wrong for the highest-volume expiration of the month. An AM-settled
# third-Friday index series STOPS TRADING Thursday afternoon and SETTLES from
# Friday's opening prices. On Thursday's close its true remaining life is roughly
# 0.7 days, not zero. A T that ends at the last trade prices it as expired and
# reports a flat Greek surface on a contract that still carries a full overnight
# gap of risk.
#
# Both timestamps come from the instrument master (D1.1-A section 6: settlement_time
# and last_trading_day are read from source, never derived from a calendar rule).
# ---------------------------------------------------------------------------

def year_fraction(now_utc, settlement_utc):
    """Years between two aware datetimes on the calendar/365 convention, to the second.

    Use the SETTLEMENT instant as the endpoint:
      PM-settled  -> 16:00 ET on expiration day
      AM-settled  -> 09:30 ET on expiration day (the SET is struck from the opening)
      VIX         -> 09:30 ET on the Wednesday expiration
    """
    if settlement_utc <= now_utc:
        return 0.0
    return (settlement_utc - now_utc).total_seconds() / SECONDS_PER_YEAR


def dte_calendar(now_utc, settlement_utc):
    """Displayed DTE. Reported alongside T so the reader can see the day count that
    produced the number - required wherever a return over a period is shown."""
    return year_fraction(now_utc, settlement_utc) * 365.0


# ---------------------------------------------------------------------------
# E0.1  European (Black-Scholes-Merton with continuous dividend yield)
#
# UNCHANGED FROM v1. Bit-identical arithmetic. This is the spot-parameterised
# branch and it is correct for equities, ETFs, and broad-market index options
# where the forward is spot carried at (r - q). It is NOT correct for VIX or for
# options on futures - route those to black76_* below.
# ---------------------------------------------------------------------------

def _d1_d2(S, K, T, r, q, sigma):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        raise ValueError("degenerate input: use the intrinsic-value path")
    vt = sigma * np.sqrt(T)
    d1 = (np.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / vt
    return d1, d1 - vt


def bsm_price(S, K, T, r, q, sigma, right):
    """right in {'C','P'}. Returns theoretical value per unit of underlying."""
    if T <= 0:
        return max(0.0, S - K) if right == "C" else max(0.0, K - S)
    d1, d2 = _d1_d2(S, K, T, r, q, sigma)
    if right == "C":
        return S * np.exp(-q * T) * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    return K * np.exp(-r * T) * norm.cdf(-d2) - S * np.exp(-q * T) * norm.cdf(-d1)


def bsm_greeks(S, K, T, r, q, sigma, right):
    """All first-order Greeks plus gamma. Raw conventions per module docstring."""
    d1, d2 = _d1_d2(S, K, T, r, q, sigma)
    pdf = norm.pdf(d1)
    disc_q, disc_r = np.exp(-q * T), np.exp(-r * T)
    sqrtT = np.sqrt(T)

    gamma = disc_q * pdf / (S * sigma * sqrtT)
    vega = S * disc_q * pdf * sqrtT                      # per 1.00 vol
    common_theta = -(S * pdf * sigma * disc_q) / (2 * sqrtT)

    if right == "C":
        delta = disc_q * norm.cdf(d1)
        theta = common_theta - r * K * disc_r * norm.cdf(d2) + q * S * disc_q * norm.cdf(d1)
        rho = K * T * disc_r * norm.cdf(d2)              # per 1.00 rate
    else:
        delta = -disc_q * norm.cdf(-d1)
        theta = common_theta + r * K * disc_r * norm.cdf(-d2) - q * S * disc_q * norm.cdf(-d1)
        rho = -K * T * disc_r * norm.cdf(-d2)

    return {"delta": delta, "gamma": gamma, "vega": vega, "theta": theta, "rho": rho,
            "d1": d1, "d2": d2}


def implied_vol(price, S, K, T, r, q, right, lo=1e-6, hi=5.0, tol=1e-8):
    """
    Brent on [lo, hi]. Raises if the price sits outside no-arbitrage bounds --
    callers must surface a typed error, never a silent NaN or a clamped 0.
    """
    intrinsic = (max(0.0, S * np.exp(-q * T) - K * np.exp(-r * T)) if right == "C"
                 else max(0.0, K * np.exp(-r * T) - S * np.exp(-q * T)))
    upper = S * np.exp(-q * T) if right == "C" else K * np.exp(-r * T)
    if price < intrinsic - 1e-10 or price > upper + 1e-10:
        raise ValueError(f"price {price} outside no-arbitrage bounds [{intrinsic}, {upper}]")
    f = lambda s: bsm_price(S, K, T, r, q, s, right) - price
    if f(lo) > 0 or f(hi) < 0:
        raise ValueError("no implied vol in bracket [1e-6, 5.0]")
    return brentq(f, lo, hi, xtol=tol)


# ---------------------------------------------------------------------------
# E0.1F  NEW - European, forward-parameterised (Black-76)
#
# The same mathematics with the forward as the primary input rather than as a
# quantity derived from spot. This is the branch for:
#
#   - VIX options            forward = the matching-expiry VIX FUTURE price
#   - options on futures     forward = the future's own price      (IM-16 fork)
#   - any index option where we prefer the market-implied forward (from put-call
#     parity on the liquid ATM strike) to an estimated dividend yield
#
#   C = e^(-rT) [ F N(d1) - K N(d2) ]
#   P = e^(-rT) [ K N(-d2) - F N(-d1) ]
#   d1 = [ln(F/K) + sigma^2 T / 2] / (sigma sqrt(T)),   d2 = d1 - sigma sqrt(T)
#
# Setting F = S*exp((r-q)T) reproduces bsm_price to floating-point noise. That
# equivalence is asserted in the fixtures - and so is the ONE place the two
# parameterisations legitimately disagree, which is theta. See black76_greeks.
# ---------------------------------------------------------------------------

FORWARD_BASIS_SPOT_CARRY = "SPOT_CARRY"              # forward = S*exp((r-q)T)
FORWARD_BASIS_OBSERVED_FUTURE = "OBSERVED_FUTURE"    # forward = a quoted futures price


def forward_from_spot(S, T, r, q):
    """Cost-of-carry forward. Valid ONLY where the underlying is holdable: equities,
    ETFs, and broad-market index baskets. NOT valid for VIX - spot VIX cannot be
    held, so this relationship does not exist for it."""
    return S * np.exp((r - q) * T)


def _d1_d2_fwd(F, K, T, sigma):
    if T <= 0 or sigma <= 0 or F <= 0 or K <= 0:
        raise ValueError("degenerate input: use the intrinsic-value path")
    vt = sigma * np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sigma * sigma * T) / vt
    return d1, d1 - vt


def black76_price(F, K, T, r, sigma, right):
    """Premium-paid-upfront convention: the payoff is discounted at r.

    US options on futures (CME equity-index, energy, rates) are premium-paid-upfront,
    so this is correct for them. Some non-US venues margin options futures-style,
    where the premium is not paid at inception and the discount factor drops out.
    If OLD ever lists such a product, that is a THIRD branch, not a parameter tweak.
    """
    if T <= 0:
        return max(0.0, F - K) if right == "C" else max(0.0, K - F)
    d1, d2 = _d1_d2_fwd(F, K, T, sigma)
    disc = np.exp(-r * T)
    if right == "C":
        return float(disc * (F * norm.cdf(d1) - K * norm.cdf(d2)))
    return float(disc * (K * norm.cdf(-d2) - F * norm.cdf(-d1)))


def black76_greeks(F, K, T, r, sigma, right):
    """Greeks with respect to the FORWARD. Raw conventions per module docstring.

    Three things that are not obvious and that will bite the TypeScript port:

    DELTA IS DISCOUNTED. d(premium)/dF = e^(-rT) N(d1). Several retail platforms
    display the undiscounted N(d1) instead, because that is the futures-contract
    hedge ratio when the option is margined futures-style. Both are defensible;
    they differ by e^(-rT), which is ~0.4% at one year. We return the discounted
    derivative as `delta` and the undiscounted figure as `delta_undiscounted` so
    the display layer chooses explicitly. LOGGED AS EN-1.

    RHO IS NEGATIVE FOR BOTH RIGHTS. rho = -T * premium, because the forward is
    held fixed and the only effect of a rate change is on the discount factor. An
    equity call has POSITIVE rho; a futures call has negative rho. The rho page
    must not generalise from the equity case.

    THETA HOLDS F FIXED, NOT S. This is the real trap. Priced through either
    parameterisation an index option has the same value, the same delta (after the
    chain rule), the same gamma and the same vega - but a theta differing by
    (r - q) * S * e^(-qT) * N(d1), because BSM theta lets the forward drift as
    calendar time passes and Black-76 theta does not. Neither is wrong; they answer
    different questions. Pick per product and record which in provenance.
    """
    d1, d2 = _d1_d2_fwd(F, K, T, sigma)
    pdf = norm.pdf(d1)
    disc = np.exp(-r * T)
    sqrtT = np.sqrt(T)

    gamma = disc * pdf / (F * sigma * sqrtT)
    vega = F * disc * pdf * sqrtT                        # per 1.00 vol
    carry_theta = -(F * disc * pdf * sigma) / (2 * sqrtT)

    if right == "C":
        delta = disc * norm.cdf(d1)
        delta_undiscounted = float(norm.cdf(d1))
        price = disc * (F * norm.cdf(d1) - K * norm.cdf(d2))
    else:
        delta = -disc * norm.cdf(-d1)
        delta_undiscounted = float(-norm.cdf(-d1))
        price = disc * (K * norm.cdf(-d2) - F * norm.cdf(-d1))

    theta = carry_theta + r * price                      # per year
    rho = -T * price                                     # per 1.00 rate

    return {"delta": float(delta), "delta_undiscounted": delta_undiscounted,
            "gamma": float(gamma), "vega": float(vega), "theta": float(theta),
            "rho": float(rho), "d1": float(d1), "d2": float(d2)}


def implied_vol_black76(price, F, K, T, r, right, lo=1e-6, hi=5.0, tol=1e-8):
    """No-arbitrage bounds on the forward parameterisation:
         call: [ e^(-rT)(F-K)+ , e^(-rT) F ]
         put:  [ e^(-rT)(K-F)+ , e^(-rT) K ]

    The upper bracket is 5.0 here as in the spot version. Note that VIX option
    implied vols routinely print above 1.0 and can exceed 2.0 in a spike; 5.0 holds,
    and nobody should narrow it to a 'sensible' equity range."""
    disc = np.exp(-r * T)
    intrinsic = disc * max(0.0, F - K) if right == "C" else disc * max(0.0, K - F)
    upper = disc * F if right == "C" else disc * K
    if price < intrinsic - 1e-10 or price > upper + 1e-10:
        raise ValueError(f"price {price} outside no-arbitrage bounds [{intrinsic}, {upper}]")
    f = lambda s: black76_price(F, K, T, r, s, right) - price
    if f(lo) > 0 or f(hi) < 0:
        raise ValueError("no implied vol in bracket [1e-6, 5.0]")
    return brentq(f, lo, hi, xtol=tol)


# ---------------------------------------------------------------------------
# E0.2  American - CRR binomial
#
# v2 GENERALISATION: the risk-neutral probability now uses a CARRY RATE b, with the
# discount rate r kept separate.
#
#     p = (e^(b*dt) - d) / (u - d)        discount = e^(-r*dt)
#
#     b = r        non-dividend-paying stock              <- v1 behaviour, the default
#     b = r - q    continuous yield: index, ETF proxy     <- American index options
#     b = 0        option on a future                     <- IM-16 fork
#
# With b = r the arithmetic is character-for-character what v1 computed, so every v1
# fixture reproduces exactly. That is asserted in the generator, not assumed.
#
# WHY b = 0 MATTERS AND IS NOT COSMETIC: on a futures option the early-exercise
# boundary is live for BOTH calls and puts even with no dividends anywhere, because
# a deep-ITM futures option is worth undiscounted intrinsic on exercise and only
# discounted intrinsic if held. Pricing an American ES call with b = r suppresses
# that phenomenon entirely and throws nothing - it quietly returns the European value.
# ---------------------------------------------------------------------------

def crr_american(S, K, T, r, sigma, right, steps=1000, dividends=None, b=None, q=None):
    """
    Cox-Ross-Rubinstein tree, escrowed-dividend model.
    `dividends` is a list of (time_in_years, cash_amount) with time < T.
    `q` continuous yield, or `b` carry rate directly. Supply at most one of
    {dividends, q}: mixing a discrete schedule with a continuous yield double-counts
    the same cash flow, so it raises rather than silently compounding.

    Chosen over Bjerksund-Stensland deliberately: single-name equity options carry
    DISCRETE dividends, which BS-2002 handles only via a continuous-yield fudge.
    1000 steps converges to well under a cent on ordinary inputs and is trivially
    auditable, which matters more here than speed -- these numbers appear on a
    public page and must be defensible on request (compliance block C3).
    """
    dividends = dividends or []
    if dividends and (q not in (None, 0.0) or b is not None):
        raise ModelRoutingError(
            "discrete dividends and a continuous carry adjustment both supplied: "
            "pick one. Mixing them double-counts the dividend stream.")
    if b is None:
        b = r if q is None else r - q
    if steps < 50:
        raise ValueError("steps < 50: refuse rather than return an unconverged value")

    pv_divs = sum(d * np.exp(-r * t) for t, d in dividends if 0 < t < T)
    S_adj = S - pv_divs
    if S_adj <= 0:
        raise ValueError("PV of dividends exceeds spot")

    dt = T / steps
    u = np.exp(sigma * np.sqrt(dt))
    d = 1.0 / u
    p = (np.exp(b * dt) - d) / (u - d)
    if not (0.0 < p < 1.0):
        raise ValueError("risk-neutral probability outside (0,1): reduce dt or check inputs")
    disc = np.exp(-r * dt)

    j = np.arange(steps + 1)
    prices = S_adj * (u ** (steps - j)) * (d ** j)
    values = np.maximum(0.0, prices - K) if right == "C" else np.maximum(0.0, K - prices)

    for i in range(steps - 1, -1, -1):
        j = np.arange(i + 1)
        prices = S_adj * (u ** (i - j)) * (d ** j)
        values = disc * (p * values[:-1] + (1 - p) * values[1:])
        t_i = i * dt
        live = prices + sum(dv * np.exp(-r * (td - t_i)) for td, dv in dividends if td > t_i)
        exer = np.maximum(0.0, live - K) if right == "C" else np.maximum(0.0, K - live)
        values = np.maximum(values, exer)
    return float(values[0])


def crr_american_futures(F, K, T, r, sigma, right, steps=1000):
    """American option on a futures contract: carry is zero, discounting is at r."""
    return crr_american(F, K, T, r, sigma, right, steps=steps, b=0.0)


# ---------------------------------------------------------------------------
# v2  MODEL ROUTING - a precondition, not a convenience
#
# D1.1-A section 6: exercise_style, settlement_method and the forward reference are
# per-contract fields read from the instrument master. No code path may reason
# "index therefore European" or "index therefore price off the index level". This
# function is the only sanctioned entry point for a caller holding a contract record;
# it refuses on absence rather than defaulting.
# ---------------------------------------------------------------------------

def select_pricing_model(contract):
    """
    contract: dict with at minimum
        exercise_style        'AMERICAN' | 'EUROPEAN'
        settlement_method     'PHYSICAL' | 'CASH'
        forward_basis         FORWARD_BASIS_SPOT_CARRY | FORWARD_BASIS_OBSERVED_FUTURE
        forward_reference_instrument   identifier; required when basis is OBSERVED_FUTURE
        dividend_mode         'DISCRETE' | 'CONTINUOUS_YIELD' | 'NONE'  (spot-carry branch)

    Returns (model_id, notes). Raises ModelRoutingError / ForwardReferenceMissing.
    """
    style = contract.get("exercise_style")
    settle = contract.get("settlement_method")
    basis = contract.get("forward_basis")
    if style not in ("AMERICAN", "EUROPEAN"):
        raise ModelRoutingError("exercise_style absent or invalid: never inferred from asset class")
    if settle not in ("PHYSICAL", "CASH"):
        raise ModelRoutingError("settlement_method absent or invalid")
    if basis not in (FORWARD_BASIS_SPOT_CARRY, FORWARD_BASIS_OBSERVED_FUTURE):
        raise ModelRoutingError("forward_basis absent: the engine does not infer the forward")

    if basis == FORWARD_BASIS_OBSERVED_FUTURE:
        if not contract.get("forward_reference_instrument"):
            raise ForwardReferenceMissing(
                "forward_basis is OBSERVED_FUTURE but no forward_reference_instrument was "
                "supplied. For a VIX option this is the matching-expiry VIX future. Refusing "
                "to substitute the spot index level: it would produce a plausible wrong price.")
        model = "B76-forward" if style == "EUROPEAN" else "CRR-1000-b0"
    else:
        dmode = contract.get("dividend_mode")
        if dmode not in ("DISCRETE", "CONTINUOUS_YIELD", "NONE"):
            raise ModelRoutingError("dividend_mode absent: required on the spot-carry branch")
        if style == "EUROPEAN":
            model = "BSM-continuous-q"
        else:
            model = ("CRR-1000-discrete-div" if dmode == "DISCRETE"
                     else "CRR-1000-continuous-q")

    notes = []
    if settle == "CASH":
        notes.append("cash-settled: no share deliverable, no early-assignment surface, "
                     "no covered-position logic")
    if style == "EUROPEAN":
        notes.append("European: suppress ex-dividend early-assignment warnings")
    if contract.get("underlying_is_mean_reverting"):
        notes.append("MODEL CAVEAT: underlying is mean-reverting (VIX family). GBM-based "
                     "touch, terminal and POP figures inherit a known distributional "
                     "mis-specification and must be labelled as model output under a "
                     "stated assumption, never as a forecast.")
    if contract.get("settlement_time") == "AM":
        notes.append("AM settlement: T runs to the settlement quotation on expiration "
                     "morning, and trading stops the prior session")
    return model, notes


def assert_standard_deliverable(contract):
    """Adjusted contracts deliver something other than `multiplier` shares of the
    underlying. Wave 1 refuses them. Publishing a plausible default here is the exact
    failure mode D1.1-A section 5 quarantines against."""
    if contract.get("adjusted") or contract.get("deliverable_is_nonstandard"):
        raise NonStandardDeliverable(
            "adjusted contract: deliverable is not the standard multiplier of shares. "
            "Pricing, Greeks, covered-position checks and expiry P&L are all wrong "
            "against the standard assumption. Not supported in Wave 1.")
    return True


# ---------------------------------------------------------------------------
# E2.1  Terminal probabilities (risk-neutral)
# ---------------------------------------------------------------------------

def prob_itm(S, K, T, r, q, sigma, right):
    """Risk-neutral P(finish ITM). Call -> N(d2); Put -> N(-d2)."""
    _, d2 = _d1_d2(S, K, T, r, q, sigma)
    return float(norm.cdf(d2)) if right == "C" else float(norm.cdf(-d2))


def prob_S_above(S, K, T, r, q, sigma):
    _, d2 = _d1_d2(S, K, T, r, q, sigma)
    return float(norm.cdf(d2))


def prob_itm_fwd(F, K, T, sigma, right):
    """v2. Forward parameterisation. A futures price is a martingale under the
    pricing measure, so the log drift is -sigma^2/2 and NO rate appears. Passing an
    interest rate in here would be a modelling error, which is why there is no r
    argument to pass it through."""
    _, d2 = _d1_d2_fwd(F, K, T, sigma)
    return float(norm.cdf(d2)) if right == "C" else float(norm.cdf(-d2))


def prob_fwd_above(F, K, T, sigma):
    _, d2 = _d1_d2_fwd(F, K, T, sigma)
    return float(norm.cdf(d2))


def expected_move(S, T, sigma, sd=1.0, exact=False):
    """
    Linear convention (industry standard): S * sigma * sqrt(T) * sd.
    `exact=True` returns the lognormal quantile band, which diverges materially
    once sigma*sqrt(T) exceeds roughly 0.35. The PAGE shows the linear figure and
    NAMES the approximation; the engine can produce both so the divergence is
    demonstrable rather than asserted.
    """
    if not exact:
        m = S * sigma * np.sqrt(T) * sd
        return {"lower": S - m, "upper": S + m, "move": m}
    vt = sigma * np.sqrt(T)
    return {"lower": S * np.exp(-0.5 * vt * vt - sd * vt),
            "upper": S * np.exp(-0.5 * vt * vt + sd * vt),
            "move": None}


def expected_move_fwd(F, T, sigma, sd=1.0, exact=False):
    """v2. Same shapes, centred on the FORWARD. For a VIX option that is the
    matching-expiry future, not spot VIX - and with the term structure in contango
    the two centres can sit further apart than the move itself at short tenors,
    which is why a spot-centred VIX expected move is not slightly wrong but
    structurally wrong."""
    return expected_move(F, T, sigma, sd=sd, exact=exact)


def straddle_implied_move(straddle_mid, S):
    """
    implied_move = ATM straddle mid / S. Returned as a fraction.

    NOTE FOR IMPLEMENTERS: do NOT apply the folk 0.85 correction factor. It is an
    undocumented heuristic with no derivation, it is repeated across the web, and
    S2 exists partly to measure whether it is worth anything. Baking it into the
    engine would make the study unable to test it.
    """
    return straddle_mid / S


# ---------------------------------------------------------------------------
# E2.2  First-passage (probability of touch)
# ---------------------------------------------------------------------------

def prob_touch(S, B, T, r, q, sigma):
    """
    P(underlying trades at or through barrier B at any time before T) under GBM.

    X_t = ln(S_t/S_0) = mu*t + sigma*W_t,  mu = r - q - sigma^2/2,  b = ln(B/S_0)

      B > S:  P = N((mu*T - b)/(sigma*sqrt(T))) + exp(2*mu*b/sigma^2) * N((-b - mu*T)/(sigma*sqrt(T)))
      B < S:  P = N((b - mu*T)/(sigma*sqrt(T))) + exp(2*mu*b/sigma^2) * N((b + mu*T)/(sigma*sqrt(T)))

    MANDATORY TEST: cross-check against Monte Carlo with >=200k daily-stepped paths.
    Continuous-monitoring closed form exceeds discretely-monitored MC slightly; the
    fixtures record both and the tolerance band between them.
    """
    if S <= 0 or B <= 0 or T <= 0 or sigma <= 0:
        raise ValueError("invalid input")
    mu = r - q - 0.5 * sigma * sigma
    b = np.log(B / S)
    vt = sigma * np.sqrt(T)
    if abs(b) < SQRT_EPS:
        return 1.0
    if b > 0:
        p = norm.cdf((mu * T - b) / vt) + np.exp(2 * mu * b / (sigma ** 2)) * norm.cdf((-b - mu * T) / vt)
    else:
        p = norm.cdf((b - mu * T) / vt) + np.exp(2 * mu * b / (sigma ** 2)) * norm.cdf((b + mu * T) / vt)
    return float(min(max(p, 0.0), 1.0))


def prob_touch_fwd(F, B, T, sigma):
    """v2. Touch on a forward/futures underlying: zero drift under the pricing
    measure, so mu = -sigma^2/2. Equivalent to prob_touch with r = q = 0. The
    separate name exists so that no caller passes a financing rate into a futures
    touch calculation and gets a quietly drifted barrier."""
    return prob_touch(F, B, T, 0.0, 0.0, sigma)


def prob_touch_mc(S, B, T, r, q, sigma, n_paths=200_000, steps=None, seed=7):
    """Discretely-monitored (daily) MC reference. Seeded: reproducibility is a
    compliance requirement, not a nicety."""
    steps = steps or max(1, int(round(T * 365)))
    rng = np.random.default_rng(seed)
    dt = T / steps
    mu = r - q - 0.5 * sigma * sigma
    inc = mu * dt + sigma * np.sqrt(dt) * rng.standard_normal((n_paths, steps))
    logpath = np.cumsum(inc, axis=1)
    b = np.log(B / S)
    hit = (logpath.max(axis=1) >= b) if b > 0 else (logpath.min(axis=1) <= b)
    return float(hit.mean())


# ---------------------------------------------------------------------------
# E1/E2 shared: piecewise-linear expiry payoff, breakevens, POP
#
# v2: the multiplier was already a parameter and stays one. What is new is that
# callers must pass the CONTRACT's multiplier from the instrument master rather than
# accepting the default, and that a stock leg is refused on a cash-settled product.
# ---------------------------------------------------------------------------

def expiry_payoff(legs, stock_qty, stock_basis, S_T, multiplier=100):
    """
    legs: [{'right':'C'|'P', 'side':'long'|'short', 'strike':K, 'qty':n, 'premium':p}]
    Premium is per-unit (per share), positive as quoted. Returns total P&L in dollars.
    """
    pnl = stock_qty * (S_T - stock_basis)
    for lg in legs:
        sign = 1.0 if lg["side"] == "long" else -1.0
        intr = max(0.0, S_T - lg["strike"]) if lg["right"] == "C" else max(0.0, lg["strike"] - S_T)
        pnl += sign * lg["qty"] * multiplier * (intr - lg["premium"])
    return pnl


def assert_position_legal_for_class(legs, stock_qty, settlement_method):
    """v2. A cash-settled product has no share deliverable, so there is no covered
    call, no wheel, no assignment into stock, and no stock leg. The refusal lives
    here rather than in the UI for the reason in SPEC.md section 1: a guardrail
    implemented in a component is not implemented."""
    if settlement_method == "CASH" and stock_qty != 0:
        raise ModelRoutingError(
            "stock leg supplied against a cash-settled contract. Cash-settled index "
            "options never deliver shares, so a covered or wheel structure against "
            "them does not exist. This is a position-construction error, not a "
            "display concern.")
    return True


def _breakpoints(legs, S_ref):
    ks = sorted({lg["strike"] for lg in legs})
    return [0.0] + ks + [max(ks + [S_ref]) * 3.0]


def payoff_profile(legs, stock_qty=0, stock_basis=0.0, S_ref=100.0, multiplier=100, costs=0.0):
    """
    Exact max/min/breakevens for a piecewise-linear expiry payoff.
    No numerical search: the function is linear between strikes, so evaluating at
    every strike plus the asymptotes is exhaustive. Unbounded ends are detected by
    the sign of the slope beyond the outermost strike.
    """
    pts = _breakpoints(legs, S_ref)
    vals = [expiry_payoff(legs, stock_qty, stock_basis, s, multiplier) - costs for s in pts]

    hi_slope = (vals[-1] - vals[-2]) / (pts[-1] - pts[-2])
    lo_slope = (vals[1] - vals[0]) / (pts[1] - pts[0])
    max_profit = float("inf") if hi_slope > 1e-9 else max(vals)
    max_loss = float("-inf") if hi_slope < -1e-9 else min(vals)
    if lo_slope < -1e-9:
        max_profit = float("inf")
    if lo_slope > 1e-9:
        max_loss = float("-inf")

    bes = []
    for i in range(len(pts) - 1):
        y0, y1 = vals[i], vals[i + 1]
        if y0 == 0.0:
            bes.append(pts[i])
        elif y0 * y1 < 0:
            bes.append(pts[i] + (pts[i + 1] - pts[i]) * (-y0) / (y1 - y0))
    return {"max_profit": max_profit, "max_loss": max_loss,
            "breakevens": sorted(set(round(b, 6) for b in bes))}


def prob_of_profit(legs, S, T, r, q, sigma, stock_qty=0, stock_basis=0.0,
                   multiplier=100, costs=0.0):
    """
    EXACT for expiry-defined profit: the profit region is a union of intervals in
    S_T, so POP = sum over intervals of [P(S_T > lo) - P(S_T > hi)]. No Monte Carlo
    is needed and none should be used -- MC here would introduce sampling noise into
    a number displayed on a public page.

    MC is required only for path-dependent questions (early management, touch-based
    exits), which belong to E5, not here.
    """
    prof = payoff_profile(legs, stock_qty, stock_basis, S, multiplier, costs)
    pts = sorted(set([0.0] + [lg["strike"] for lg in legs] + prof["breakevens"] + [S * 10]))
    total = 0.0
    for i in range(len(pts) - 1):
        lo, hi = pts[i], pts[i + 1]
        mid = 0.5 * (lo + hi)
        if expiry_payoff(legs, stock_qty, stock_basis, mid, multiplier) - costs > 0:
            p_lo = 1.0 if lo <= 0 else prob_S_above(S, lo, T, r, q, sigma)
            p_hi = prob_S_above(S, hi, T, r, q, sigma)
            total += p_lo - p_hi
    return float(min(max(total, 0.0), 1.0))


def prob_of_profit_fwd(legs, F, T, sigma, multiplier=100, costs=0.0):
    """v2. POP for a position on a forward/futures underlying. No stock leg is
    possible, so none is accepted; zero drift, so no rate argument."""
    prof = payoff_profile(legs, 0, 0.0, F, multiplier, costs)
    pts = sorted(set([0.0] + [lg["strike"] for lg in legs] + prof["breakevens"] + [F * 10]))
    total = 0.0
    for i in range(len(pts) - 1):
        lo, hi = pts[i], pts[i + 1]
        mid = 0.5 * (lo + hi)
        if expiry_payoff(legs, 0, 0.0, mid, multiplier) - costs > 0:
            p_lo = 1.0 if lo <= 0 else prob_fwd_above(F, lo, T, sigma)
            p_hi = prob_fwd_above(F, hi, T, sigma)
            total += p_lo - p_hi
    return float(min(max(total, 0.0), 1.0))


# ---------------------------------------------------------------------------
# E4  Return & Yield -- with the 2220(d)(3)(H) guardrail enforced in the ENGINE
# ---------------------------------------------------------------------------

MIN_ANNUALIZATION_DAYS = 60


def annualize(period_return, days, allow=False):
    """
    (1 + r)^(365/days) - 1, refused below 60 days.

    FINRA Rule 2220(d)(3)(H): annualized rates of return may not be based upon any
    less than a sixty-day experience. The floor lives HERE rather than in the UI so
    that no second consumer -- app, email, a future surface nobody has thought of --
    can route around it.
    """
    if days < MIN_ANNUALIZATION_DAYS:
        raise AnnualizationRefused(
            f"annualization refused: {days}-day period is below the {MIN_ANNUALIZATION_DAYS}-day "
            f"floor (FINRA Rule 2220(d)(3)(H))")
    if not allow:
        raise AnnualizationRefused("annualization is opt-in; caller must pass allow=True")
    return (1.0 + period_return) ** (365.0 / days) - 1.0


def csp_return(strike, premium, days, costs, contracts=1, multiplier=100,
               settlement_method="PHYSICAL", collateral_basis="CASH_SECURED"):
    """
    Cash-secured put. Costs are a REQUIRED positional argument by design: a return
    computed without costs is not compliant under 2220(d)(3)(D) and must be
    impossible to produce by omission.

    v2: `collateral_basis` is explicit.
      CASH_SECURED  -> collateral = strike * multiplier * contracts. Correct for
                       equity puts AND for cash-settled index puts: the index put is
                       never assigned into shares, but K*multiplier remains the true
                       maximum settlement obligation, so the denominator is the same
                       number for a different reason. Say so on the page.
      SPAN_MARGIN   -> REFUSED. An option on a future has no collateral figure. The
                       margin requirement is set by the clearing house, changes daily,
                       and is not derivable from the contract terms. Return-on-margin
                       is a different, larger and more flattering number than
                       return-on-collateral, and presenting one under the other's
                       heading on a yield page is what 2220 exists to prevent.
    """
    if collateral_basis == "SPAN_MARGIN":
        raise CollateralUndefined(
            "return-on-collateral is undefined for an option on a future: the position "
            "is SPAN-margined, the requirement is set by the clearing house and varies "
            "daily, and it is not derivable from the contract terms. Supply an explicit "
            "margin figure and label the result return-on-margin, which is a different "
            "number and must not be displayed under a collateral heading.")
    if collateral_basis != "CASH_SECURED":
        raise ModelRoutingError(f"unknown collateral_basis {collateral_basis!r}")

    collateral = strike * multiplier * contracts
    net_premium = premium * multiplier * contracts - costs
    r = net_premium / collateral
    out = {"collateral": collateral, "net_premium": net_premium, "costs": costs,
           "return_on_collateral": r, "days": days,
           "breakeven": strike - (net_premium / (multiplier * contracts)),
           "cycles_per_year": 365.0 / days, "annualized": None}
    if settlement_method == "CASH":
        out["settlement_note"] = ("cash-settled: no assignment into shares. The breakeven is "
                                  "an index level at settlement, not a share purchase price, "
                                  "and there is no wheel continuation from this position.")
    return out


def covered_call_returns(basis, strike, premium, days, costs, shares=100, multiplier=100,
                         settlement_method="PHYSICAL"):
    """v2: refuses on cash-settled products. There is no covered call on a cash-settled
    index, because there are no shares to cover it with. The structure that looks like
    one is a naked short call alongside an unrelated long portfolio, with a different
    risk profile and a different margin treatment."""
    if settlement_method == "CASH":
        raise ModelRoutingError(
            "covered-call returns requested against a cash-settled contract. No share "
            "deliverable exists, so the position is not covered and static / if-called "
            "returns are undefined.")
    contracts = shares // multiplier
    net_premium = premium * multiplier * contracts - costs
    cost_basis_total = basis * shares
    static = net_premium / cost_basis_total
    if_called = (net_premium + (strike - basis) * shares) / cost_basis_total
    return {"static_return": static, "if_called_return": if_called,
            "net_premium": net_premium, "costs": costs, "days": days,
            "breakeven": basis - (net_premium / shares),
            "downside_protection_pct": (net_premium / shares) / basis,
            "upside_forgone_at": lambda p: max(0.0, (p - strike)) * shares,
            "cycles_per_year": 365.0 / days, "annualized": None}


def futures_option_return_on_margin(premium, days, costs, initial_margin,
                                    contracts=1, multiplier=50):
    """v2. The only sanctioned way to express a yield on a futures option. The margin
    figure is a REQUIRED argument with no default, sourced from the clearing house,
    and the label travels inside the return value so it cannot be rendered under a
    collateral heading by accident."""
    if not initial_margin or initial_margin <= 0:
        raise CollateralUndefined("initial_margin is required and must be positive")
    net_premium = premium * multiplier * contracts - costs
    basis_total = initial_margin * contracts
    return {"label": "return_on_margin", "net_premium": net_premium, "costs": costs,
            "margin_basis": basis_total, "return_on_margin": net_premium / basis_total,
            "days": days, "cycles_per_year": 365.0 / days, "annualized": None,
            "disclosure": ("margin is set by the clearing house, varies daily, and is not a "
                           "cash-secured position. This figure is not comparable to a return "
                           "on collateral and must not be labelled as one.")}
