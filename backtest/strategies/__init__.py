from backtest.strategies.rv_breakout import RVBreakoutStrategy
from backtest.strategies.vrp_reversion import VRPReversionStrategy

STRATEGY_REGISTRY = {
    "rv_breakout": RVBreakoutStrategy,
    "vrp_reversion": VRPReversionStrategy,
}
