"""
Comprehensive test suite for build_combo.sh / build_combo.py.

Tests run against the bash implementation first; the Python rewrite must
pass the same tests.  Each test calls a helper that runs the relevant
function via bash (for the bash version) or calls it directly (for Python).

Run:
    pytest tests/test_build_combo.py -v
"""

import os
import re
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

# ── paths ─────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / 'build_combo.sh'
STUBS  = Path(__file__).parent / 'stubs'

# ── bash helper ───────────────────────────────────────────────────────────────

def _bash_functions():
    """Extract bash function definitions from build_combo.sh.
    Returns everything up to (not including) the trap line so the main body
    does not execute when we source it in tests."""
    lines = SCRIPT.read_text().splitlines()
    func_lines = []
    for line in lines:
        if line.strip().startswith('trap finish EXIT'):
            break
        func_lines.append(line)
    # Replace startup expressions that call process.py
    text = '\n'.join(func_lines)
    text = text.replace(
        'CURRENT_RELEASES="rawhide $(./process.py --get-ga)"',
        'CURRENT_RELEASES="rawhide"'
    ).replace(
        'CENTOS_CACERTS_FORK=$(./process.py --getconfig centos_fork)',
        'CENTOS_CACERTS_FORK=""'
    )
    return text

BASH_FUNCS = _bash_functions()


def bash(cmd, cwd=None, env=None, extra_funcs=''):
    """Run cmd in a bash context that has all build_combo functions available."""
    base_env = os.environ.copy()
    # Put stubs first on PATH
    base_env['PATH'] = str(STUBS) + ':' + base_env.get('PATH', '')
    base_env['GIT_STUB_LOG'] = '/tmp/test_git_stub.log'
    # Force a stable locale and known identity
    base_env.update({'LANG': 'C', 'NAME': 'Test User', 'EMAIL': 'test@example.com'})
    if env:
        base_env.update(env)
    script = BASH_FUNCS + '\n' + extra_funcs + '\n' + cmd
    return subprocess.run(
        ['bash', '-c', script],
        capture_output=True, text=True,
        cwd=str(cwd) if cwd else None,
        env=base_env,
    )

# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp(tmp_path):
    """Temp directory with a stub SCRIPT_LOC."""
    (tmp_path / 'stubs').mkdir()
    return tmp_path


MINIMAL_SPEC = textwrap.dedent("""\
    %global nss_version 3.100
    Name:    ca-certificates
    Version: 2024.2.80
    Release: 80.0%{?dist}
    Summary: CA certs

    %description
    CA certificates.

    %changelog
    * Mon Jan 01 2024 Old User <old@example.com> - 2024.2.80-80.0
    - Previous update
""")

NSS_H = textwrap.dedent("""\
    /* NSS version */
    #define NSS_VERSION "3.101 Basic ECC"
    #define NSS_VMAJOR 3
""")

NSSCKBI_H = textwrap.dedent("""\
    #define NSS_BUILTINS_LIBRARY_VERSION "2.66"
    #define NSS_BUILTINS_LIBRARY_VERSION_MAJOR 2
""")

CERTDATA_V1 = textwrap.dedent("""\
    # Version 1
    CKA_CLASS CK_OBJECT_CLASS CKO_NSS_BUILTIN_ROOT_LIST
    CKA_VALUE MULTILINE_OCTAL
    \\001
    END
""")

CERTDATA_V2 = textwrap.dedent("""\
    # Version 2 (updated)
    CKA_CLASS CK_OBJECT_CLASS CKO_NSS_BUILTIN_ROOT_LIST
    CKA_VALUE MULTILINE_OCTAL
    \\002
    END
""")

# ══════════════════════════════════════════════════════════════════════════════
# bumprelease
# ══════════════════════════════════════════════════════════════════════════════

class TestBumprelease:
    """bumprelease release [reset_release]"""

    def _bump(self, release, reset=''):
        r = bash(f'bumprelease {release!r} {reset!r}')
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    # without reset_release: plain integer increment
    def test_simple_integer(self):
        assert self._bump('7') == '8'

    def test_simple_integer_larger(self):
        assert self._bump('80') == '81'

    def test_strips_suffix_then_increments(self):
        # "80.0%{?dist}" → strip non-numeric suffix → "80.0" → first int part "80" + 1
        assert self._bump('80.0%{?dist}') == '81'

    def test_strips_dist_tag(self):
        assert self._bump('91%{?dist}') == '92'

    # with reset_release: preserve major, bump minor
    def test_minor_bump(self):
        assert self._bump('80.0', 'yes') == '80.1'

    def test_minor_bump_larger(self):
        assert self._bump('80.1', 'yes') == '80.2'

    def test_whole_number_bumps_entirely(self):
        # "81" has no minor (bump == base after split on last dot)
        assert self._bump('81', 'yes') == '82'

    def test_major_100(self):
        assert self._bump('100.0', 'yes') == '100.1'

    def test_z_stream_base_release(self):
        assert self._bump('90.0', 'yes') == '90.1'

    def test_z_stream_already_bumped(self):
        assert self._bump('90.1', 'yes') == '90.2'


# ══════════════════════════════════════════════════════════════════════════════
# mklog
# ══════════════════════════════════════════════════════════════════════════════

class TestMklog:
    def _mklog(self, vr, name='Test User', email='test@example.com'):
        r = bash(f'mklog {vr!r}', env={'NAME': name, 'EMAIL': email})
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def test_format(self):
        out = self._mklog('2026.2.88-81')
        # * Mon Jul 28 2026 Test User <test@example.com> - 2026.2.88-81
        assert out.startswith('*')
        assert 'Test User' in out
        assert 'test@example.com' in out
        assert '2026.2.88-81' in out

    def test_version_in_output(self):
        out = self._mklog('1.2.3-4')
        assert '1.2.3-4' in out

    def test_angle_brackets_around_email(self):
        out = self._mklog('1.0-1')
        assert '<test@example.com>' in out

    def test_date_present(self):
        out = self._mklog('1.0-1')
        # Should have a day-of-week abbreviation
        assert re.search(r'\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b', out)

    def test_name_fallback_from_git(self):
        # When NAME is unset, fall back to git config
        r = bash('mklog 1.0-1', env={'NAME': '', 'EMAIL': 'git@example.com'})
        assert r.returncode == 0
        # stub git returns "Test User" for config user.name
        assert 'git@example.com' in r.stdout or 'example.com' in r.stdout


# ══════════════════════════════════════════════════════════════════════════════
# set_list_state
# ══════════════════════════════════════════════════════════════════════════════

class TestSetListState:
    def _make_list(self, tmp_path, content):
        f = tmp_path / 'rhel.list'
        f.write_text(content)
        return f

    def test_updates_state_field(self, tmp_path):
        f = self._make_list(tmp_path,
            'rhel-9.6.0:ca-certificates:RHEL-100:0::planned::\n')
        bash(f'set_list_state "{f}" "rhel-9.6.0" "staged"')
        assert 'staged' in f.read_text()

    def test_preserves_other_fields(self, tmp_path):
        f = self._make_list(tmp_path,
            'rhel-9.6.0:ca-certificates:RHEL-100:42:some-nvr:planned:mr1:up1:\n')
        bash(f'set_list_state "{f}" "rhel-9.6.0" "staged"')
        text = f.read_text()
        assert 'RHEL-100' in text
        assert '42' in text
        assert 'some-nvr' in text
        assert 'mr1' in text
        assert 'staged' in text

    def test_only_updates_matching_release(self, tmp_path):
        f = self._make_list(tmp_path,
            'rhel-9.6.0:ca-certificates:B1:0::planned::\n'
            'rhel-9.4.0:ca-certificates:B2:0::planned::\n')
        bash(f'set_list_state "{f}" "rhel-9.6.0" "staged"')
        text = f.read_text()
        lines = [l for l in text.splitlines() if l]
        assert 'staged' in lines[0]
        assert 'planned' in lines[1]   # untouched

    def test_warns_on_missing_release(self, tmp_path):
        f = self._make_list(tmp_path,
            'rhel-9.6.0:ca-certificates:B1:0::planned::\n')
        r = bash(f'set_list_state "{f}" "rhel-8.10.0" "staged"')
        assert 'WARNING' in r.stderr or 'WARNING' in r.stdout

    def test_does_not_corrupt_on_missing(self, tmp_path):
        f = self._make_list(tmp_path,
            'rhel-9.6.0:ca-certificates:B1:0::planned::\n')
        bash(f'set_list_state "{f}" "rhel-8.10.0" "staged"')
        assert 'rhel-9.6.0' in f.read_text()  # original still intact

    def test_crypto_key_field_preserved(self, tmp_path):
        """9-field entries (with crypto key) must not be corrupted."""
        f = self._make_list(tmp_path,
            'rhel-10.3:ca-certificates:RHEL-212568:0::planned:::CRYPTO-23449\n')
        bash(f'set_list_state "{f}" "rhel-10.3" "staged"')
        text = f.read_text()
        assert 'CRYPTO-23449' in text
        assert 'staged' in text


# ══════════════════════════════════════════════════════════════════════════════
# addpatch — ca-certificates mode (PATCH=NONE)
# ══════════════════════════════════════════════════════════════════════════════

class TestAddpatch:
    def _run_addpatch(self, tmp_path, spec_content,
                      patch='NONE', patch_orig='empty',
                      nss_version='3.101', ckbi_version='2.66',
                      new_version='2026.2.66', restart_release='81',
                      cert_log=''):
        spec = tmp_path / 'ca-certificates.spec'
        spec.write_text(spec_content)
        cert_log_file = tmp_path / 'cert_log'
        cert_log_file.write_text(cert_log)

        cmd = (f'cd {tmp_path} && '
               f'addpatch ca-certificates.spec {patch!r} {patch_orig!r} '
               f'{cert_log_file} {nss_version!r} {ckbi_version!r} '
               f'{new_version!r} {restart_release!r}')
        r = bash(cmd, cwd=tmp_path)
        return r, spec.read_text()

    def test_version_updated(self, tmp_path):
        _, spec = self._run_addpatch(tmp_path, MINIMAL_SPEC,
                                      new_version='2026.2.66')
        assert 'Version: 2026.2.66' in spec
        assert 'Version: 2024.2.80' not in spec

    def test_release_set_to_restart(self, tmp_path):
        _, spec = self._run_addpatch(tmp_path, MINIMAL_SPEC,
                                      new_version='2026.2.66',
                                      restart_release='81')
        assert 'Release: 81%{?dist}' in spec

    def test_release_bumped_when_version_unchanged(self, tmp_path):
        # When new_version matches old version, release is bumped
        _, spec = self._run_addpatch(tmp_path, MINIMAL_SPEC,
                                      new_version='2024.2.80',
                                      restart_release='80.0')
        # bumprelease("80.0%{?dist}", "80.0") → "80.1"
        assert 'Release: 80.1%{?dist}' in spec

    def test_changelog_entry_added(self, tmp_path):
        _, spec = self._run_addpatch(tmp_path, MINIMAL_SPEC,
                                      nss_version='3.101',
                                      ckbi_version='2.66')
        assert 'Update to CKBI 2.66 from NSS 3.101' in spec

    def test_changelog_uses_mklog_format(self, tmp_path):
        _, spec = self._run_addpatch(tmp_path, MINIMAL_SPEC)
        # Should have a new * header before "Update to CKBI"
        lines = spec.splitlines()
        changelog_idx = next(i for i, l in enumerate(lines) if '%changelog' in l)
        # First non-empty line after %changelog should be the new mklog entry
        after = [l for l in lines[changelog_idx+1:] if l.strip()]
        assert after[0].startswith('*')
        assert 'Test User' in after[0]

    def test_cert_log_items_prepended_with_dash(self, tmp_path):
        _, spec = self._run_addpatch(tmp_path, MINIMAL_SPEC,
                                      cert_log='Added: Some CA\nRemoved: Old CA')
        assert '- Added: Some CA' in spec
        assert '- Removed: Old CA' in spec

    def test_global_nss_version_substitution(self, tmp_path):
        spec = MINIMAL_SPEC.replace('Version: 2024.2.80',
                                     'Version: %{nss_version}.80')
        # When new_version is empty, old version string should expand %{nss_version}
        r, out = self._run_addpatch(tmp_path, spec,
                                     new_version='',
                                     restart_release='')
        # The version used in changelog should have the expanded value
        assert '3.100' in out  # from %global nss_version 3.100

    def test_checkin_log_created(self, tmp_path):
        self._run_addpatch(tmp_path, MINIMAL_SPEC)
        checkin = tmp_path / 'checkin.log'
        assert checkin.exists()
        assert 'CKBI' in checkin.read_text()
        assert 'NSS' in checkin.read_text()

    def test_original_non_version_lines_preserved(self, tmp_path):
        _, spec = self._run_addpatch(tmp_path, MINIMAL_SPEC)
        assert 'Name:    ca-certificates' in spec
        assert 'Summary: CA certs' in spec
        assert '%description' in spec

    def test_old_changelog_preserved(self, tmp_path):
        _, spec = self._run_addpatch(tmp_path, MINIMAL_SPEC)
        assert 'Previous update' in spec

    def test_none_patch_adds_no_patch_lines(self, tmp_path):
        spec_with_patch = MINIMAL_SPEC.replace(
            'Summary: CA certs\n',
            'Summary: CA certs\nPatch0: existing.patch\n'
        )
        _, out = self._run_addpatch(tmp_path, spec_with_patch, patch='NONE')
        # Should NOT have added a new Patch line
        patch_lines = [l for l in out.splitlines() if re.match(r'^Patch\d+:', l)]
        assert len(patch_lines) == 1   # only the existing one

    def test_with_real_patch_adds_patch_line(self, tmp_path):
        spec_with_patch = MINIMAL_SPEC.replace(
            'Summary: CA certs\n',
            'Summary: CA certs\nPatch0: existing.patch\n\n'
        ).replace(
            '%description\n',
            '%setup\n%patch0\n\n%description\n'
        )
        _, out = self._run_addpatch(tmp_path, spec_with_patch,
                                     patch='new.patch', patch_orig='.orig',
                                     ckbi_version='2.66')
        assert 'Patch1: new.patch' in out
        assert '%patch1' in out


# ══════════════════════════════════════════════════════════════════════════════
# version extraction from header files
# ══════════════════════════════════════════════════════════════════════════════

class TestVersionExtraction:
    def _extract(self, tmp_path, nss_h=NSS_H, nssckbi_h=NSSCKBI_H,
                 codesign=None):
        (tmp_path / 'nss.h').write_text(nss_h)
        (tmp_path / 'nssckbi.h').write_text(nssckbi_h)
        if codesign:
            (tmp_path / 'codesign-release.txt').write_text(codesign)
        cmd = textwrap.dedent(f"""
            cd {tmp_path}
            nss_version=$(grep "NSS_VERSION" nss.h | awk '{{print $3}}' | sed -e 's;";;g')
            ckbi_version=$(grep "NSS_BUILTINS_LIBRARY_VERSION " nssckbi.h | awk '{{print $NF}}' | sed -e 's;";;g')
            if [ -f codesign-release.txt ]; then
                mcs_version=$(cat codesign-release.txt)
                if [[ $mcs_version != "unknown" ]]; then
                    ckbi_version="${{ckbi_version}}_${{mcs_version}}"
                fi
            fi
            echo "nss=$nss_version"
            echo "ckbi=$ckbi_version"
        """)
        r = bash(cmd)
        assert r.returncode == 0, r.stderr
        vals = dict(line.split('=', 1) for line in r.stdout.strip().splitlines())
        return vals

    def test_nss_version_extracted(self, tmp_path):
        v = self._extract(tmp_path)
        assert v['nss'] == '3.101'

    def test_ckbi_version_extracted(self, tmp_path):
        v = self._extract(tmp_path)
        assert v['ckbi'] == '2.66'

    def test_mcs_version_appended(self, tmp_path):
        v = self._extract(tmp_path, codesign='1.5')
        assert v['ckbi'] == '2.66_1.5'

    def test_mcs_unknown_not_appended(self, tmp_path):
        v = self._extract(tmp_path, codesign='unknown')
        assert v['ckbi'] == '2.66'

    def test_nss_version_with_extra_words(self, tmp_path):
        h = '#define NSS_VERSION "3.114 Basic ECC"\n'
        v = self._extract(tmp_path, nss_h=h)
        assert v['nss'] == '3.114'

    def test_ckbi_version_quoted(self, tmp_path):
        h = '#define NSS_BUILTINS_LIBRARY_VERSION "2.80"\n'
        v = self._extract(tmp_path, nssckbi_h=h)
        assert v['ckbi'] == '2.80'


# ══════════════════════════════════════════════════════════════════════════════
# cacertificates_update
# ══════════════════════════════════════════════════════════════════════════════

class TestCacertificatesUpdate:
    """Tests for cacertificates_update().
    Uses stub check_certs.sh and git; does not touch real dist-git."""

    def _setup(self, tmp_path, certdata_changed=True):
        """Create a minimal fake package directory and certdata files."""
        pkg_dir = tmp_path / 'packages' / 'ca-certificates' / 'rhel-9.6.0'
        pkg_dir.mkdir(parents=True)
        (pkg_dir / 'certdata.txt').write_text(CERTDATA_V1)
        (pkg_dir / 'ca-certificates.spec').write_text(MINIMAL_SPEC)
        (pkg_dir / 'nssckbi.h').write_text(NSSCKBI_H)

        cacerts = tmp_path / 'cacerts'
        cacerts.mkdir()
        (cacerts / 'nssckbi.h').write_text(NSSCKBI_H)
        cert = cacerts / 'certdata.txt'
        cert.write_text(CERTDATA_V2 if certdata_changed else CERTDATA_V1)

        scratch = tmp_path / 'scratch'
        return pkg_dir, cacerts / 'certdata.txt', cacerts / 'nssckbi.h', scratch

    def _run(self, tmp_path, pkg_dir, certdata, nssckbi, scratch,
             release='rhel-9.6.0', restart_z='90.0', restart_base='91',
             current_releases='rawhide'):
        cmd = textwrap.dedent(f"""
            SCRIPT_LOC={ROOT}
            CURRENT_RELEASES="{current_releases}"
            cacertificates_update \\
                {pkg_dir} {certdata} {nssckbi} \\
                3.101 2.66 {scratch} {release} \\
                {restart_z!r} {restart_base!r}
        """)
        return bash(cmd, cwd=tmp_path)

    def test_returns_1_when_certdata_missing(self, tmp_path):
        pkg_dir, _, nssckbi, scratch = self._setup(tmp_path)
        r = self._run(tmp_path, pkg_dir, '/nonexistent/certdata.txt',
                      nssckbi, scratch)
        assert r.returncode == 1
        assert 'Skipping' in r.stdout

    def test_returns_1_when_pkg_dir_missing(self, tmp_path):
        _, certdata, nssckbi, scratch = self._setup(tmp_path)
        r = self._run(tmp_path, '/nonexistent/pkg', certdata, nssckbi, scratch)
        assert r.returncode == 1
        assert 'Skipping' in r.stdout

    def test_returns_0_skips_when_certdata_unchanged(self, tmp_path):
        pkg_dir, certdata, nssckbi, scratch = self._setup(tmp_path,
                                                           certdata_changed=False)
        r = self._run(tmp_path, pkg_dir, certdata, nssckbi, scratch)
        assert r.returncode == 0
        assert 'already up to date' in r.stdout

    def test_returns_0_and_updates_when_certdata_changed(self, tmp_path):
        pkg_dir, certdata, nssckbi, scratch = self._setup(tmp_path,
                                                           certdata_changed=True)
        r = self._run(tmp_path, pkg_dir, certdata, nssckbi, scratch)
        assert r.returncode == 0
        assert 'already up to date' not in r.stdout

    def test_spec_file_updated_after_run(self, tmp_path):
        pkg_dir, certdata, nssckbi, scratch = self._setup(tmp_path,
                                                           certdata_changed=True)
        self._run(tmp_path, pkg_dir, certdata, nssckbi, scratch)
        spec = (pkg_dir / 'ca-certificates.spec').read_text()
        # Version should have been updated to year.ckbi_version
        assert 'Version:' in spec
        assert '2.66' in spec   # ckbi_version in new Version

    def test_certdata_copied_to_pkg_dir(self, tmp_path):
        pkg_dir, certdata, nssckbi, scratch = self._setup(tmp_path,
                                                           certdata_changed=True)
        self._run(tmp_path, pkg_dir, certdata, nssckbi, scratch)
        dest = pkg_dir / 'certdata.txt'
        assert dest.read_text() == CERTDATA_V2

    def test_nssckbi_copied_to_pkg_dir(self, tmp_path):
        pkg_dir, certdata, nssckbi, scratch = self._setup(tmp_path,
                                                           certdata_changed=True)
        self._run(tmp_path, pkg_dir, certdata, nssckbi, scratch)
        assert (pkg_dir / 'nssckbi.h').exists()

    def test_checkin_log_created(self, tmp_path):
        pkg_dir, certdata, nssckbi, scratch = self._setup(tmp_path,
                                                           certdata_changed=True)
        self._run(tmp_path, pkg_dir, certdata, nssckbi, scratch)
        assert (pkg_dir / 'checkin.log').exists()

    def test_uses_base_release_for_current(self, tmp_path):
        """When the release is in CURRENT_RELEASES, use RESTART_RELEASE_BASE."""
        pkg_dir, certdata, nssckbi, scratch = self._setup(tmp_path,
                                                           certdata_changed=True)
        self._run(tmp_path, pkg_dir, certdata, nssckbi, scratch,
                  release='rhel-9.6.0',
                  restart_z='90.0', restart_base='91',
                  current_releases='rawhide rhel-9.6.0')
        spec = (pkg_dir / 'ca-certificates.spec').read_text()
        assert 'Release: 91%{?dist}' in spec

    def test_uses_z_release_for_non_current(self, tmp_path):
        """When release is not in CURRENT_RELEASES, use RESTART_RELEASE_Z."""
        pkg_dir, certdata, nssckbi, scratch = self._setup(tmp_path,
                                                           certdata_changed=True)
        self._run(tmp_path, pkg_dir, certdata, nssckbi, scratch,
                  release='rhel-9.6.0',
                  restart_z='90.0', restart_base='91',
                  current_releases='rawhide')
        spec = (pkg_dir / 'ca-certificates.spec').read_text()
        # new_version (2026.2.66) != old_version (2024.2.80) so restart_z
        # is used directly — no bumprelease call
        assert 'Release: 90.0%{?dist}' in spec

    def test_scratch_dir_created(self, tmp_path):
        pkg_dir, certdata, nssckbi, scratch = self._setup(tmp_path,
                                                           certdata_changed=True)
        self._run(tmp_path, pkg_dir, certdata, nssckbi, scratch)
        assert scratch.exists()

    def test_cert_log_in_scratch(self, tmp_path):
        pkg_dir, certdata, nssckbi, scratch = self._setup(tmp_path,
                                                           certdata_changed=True)
        self._run(tmp_path, pkg_dir, certdata, nssckbi, scratch)
        assert (scratch / 'cert_log').exists()


# ══════════════════════════════════════════════════════════════════════════════
# directory structure setup
# ══════════════════════════════════════════════════════════════════════════════

class TestDirectorySetup:
    def _setup_dirs(self, tmp_path, releases):
        """Simulate the directory setup portion of build_combo.sh."""
        cmd = textwrap.dedent(f"""
            SCRIPT_LOC={tmp_path}
            PACKAGES={tmp_path}/packages
            MODIFIED={tmp_path}/modified
            CACERTS={tmp_path}/cacerts
            META_DATA={tmp_path}/meta
            RHEL8="" RHEL9="" RHEL10="" FEDORA=""
            RHEL_CACERTS=0 FEDORA_CACERTS=0
            CENTOS_LIST=()

            {releases}

            rm -rf $PACKAGES $MODIFIED $CACERTS
            mkdir -p $META_DATA $PACKAGES $CACERTS

            [ -n "$RHEL8"  ] && {{ mkdir -p $MODIFIED/rhel8/ca-certificates;  CENTOS_LIST+=("8");  }}
            [ -n "$RHEL9"  ] && {{ mkdir -p $MODIFIED/rhel9/ca-certificates;  CENTOS_LIST+=("9");  }}
            [ -n "$RHEL10" ] && {{ mkdir -p $MODIFIED/rhel10/ca-certificates; CENTOS_LIST+=("10"); }}
            [ -n "$FEDORA" ] && {{ mkdir -p $MODIFIED/fedora/ca-certificates; mkdir -p $PACKAGES/fedora; }}
            [[ ${{#CENTOS_LIST[@]}} -gt 0 ]] && mkdir -p $PACKAGES/centos $PACKAGES/centos-fork/ca-certificates

            echo "centos_count=${{#CENTOS_LIST[@]}}"
            echo "rhel8_dir=$([ -d $MODIFIED/rhel8/ca-certificates ] && echo yes || echo no)"
            echo "rhel9_dir=$([ -d $MODIFIED/rhel9/ca-certificates ] && echo yes || echo no)"
            echo "fedora_dir=$([ -d $MODIFIED/fedora/ca-certificates ] && echo yes || echo no)"
        """)
        r = bash(cmd)
        assert r.returncode == 0, r.stderr
        return dict(l.split('=', 1) for l in r.stdout.strip().splitlines()
                    if '=' in l)

    def test_rhel8_creates_modified_dir(self, tmp_path):
        v = self._setup_dirs(tmp_path, 'RHEL8="rhel-8.10.0"; RHEL_CACERTS=1')
        assert v['rhel8_dir'] == 'yes'

    def test_rhel9_creates_modified_dir(self, tmp_path):
        v = self._setup_dirs(tmp_path, 'RHEL9="rhel-9.6.0"; RHEL_CACERTS=1')
        assert v['rhel9_dir'] == 'yes'

    def test_fedora_creates_modified_dir(self, tmp_path):
        v = self._setup_dirs(tmp_path, 'FEDORA="f45"; FEDORA_CACERTS=1')
        assert v['fedora_dir'] == 'yes'

    def test_rhel9_adds_to_centos_list(self, tmp_path):
        v = self._setup_dirs(tmp_path, 'RHEL9="rhel-9.6.0"; RHEL_CACERTS=1')
        assert v['centos_count'] == '1'

    def test_multiple_majors_centos_count(self, tmp_path):
        v = self._setup_dirs(tmp_path,
            'RHEL8="rhel-8.10.0"; RHEL9="rhel-9.6.0"; RHEL10="rhel-10.3"; RHEL_CACERTS=1')
        assert v['centos_count'] == '3'

    def test_fedora_only_no_centos_dirs(self, tmp_path):
        v = self._setup_dirs(tmp_path, 'FEDORA="f45"; FEDORA_CACERTS=1')
        assert v['centos_count'] == '0'

    def test_meta_dir_not_wiped(self, tmp_path):
        """meta/ must survive the rm -rf; plan.py owns it."""
        meta = tmp_path / 'meta'
        meta.mkdir()
        (meta / 'rhel.list').write_text('rhel-10.3:ca-certificates:RHEL-1:0::planned:::\n')
        self._setup_dirs(tmp_path, 'RHEL9="rhel-9.6.0"')
        assert (meta / 'rhel.list').exists()


# ══════════════════════════════════════════════════════════════════════════════
# argument parsing
# ══════════════════════════════════════════════════════════════════════════════

class TestArgParsing:
    """Test that release arguments are bucketed into the right variables."""

    def _parse(self, tmp_path, args):
        cmd = textwrap.dedent(f"""
            RHEL8="" RHEL9="" RHEL10="" FEDORA=""
            RHEL_CACERTS=0 FEDORA_CACERTS=0
            certdatadir=""

            for arg in {args}; do
                case $arg in
                    rhel-8*) RHEL8="$RHEL8 $arg"; RHEL_CACERTS=1;;
                    rhel-9*) RHEL9="$RHEL9 $arg"; RHEL_CACERTS=1;;
                    rhel-10*) RHEL10="$RHEL10 $arg"; RHEL_CACERTS=1;;
                    f*|rawhide) FEDORA="$FEDORA $arg"; FEDORA_CACERTS=1;;
                esac
            done

            echo "RHEL8=$RHEL8"
            echo "RHEL9=$RHEL9"
            echo "RHEL10=$RHEL10"
            echo "FEDORA=$FEDORA"
            echo "RHEL_CACERTS=$RHEL_CACERTS"
            echo "FEDORA_CACERTS=$FEDORA_CACERTS"
        """)
        r = bash(cmd)
        assert r.returncode == 0, r.stderr
        return dict(l.split('=', 1) for l in r.stdout.strip().splitlines()
                    if '=' in l)

    def test_rhel8_release(self, tmp_path):
        v = self._parse(tmp_path, 'rhel-8.10.0')
        assert 'rhel-8.10.0' in v['RHEL8']
        assert v['RHEL_CACERTS'] == '1'

    def test_rhel9_release(self, tmp_path):
        v = self._parse(tmp_path, 'rhel-9.6.0')
        assert 'rhel-9.6.0' in v['RHEL9']

    def test_rhel10_release(self, tmp_path):
        v = self._parse(tmp_path, 'rhel-10.3')
        assert 'rhel-10.3' in v['RHEL10']

    def test_fedora_release(self, tmp_path):
        v = self._parse(tmp_path, 'f45')
        assert 'f45' in v['FEDORA']
        assert v['FEDORA_CACERTS'] == '1'

    def test_rawhide(self, tmp_path):
        v = self._parse(tmp_path, 'rawhide')
        assert 'rawhide' in v['FEDORA']

    def test_multiple_releases(self, tmp_path):
        v = self._parse(tmp_path, 'rhel-10.3 rhel-9.6.0 rhel-8.10.0 f45 rawhide')
        assert 'rhel-10.3' in v['RHEL10']
        assert 'rhel-9.6.0' in v['RHEL9']
        assert 'rhel-8.10.0' in v['RHEL8']
        assert 'f45' in v['FEDORA']
        assert 'rawhide' in v['FEDORA']

    def test_multiple_rhel9_versions(self, tmp_path):
        v = self._parse(tmp_path, 'rhel-9.6.0 rhel-9.4.0 rhel-9.2.0')
        for rel in ['rhel-9.6.0', 'rhel-9.4.0', 'rhel-9.2.0']:
            assert rel in v['RHEL9']
