"""
Shared pytest fixtures and helpers for build_combo tests.
"""
import sys
from pathlib import Path

# Make the package importable when running from the repo root
sys.path.insert(0, str(Path(__file__).parent.parent))
