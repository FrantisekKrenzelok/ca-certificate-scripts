"""
caupdate/release_config.py — Per-major RHEL release configuration.

Reads release_config.toml from the scripts root directory.
Inheritance model:
  [<major>]                    — major-level defaults
  [<major>.releases."<minor>"] — per-release overrides (applied on top)

Falls back to safe defaults if the file is missing.
"""

import re
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
    'centos_stream':    False,
    'centos_branch':    '',
    'main_branch':      None,   # computed on demand: rhel{major}-main
    'restart_release_z':    None,
    'restart_release_base': None,
    'jira_keep_zero_below_minor': None,
}


@lru_cache(maxsize=1)
def _load() -> dict:
    """Load and cache the TOML config. Returns {} on any error."""
    if tomllib is None:
        print('WARNING: tomllib not available — using release config defaults',
              file=sys.stderr)
        return {}
    try:
        with open(_CONFIG_FILE, 'rb') as f:
            return tomllib.load(f)
    except FileNotFoundError:
        print(f'WARNING: {_CONFIG_FILE} not found — using defaults',
              file=sys.stderr)
        return {}


# ── internal helpers ──────────────────────────────────────────────────────────

def _major_section(major: int) -> dict:
    """Return the top-level [<major>] section (without releases sub-table)."""
    data = _load().get(str(major), {})
    return {k: v for k, v in data.items() if k != 'releases'}


def _release_overrides(major: int, minor: int) -> dict:
    """Return [<major>.releases."<minor>"] overrides, or {}."""
    return (_load()
            .get(str(major), {})
            .get('releases', {})
            .get(str(minor), {}))


def _get_release(major: int, minor: int | None) -> dict:
    """Merge major defaults with per-release overrides."""
    base = dict(_DEFAULTS)
    base.update(_major_section(major))
    if minor is not None:
        base.update(_release_overrides(major, minor))
    return base


# ── public API ────────────────────────────────────────────────────────────────

def get(major: int, key: str):
    """Return the major-level value for *key*, or the built-in default."""
    section = _major_section(major)
    return section.get(key, _DEFAULTS.get(key))


def uses_centos_stream(major: int) -> bool:
    """Return True if this RHEL major version goes through CentOS Stream."""
    return bool(get(major, 'centos_stream'))


def zstream_clone(major: int) -> bool:
    """Return True if GA bugs for this major need Jira z-stream clone requests."""
    return bool(get(major, 'zstream_clone'))


def centos_branch(major: int) -> str:
    """Return the CentOS Stream branch name (e.g. 'c9s'). Empty = no stream."""
    return str(get(major, 'centos_branch') or '')


def main_branch(major: int, minor: int | None = None) -> str:
    """Return the dist-git main branch for creating z-stream branches.

    If *minor* is given, per-release overrides from
    [<major>.releases."<minor>"] are applied first.
    Fallback: 'rhel<major>-main'.
    """
    cfg = _get_release(major, minor)
    return cfg.get('main_branch') or f'rhel{major}-main'


def restart_releases(major: int) -> tuple[str, str]:
    """Return (restart_release_z, restart_release_base) for this major.

    These are the RPM Release: field values used when certdata changes:
      restart_release_z    — used when the release is a z-stream
      restart_release_base — used when the release is GA/current
    Fallback formula: X0.0 / X1 (e.g. RHEL 8 → 80.0 / 81).
    """
    z = get(major, 'restart_release_z')
    b = get(major, 'restart_release_base')
    if z is None:
        z = f'{major * 10}.0'
    if b is None:
        b = str(major * 10 + 1)
    return str(z), str(b)


def fedora_restart_releases() -> tuple[str, str]:
    """Return (restart_release_z, restart_release_base) for Fedora."""
    data = _load().get('fedora', {})
    return str(data.get('restart_release_z', '1')), \
           str(data.get('restart_release_base', '1'))


def version_parts(major: int) -> int:
    """Return the version format for this major: number of X.Y[.Z] components.

    Applies to dist-git branch names, Jira fixVersions, and release strings.

    2 (default) → X.Y    (e.g. rhel-10.3  — new-style releases)
    3           → X.Y.Z  (e.g. rhel-9.6.0 — legacy releases, set explicitly)
    """
    return int(get(major, 'version_parts') or 2)


def dist_branch(major: int, minor: int) -> str | None:
    """Return the explicit dist-git branch for this specific release, or None.

    When set (e.g. [8.releases."10"] dist_branch = "rhel-8-main"), this release
    has no dedicated dist-git branch — the returned branch should be used directly
    without creating a new one at origin.
    """
    return _release_overrides(major, minor).get('dist_branch')


def jira_keep_zero(major: int, minor: int) -> bool:
    """Return True if the Jira fixVersion should keep the trailing .0.

    Reads jira_keep_zero_below_minor from [<major>]: keep .0 when
    minor < threshold. If the key is absent, no .0 is kept.
    """
    threshold = get(major, 'jira_keep_zero_below_minor')
    if threshold is None:
        return False
    return minor < int(threshold)


def is_sustaining_release(pv_name: str) -> bool:
    """Return True if the errata product name indicates a Sustaining Engineering stream."""
    return any(x in pv_name for x in ('E4S', 'E2S', 'AUS', 'TUS'))
