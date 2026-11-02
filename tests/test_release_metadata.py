"""Tests for source identity, release metadata and CLI import compatibility."""
from pathlib import Path
import sys

if sys.version_info >= (3, 11):
    import tomllib as tomli
else:
    import tomli


ROOT = Path(__file__).resolve().parents[1]


def test_release_identity_and_distribution_name():
    metadata = tomli.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert metadata["name"] == "rootrewind"
    assert metadata["authors"][1]["email"] == "saraasaalari@yahoo.com"
    assert metadata["maintainers"][1]["email"] == "saraasaalari@yahoo.com"
    assert metadata["scripts"]["rootrewind"] == "ttd.cli:main"
    assert metadata["authors"][0]["name"] == "sarahsalary"
    assert metadata["scripts"]["ttd"] == "ttd.cli:main"
    assert metadata["urls"]["Repository"] == "https://github.com/sarahsalary/time-travel-debugger"
    from ttd import __version__
    assert metadata["version"] == __version__


def test_mit_licence_holder():
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Copyright (c) 2026 sarahsalary" in license_text
    assert "Permission is hereby granted" in license_text
