from .base import Strategy
from .ema_crossover import EMACrossover
from .orb import OpeningRangeBreakout
from .supertrend_strategy import SupertrendStrategy

REGISTRY = {
    EMACrossover.name: EMACrossover,
    OpeningRangeBreakout.name: OpeningRangeBreakout,
    SupertrendStrategy.name: SupertrendStrategy,
}


def build_strategy(name: str, **params) -> Strategy:
    if name not in REGISTRY:
        raise ValueError(f"Unknown strategy '{name}'. Available: {', '.join(REGISTRY)}")
    return REGISTRY[name](**params)
