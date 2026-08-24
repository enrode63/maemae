from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True)
class TeamScoreConfig:
    timeframe: str
    threshold: float
    weights: dict[str, float]

    def json(self) -> dict[str, Any]:
        return {"timeframe": self.timeframe, "threshold": self.threshold,
                "weights": dict(self.weights), "score_range": [0, 100]}


TEAM_SCORE_CONFIGS = {
    "scalping": TeamScoreConfig("1m/5m", 72, {"trend": .20, "momentum": .30, "volume": .25, "volatility_risk": .15, "support_resistance": .10}),
    "day": TeamScoreConfig("15m/1h", 68, {"trend": .25, "momentum": .25, "volume": .20, "volatility_risk": .15, "support_resistance": .15}),
    "swing": TeamScoreConfig("4h/1d", 65, {"trend": .30, "momentum": .20, "volume": .15, "volatility_risk": .15, "market_regime": .20}),
    "longterm": TeamScoreConfig("1d/1w", 62, {"trend": .30, "momentum": .10, "volume": .10, "volatility_risk": .20, "market_regime": .30}),
}


def score_signals(team: str, raw: object) -> dict[str, Any]:
    """Score derived signals on a common 0..100 scale, failing closed."""
    if team not in TEAM_SCORE_CONFIGS:
        raise ValueError("invalid team")
    config = TEAM_SCORE_CONFIGS[team]
    signals = raw if isinstance(raw, dict) else {}
    breakdown, missing = {}, []
    for name, weight in config.weights.items():
        value = signals.get(name)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 <= float(value) <= 100):
            missing.append(name)
            continue
        normalized = round(float(value), 4)
        breakdown[name] = {"value": normalized, "weight": weight,
                           "weighted_score": round(normalized * weight, 4)}
    score = round(sum(item["weighted_score"] for item in breakdown.values()), 2) if not missing else 0.0
    return {"score": score, "signal_breakdown": breakdown, "missing_signals": missing,
            "threshold": config.threshold, "timeframe": config.timeframe,
            "decision": "PAPER_TRADE" if not missing and score >= config.threshold else "NO_TRADE"}
