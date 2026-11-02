"""Multi-event alert correlation engine."""

from .engine import CorrelationEngine
from .rules import (
    CorrelationRule,
    SequenceRule,
    ThresholdCorrelationRule,
    WindowRule,
    CorrelationMatch,
)
from .config import load_correlation_rules

__all__ = [
    "CorrelationEngine",
    "CorrelationRule",
    "SequenceRule",
    "ThresholdCorrelationRule",
    "WindowRule",
    "CorrelationMatch",
    "load_correlation_rules",
]