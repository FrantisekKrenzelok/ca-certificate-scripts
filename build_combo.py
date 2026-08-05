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

from caupdate.tui import PipelineOutput
from caupdate.release_config import uses_centos_stream
from caupdate.prereqs import check_prereqs


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
    """Update the state (field 6) of a release line in rhel.list/fedora.list.
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
            for entry in cert_log.splitlines():
                if entry.strip():
                    out.append(f'- {entry}\n')
            out.append('\n')
            continue

        out.append(line)

    spec_path.write_text(''.join(out))

    # write checkin.log
    checkin = spec_path.parent / 'checkin.log'
    with checkin.open('w') as f:
        f.write(f'Update to CKBI {ckbi_version} from NSS {nss_version}\n')
        f.write(cert_log)


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
                           current_releases: list[str],
                           verbose: bool = True) -> int:
    """Update a ca-certificates dist-git checkout.  Returns 0 on success."""

    if not certdata.is_file():
        print(f'!!!Skipping ca-certificates build for {release}. '
              f'no certdata.txt generated')
        return 1

    if not pkg_dir.is_dir():
        print(f'!!!Skipping ca-certificates build for {release}. '
              f'no git repository found')
        return 1

    restart_release = (restart_release_base
                       if release in current_releases
                       else restart_release_z)

    scratch.mkdir(parents=True, exist_ok=True)

    # diff certdata
    old_certdata = pkg_dir / 'certdata.txt'
    cert_log     = scratch / 'cert_log'
    _run([str(SCRIPT_LOC / 'check_certs.sh'),
          str(old_certdata), str(certdata)],
         stdout=cert_log, cwd=pkg_dir)

    if old_certdata.read_bytes() == certdata.read_bytes():
        print(f'Skipping ca-certificates build for {release}. '
              f'certdata is already up to date')
        return 0

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
    _run(['git', 'add', 'ca-certificates.spec', 'nssckbi.h', 'certdata.txt'],
         cwd=pkg_dir)
    if verbose:
        _run(['git', 'status'], cwd=pkg_dir)

    return 0


# ── helpers ───────────────────────────────────────────────────────────────────

def _run(cmd, cwd=None, stdout=None):
    if stdout is not None:
        with open(stdout, 'w') as f:
            subprocess.run(cmd, cwd=cwd, stdout=f,
                           stderr=subprocess.DEVNULL, check=False)
    else:
        subprocess.run(cmd, cwd=cwd, check=False)


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


# ── main ──────────────────────────────────────────────────────────────────────

def build_current_releases() -> list[str]:
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
    out.set_columns(['Release', 'Major', 'State'])

    meta = SCRIPT_LOC / 'meta'

    # ── collect releases — from meta lists or explicit CLI args ───────────────
    rhel8, rhel9, rhel10, fedora = [], [], [], []
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
        if rel.startswith('rhel-8'):
            rhel8.append(rel); rhel_cacerts = True
        elif rel.startswith('rhel-9'):
            rhel9.append(rel); rhel_cacerts = True
        elif rel.startswith('rhel-10'):
            rhel10.append(rel); rhel_cacerts = True
        elif re.match(r'^f\d+$|^rawhide$', rel):
            fedora.append(rel); fedora_cacerts = True
        else:
            print(f'Unknown release: {rel}', file=sys.stderr)
            sys.exit(1)

    current_releases = build_current_releases()
    centos_fork = subprocess.run(
        ['python3', str(SCRIPT_LOC / 'process.py'), '--getconfig', 'centos_fork'],
        capture_output=True, text=True, cwd=SCRIPT_LOC).stdout.strip()

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
    cacerts.mkdir()

    centos_list = []
    if rhel8:
        (modified / 'rhel8').mkdir(parents=True)
        if uses_centos_stream(8):
            centos_list.append('8')
    if rhel9:
        (modified / 'rhel9').mkdir(parents=True)
        if uses_centos_stream(9):
            centos_list.append('9')
    if rhel10:
        (modified / 'rhel10').mkdir(parents=True)
        if uses_centos_stream(10):
            centos_list.append('10')
    if fedora:
        (modified / 'fedora').mkdir(parents=True)
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
        if args.d:
            base_url = NSS_DEV_URL
        elif args.n:
            rel_tag = args.n.replace('.', '_')
            base_url = (f'https://hg.mozilla.org/projects/nss/raw-file/'
                        f'NSS_{rel_tag}_{args.t}/lib')
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
    (meta / 'mcsversion.txt').write_text(
        codesign_file.read_text().strip() if codesign_file.exists() else '')
    (meta / 'ckbiversion.txt').write_text(ckbi_version)

    # ── clone dist-git ────────────────────────────────────────────────────────
    os.chdir(packages)

    if rhel_cacerts:
        print('>> fetching rhel ca-certificates')
        _run(['rhpkg', '-q', 'clone', '-B', 'ca-certificates', 'rhel'])

        # ── create remote z-stream branches from major-main ───────────────────
        # For z-stream releases on CentOS Stream majors, ensure the branch
        # exists at origin, creating it from major-main if needed, then add
        # a local worktree so the release dir exists alongside the others.
        rhel_git = packages / 'rhel'
        for rel in rhel9 + rhel10:
            major = safe_int(release_get_major(rel))
            if not uses_centos_stream(major):
                continue

            main_branch = f'rhel{major}-main'
            worktree_path = rhel_git / rel

            if worktree_path.is_dir():
                continue

            # Check if branch exists at origin
            r = subprocess.run(
                ['git', 'ls-remote', '--heads', 'origin', rel],
                capture_output=True, text=True, cwd=rhel_git)
            branch_exists = bool(r.stdout.strip())

            if not branch_exists:
                print(f'>> creating remote branch {rel} from {main_branch}')
                _run(['git', 'push', 'origin',
                      f'refs/heads/{main_branch}:refs/heads/{rel}'],
                     cwd=rhel_git)

            # Fetch and add local worktree
            _run(['git', 'fetch', 'origin', rel], cwd=rhel_git)
            _run(['git', 'worktree', 'add', str(worktree_path), rel],
                 cwd=rhel_git)
            out.log(f'  {rel}: worktree ready at packages/rhel/{rel}')

        if centos_list:
            print('>> fetching centos ca-certificates')
            _run(['centpkg', '-q', 'clone', '-B', 'ca-certificates', 'centos'])
            # Get upstream URL from the centos git repo root (before moving worktrees)
            r = subprocess.run(['git', 'config', '--get', 'remote.origin.url'],
                               capture_output=True, text=True,
                               cwd=packages / 'centos')
            ca_upstream = r.stdout.strip()

            fork_base = packages / 'centos-fork'
            fork_base.mkdir(parents=True)
            for version in centos_list:
                branch = f'c{version}s'
                print(f'Cloning {branch} from {centos_fork}')
                _run(['git', 'clone',
                      '-c', 'url.git@gitlab.com:.insteadOf=https://gitlab.com/',
                      centos_fork, '-b', branch, branch],
                     cwd=fork_base)
                branch_dir = fork_base / branch
                if not branch_dir.is_dir():
                    print(f'Folder {branch} not found')
                    continue
                _run(['git', 'remote', 'add', 'upstream', ca_upstream], cwd=branch_dir)
                _run(['git', 'fetch', 'upstream'], cwd=branch_dir)
                _run(['git', 'pull', 'upstream', branch], cwd=branch_dir)
                _run(['git', 'push', 'origin', branch], cwd=branch_dir)
                _run(['git', 'checkout', '-b', branch, f'origin/{branch}'],
                     cwd=branch_dir)
                _run(['git', 'branch', '-u', f'upstream/{branch}'], cwd=branch_dir)

    if fedora_cacerts:
        print('>> fetching fedora ca-certificates')
        _run(['fedpkg', '-q', 'clone', '-B', 'ca-certificates'],
             cwd=packages / 'fedora')
        # Move worktrees from packages/fedora/ca-certificates/<rel> → packages/fedora/<rel>
        fedora_ca_git = packages / 'fedora'
        for rel in fedora:
            src = fedora_ca_git / rel
            if src.is_dir():
                _run(['git', 'worktree', 'move', rel, str(packages / 'fedora' / rel)],
                     cwd=fedora_ca_git)

    # ── modify certdata ───────────────────────────────────────────────────────
    os.chdir(SCRIPT_LOC)
    converter = str(SCRIPT_LOC / 'certdata-upstream-to-certdata-rhel.py')
    src_certdata = str(cacerts / 'certdata.txt')

    print('*' * 66)
    print('*' + ' Modifying certdata.txt for releases '.center(64) + '*')
    print('*' * 66)

    for maj, dest in [('fedora', modified / 'fedora'),
                      ('rhel10', modified / 'rhel10'),
                      ('rhel9',  modified / 'rhel9'),
                      ('rhel8',  modified / 'rhel8')]:
        rel_list = {'fedora': fedora, 'rhel10': rhel10,
                    'rhel9': rhel9, 'rhel8': rhel8}[maj]
        if rel_list:
            print(f' - Creating {maj.upper()} certdata.txt')
            _run(['python3', converter,
                  '--input', src_certdata,
                  '--output', str(dest / 'certdata.txt')])

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
                current_releases, verbose)
            errors += rc
            set_list_state(list_file, rel, 'staged')
            out.update_row(rel, [rel, major, 'staged' if rc == 0 else 'error'])

    with out:
        out.set_subtitle(f'NSS {nss_version} · CKBI {ckbi_version}')

        _update(rhel8, modified / 'rhel8' / 'certdata.txt',
                lambda r: packages / 'rhel' / r, '80.0', '81',
                rhel_list_file, 'rhel-8')

        for rel in rhel9:
            out.log(f'**** ca-certificates {rel} ****')
            out.update_row(rel, [rel, 'rhel-9', 'updating…'])
            if uses_centos_stream(9) and rel in current_releases:
                pkg = packages / 'centos-fork' / 'c9s'
            else:
                pkg = packages / 'rhel' / rel
            rc = cacertificates_update(
                pkg, modified / 'rhel9' / 'certdata.txt',
                nssckbi, nss_version, ckbi_version,
                scratch / rel.replace('/', '_'),
                rel, '90.0', '91', current_releases, verbose)
            errors += rc
            set_list_state(rhel_list_file, rel, 'staged')
            out.update_row(rel, [rel, 'rhel-9', 'staged' if rc == 0 else 'error'])

        for rel in rhel10:
            out.log(f'**** ca-certificates {rel} ****')
            out.update_row(rel, [rel, 'rhel-10', 'updating…'])
            if uses_centos_stream(10) and rel in current_releases:
                pkg = packages / 'centos-fork' / 'c10s'
            else:
                pkg = packages / 'rhel' / rel
            rc = cacertificates_update(
                pkg, modified / 'rhel10' / 'certdata.txt',
                nssckbi, nss_version, ckbi_version,
                scratch / rel.replace('/', '_'),
                rel, '100.0', '101', current_releases, verbose)
            errors += rc
            set_list_state(rhel_list_file, rel, 'staged')
            out.update_row(rel, [rel, 'rhel-10', 'staged' if rc == 0 else 'error'])

        _update(fedora, modified / 'fedora' / 'certdata.txt',
                lambda r: packages / 'fedora' / r, '1.0', '2',
                fedora_list_file, 'fedora')

        out.log(f'Finished updates for ca-certificates {ckbi_version} '
                f'from NSS {nss_version} with {errors} errors')

    os.chdir(SCRIPT_LOC)
    print('The following directories are ready for checkin:')
    for checkin in packages.rglob('checkin.log'):
        print(str(checkin.parent))


if __name__ == '__main__':
    main()
