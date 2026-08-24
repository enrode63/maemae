from .service import AutomationService, AutomationConfig
from .scoring import TEAM_SCORE_CONFIGS, score_signals
from .providers import FreeEodMarketDataProvider, market_provider_from_env

__all__ = ["AutomationService", "AutomationConfig", "TEAM_SCORE_CONFIGS", "score_signals",
           "FreeEodMarketDataProvider", "market_provider_from_env"]
