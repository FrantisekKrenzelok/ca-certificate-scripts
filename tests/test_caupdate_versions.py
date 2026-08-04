"""
Unit tests for caupdate/versions.py

Network calls are mocked so tests run offline.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import caupdate.versions as ver


NSS_H_CONTENT = """
/* NSS version */
#define NSS_VERSION "3.101 Basic ECC"
#define NSS_VMAJOR 3
"""

NSSCKBI_H_CONTENT = """
#define NSS_BUILTINS_LIBRARY_VERSION "2.66"
#define NSS_BUILTINS_LIBRARY_VERSION_MAJOR 2
"""


def _make_mock_get(nss_text=NSS_H_CONTENT, ckbi_text=NSSCKBI_H_CONTENT):
    responses = iter([
        _mock_resp(nss_text),
        _mock_resp(ckbi_text),
    ])
    return lambda *a, **kw: next(responses)


def _mock_resp(text):
    r = MagicMock()
    r.text = text
    r.raise_for_status = MagicMock()
    return r


class TestFetchNssVersions:
    @patch('requests.get')
    def test_extracts_nss_version(self, mock_get):
        mock_get.side_effect = _make_mock_get()
        nss, ckbi = ver.fetch_nss_versions()
        assert nss == '3.101'

    @patch('requests.get')
    def test_extracts_ckbi_version(self, mock_get):
        mock_get.side_effect = _make_mock_get()
        nss, ckbi = ver.fetch_nss_versions()
        assert ckbi == '2.66'

    @patch('requests.get')
    def test_makes_two_requests(self, mock_get):
        mock_get.side_effect = _make_mock_get()
        ver.fetch_nss_versions()
        assert mock_get.call_count == 2

    @patch('requests.get')
    def test_first_request_is_nss_h(self, mock_get):
        mock_get.side_effect = _make_mock_get()
        ver.fetch_nss_versions()
        first_url = mock_get.call_args_list[0][0][0]
        assert 'nss.h' in first_url

    @patch('requests.get')
    def test_second_request_is_nssckbi_h(self, mock_get):
        mock_get.side_effect = _make_mock_get()
        ver.fetch_nss_versions()
        second_url = mock_get.call_args_list[1][0][0]
        assert 'nssckbi.h' in second_url

    @patch('requests.get')
    def test_uses_custom_base_url(self, mock_get):
        mock_get.side_effect = _make_mock_get()
        ver.fetch_nss_versions(base_url='https://custom.example.com/lib')
        first_url = mock_get.call_args_list[0][0][0]
        assert first_url.startswith('https://custom.example.com/lib')

    @patch('requests.get')
    def test_strips_version_suffix(self, mock_get):
        # "3.114 Basic ECC" → only "3.114" extracted
        nss_h = '#define NSS_VERSION "3.114 Basic ECC"\n'
        mock_get.side_effect = _make_mock_get(nss_text=nss_h)
        nss, _ = ver.fetch_nss_versions()
        assert nss == '3.114'
        assert 'Basic' not in nss

    @patch('requests.get')
    def test_strips_quotes(self, mock_get):
        ckbi_h = '#define NSS_BUILTINS_LIBRARY_VERSION "2.80"\n'
        mock_get.side_effect = _make_mock_get(ckbi_text=ckbi_h)
        _, ckbi = ver.fetch_nss_versions()
        assert ckbi == '2.80'
        assert '"' not in ckbi

    @patch('requests.get')
    def test_unknown_on_no_match(self, mock_get):
        mock_get.side_effect = _make_mock_get(
            nss_text='/* no version here */',
            ckbi_text='/* no version here */')
        nss, ckbi = ver.fetch_nss_versions()
        assert nss == 'unknown'
        assert ckbi == 'unknown'

    @patch('requests.get')
    def test_propagates_http_error(self, mock_get):
        import requests
        mock_get.return_value = MagicMock(
            text='',
            raise_for_status=MagicMock(side_effect=requests.HTTPError('404')))
        with pytest.raises(requests.HTTPError):
            ver.fetch_nss_versions()

    def test_default_base_url_is_mozilla(self):
        assert 'hg.mozilla.org' in ver.NSS_BASE_URL
        assert 'nss' in ver.NSS_BASE_URL.lower()
