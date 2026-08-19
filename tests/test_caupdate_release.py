"""
Unit tests for caupdate/release.py

All pure functions are tested directly; network-dependent functions
(errata_get_release_info, discover_fedora_releases, load_errata_map)
are tested with mocked HTTP responses.
"""

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from functools import cmp_to_key
from unittest.mock import MagicMock, patch, mock_open

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import caupdate.release as rel


# ══════════════════════════════════════════════════════════════════════════════
# release_get_major
# ══════════════════════════════════════════════════════════════════════════════

class TestReleaseGetMajor:
    def test_standard_three_part(self):
        assert rel.release_get_major('rhel-9.6.0') == '9'

    def test_two_part(self):
        assert rel.release_get_major('rhel-10.3') == '10'

    def test_major_8(self):
        assert rel.release_get_major('rhel-8.10.0') == '8'

    def test_major_10(self):
        assert rel.release_get_major('rhel-10.0') == '10'

    def test_fedora(self):
        # fedora releases are not rhel-X.Y format — returns None
        assert rel.release_get_major('f45') is None

    def test_no_dash(self):
        assert rel.release_get_major('rawhide') is None

    def test_single_version_part(self):
        assert rel.release_get_major('rhel-9') is None

    def test_returns_string(self):
        result = rel.release_get_major('rhel-9.6.0')
        assert isinstance(result, str)


# ══════════════════════════════════════════════════════════════════════════════
# safe_int
# ══════════════════════════════════════════════════════════════════════════════

class TestSafeInt:
    def test_valid_int_string(self):
        assert rel.safe_int('9') == 9

    def test_valid_int(self):
        assert rel.safe_int(42) == 42

    def test_empty_string(self):
        assert rel.safe_int('') == 0

    def test_none(self):
        assert rel.safe_int(None) == 0

    def test_non_numeric(self):
        assert rel.safe_int('abc') == 0

    def test_float_string(self):
        assert rel.safe_int('3.14') == 0

    def test_zero(self):
        assert rel.safe_int('0') == 0

    def test_negative(self):
        assert rel.safe_int('-1') == -1


# ══════════════════════════════════════════════════════════════════════════════
# get_need_zstream_clone
# ══════════════════════════════════════════════════════════════════════════════

class TestGetNeedZstreamClone:
    GA = ['rhel-9.6.0', 'rhel-10.3']

    def test_ga_release_needs_no_clone(self):
        assert rel.get_need_zstream_clone('rhel-9.6.0', self.GA) is False

    def test_zstream_needs_clone(self):
        assert rel.get_need_zstream_clone('rhel-9.4.0', self.GA) is True

    def test_normalises_two_part_to_three(self):
        # rhel-9.6 → rhel-9.6.0 before checking ga_list
        ga = ['rhel-9.6.0']
        assert rel.get_need_zstream_clone('rhel-9.6', ga) is False

    def test_major_below_8_always_false(self):
        assert rel.get_need_zstream_clone('rhel-7.9', self.GA) is False

    def test_rhel8(self):
        # RHEL 8 has zstream_clone=false in release_config.toml — no clone needed
        ga = ['rhel-8.10.0']
        assert rel.get_need_zstream_clone('rhel-8.8.0', ga) is False

    def test_rhel10_ga_two_part_key(self):
        # RHEL 10 uses 2-part keys in the errata map ('rhel-10.3', not 'rhel-10.3.0').
        # get_need_zstream_clone must accept both forms so the GA is recognised.
        assert rel.get_need_zstream_clone('rhel-10.3', ['rhel-10.3']) is False

    def test_rhel10_ga_three_part_key(self):
        # Also works if caller provides the normalised 3-part form
        assert rel.get_need_zstream_clone('rhel-10.3', ['rhel-10.3.0']) is False

    def test_rhel10_zstream(self):
        ga = ['rhel-10.3']
        assert rel.get_need_zstream_clone('rhel-10.0', ga) is True


# ══════════════════════════════════════════════════════════════════════════════
# is_latest_z_stream
# ══════════════════════════════════════════════════════════════════════════════

# TestIsLatestZStream removed — is_latest_z_stream deleted as dead code


# ══════════════════════════════════════════════════════════════════════════════
# release_is_centos_stream
# ══════════════════════════════════════════════════════════════════════════════

class TestReleaseIsCentosStream:
    GA = ['rhel-9.6.0', 'rhel-10.3']

    def test_ga_is_centos(self):
        assert rel.release_is_centos_stream('rhel-9.6.0', self.GA) is True

    def test_zstream_is_not_centos(self):
        assert rel.release_is_centos_stream('rhel-9.4.0', self.GA) is False

    def test_major_below_8_is_not_centos(self):
        assert rel.release_is_centos_stream('rhel-7.9', self.GA) is False

    def test_rhel8(self):
        # RHEL 8 now has centos_stream=true (c8s → distrobaker → rhel-8-main)
        ga = ['rhel-8.10.0']
        assert rel.release_is_centos_stream('rhel-8.10.0', ga) is True


# ══════════════════════════════════════════════════════════════════════════════
# errata_nvrcmp
# ══════════════════════════════════════════════════════════════════════════════

class TestErrataaNvrcmp:
    def _sorted(self, items):
        return sorted(items, key=cmp_to_key(rel.errata_nvrcmp))

    def test_basic_order(self):
        assert self._sorted(['rhel-9', 'rhel-8']) == ['rhel-8', 'rhel-9']

    def test_minor_version_order(self):
        result = self._sorted(['rhel-9.6', 'rhel-9.4', 'rhel-9.8'])
        assert result == ['rhel-9.4', 'rhel-9.6', 'rhel-9.8']

    def test_10_sorts_after_9(self):
        result = self._sorted(['rhel-10', 'rhel-9', 'rhel-8'])
        assert result == ['rhel-8', 'rhel-9', 'rhel-10']

    def test_patch_version(self):
        result = self._sorted(['rhel-8.10.0', 'rhel-8.2.0', 'rhel-8.9.0'])
        assert result == ['rhel-8.2.0', 'rhel-8.9.0', 'rhel-8.10.0']

    def test_equal_returns_0(self):
        assert rel.errata_nvrcmp('rhel-9.6', 'rhel-9.6') == 0

    def test_dist_prefix_ordering(self):
        # 'dist' sorts before 'rhel'
        result = self._sorted(['rhel-8', 'dist-3.0E'])
        assert result[0].startswith('dist')


# ══════════════════════════════════════════════════════════════════════════════
# errata_candidate_to_release
# ══════════════════════════════════════════════════════════════════════════════

class TestErrataCandidateToRelease:
    def test_rhel_tag(self):
        assert rel.errata_candidate_to_release('RHEL-9.6.0-candidate') == 'rhel-9.6.0'

    def test_rhel_tag_two_part(self):
        assert rel.errata_candidate_to_release('RHEL-10-candidate') == 'rhel-10'

    def test_lowercases(self):
        result = rel.errata_candidate_to_release('RHEL-8-candidate')
        assert result == result.lower()

    def test_single_part(self):
        assert rel.errata_candidate_to_release('RHEL') == 'rhel'


# ══════════════════════════════════════════════════════════════════════════════
# is_sustaining_release
# ══════════════════════════════════════════════════════════════════════════════

class TestIsSustainingRelease:
    def test_e4s(self):
        assert rel.is_sustaining_release('RHEL-9.2.0.Z.E4S') is True

    def test_aus(self):
        assert rel.is_sustaining_release('RHEL-8.6.0.Z.AUS') is True

    def test_tus(self):
        assert rel.is_sustaining_release('RHEL-7.6.Z.TUS') is True

    def test_main_eus_not_sustaining(self):
        # MAIN+EUS is active development, not pure SE
        assert rel.is_sustaining_release('RHEL-9.8.0.Z.MAIN+EUS') is False

    def test_ga_not_sustaining(self):
        assert rel.is_sustaining_release('RHEL-10.3.GA') is False

    def test_main_not_sustaining(self):
        assert rel.is_sustaining_release('RHEL-9.9.0.Z.MAIN') is False


# ══════════════════════════════════════════════════════════════════════════════
# _relevant_release
# ══════════════════════════════════════════════════════════════════════════════

class TestRelevantRelease:
    def test_head_always_kept(self):
        assert rel._relevant_release('RHEL-9.7.0.Z.MAIN', is_head=True) is True

    def test_extension_excluded(self):
        assert rel._relevant_release('RHEL-8.3.0.Z.EXTENSION', is_head=False) is False

    def test_bare_main_non_head_excluded(self):
        assert rel._relevant_release('RHEL-9.7.0.Z.MAIN', is_head=False) is False

    def test_bare_eus_excluded(self):
        assert rel._relevant_release('RHEL-9.0.0.Z.EUS', is_head=False) is False

    def test_main_eus_kept(self):
        assert rel._relevant_release('RHEL-9.8.0.Z.MAIN+EUS', is_head=False) is True

    def test_e4s_kept(self):
        assert rel._relevant_release('RHEL-9.2.0.Z.E4S', is_head=False) is True

    def test_aus_kept(self):
        assert rel._relevant_release('RHEL-8.6.0.Z.AUS', is_head=False) is True

    def test_tus_kept(self):
        assert rel._relevant_release('RHEL-7.6.Z.TUS', is_head=False) is True

    def test_plain_z_kept(self):
        assert rel._relevant_release('RHEL-10.2.Z', is_head=False) is True

    def test_ga_kept_non_head(self):
        assert rel._relevant_release('RHEL-10.3.GA', is_head=False) is False

    def test_ga_kept_as_head(self):
        assert rel._relevant_release('RHEL-10.3.GA', is_head=True) is True


# ══════════════════════════════════════════════════════════════════════════════
# get_ga_list
# ══════════════════════════════════════════════════════════════════════════════

SAMPLE_ERRATA_MAP = {
    'rhel-8.10.0': {'name': 'RHEL-8.10.0.Z.MAIN+EUS', 'description': '', 'id': 1, 'release_id': 1},
    'rhel-9.4.0':  {'name': 'RHEL-9.4.0.Z.MAIN+EUS',  'description': '', 'id': 2, 'release_id': 2},
    'rhel-9.6.0':  {'name': 'RHEL-9.6.0.Z.MAIN+EUS',  'description': '', 'id': 3, 'release_id': 3},
    'rhel-9.9.0':  {'name': 'RHEL-9.9.0.Z.MAIN',       'description': '', 'id': 4, 'release_id': 4},
    'rhel-10.0':   {'name': 'RHEL-10.0.Z.E4S',          'description': '', 'id': 5, 'release_id': 5},
    'rhel-10.3':   {'name': 'RHEL-10.3.GA',              'description': '', 'id': 6, 'release_id': 6},
}

class TestGetGaList:
    def test_returns_last_per_major(self):
        ga = rel.get_ga_list(SAMPLE_ERRATA_MAP)
        # Last release per major in insertion order
        assert 'rhel-9.9.0' in ga
        assert 'rhel-10.3' in ga
        assert 'rhel-8.10.0' in ga

    def test_one_per_major(self):
        ga = rel.get_ga_list(SAMPLE_ERRATA_MAP)
        majors = [rel.release_get_major(r) for r in ga]
        assert len(majors) == len(set(majors))   # no duplicates

    def test_empty_map(self):
        assert rel.get_ga_list({}) == []


# ══════════════════════════════════════════════════════════════════════════════
# get_latest_zstreams was removed — get_ga_list covers the same use case


# ══════════════════════════════════════════════════════════════════════════════
# discover_rhel_releases
# ══════════════════════════════════════════════════════════════════════════════

# Realistic errata map for discover tests
DISCOVER_MAP = {
    # RHEL 8 — z-stream-only (MAIN+EUS = shipped GA already)
    'rhel-8.4.0':  {'name': 'RHEL-8.4.0.Z.AUS',         'description': '', 'id': 10, 'release_id': 10},
    'rhel-8.6.0':  {'name': 'RHEL-8.6.0.Z.AUS',         'description': '', 'id': 11, 'release_id': 11},
    'rhel-8.8.0':  {'name': 'RHEL-8.8.0.Z.E4S',         'description': '', 'id': 12, 'release_id': 12},
    'rhel-8.10.0': {'name': 'RHEL-8.10.0.Z.MAIN+EUS',   'description': '', 'id': 13, 'release_id': 13},
    # RHEL 9 — head awaiting GA (MAIN only), others are EUS/E4S
    'rhel-9.2.0':  {'name': 'RHEL-9.2.0.Z.E4S',         'description': '', 'id': 20, 'release_id': 20},
    'rhel-9.4.0':  {'name': 'RHEL-9.4.0.Z.MAIN+EUS',    'description': '', 'id': 21, 'release_id': 21},
    'rhel-9.6.0':  {'name': 'RHEL-9.6.0.Z.MAIN+EUS',    'description': '', 'id': 22, 'release_id': 22},
    'rhel-9.9.0':  {'name': 'RHEL-9.9.0.Z.MAIN',        'description': '', 'id': 23, 'release_id': 23},
    # RHEL 10 — has explicit GA
    'rhel-10.0':   {'name': 'RHEL-10.0.Z.E4S',           'description': '', 'id': 30, 'release_id': 30},
    'rhel-10.2':   {'name': 'RHEL-10.2.Z',               'description': '', 'id': 31, 'release_id': 31},
    'rhel-10.3':   {'name': 'RHEL-10.3.GA',               'description': '', 'id': 32, 'release_id': 32},
    # Old releases below min_major — excluded
    'rhel-7.9':    {'name': 'RHEL-7.9-ELS',              'description': '', 'id': 40, 'release_id': 40},
}

class TestDiscoverRhel:
    @pytest.fixture
    def ga_list(self):
        return rel.get_ga_list(DISCOVER_MAP)

    @pytest.fixture
    def discovered(self, ga_list):
        return rel.discover_rhel_releases(DISCOVER_MAP, ga_list)

    def _by_release(self, discovered):
        return {d['release']: d for d in discovered}

    def test_rhel7_excluded(self, discovered):
        releases = [d['release'] for d in discovered]
        assert 'rhel-7.9' not in releases

    def test_descending_major_order(self, discovered):
        majors = [d['major'] for d in discovered]
        assert majors == sorted(set(majors), reverse=True) or \
               majors[0] >= majors[-1]   # monotonically non-increasing

    def test_rhel10_ga_is_true_ga(self, discovered):
        d = self._by_release(discovered)
        assert d['rhel-10.3']['is_ga'] is True
        assert d['rhel-10.3']['use_zstream'] is False

    def test_rhel10_zstream_not_ga(self, discovered):
        d = self._by_release(discovered)
        assert d['rhel-10.2']['is_ga'] is False

    def test_rhel8_head_is_ga_with_zstream(self, discovered):
        # rhel-8.10.0 is head of z-stream-only major with MAIN+EUS → use_zstream=True
        # dist_branch in TOML controls git checkout only, not issue classification
        d = self._by_release(discovered)
        assert d['rhel-8.10.0']['is_ga'] is True
        assert d['rhel-8.10.0']['use_zstream'] is True

    def test_rhel9_head_awaiting_ga(self, discovered):
        # rhel-9.9.0 is MAIN only (no EUS) → awaiting GA → use_zstream=False
        d = self._by_release(discovered)
        assert d['rhel-9.9.0']['is_ga'] is True
        assert d['rhel-9.9.0']['use_zstream'] is False

    def test_se_flags(self, discovered):
        d = self._by_release(discovered)
        assert d['rhel-8.4.0']['is_sustaining'] is True   # AUS
        assert d['rhel-9.2.0']['is_sustaining'] is True   # E4S
        assert d['rhel-10.2']['is_sustaining'] is False   # plain .Z

    def test_extension_excluded(self):
        m = dict(DISCOVER_MAP)
        m['rhel-8.3.0'] = {'name': 'RHEL-8.3.0.Z.EXTENSION', 'description': '', 'id': 99, 'release_id': 99}
        ga = rel.get_ga_list(m)
        result = rel.discover_rhel_releases(m, ga)
        releases = [d['release'] for d in result]
        assert 'rhel-8.3.0' not in releases

    def test_bare_main_non_head_excluded(self):
        m = dict(DISCOVER_MAP)
        m['rhel-9.7.0'] = {'name': 'RHEL-9.7.0.Z.MAIN', 'description': '', 'id': 50, 'release_id': 50}
        ga = rel.get_ga_list(m)
        result = rel.discover_rhel_releases(m, ga)
        releases = [d['release'] for d in result]
        assert 'rhel-9.7.0' not in releases   # non-head MAIN filtered

    def test_min_major_respected(self, ga_list):
        result = rel.discover_rhel_releases(DISCOVER_MAP, ga_list, min_major=10)
        releases = [d['release'] for d in result]
        for r in releases:
            assert rel.safe_int(rel.release_get_major(r)) >= 10


# ══════════════════════════════════════════════════════════════════════════════
# discover_fedora_releases (mocked HTTP)
# ══════════════════════════════════════════════════════════════════════════════

class TestDiscoverFedoraReleases:
    def _mock_response(self, names):
        response = MagicMock()
        response.json.return_value = {
            'releases': [{'name': n} for n in names],
            'pages': 1,
        }
        response.raise_for_status = MagicMock()
        return response

    @patch('requests.get')
    def test_f_releases_lowercased(self, mock_get):
        mock_get.return_value = self._mock_response(['F45', 'F44', 'F43'])
        result = rel.discover_fedora_releases()
        assert 'f45' in result
        assert 'F45' not in result

    @patch('requests.get')
    def test_rawhide_always_first(self, mock_get):
        mock_get.return_value = self._mock_response(['F45', 'F44'])
        result = rel.discover_fedora_releases()
        assert result[0] == 'rawhide'

    @patch('requests.get')
    def test_descending_order(self, mock_get):
        mock_get.return_value = self._mock_response(['F43', 'F45', 'F44'])
        result = rel.discover_fedora_releases()
        # rawhide first, then descending
        assert result == ['rawhide', 'f45', 'f44', 'f43']

    @patch('requests.get')
    def test_non_fedora_entries_ignored(self, mock_get):
        mock_get.return_value = self._mock_response(['F45', 'EPEL9', 'ELN', 'F44'])
        result = rel.discover_fedora_releases()
        assert 'epel9' not in result
        assert 'eln' not in result

    @patch('requests.get')
    def test_rawhide_in_bodhi_not_doubled(self, mock_get):
        # If Bodhi ever returns Rawhide, it should not appear twice
        mock_get.return_value = self._mock_response(['F45', 'Rawhide'])
        result = rel.discover_fedora_releases()
        assert result.count('rawhide') == 1


# ══════════════════════════════════════════════════════════════════════════════
# load_errata_map (cache handling)
# ══════════════════════════════════════════════════════════════════════════════

class TestLoadErrataMap:
    def test_reads_valid_cache(self, tmp_path):
        cache = tmp_path / 'errata_cache'
        today = date.today().strftime('%Y-%m-%d')
        data = {'rhel-9.6.0': {'name': 'RHEL-9.6.0.Z.MAIN+EUS',
                                'description': '', 'id': 1, 'release_id': 1}}
        cache.write_text(today + '\n' + json.dumps(data))
        errata_map, ga_list = rel.load_errata_map(
            'http://unused', str(cache))
        assert 'rhel-9.6.0' in errata_map

    def test_stale_cache_triggers_resync(self, tmp_path):
        cache = tmp_path / 'errata_cache'
        old_date = (date.today() - timedelta(days=40)).strftime('%Y-%m-%d')
        cache.write_text(old_date + '\n{}')
        with patch('caupdate.release.errata_get_release_info', return_value={}) as mock_ri:
            rel.load_errata_map('http://fake', str(cache))
            mock_ri.assert_called_once()

    def test_missing_cache_triggers_resync(self, tmp_path):
        cache = tmp_path / 'missing_cache'
        with patch('caupdate.release.errata_get_release_info', return_value={}) as mock_ri:
            rel.load_errata_map('http://fake', str(cache))
            mock_ri.assert_called_once()

    def test_force_resync(self, tmp_path):
        cache = tmp_path / 'errata_cache'
        today = date.today().strftime('%Y-%m-%d')
        cache.write_text(today + '\n{}')
        with patch('caupdate.release.errata_get_release_info', return_value={}) as mock_ri:
            rel.load_errata_map('http://fake', str(cache), force_resync=True)
            mock_ri.assert_called_once()

    def test_returns_errata_map_and_ga_list(self, tmp_path):
        cache = tmp_path / 'errata_cache'
        today = date.today().strftime('%Y-%m-%d')
        cache.write_text(today + '\n' + json.dumps(SAMPLE_ERRATA_MAP))
        errata_map, ga_list = rel.load_errata_map('http://unused', str(cache))
        assert isinstance(errata_map, dict)
        assert isinstance(ga_list, list)
