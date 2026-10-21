"""Public read-only index using the existing duplicate-aware implementation."""
from pathlib import Path
from .catalog import LoadedDataset
from .validator import _build_indexes


def build_indexes(catalogs):
    return _build_indexes(LoadedDataset(Path("."), "", catalogs=catalogs))
