"""Shared test fixtures for the Time-Travel Debugger."""
from pathlib import Path
import pytest


@pytest.fixture
def root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def demo(root: Path) -> Path:
    return root / "examples" / "demo.jsonl"
