#!/usr/bin/python3
# vim:set et sw=4:
"""
build_combo.py — Python rewrite of build_combo.sh

Downloads upstream Mozilla NSS certdata, applies per-release modifications,
and updates dist-git package trees.  Mirrors the behaviour of build_combo.sh
exactly; use the same test suite to validate both.

Usage:
    ./build_combo.py [-q] [-d] [-n nss_release] [-t nss_type]
                     [-f cert_datadir] [-p prune_date]
                     releases...
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

import gitlab

from caupdate.tui import PipelineOutput
from caupdate.release_config import (
    uses_centos_stream, centos_branch as cfg_centos_branch,
    main_branch as cfg_main_branch, restart_releases,
    fedora_restart_releases, dist_branch as cfg_dist_branch,
    version_parts as cfg_version_parts,
)
from caupdate.prereqs import check_prereqs
from caupdate.release import release_get_major, safe_int


# ── constants ─────────────────────────────────────────────────────────────────

NSS_BASE_URL = ('https://hg.mozilla.org/releases/mozilla-release'
                '/raw-file/default/security/nss/lib')
NSS_DEV_URL  = 'https://hg.mozilla.org/projects/nss/raw-file/default/lib'

SCRIPT_LOC = Path(__file__).parent.resolve()


# ── pure functions ────────────────────────────────────────────────────────────

def bump_release(release: str, reset_release: str = '') -> str:
    """Increment an RPM Release field.

    Without reset_release: strip trailing non-numeric chars, take the leading
    integer part and add 1  (e.g. "80.0%{?dist}" → "81").

    With reset_release: preserve the major, bump the minor
    (80.0 → 80.1, 81 → 82).
    """
    # strip trailing non-digit/non-dot characters (like %{?dist})
    clean = re.sub(r'[^0-9.]+$', '', release)

    if not reset_release:
        # grab the first integer field
        first_int = re.match(r'^(\d+)', clean)
        if not first_int:
            return release
        return str(int(first_int.group(1)) + 1)

    # ca-certificates mode: bump minor, preserve major
    if '.' not in clean:
        return str(int(clean) + 1)
    base, _, bump = clean.rpartition('.')
    if bump == base:        # edge case: single-element "81" split on itself
        return str(int(base) + 1)
    return f'{base}.{int(bump) + 1}'


def make_log(vr: str, name: str = '', email: str = '') -> str:
    """Generate an RPM %changelog header line."""
    name  = name  or _git_config('user.name')  or _whoami()
    email = email or _git_config('user.email') or f'{_whoami()}@{_hostname()}'
    day = date.today().strftime('%a %b %d %Y')
    return f'*{day} {name} <{email}> - {vr}'


def set_list_state(list_file: Path, release: str, new_state: str) -> None:
    """Update the state (field 5) of a release line in rhel.list/fedora.list.
    Format: release:bugnumber:erratanumber:nvr:state:...
    Mirrors bash set_list_state(): warns and returns if file or release missing."""
    if not list_file.exists():
        print(f'WARNING: {release} not found in {list_file} (file missing — '
              f'did plan.py run first?)', file=sys.stderr)
        return
    text = list_file.read_text()
    pattern = rf'^({re.escape(release)}:[^:]*:[^:]*:[^:]*:[^:]*:)[^:]*(.*)'
    new_text, n = re.subn(pattern,
                          rf'\g<1>{new_state}\g<2>',
                          text, flags=re.MULTILINE)
    if n == 0:
        print(f'WARNING: {release} not found in {list_file}', file=sys.stderr)
        return
    list_file.write_text(new_text)


def set_list_branch(list_file: Path, release: str, branch: str) -> None:
    """Write the dist-git branch for a release into field 1 (second column) of rhel.list."""
    if not list_file.exists():
        return
    lines = list_file.read_text().splitlines()
    new_lines = []
    for line in lines:
        if not line.strip():
            continue
        fields = line.split(':')
        if fields[0] == release:
            while len(fields) < 2:
                fields.append('')
            fields[1] = branch
        new_lines.append(':'.join(fields))
    list_file.write_text('\n'.join(new_lines) + '\n')


def _nss_tag(nss_version: str) -> str:
    """Convert NSS version string to release tag. '3.125' → 'NSS_3_125_RTM'"""
    return 'NSS_' + nss_version.replace('.', '_') + '_RTM'


def _rhel_ver_str(release: str) -> str:
    """Extract 'major.minor' string for rlIsRHEL from a release string."""
    m = re.match(r'^rhel-(\d+)\.(\d+)', release)
    if not m:
        return ''
    maj, minor = m.group(1), m.group(2)
    return f'{maj}.{minor}'


def update_upstream_tag_crosscheck(tests_dir: Path, nss_version: str,
                                   codesign_tag: str, cacerts: Path,
                                   releases_by_major: dict) -> None:
    """Update Sanity/upstream-tag-crosscheck for a new NSS/codesign version.

    - Adds hashes of the new certdata and codesign PEM to sha256sums
    - Updates runtest.sh with rlIsRHEL entries for each release in this cycle
    - Uploads new files to the LOOKASIDE server (graceful failure if unreachable)
    """
    import hashlib

    crosscheck  = tests_dir / 'Sanity' / 'upstream-tag-crosscheck'
    sha256_path = crosscheck / 'sha256sums'
    runtest_path = crosscheck / 'runtest.sh'
    makefile_path = crosscheck / 'Makefile'

    nss_tag      = _nss_tag(nss_version)
    nss_ver_fmt  = nss_version.replace('.', '_')
    certdata_fname = f'certdata-{nss_tag}.txt'
    codesign_fname = f'codesign-{codesign_tag}.pem' if codesign_tag else None

    # ── hashes (from locally downloaded files, already at the correct tag) ────
    orig = cacerts / 'certdata.txt.orig'
    codesign_pem = cacerts / 'microsoft_sign_obj_ca.pem'
    certdata_hash = hashlib.sha256(orig.read_bytes()).hexdigest() if orig.exists() else None
    codesign_hash = (hashlib.sha256(codesign_pem.read_bytes()).hexdigest()
                     if codesign_pem.exists() and codesign_fname else None)

    # ── sha256sums ────────────────────────────────────────────────────────────
    lines = sha256_path.read_text().splitlines() if sha256_path.exists() else []
    changed = False
    for fname, fhash in [(certdata_fname, certdata_hash),
                         (codesign_fname, codesign_hash)]:
        if not fname or not fhash:
            continue
        if any(l.endswith(f'  {fname}') for l in lines):
            print(f'  tests: {fname} already in sha256sums')
            continue
        lines.append(f'{fhash}  {fname}')
        lines = sorted(lines, key=lambda l: l.split()[-1] if l.strip() else '')
        print(f'  tests: added {fname} hash to sha256sums')
        changed = True
    if changed:
        sha256_path.write_text('\n'.join(lines) + '\n')

    # ── upload new files to LOOKASIDE ─────────────────────────────────────────
    lookaside_base = 'eng-shell1.bast-001.prod.rdu2.dc.redhat.com:/export/engineering_qa/rhts/lookaside/crypto/ca-certificates'
    for local, remote_subdir, fname in [
        (orig,        'certdata',     certdata_fname),
        (codesign_pem,'code-signing', codesign_fname),
    ]:
        if not fname or not local.exists():
            continue
        if any(l.endswith(f'  {fname}') for l in sha256_path.read_text().splitlines()):
            rc = _run(['rsync', str(local),
                       f'{lookaside_base}/{remote_subdir}/{fname}'])
            if rc == 0:
                print(f'  tests: uploaded {fname} to LOOKASIDE')

    # ── runtest.sh ────────────────────────────────────────────────────────────
    runtest = runtest_path.read_text()
    codesign_var = codesign_tag if codesign_tag else '-'

    def _make_line(ver_str, nss_ver_fmt, codesign_var):
        return (f'elif rlIsRHEL {ver_str:<4}; then '
                f'VER={nss_ver_fmt}   CODESIGN={codesign_var} '
                f'MODS=(reorder-comments)')

    for major, releases in sorted(releases_by_major.items()):
        # Check if a generic major-only catch-all exists (e.g. 'rlIsRHEL 10  ;')
        generic_pat = re.search(
            rf'elif rlIsRHEL {major}\s+;[^\n]*', runtest)
        if generic_pat:
            # Update the generic entry — it covers all X.Y for this major
            new_line = _make_line(str(major), nss_ver_fmt, codesign_var)
            runtest = runtest[:generic_pat.start()] + new_line + runtest[generic_pat.end():]
            continue

        # No generic catch-all — update/insert per-minor entries in order
        for rel in sorted(releases,
                          key=lambda r: [int(x) for x in re.findall(r'\d+', r)]):
            ver_str = _rhel_ver_str(rel)
            if not ver_str:
                continue
            new_line = _make_line(ver_str, nss_ver_fmt, codesign_var)
            existing = re.search(
                rf'elif rlIsRHEL {re.escape(ver_str)}\s*;[^\n]*', runtest)
            if existing:
                runtest = (runtest[:existing.start()]
                           + new_line + runtest[existing.end():])
            else:
                # Insert before the `else` fallback
                runtest = runtest.replace(
                    'else                     VER=',
                    f'{new_line}\n        else                     VER=')

    # Update the else fallback
    runtest = re.sub(
        r'else\s+VER=\S+\s+CODESIGN=\S+\s+MODS=\(reorder-comments\)',
        f'else                     VER={nss_ver_fmt}   '
        f'CODESIGN={codesign_var} MODS=(reorder-comments)',
        runtest)

    runtest_path.write_text(runtest)
    print(f'  tests: updated runtest.sh  VER={nss_ver_fmt}  CODESIGN={codesign_var}')

    # ── stage changes ─────────────────────────────────────────────────────────
    _run(['git', 'add',
          'Sanity/upstream-tag-crosscheck/sha256sums',
          'Sanity/upstream-tag-crosscheck/runtest.sh'],
         cwd=tests_dir)


def format_cert_log(raw: str) -> str:
    """Convert check_certs.sh output to clean commit-message format.

    Input:
        '   Removing:\\n    # Certificate "Foo CA"\\n   Adding:\\n    # Certificate "Bar CA"'
    Output:
        'Removing:\\n- Foo CA\\n\\nAdding:\\n- Bar CA'
    """
    lines = []
    prev_was_section = False
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        if s in ('Removing:', 'Adding:'):
            if lines and not prev_was_section:
                lines.append('')
            lines.append(s)
            prev_was_section = True
        else:
            # '# Certificate "Foo CA"'  →  '- Foo CA'
            m = re.match(r'^#\s*Certificate\s+"(.+)"', s)
            lines.append(f'- {m.group(1)}' if m else f'- {s}')
            prev_was_section = False
    return '\n'.join(lines)


def extract_version(header_text: str, define: str) -> str:
    """Extract the first quoted token after a #define line."""
    m = re.search(rf'#define\s+{re.escape(define)}\s+"([^"\s]+)', header_text)
    return m.group(1) if m else ''


# ── spec-file patching ────────────────────────────────────────────────────────

def add_patch(spec_path: Path,
              patch: str,
              patch_orig: str,
              cert_log_path: Path,
              nss_version: str,
              ckbi_version: str,
              new_version: str = '',
              restart_release: str = '',
              name: str = '',
              email: str = '',
              verbose: bool = False) -> None:
    """Rewrite *spec_path* with updated Version, Release, %changelog, and
    optionally a new Patch entry.  Writes checkin.log alongside the spec."""

    lines       = spec_path.read_text().splitlines(keepends=True)
    cert_log    = cert_log_path.read_text() if cert_log_path.exists() else ''

    glob_nss_version = ''
    in_patches   = 0   # 0=before, 1=inside, 2=done
    in_setup     = 0
    max_patch    = 0
    patch_num    = 0
    version      = ''
    old_version  = ''
    out          = []

    for line in lines:
        stripped = line.rstrip('\n')

        # ── Patch source/setup insertion ──────────────────────────────────────
        if patch != 'NONE':
            if re.match(r'^Patch.*: ', stripped) and in_patches <= 1:
                m = re.match(r'^Patch(\d+):', stripped)
                if m and int(m.group(1)) > max_patch:
                    max_patch = int(m.group(1))
                in_patches = 1
                out.append(line)
                continue

            if stripped.startswith('%patch') and in_setup <= 1:
                in_setup = 1
                out.append(line)
                continue

            if not stripped.strip():   # blank line = end of block
                if in_patches == 1:
                    patch_num = max_patch + 1
                    out.append(f'# Update certdata.txt to version {ckbi_version}\n')
                    out.append(f'Patch{patch_num}: {patch}\n')
                    in_patches = 2
                if in_setup == 1:
                    out.append(f'%patch{patch_num} -p1 -b {patch_orig}\n')
                    in_setup = 2
                out.append(line)
                continue

        # ── %global substitution ──────────────────────────────────────────────
        if stripped.startswith('%global'):
            parts = stripped.split()
            if len(parts) >= 3 and parts[1] == 'nss_version':
                glob_nss_version = parts[2]
            out.append(line)
            continue

        # ── Version: ──────────────────────────────────────────────────────────
        if stripped.startswith('Version: '):
            if not new_version:
                out.append(line)
                version = stripped[len('Version: '):]
                version = version.replace('%{nss_version}', glob_nss_version)
            else:
                old_version = stripped[len('Version: '):]
                version     = new_version
                if verbose:
                    print(f'Old Version: {old_version}', file=sys.stderr)
                    print(f'New Version: {version}',     file=sys.stderr)
                out.append(f'Version: {version}\n')
            continue

        # ── Release: ──────────────────────────────────────────────────────────
        if stripped.startswith('Release: '):
            cur_rel = stripped[len('Release: '):]
            if not new_version or new_version == old_version:
                release = bump_release(cur_rel, restart_release)
            else:
                release = restart_release
            out.append(f'Release: {release}%{{?dist}}\n')
            continue

        # ── %changelog ────────────────────────────────────────────────────────
        if stripped.startswith('%changelog'):
            out.append(line)
            out.append(make_log(f'{version}-{release}', name, email) + '\n')
            out.append(f'- Update to CKBI {ckbi_version} from NSS {nss_version}\n')
            for entry in format_cert_log(cert_log).splitlines():
                out.append(f'  {entry}\n')
            out.append('\n')
            continue

        out.append(line)

    spec_path.write_text(''.join(out))

    # write checkin.log
    checkin = spec_path.parent / 'checkin.log'
    with checkin.open('w') as f:
        f.write(f'Update to CKBI {ckbi_version} from NSS {nss_version}\n')
        f.write('\n')
        f.write(format_cert_log(cert_log))
        f.write('\n')


# ── package update ────────────────────────────────────────────────────────────

def cacertificates_update(pkg_dir: Path,
                           certdata: Path,
                           nssckbi: Path,
                           nss_version: str,
                           ckbi_version: str,
                           scratch: Path,
                           release: str,
                           restart_release_z: str,
                           restart_release_base: str,
                           ga_releases: list[str],
                           verbose: bool = True) -> int:
    """Update a ca-certificates dist-git checkout.  Returns 0 on success."""

    print(f'  [{release}] pkg_dir  : {pkg_dir}')
    print(f'  [{release}] certdata : {certdata}')

    if not certdata.is_file():
        print(f'!!!Skipping ca-certificates build for {release}: '
              f'certdata.txt not found at {certdata}')
        return 1

    if not pkg_dir.is_dir():
        print(f'!!!Skipping ca-certificates build for {release}: '
              f'pkg dir not found: {pkg_dir}')
        return 1

    restart_release = (restart_release_base
                       if release in ga_releases
                       else restart_release_z)
    print(f'  [{release}] restart_release: {restart_release} '
          f'(ga={release in ga_releases})')

    scratch.mkdir(parents=True, exist_ok=True)

    # diff certdata
    old_certdata = pkg_dir / 'certdata.txt'
    cert_log     = scratch / 'cert_log'
    if not old_certdata.exists():
        print(f'  [{release}] WARNING: no existing certdata.txt in {pkg_dir}')
    _run([str(SCRIPT_LOC / 'check_certs.sh'),
          str(old_certdata), str(certdata)],
         stdout=cert_log, cwd=pkg_dir)

    if old_certdata.exists() and old_certdata.read_bytes() == certdata.read_bytes():
        print(f'  [{release}] certdata unchanged — skipping update')
        return 0

    print(f'  [{release}] certdata differs — applying update')
    print('>>> update ca-certificates.spec file')
    year = date.today().strftime('%Y')

    add_patch(
        pkg_dir / 'ca-certificates.spec',
        patch='NONE', patch_orig='empty',
        cert_log_path=cert_log,
        nss_version=nss_version,
        ckbi_version=ckbi_version,
        new_version=f'{year}.{ckbi_version}',
        restart_release=restart_release,
        verbose=verbose,
    )

    shutil.copy2(certdata, pkg_dir / 'certdata.txt')
    shutil.copy2(nssckbi,  pkg_dir / 'nssckbi.h')

    if verbose:
        _run(['git', '--no-pager', 'diff', 'ca-certificates.spec'], cwd=pkg_dir)
    rc_add = _run(['git', 'add', 'ca-certificates.spec', 'nssckbi.h', 'certdata.txt'],
                  cwd=pkg_dir)
    if rc_add != 0:
        print(f'  [{release}] ERROR: git add failed (rc={rc_add})', file=sys.stderr)
        return rc_add

    r = subprocess.run(['git', 'status', '--short'], capture_output=True,
                       text=True, cwd=pkg_dir)
    staged = [l for l in r.stdout.splitlines() if l.startswith(('A ', 'M ', 'D '))]
    if staged:
        print(f'  [{release}] staged: {", ".join(staged)}')
    else:
        print(f'  [{release}] WARNING: git add ran but nothing is staged — '
              f'check pkg_dir content')
    if verbose:
        _run(['git', 'status'], cwd=pkg_dir)

    return 0


# ── helpers ───────────────────────────────────────────────────────────────────

def _run(cmd, cwd=None, stdout=None) -> int:
    """Run a command, print stderr on failure, return exit code."""
    cmd_str = ' '.join(str(c) for c in cmd)
    if stdout is not None:
        with open(stdout, 'w') as f:
            r = subprocess.run(cmd, cwd=cwd, stdout=f,
                               stderr=subprocess.PIPE, check=False)
    else:
        r = subprocess.run(cmd, cwd=cwd, stderr=subprocess.PIPE, check=False)
    if r.returncode != 0:
        print(f'  [ERROR rc={r.returncode}] {cmd_str}', file=sys.stderr)
        if r.stderr:
            print(r.stderr.decode(errors='replace').rstrip(), file=sys.stderr)
    return r.returncode


def _read_config(cfg_file: Path) -> dict:
    """Parse a key:value config file (same format as config.cfg)."""
    cfg = {}
    if not cfg_file.exists():
        return cfg
    for line in cfg_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if ':' in line:
            k, _, v = line.partition(':')
            cfg[k.strip()] = v.strip()
    return cfg


def _gitlab_project_path(url: str) -> str:
    """Extract namespace/project from an SSH or HTTPS GitLab URL."""
    # SSH:   git@gitlab.com:namespace/project.git
    m = re.match(r'^git@[^:]+:(.+?)(?:\.git)?$', url)
    if m:
        return m.group(1)
    # HTTPS: https://gitlab.com/namespace/project[.git]
    m = re.match(r'^https?://[^/]+/(.+?)(?:\.git)?$', url)
    if m:
        return m.group(1)
    return url


def _gitlab_upstream_url(gl: 'gitlab.Gitlab', centos_fork: str) -> str:
    """Return the SSH URL of the upstream project that centos_fork is forked from."""
    fork_project = gl.projects.get(_gitlab_project_path(centos_fork))
    if fork_project.forked_from_project:
        upstream = gl.projects.get(fork_project.forked_from_project['id'])
        return upstream.ssh_url_to_repo
    return ''


def _git_config(key):
    r = subprocess.run(['git', 'config', key],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ''


def _whoami():
    r = subprocess.run(['whoami'], capture_output=True, text=True)
    return r.stdout.strip()


def _hostname():
    r = subprocess.run(['hostname'], capture_output=True, text=True)
    return r.stdout.strip()


def _distgit_branch(release: str) -> str:
    """Convert a release string (always rhel-X.Y.Z) to its dist-git branch name.

    Reads distgit_version_parts from TOML per major:
      3 (default) → keep X.Y.Z as-is   (e.g. rhel-9.6.0, rhel-8.10.0)
      2           → strip trailing .0   (e.g. rhel-10.3.0 → rhel-10.3)
    """
    m = re.match(r'^rhel-(\d+)\.(\d+)\.(\d+)$', release)
    if not m:
        return release
    major = int(m.group(1))
    if cfg_version_parts(major) == 2:
        return f'rhel-{m.group(1)}.{m.group(2)}'
    return release


# ── main ──────────────────────────────────────────────────────────────────────

def build_ga_releases() -> list[str]:
    """Return the GA (head) release per major plus rawhide.
    GA = the latest active release for each major; these go through
    centos-fork for centos_stream majors and get the base RPM Release number."""
    r = subprocess.run(['python3', str(SCRIPT_LOC / 'process.py'), '--get-ga'],
                       capture_output=True, text=True, cwd=SCRIPT_LOC)
    return ['rawhide'] + r.stdout.split()


def main():
    ap = argparse.ArgumentParser(
        description='''
Download upstream Mozilla NSS certdata, apply per-release modifications,
and update dist-git package trees for ca-certificates.

Pipeline usage (reads releases from meta/ written by plan.py):
  ./build_combo.py

Manual usage:
  ./build_combo.py rhel-10.3 rhel-9.9.0 f45 rawhide

Required tools: git, rhpkg, centpkg (for CentOS Stream majors),
fedpkg (for Fedora), kinit (Kerberos ticket for dist-git operations).
''',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument('-q', action='store_true',
                    help='Quiet mode — suppress git diff and status output')
    ap.add_argument('--human', action='store_true',
                    help='Rich TUI output with live status table and log panel')
    ap.add_argument('-d', action='store_true',
                    help='Use NSS development tip instead of latest release')
    ap.add_argument('-n', metavar='NSS_RELEASE',
                    help='Fetch a specific NSS release (e.g. 3_114)')
    ap.add_argument('-t', metavar='NSS_TYPE', default='RTM',
                    help='NSS release type: RTM, BETA1, BETA2 (default: RTM)')
    ap.add_argument('-f', metavar='CERT_DATA_DIR',
                    help='Copy certdata.txt, nss.h, nssckbi.h from a local '
                         'directory instead of downloading from Mozilla')
    ap.add_argument('-p', metavar='PRUNE_DATE', default='NEVER',
                    help='Distrust-after date for certdata pruning (default: NEVER)')
    ap.add_argument('releases', nargs='*',
                    help='Release targets e.g. rhel-10.3 rhel-9.9.0 f45 rawhide. '
                         'Omit to read from meta/rhel.list and meta/fedora.list.')
    args = ap.parse_args()

    # ── pre-flight checks ─────────────────────────────────────────────────────
    needed = ['git', 'kerberos']
    if not args.f:
        pass   # wget replaced by urllib; no extra tool needed
    # We don't know yet which distros are needed, so check all packaging tools
    needed += ['rhpkg', 'fedpkg']
    check_prereqs(needed, 'build_combo.py')

    verbose = not args.q
    out = PipelineOutput(human=args.human, title='build_combo.py')
    out.set_columns(['Release', 'Branch', 'State'])

    meta = SCRIPT_LOC / 'meta'

    # ── collect releases — from meta lists or explicit CLI args ───────────────
    rhel_by_major: dict[int, list[str]] = {}   # major → [release, ...]
    fedora: list[str] = []
    rhel_cacerts = fedora_cacerts = False

    release_sources = list(args.releases)

    if not release_sources:
        # Pipeline mode: read from meta files written by plan.py
        rhel_list_file   = meta / 'rhel.list'
        fedora_list_file = meta / 'fedora.list'
        for path in (rhel_list_file, fedora_list_file):
            if path.exists():
                for line in path.read_text().splitlines():
                    if line.strip():
                        release_sources.append(line.split(':')[0])
        if not release_sources:
            print('No releases specified and meta/rhel.list / meta/fedora.list are empty.',
                  file=sys.stderr)
            print('Run plan.py first, or pass releases as arguments.', file=sys.stderr)
            sys.exit(1)

    for rel in release_sources:
        if rel.startswith('rhel-'):
            major = safe_int(release_get_major(rel))
            if major > 0:
                rhel_by_major.setdefault(major, []).append(rel)
                rhel_cacerts = True
            else:
                print(f'Unknown release: {rel}', file=sys.stderr)
                sys.exit(1)
        elif re.match(r'^f\d+$|^rawhide$', rel):
            fedora.append(rel); fedora_cacerts = True
        else:
            print(f'Unknown release: {rel}', file=sys.stderr)
            sys.exit(1)

    # Normalise to 3-part (rhel-X.Y.0) so the set matches the meta list format,
    # which always uses X.Y.Z regardless of how the errata map keys are stored.
    ga_releases = {
        re.sub(r'^(rhel-\d+\.\d+)$', r'\1.0', r)
        for r in build_ga_releases()
    }
    cfg = _read_config(SCRIPT_LOC / 'config.cfg')
    centos_fork   = cfg.get('centos_fork', '')
    glab_url_base = cfg.get('glab_url_base', 'https://gitlab.com/')
    glab_api_key  = cfg.get('glab_api_key', '')

    gl = None
    if glab_api_key:
        try:
            gl = gitlab.Gitlab(url=glab_url_base, private_token=glab_api_key)
            gl.auth()
        except Exception as e:
            print(f'WARNING: GitLab auth failed: {e}', file=sys.stderr)
            gl = None

    packages = SCRIPT_LOC / 'packages'
    modified = SCRIPT_LOC / 'modified'
    cacerts  = SCRIPT_LOC / 'cacerts'
    scratch  = SCRIPT_LOC / f'scratch.{os.getpid()}'

    # ── directory setup ───────────────────────────────────────────────────────
    print('*' * 66)
    print('*' + ' Setting up directories '.center(64) + '*')
    print('*' * 66)
    for d in (packages, modified, cacerts):
        shutil.rmtree(d, ignore_errors=True)
    meta.mkdir(exist_ok=True)
    packages.mkdir()
    (packages / 'rhel').mkdir()
    cacerts.mkdir()

    modified.mkdir(parents=True)
    centos_list = []
    for major in sorted(rhel_by_major.keys()):
        if uses_centos_stream(major):
            centos_list.append(str(major))
    if fedora:
        (packages / 'fedora').mkdir()

    # ── fetch sources ─────────────────────────────────────────────────────────
    print('*' * 66)
    print('*' + ' Fetching Sources '.center(64) + '*')
    print('*' * 66)

    if args.f:
        src = Path(args.f)
        print(f'copying source data from directory: {src}')
        for fname in ('nss.h', 'nssckbi.h', 'certdata.txt'):
            shutil.copy2(src / fname, cacerts / fname)
    else:
        # -n on CLI takes precedence; then config.cfg 'nss' key; then HEAD
        # Config may store just the minor (e.g. '126'); normalise to '3.126'
        nss_ver = args.n or cfg.get('nss', '')
        if nss_ver and '.' not in nss_ver:
            nss_ver = f'3.{nss_ver}'
        if args.d:
            base_url = NSS_DEV_URL
        elif nss_ver:
            rel_tag = nss_ver.replace('.', '_')
            base_url = (f'https://hg.mozilla.org/projects/nss/raw-file/'
                        f'NSS_{rel_tag}_{args.t}/lib')
            if not args.n:
                print(f'>> using NSS {nss_ver} from config.cfg')
        else:
            base_url = NSS_BASE_URL

        print(f'fetching source data from mozilla: {base_url}')
        import urllib.request
        for url, dest in [
            (f'{base_url}/nss/nss.h',               cacerts / 'nss.h'),
            (f'{base_url}/ckfw/builtins/nssckbi.h',  cacerts / 'nssckbi.h'),
            (f'{base_url}/ckfw/builtins/certdata.txt', cacerts / 'certdata.txt.orig'),
        ]:
            print(f'>> fetching {url}')
            try:
                urllib.request.urlretrieve(url, dest)
            except Exception as e:
                print(f'fetching {url} failed: {e}')
                sys.exit(1)

        # merge MS code signing certs
        sign_obj_cas = cacerts / 'microsoft_sign_obj_ca.pem'
        _run([str(SCRIPT_LOC / 'fetch_objsign.sh'), '-n', '-o', str(sign_obj_cas)],
             cwd=cacerts)
        _run(['python3', str(SCRIPT_LOC / 'mergepem2certdata.py'),
              '-c', str(cacerts / 'certdata.txt.orig'),
              '-p', str(sign_obj_cas),
              '-o', str(cacerts / 'certdata.txt'),
              '-t', 'CKA_TRUST_CODE_SIGNING',
              '-l', 'Microsoft Code Signing Only Certificate',
              '-x', args.p])

    # extract versions
    nss_version  = extract_version((cacerts / 'nss.h').read_text(), 'NSS_VERSION')
    ckbi_version = extract_version((cacerts / 'nssckbi.h').read_text(),
                                   'NSS_BUILTINS_LIBRARY_VERSION')

    codesign_file = cacerts / 'codesign-release.txt'
    if codesign_file.exists():
        mcs = codesign_file.read_text().strip()
        if mcs and mcs != 'unknown':
            ckbi_version = f'{ckbi_version}_{mcs}'

    (meta / 'nssversion.txt').write_text(nss_version)
    codesign_tag = codesign_file.read_text().strip() if codesign_file.exists() else ''
    (meta / 'mcsversion.txt').write_text(codesign_tag)
    (meta / 'ckbiversion.txt').write_text(ckbi_version)

    # ── clone / update tests repo ─────────────────────────────────────────────
    print('>> fetching ca-certificates tests')
    tests_dir = packages / 'tests'
    if not tests_dir.exists():
        _run(['rhpkg', '-q', 'clone', 'tests/ca-certificates', 'tests'],
             cwd=packages)
    else:
        _run(['git', 'pull'], cwd=tests_dir)

    tests_state_file = meta / 'tests_state.txt'
    if tests_dir.exists():
        print('>> updating upstream-tag-crosscheck test')
        update_upstream_tag_crosscheck(
            tests_dir, nss_version,
            codesign_tag if codesign_tag and codesign_tag != 'unknown' else '',
            cacerts, rhel_by_major)
        tests_state_file.write_text(
            f'nss={nss_version}\nckbi={ckbi_version}\nstate=staged\n')
    else:
        print('WARNING: tests clone failed — skipping test update', file=sys.stderr)

    # ── clone dist-git ────────────────────────────────────────────────────────
    os.chdir(packages)

    if rhel_cacerts:
        print('>> fetching rhel ca-certificates')
        # Clone into packages/rhel/ca-certificates/ (plain clone, no -B).
        # The repo root at packages/rhel/ca-certificates/ is used to create
        # branch worktrees at packages/rhel/<branch>/ via git worktree add.
        rhel_repo = packages / 'rhel' / 'ca-certificates'
        if not rhel_repo.exists():
            r = subprocess.run(['rhpkg', '-q', 'clone', 'ca-certificates'],
                               cwd=packages / 'rhel')
            if r.returncode != 0 or not rhel_repo.exists():
                print('ERROR: rhpkg clone failed — check VPN/Kerberos and retry.',
                      file=sys.stderr)
                sys.exit(r.returncode or 1)
        _run(['git', 'fetch', '--all'], cwd=rhel_repo)

        # Read remote branch names from local tracking refs (no network call needed
        # after git fetch --all has populated them)
        r = subprocess.run(['git', 'branch', '-r'],
                           capture_output=True, text=True, cwd=rhel_repo)
        remote_branches = {
            line.strip().removeprefix('origin/')
            for line in r.stdout.splitlines()
            if line.strip().startswith('origin/') and '->' not in line
        }
        out.log(f'Found {len(remote_branches)} remote branches')

        for rel in [r for rels in rhel_by_major.values() for r in rels]:
            major         = safe_int(release_get_major(rel))
            m_rel         = re.match(r'^rhel-\d+\.(\d+)', rel)
            minor         = int(m_rel.group(1)) if m_rel else None

            # dist_branch override: use the remote branch name as both
            # the local branch and the worktree directory (e.g. rhel-8-main)
            explicit = cfg_dist_branch(major, minor) if minor is not None else None
            branch        = explicit if explicit else _distgit_branch(rel)
            worktree_path = packages / 'rhel' / branch

            if worktree_path.is_dir():
                out.log(f'  {branch}: worktree already exists')
                continue

            if explicit:
                out.log(f'  {branch}: using {explicit} directly (no dedicated branch)')
                _run(['git', 'worktree', 'add', '-B', branch,
                      str(worktree_path), f'origin/{explicit}'],
                     cwd=rhel_repo)
            elif branch not in remote_branches:
                if uses_centos_stream(major):
                    # For centos_stream majors, a missing branch means this is the
                    # current GA — it flows through centos-fork and has no dedicated
                    # dist-git branch.  Never create branches here; skip it.
                    out.log(f'  {branch}: not at origin — GA via centos-fork, skipping')
                    continue
                # Non-centos_stream major (e.g. RHEL 8): create the z-stream branch
                # from main.
                mb = cfg_main_branch(major, minor)
                print(f'>> creating remote branch {branch} from {mb}')
                _run(['git', 'checkout', mb], cwd=rhel_repo)
                _run(['git', 'push', 'origin', f'{mb}:{branch}'], cwd=rhel_repo)
                _run(['git', 'fetch', 'origin', branch], cwd=rhel_repo)
                _run(['git', 'worktree', 'add', '-B', branch,
                      str(worktree_path), f'origin/{branch}'],
                     cwd=rhel_repo)
            else:
                out.log(f'  {branch}: found at origin')
                _run(['git', 'worktree', 'add', '-B', branch,
                      str(worktree_path), f'origin/{branch}'],
                     cwd=rhel_repo)

            out.log(f'  {branch}: worktree ready')

        # reset the source folder branch
        _run(['git', 'checkout', 'master'], cwd=rhel_repo)

        if centos_list:
            centos_pkg = packages / 'centos'
            if not centos_pkg.exists():
                print('>> fetching centos ca-certificates')
                rc = subprocess.run(['centpkg', '-q', 'clone', '-B',
                                     'ca-certificates', 'centos'])
                if rc.returncode != 0 or not centos_pkg.exists():
                    print('ERROR: centpkg clone failed — check VPN/Kerberos and retry.',
                          file=sys.stderr)
                    sys.exit(rc.returncode or 1)
            # Resolve upstream URL from GitLab fork metadata once
            ca_upstream = ''
            if gl and centos_fork:
                ca_upstream = _gitlab_upstream_url(gl, centos_fork)
            if not ca_upstream:
                print('ERROR: could not determine upstream URL from GitLab — '
                      'check gitlab_api_key and centos_fork in config.cfg',
                      file=sys.stderr)
                sys.exit(1)

            fork_base = packages / 'centos-fork'
            fork_base.mkdir(parents=True, exist_ok=True)
            for version in centos_list:
                branch = f'c{version}s'
                branch_dir = fork_base / branch
                if not branch_dir.exists():
                    print(f'Cloning {branch} from {centos_fork}')
                    rc = subprocess.run(
                        ['git', 'clone',
                         '-c', 'url.git@gitlab.com:.insteadOf=https://gitlab.com/',
                         centos_fork, '-b', branch, branch],
                        cwd=fork_base)
                    if rc.returncode != 0 or not branch_dir.exists():
                        print(f'ERROR: git clone of centos-fork/{branch} failed.',
                              file=sys.stderr)
                        sys.exit(rc.returncode or 1)
                    _run(['git', 'remote', 'add', 'upstream', ca_upstream],
                         cwd=branch_dir)
                else:
                    out.log(f'  centos-fork/{branch}: already cloned')
                # Always sync with upstream — every run needs the latest content
                _run(['git', 'fetch', 'upstream'], cwd=branch_dir)
                _run(['git', 'pull', 'upstream', branch], cwd=branch_dir)
                _run(['git', 'push', 'origin', branch], cwd=branch_dir)

    if fedora_cacerts:
        fedora_clone_root = packages / 'fedora' / 'ca-certificates'
        if not fedora_clone_root.exists():
            print('>> fetching fedora ca-certificates')
            rc = subprocess.run(['fedpkg', '-q', 'clone', '-B', 'ca-certificates'],
                                cwd=packages / 'fedora')
            if rc.returncode != 0 or not fedora_clone_root.exists():
                print('ERROR: fedpkg clone failed — check VPN/Kerberos and retry.',
                      file=sys.stderr)
                sys.exit(rc.returncode or 1)
        # Move worktrees from packages/fedora/ca-certificates/<rel> → packages/fedora/<rel>
        fedora_base = packages / 'fedora'
        for rel in fedora:
            src = fedora_base / rel
            if src.is_dir():
                _run(['git', 'worktree', 'move', rel, str(packages / 'fedora' / rel)],
                     cwd=fedora_base)

    # ── modify certdata ───────────────────────────────────────────────────────
    os.chdir(SCRIPT_LOC)
    converter = str(SCRIPT_LOC / 'certdata-upstream-to-certdata-rhel.py')
    src_certdata = str(cacerts / 'certdata.txt')

    print('*' * 66)
    print('*' + ' Modifying certdata.txt for releases '.center(64) + '*')
    print('*' * 66)

    # certdata is identical across all RHEL majors and Fedora — generate once
    modified_certdata = modified / 'certdata.txt'
    print(' - Creating certdata.txt')
    _run(['python3', converter,
          '--input', src_certdata,
          '--output', str(modified_certdata)])

    # ── update packages ───────────────────────────────────────────────────────
    print('*' * 66)
    print('*' + ' Updating RHEL packages '.center(64) + '*')
    print('*' * 66)

    errors = 0
    rhel_list_file   = meta / 'rhel.list'
    fedora_list_file = meta / 'fedora.list'
    nssckbi = cacerts / 'nssckbi.h'

    def _update(releases, certdata_file, pkg_dir_fn, rz, rb, list_file, major):
        nonlocal errors
        for rel in releases:
            out.log(f'**** ca-certificates {rel} ****')
            out.update_row(rel, [rel, major, 'updating…'])
            rc = cacertificates_update(
                pkg_dir_fn(rel),
                certdata_file,
                nssckbi,
                nss_version, ckbi_version,
                scratch / rel.replace('/', '_'),
                rel, rz, rb,
                ga_releases, verbose)
            errors += rc
            set_list_state(list_file, rel, 'staged')
            out.update_row(rel, [rel, major, 'staged' if rc == 0 else 'error'])

    with out:
        out.set_subtitle(f'NSS {nss_version} · CKBI {ckbi_version}')

        # Generic loop over all RHEL majors — no hardcoded version numbers
        # centos-fork (GA releases) — driven by centos_list, independent of
        # whether the GA release appears in rhel_by_major.
        # Branch = cXs (the centos fork branch)
        for version in centos_list:
            branch = f'c{version}s'
            major  = int(version)
            pkg    = packages / 'centos-fork' / branch
            rz, rb = restart_releases(major)
            # Find the GA release name for display (the one with no rhel worktree)
            ga_rel = next(
                (r for r in rhel_by_major.get(major, [])
                 if not (packages / 'rhel' / _distgit_branch(r)).is_dir()),
                branch)
            out.log(f'**** ca-certificates {ga_rel} via centos-fork/{branch} ****')
            out.update_row(ga_rel, [ga_rel, branch, 'updating…'])
            rc = cacertificates_update(
                pkg, modified_certdata,
                nssckbi, nss_version, ckbi_version,
                scratch / branch,
                ga_rel, rz, rb, ga_releases, verbose)
            errors += rc
            set_list_state(rhel_list_file, ga_rel, 'staged')
            set_list_branch(rhel_list_file, ga_rel, branch)   # c9s / c10s
            out.update_row(ga_rel, [ga_rel, branch,
                                    'staged' if rc == 0 else 'error'])

        # RHEL z-stream worktrees
        for major, releases in sorted(rhel_by_major.items()):
            rz, rb = restart_releases(major)
            for rel in releases:
                _m = re.match(r'^rhel-(\d+)\.(\d+)', rel)
                _minor = int(_m.group(2)) if _m else None
                _explicit = cfg_dist_branch(major, _minor) if _minor is not None else None
                # Worktree dir = dist_branch name if overridden, else _distgit_branch
                dist_branch_name = _explicit or _distgit_branch(rel)
                rhel_worktree = packages / 'rhel' / dist_branch_name
                if not rhel_worktree.is_dir():
                    out.log(f'  {rel}: no rhel worktree (GA via centos-fork) — skipping')
                    continue
                out.log(f'**** ca-certificates {rel} (branch={dist_branch_name}) ****')
                out.update_row(rel, [rel, dist_branch_name, 'updating…'])
                rc = cacertificates_update(
                    rhel_worktree, modified_certdata,
                    nssckbi, nss_version, ckbi_version,
                    scratch / rel.replace('/', '_'),
                    rel, rz, rb, ga_releases, verbose)
                errors += rc
                set_list_state(rhel_list_file, rel, 'staged')
                set_list_branch(rhel_list_file, rel, dist_branch_name)
                out.update_row(rel, [rel, dist_branch_name,
                                     'staged' if rc == 0 else 'error'])

        fz, fb = fedora_restart_releases()
        _update(fedora, modified_certdata,
                lambda r: packages / 'fedora' / r, fz, fb,
                fedora_list_file, 'fedora')

        out.log(f'Finished updates for ca-certificates {ckbi_version} '
                f'from NSS {nss_version} with {errors} errors')

    os.chdir(SCRIPT_LOC)
    print('The following directories are ready for checkin:')
    for checkin in sorted(packages.rglob('checkin.log')):
        rel_path = checkin.parent.relative_to(packages)
        print(f'  {rel_path}  ({checkin.parent})')


if __name__ == '__main__':
    main()
