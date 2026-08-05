"""
caupdate/prereqs.py — Pre-flight checks for pipeline script dependencies.

Each script calls check_prereqs() with the tools it needs.
Missing tools are reported together so the user can install everything at once.
"""

import shutil
import subprocess
import sys


def _has(cmd: str) -> bool:
    """Return True if cmd is on PATH."""
    return shutil.which(cmd) is not None


def _has_kerberos() -> bool:
    """Return True if a valid Kerberos ticket exists."""
    return subprocess.run(['klist', '-s'],
                         capture_output=True).returncode == 0


# ── tool catalogue ────────────────────────────────────────────────────────────

TOOLS = {
    # name           check-fn                install hint
    'git':          (_has,        'git'),
    'rhpkg':        (_has,        'rhpkg — install with: dnf install rhpkg'),
    'centpkg':      (_has,        'centpkg — install with: dnf install centpkg'),
    'fedpkg':       (_has,        'fedpkg — install with: dnf install fedpkg'),
    'koji':         (_has,        'koji — install with: dnf install koji'),
    'brew':         (_has,        'brew — available via internal Red Hat tools'),
    'bodhi':        (_has,        'bodhi — install with: dnf install bodhi-client'),
    'kinit':        (_has,        'kinit (krb5-workstation) — dnf install krb5-workstation'),
    'kerberos':     (_has_kerberos, 'a valid Kerberos ticket — run: kinit'),
}


def check_prereqs(required: list[str], script: str = '') -> None:
    """
    Verify that all required tools/conditions are present.
    Prints a combined error message and exits if anything is missing.

    Args:
        required:  list of keys from TOOLS
        script:    calling script name (for error messages)
    """
    missing = []
    for name in required:
        if name not in TOOLS:
            continue
        check_fn, hint = TOOLS[name]
        ok = check_fn(name) if check_fn is _has else check_fn()
        if not ok:
            missing.append(f'  • {hint}')

    if missing:
        label = f'{script}: ' if script else ''
        print(f'\n{label}Missing prerequisites:\n', file=sys.stderr)
        for m in missing:
            print(m, file=sys.stderr)
        print(file=sys.stderr)
        sys.exit(1)
