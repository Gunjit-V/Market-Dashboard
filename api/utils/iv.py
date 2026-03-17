"""
Black-Scholes Implied Volatility Solver

Computes IV from option prices using the Newton-Raphson method.
Uses standard BS model assumptions with Indian market conventions:
  - Risk-free rate: ~6.5% (India 91-day T-bill proxy)
  - No dividends (Nifty index)
  - European-style options (Nifty options are European)
"""

import math
from scipy.stats import norm
from typing import Optional

RISK_FREE_RATE = 0.065  # 6.5% — India 91-day T-bill proxy
TRADING_DAYS = 252


def bs_price(
    S: float,      # spot / underlying price
    K: float,      # strike price
    T: float,      # time to expiry in years
    r: float,      # risk-free rate
    sigma: float,  # volatility (annualised decimal)
    option_type: str = "CE",  # "CE" or "PE"
) -> float:
    """Standard Black-Scholes option price."""
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if option_type == "CE" else (K - S))

    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    if option_type == "CE":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def bs_vega(
    S: float, K: float, T: float, r: float, sigma: float
) -> float:
    """BS vega — sensitivity of price to volatility. Used in Newton-Raphson."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    return S * math.sqrt(T) * norm.pdf(d1)


def implied_volatility(
    option_price: float,
    S: float,
    K: float,
    T: float,
    r: float = RISK_FREE_RATE,
    option_type: str = "CE",
    max_iterations: int = 100,
    tolerance: float = 1e-6,
) -> Optional[float]:
    """
    Compute implied volatility using Newton-Raphson method.

    Returns None if:
      - Option price is below intrinsic value (no valid IV)
      - Convergence fails
      - Inputs are invalid
    """
    if T <= 0 or S <= 0 or K <= 0 or option_price <= 0:
        return None

    # Check against intrinsic value
    intrinsic = max(0.0, (S - K) if option_type == "CE" else (K - S))
    if option_price < intrinsic - 0.01:
        return None

    # Initial guess via Brenner-Subrahmanyam approximation
    sigma = math.sqrt(2 * math.pi / T) * (option_price / S)
    sigma = max(0.01, min(sigma, 5.0))  # clamp to reasonable range

    for _ in range(max_iterations):
        price = bs_price(S, K, T, r, sigma, option_type)
        vega = bs_vega(S, K, T, r, sigma)

        if vega < 1e-12:
            # Vega too small — try bisection fallback
            return _bisection_iv(option_price, S, K, T, r, option_type)

        diff = price - option_price
        if abs(diff) < tolerance:
            return sigma if 0.001 < sigma < 5.0 else None

        sigma -= diff / vega
        sigma = max(0.001, min(sigma, 5.0))

    # Fallback to bisection if Newton didn't converge
    return _bisection_iv(option_price, S, K, T, r, option_type)


def _bisection_iv(
    option_price: float,
    S: float, K: float, T: float, r: float,
    option_type: str,
    lo: float = 0.001,
    hi: float = 5.0,
    max_iterations: int = 100,
    tolerance: float = 1e-5,
) -> Optional[float]:
    """Bisection fallback for IV when Newton-Raphson fails."""
    for _ in range(max_iterations):
        mid = (lo + hi) / 2
        price = bs_price(S, K, T, r, mid, option_type)
        if abs(price - option_price) < tolerance:
            return mid
        if price > option_price:
            hi = mid
        else:
            lo = mid
    return None


def time_to_expiry_years(expiry_date, current_date=None) -> float:
    """Convert expiry date to time in years (using calendar days / 365)."""
    from datetime import date, datetime
    if current_date is None:
        current_date = date.today()
    if isinstance(expiry_date, datetime):
        expiry_date = expiry_date.date()
    if isinstance(current_date, datetime):
        current_date = current_date.date()
    days = (expiry_date - current_date).days
    return max(days / 365.0, 1 / 365.0)  # minimum 1 day to avoid div by 0
