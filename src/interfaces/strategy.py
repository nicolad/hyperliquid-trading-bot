"""
Strategy Interface

Simple interface for implementing trading strategies.
Newbies can add new strategies by implementing this interface.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from enum import Enum


class SignalType(Enum):
    """Trading signal types"""

    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    CLOSE = "close"


@dataclass
class TradingSignal:
    """A trading signal from a strategy"""

    signal_type: SignalType
    asset: str
    size: float
    price: Optional[float] = None  # None = market order
    reason: str = ""
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


@dataclass
class MarketData:
    """Market data provided to strategies"""

    asset: str
    price: float
    volume_24h: float
    timestamp: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    volatility: Optional[float] = None


@dataclass
class Position:
    """Current position information"""

    asset: str
    size: float  # Positive = long, negative = short
    entry_price: float
    current_value: float
    unrealized_pnl: float
    timestamp: float


class TradingStrategy(ABC):
    """
    Base interface for all trading strategies.

    This is the ONLY class newbies need to understand to add new strategies.

    Example implementation:

    class MyStrategy(TradingStrategy):
        def __init__(self, config):
            super().__init__("my_strategy", config)

        def generate_signals(self, market_data, positions, balance):
            if market_data.price < 50000:
                return [TradingSignal(SignalType.BUY, "BTC", 0.001, reason="Price dip")]
            return []
    """

    def __init__(self, name: str, config: Dict[str, Any]):
        self.name = name
        self.config = config
        self.is_active = True

    @abstractmethod
    def generate_signals(
        self, market_data: MarketData, positions: List[Position], balance: float
    ) -> List[TradingSignal]:
        """
        Generate trading signals based on market data and current positions.

        Args:
            market_data: Latest market data for the asset
            positions: Current positions
            balance: Available balance

        Returns:
            List of trading signals (can be empty)
        """
        pass

    def on_trade_executed(
        self, signal: TradingSignal, executed_price: float, executed_size: float
    ) -> None:
        """
        Called when a signal results in a trade execution.
        Override to implement custom logic (e.g., tracking, logging).
        """
        pass

    def on_error(self, error: Exception, context: Dict[str, Any]) -> None:
        """
        Called when an error occurs in strategy execution.
        Override to implement custom error handling.
        """
        pass

    def get_status(self) -> Dict[str, Any]:
        """
        Get strategy status and metrics.
        Override to provide strategy-specific information.
        """
        return {"name": self.name, "active": self.is_active, "config": self.config}

    def start(self) -> None:
        """Called when strategy starts. Override for setup logic."""
        self.is_active = True

    def stop(self) -> None:
        """Called when strategy stops. Override for cleanup logic."""
        self.is_active = False

    def update_config(self, new_config: Dict[str, Any]) -> None:
        """Update strategy configuration. Override for custom logic."""
        self.config.update(new_config)
