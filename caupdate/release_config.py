"""
caupdate/release_config.py — Per-major RHEL release configuration.

Reads release_config.toml from the scripts root directory.
Falls back to safe defaults if the file is missing.
"""

import sys
from functools import lru_cache
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        tomllib = None   # type: ignore

_CONFIG_FILE = Path(__file__).parent.parent / 'release_config.toml'

_DEFAULTS = {
    'centos_stream': False,
}


@lru_cache(maxsize=1)
def _load() -> dict:
    """Load and cache the TOML config.  Returns {} on any error."""
    if tomllib is None:
        print('WARNING: tomllib not available (Python < 3.11) — '
              'using built-in release config defaults', file=__import__('sys').stderr)
        return {}
    try:
        with open(_CONFIG_FILE, 'rb') as f:
            return tomllib.load(f)
    except FileNotFoundError:
        print(f'WARNING: {_CONFIG_FILE} not found — using defaults',
              file=__import__('sys').stderr)
        return {}


def get(major: int, key: str):
    """Return the config value for a given RHEL major and key."""
    data = _load()
    section = data.get(str(major), {})
    return section.get(key, _DEFAULTS.get(key))


def uses_centos_stream(major: int) -> bool:
    """Return True if this RHEL major version goes through CentOS Stream."""
    return bool(get(major, 'centos_stream'))
