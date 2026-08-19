"""
Same tests as test_build_combo.py but exercising the Python implementation
(build_combo.py) directly — no subprocess overhead.

These tests must pass identically to the bash tests.
"""

import os
import re
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import build_combo as bc

STUBS = Path(__file__).parent / 'stubs'

# ── fixtures / spec data (mirrored from bash test file) ───────────────────────

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
# bump_release
# ══════════════════════════════════════════════════════════════════════════════

class TestBumpreleaseP:
    def test_simple_integer(self):
        assert bc.bump_release('7') == '8'

    def test_simple_integer_larger(self):
        assert bc.bump_release('80') == '81'

    def test_strips_suffix_then_increments(self):
        assert bc.bump_release('80.0%{?dist}') == '81'

    def test_strips_dist_tag(self):
        assert bc.bump_release('91%{?dist}') == '92'

    def test_minor_bump(self):
        assert bc.bump_release('80.0', 'yes') == '80.1'

    def test_minor_bump_larger(self):
        assert bc.bump_release('80.1', 'yes') == '80.2'

    def test_whole_number_bumps_entirely(self):
        assert bc.bump_release('81', 'yes') == '82'

    def test_major_100(self):
        assert bc.bump_release('100.0', 'yes') == '100.1'

    def test_z_stream_base_release(self):
        assert bc.bump_release('90.0', 'yes') == '90.1'

    def test_z_stream_already_bumped(self):
        assert bc.bump_release('90.1', 'yes') == '90.2'


# ══════════════════════════════════════════════════════════════════════════════
# make_log
# ══════════════════════════════════════════════════════════════════════════════

class TestMakelogP:
    def test_format(self):
        out = bc.make_log('2026.2.88-81', 'Test User', 'test@example.com')
        assert out.startswith('*')
        assert 'Test User' in out
        assert 'test@example.com' in out
        assert '2026.2.88-81' in out

    def test_version_in_output(self):
        out = bc.make_log('1.2.3-4', 'A', 'a@b.com')
        assert '1.2.3-4' in out

    def test_angle_brackets_around_email(self):
        out = bc.make_log('1.0-1', 'A', 'test@example.com')
        assert '<test@example.com>' in out

    def test_date_present(self):
        out = bc.make_log('1.0-1', 'A', 'a@b.com')
        assert re.search(r'\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b', out)


# ══════════════════════════════════════════════════════════════════════════════
# set_list_state
# ══════════════════════════════════════════════════════════════════════════════

class TestSetListStateP:
    def _make(self, tmp_path, content):
        f = tmp_path / 'rhel.list'
        f.write_text(content)
        return f

    def test_updates_state_field(self, tmp_path):
        f = self._make(tmp_path,
            'rhel-9.6.0::RHEL-100:0::planned::\n')
        bc.set_list_state(f, 'rhel-9.6.0', 'staged')
        assert 'staged' in f.read_text()

    def test_preserves_other_fields(self, tmp_path):
        f = self._make(tmp_path,
            'rhel-9.6.0::RHEL-100:42:some-nvr:planned:mr1:up1:\n')
        bc.set_list_state(f, 'rhel-9.6.0', 'staged')
        text = f.read_text()
        for token in ('RHEL-100', '42', 'some-nvr', 'mr1', 'staged'):
            assert token in text

    def test_only_updates_matching_release(self, tmp_path):
        f = self._make(tmp_path,
            'rhel-9.6.0::B1:0::planned::\n'
            'rhel-9.4.0::B2:0::planned::\n')
        bc.set_list_state(f, 'rhel-9.6.0', 'staged')
        lines = [l for l in f.read_text().splitlines() if l]
        assert 'staged'  in lines[0]
        assert 'planned' in lines[1]

    def test_warns_on_missing_release(self, tmp_path, capsys):
        f = self._make(tmp_path,
            'rhel-9.6.0::B1:0::planned::\n')
        bc.set_list_state(f, 'rhel-8.10.0', 'staged')
        assert 'WARNING' in capsys.readouterr().err

    def test_does_not_corrupt_on_missing(self, tmp_path):
        f = self._make(tmp_path,
            'rhel-9.6.0::B1:0::planned::\n')
        bc.set_list_state(f, 'rhel-8.10.0', 'staged')
        assert 'rhel-9.6.0' in f.read_text()

    def test_crypto_key_field_preserved(self, tmp_path):
        f = self._make(tmp_path,
            'rhel-10.3::RHEL-212568:0::planned:::CRYPTO-23449\n')
        bc.set_list_state(f, 'rhel-10.3', 'staged')
        text = f.read_text()
        assert 'CRYPTO-23449' in text
        assert 'staged' in text


# ══════════════════════════════════════════════════════════════════════════════
# extract_version
# ══════════════════════════════════════════════════════════════════════════════

class TestExtractVersionP:
    def test_nss_version_extracted(self):
        assert bc.extract_version(NSS_H, 'NSS_VERSION') == '3.101'

    def test_ckbi_version_extracted(self):
        assert bc.extract_version(NSSCKBI_H, 'NSS_BUILTINS_LIBRARY_VERSION') == '2.66'

    def test_nss_version_with_extra_words(self):
        h = '#define NSS_VERSION "3.114 Basic ECC"\n'
        assert bc.extract_version(h, 'NSS_VERSION') == '3.114'

    def test_ckbi_version_quoted(self):
        h = '#define NSS_BUILTINS_LIBRARY_VERSION "2.80"\n'
        assert bc.extract_version(h, 'NSS_BUILTINS_LIBRARY_VERSION') == '2.80'


# ══════════════════════════════════════════════════════════════════════════════
# add_patch
# ══════════════════════════════════════════════════════════════════════════════

class TestAddpatchP:
    def _run(self, tmp_path, spec_content,
             patch='NONE', patch_orig='empty',
             nss_version='3.101', ckbi_version='2.66',
             new_version='2026.2.66', restart_release='81',
             cert_log=''):
        spec = tmp_path / 'ca-certificates.spec'
        spec.write_text(spec_content)
        cert_log_file = tmp_path / 'cert_log'
        cert_log_file.write_text(cert_log)
        bc.add_patch(spec, patch, patch_orig, cert_log_file,
                     nss_version, ckbi_version, new_version, restart_release,
                     name='Test User', email='test@example.com')
        return spec.read_text()

    def test_version_updated(self, tmp_path):
        out = self._run(tmp_path, MINIMAL_SPEC, new_version='2026.2.66')
        assert 'Version: 2026.2.66' in out
        assert 'Version: 2024.2.80' not in out

    def test_release_set_to_restart(self, tmp_path):
        out = self._run(tmp_path, MINIMAL_SPEC,
                        new_version='2026.2.66', restart_release='81')
        assert 'Release: 81%{?dist}' in out

    def test_release_bumped_when_version_unchanged(self, tmp_path):
        out = self._run(tmp_path, MINIMAL_SPEC,
                        new_version='2024.2.80', restart_release='80.0')
        assert 'Release: 80.1%{?dist}' in out

    def test_changelog_entry_added(self, tmp_path):
        out = self._run(tmp_path, MINIMAL_SPEC,
                        nss_version='3.101', ckbi_version='2.66')
        assert 'Update to CKBI 2.66 from NSS 3.101' in out

    def test_changelog_uses_make_log_format(self, tmp_path):
        out = self._run(tmp_path, MINIMAL_SPEC)
        lines = out.splitlines()
        cl_idx = next(i for i, l in enumerate(lines) if '%changelog' in l)
        after = [l for l in lines[cl_idx+1:] if l.strip()]
        assert after[0].startswith('*')
        assert 'Test User' in after[0]

    def test_cert_log_items_prepended_with_dash(self, tmp_path):
        out = self._run(tmp_path, MINIMAL_SPEC,
                        cert_log='Added: Some CA\nRemoved: Old CA')
        assert '- Added: Some CA' in out
        assert '- Removed: Old CA' in out

    def test_checkin_log_created(self, tmp_path):
        self._run(tmp_path, MINIMAL_SPEC)
        checkin = tmp_path / 'checkin.log'
        assert checkin.exists()
        assert 'CKBI' in checkin.read_text()

    def test_original_non_version_lines_preserved(self, tmp_path):
        out = self._run(tmp_path, MINIMAL_SPEC)
        assert 'Name:    ca-certificates' in out
        assert 'Summary: CA certs' in out
        assert '%description' in out

    def test_old_changelog_preserved(self, tmp_path):
        out = self._run(tmp_path, MINIMAL_SPEC)
        assert 'Previous update' in out

    def test_none_patch_adds_no_extra_patch_lines(self, tmp_path):
        spec = MINIMAL_SPEC.replace(
            'Summary: CA certs\n',
            'Summary: CA certs\nPatch0: existing.patch\n')
        out = self._run(tmp_path, spec, patch='NONE')
        patch_lines = [l for l in out.splitlines() if re.match(r'^Patch\d+:', l)]
        assert len(patch_lines) == 1

    def test_with_real_patch_adds_patch_line(self, tmp_path):
        spec = MINIMAL_SPEC.replace(
            'Summary: CA certs\n',
            'Summary: CA certs\nPatch0: existing.patch\n\n'
        ).replace('%description\n', '%setup\n%patch0\n\n%description\n')
        out = self._run(tmp_path, spec,
                        patch='new.patch', patch_orig='.orig',
                        ckbi_version='2.66')
        assert 'Patch1: new.patch' in out
        assert '%patch1' in out


# ══════════════════════════════════════════════════════════════════════════════
# cacertificates_update
# ══════════════════════════════════════════════════════════════════════════════

class TestCacertificatesUpdateP:
    def _setup(self, tmp_path, certdata_changed=True):
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
        return pkg_dir, cert, cacerts / 'nssckbi.h', scratch

    def _run(self, tmp_path, pkg_dir, certdata, nssckbi, scratch,
             release='rhel-9.6.0', rz='90.0', rb='91',
             current_releases=None, check_certs_stub=True):
        # Patch PATH for check_certs.sh stub
        env_path = str(STUBS) + ':' + os.environ.get('PATH', '')
        old_path = os.environ.get('PATH', '')
        os.environ['PATH'] = env_path
        # Temporarily replace _run to avoid real git
        old_run = bc._run

        git_log = []
        def stub_run(cmd, cwd=None, stdout=None):
            if cmd[0] in ('git', str(STUBS / 'git')):
                git_log.append(cmd)
                if stdout:
                    Path(stdout).write_text('')
                return 0
            elif 'check_certs' in str(cmd[0]):
                if stdout:
                    Path(stdout).write_text('Added: Test CA\nRemoved: Old CA\n')
                return 0
            else:
                return old_run(cmd, cwd=cwd, stdout=stdout)
        bc._run = stub_run
        try:
            rc = bc.cacertificates_update(
                pkg_dir, certdata, nssckbi,
                '3.101', '2.66', scratch,
                release, rz, rb,
                current_releases or ['rawhide'],
                verbose=False)
        finally:
            bc._run = old_run
            os.environ['PATH'] = old_path
        return rc, git_log

    def test_returns_1_when_certdata_missing(self, tmp_path, capsys):
        pkg_dir, _, nssckbi, scratch = self._setup(tmp_path)
        rc, _ = self._run(tmp_path, pkg_dir,
                          Path('/nonexistent/certdata.txt'), nssckbi, scratch)
        assert rc == 1

    def test_returns_1_when_pkg_dir_missing(self, tmp_path, capsys):
        _, certdata, nssckbi, scratch = self._setup(tmp_path)
        rc, _ = self._run(tmp_path, Path('/nonexistent'), certdata, nssckbi, scratch)
        assert rc == 1

    def test_returns_0_skips_when_certdata_unchanged(self, tmp_path, capsys):
        pkg_dir, certdata, nssckbi, scratch = self._setup(tmp_path,
                                                           certdata_changed=False)
        rc, _ = self._run(tmp_path, pkg_dir, certdata, nssckbi, scratch)
        assert rc == 0
        assert 'unchanged' in capsys.readouterr().out

    def test_returns_0_and_updates_when_certdata_changed(self, tmp_path, capsys):
        pkg_dir, certdata, nssckbi, scratch = self._setup(tmp_path,
                                                           certdata_changed=True)
        rc, _ = self._run(tmp_path, pkg_dir, certdata, nssckbi, scratch)
        assert rc == 0
        assert 'unchanged' not in capsys.readouterr().out

    def test_spec_file_updated_after_run(self, tmp_path):
        pkg_dir, certdata, nssckbi, scratch = self._setup(tmp_path, True)
        self._run(tmp_path, pkg_dir, certdata, nssckbi, scratch)
        spec = (pkg_dir / 'ca-certificates.spec').read_text()
        assert '2.66' in spec

    def test_certdata_copied_to_pkg_dir(self, tmp_path):
        pkg_dir, certdata, nssckbi, scratch = self._setup(tmp_path, True)
        self._run(tmp_path, pkg_dir, certdata, nssckbi, scratch)
        assert (pkg_dir / 'certdata.txt').read_text() == CERTDATA_V2

    def test_nssckbi_copied_to_pkg_dir(self, tmp_path):
        pkg_dir, certdata, nssckbi, scratch = self._setup(tmp_path, True)
        self._run(tmp_path, pkg_dir, certdata, nssckbi, scratch)
        assert (pkg_dir / 'nssckbi.h').exists()

    def test_checkin_log_created(self, tmp_path):
        pkg_dir, certdata, nssckbi, scratch = self._setup(tmp_path, True)
        self._run(tmp_path, pkg_dir, certdata, nssckbi, scratch)
        assert (pkg_dir / 'checkin.log').exists()

    def test_uses_base_release_for_current(self, tmp_path):
        pkg_dir, certdata, nssckbi, scratch = self._setup(tmp_path, True)
        self._run(tmp_path, pkg_dir, certdata, nssckbi, scratch,
                  release='rhel-9.6.0', rz='90.0', rb='91',
                  current_releases=['rawhide', 'rhel-9.6.0'])
        spec = (pkg_dir / 'ca-certificates.spec').read_text()
        assert 'Release: 91%{?dist}' in spec

    def test_uses_z_release_for_non_current(self, tmp_path):
        pkg_dir, certdata, nssckbi, scratch = self._setup(tmp_path, True)
        self._run(tmp_path, pkg_dir, certdata, nssckbi, scratch,
                  release='rhel-9.6.0', rz='90.0', rb='91',
                  current_releases=['rawhide'])
        spec = (pkg_dir / 'ca-certificates.spec').read_text()
        assert 'Release: 90.0%{?dist}' in spec

    def test_scratch_dir_created(self, tmp_path):
        pkg_dir, certdata, nssckbi, scratch = self._setup(tmp_path, True)
        self._run(tmp_path, pkg_dir, certdata, nssckbi, scratch)
        assert scratch.exists()


# ══════════════════════════════════════════════════════════════════════════════
# _distgit_branch — TOML-driven branch name conversion
# ══════════════════════════════════════════════════════════════════════════════

class TestVersionPartsBranch:
    """_distgit_branch converts release strings to dist-git branch names.

    RHEL 8/9 use distgit_version_parts=3 → keep X.Y.Z.
    RHEL 10+  use distgit_version_parts=2 (default) → strip .0 → X.Y.
    """

    def test_rhel8_keeps_three_parts(self):
        assert bc._distgit_branch('rhel-8.10.0') == 'rhel-8.10.0'

    def test_rhel9_keeps_three_parts(self):
        assert bc._distgit_branch('rhel-9.6.0') == 'rhel-9.6.0'

    def test_rhel10_strips_zero(self):
        assert bc._distgit_branch('rhel-10.3.0') == 'rhel-10.3'

    def test_rhel10_minor_zero_strips(self):
        assert bc._distgit_branch('rhel-10.0.0') == 'rhel-10.0'

    def test_non_rhel_passthrough(self):
        assert bc._distgit_branch('rawhide') == 'rawhide'

    def test_two_part_passthrough(self):
        # 2-part inputs (shouldn't normally occur) pass through unchanged
        assert bc._distgit_branch('rhel-9.6') == 'rhel-9.6'
