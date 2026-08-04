"""
Unit tests for caupdate/issues.py

JiraSession HTTP calls are mocked; pure functions are tested directly.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import caupdate.issues as iss


# ══════════════════════════════════════════════════════════════════════════════
# jira_fixversion
# ══════════════════════════════════════════════════════════════════════════════

class TestJiraFixversion:
    # RHEL 8 (minor < 10): keep .0
    def test_rhel8_keeps_trailing_zero(self):
        assert iss.jira_fixversion('rhel-8.4.0')  == 'rhel-8.4.0'
        assert iss.jira_fixversion('rhel-8.6.0')  == 'rhel-8.6.0'
        assert iss.jira_fixversion('rhel-8.8.0')  == 'rhel-8.8.0'

    # RHEL 8.10 exception: strip .0
    def test_rhel8_10_strips_zero(self):
        assert iss.jira_fixversion('rhel-8.10.0') == 'rhel-8.10'

    # RHEL 9.2 and below: keep .0
    def test_rhel9_2_keeps_trailing_zero(self):
        assert iss.jira_fixversion('rhel-9.0.0')  == 'rhel-9.0.0'
        assert iss.jira_fixversion('rhel-9.2.0')  == 'rhel-9.2.0'

    # RHEL 9.4+: strip .0
    def test_rhel9_strips_zero_above_2(self):
        assert iss.jira_fixversion('rhel-9.4.0')  == 'rhel-9.4'
        assert iss.jira_fixversion('rhel-9.6.0')  == 'rhel-9.6'
        assert iss.jira_fixversion('rhel-9.9.0')  == 'rhel-9.9'

    # RHEL 10 (already 2-part in errata map): no change
    def test_rhel10_unchanged(self):
        assert iss.jira_fixversion('rhel-10.0')   == 'rhel-10.0'
        assert iss.jira_fixversion('rhel-10.2')   == 'rhel-10.2'
        assert iss.jira_fixversion('rhel-10.3')   == 'rhel-10.3'

    # Non-matching format: returned as-is
    def test_no_match_returned_as_is(self):
        assert iss.jira_fixversion('rawhide')       == 'rawhide'
        assert iss.jira_fixversion('rhel-9.6')      == 'rhel-9.6'

    # z-stream suffix applied by caller (not jira_fixversion itself)
    def test_z_suffix_applied_separately(self):
        fv = iss.jira_fixversion('rhel-8.4.0')
        assert fv + '.z' == 'rhel-8.4.0.z'

        fv = iss.jira_fixversion('rhel-9.6.0')
        assert fv + '.z' == 'rhel-9.6.z'

        fv = iss.jira_fixversion('rhel-8.10.0')
        assert fv + '.z' == 'rhel-8.10.z'


# ══════════════════════════════════════════════════════════════════════════════
# constants
# ══════════════════════════════════════════════════════════════════════════════

class TestConstants:
    def test_jira_proj(self):
        assert iss.JIRA_PROJ == 'RHEL'

    def test_jira_issue_type(self):
        assert iss.JIRA_ISSUE_TYPE == 'Bug'

    def test_bug_summary_short_has_placeholder(self):
        assert '%s' in iss.bug_summary_short

    def test_bug_summary_formatted(self):
        result = iss.bug_summary % ('2026', '2.88', '3.125', '153', '1.5', 'rhel-10.3')
        assert '2026' in result
        assert 'rhel-10.3' in result


# ══════════════════════════════════════════════════════════════════════════════
# JiraSession
# ══════════════════════════════════════════════════════════════════════════════

def _make_session(url='https://redhat.atlassian.net', user='user@example.com',
                  token='tok', is_cloud=True):
    """Build a JiraSession with a mock python-jira client."""
    client = MagicMock()
    session = iss.JiraSession(client, url, token, user=user)
    return session


class TestJiraSessionHeaders:
    def test_cloud_uses_basic_auth(self):
        s = _make_session(url='https://redhat.atlassian.net', user='u@h.com', token='tok')
        assert s.is_cloud is True
        headers = s._headers()
        assert headers['Authorization'].startswith('Basic ')

    def test_server_uses_bearer(self):
        s = _make_session(url='https://issues.redhat.com', user=None, token='tok')
        s.is_cloud = False   # force server mode
        headers = s._headers()
        assert headers['Authorization'] == 'Bearer tok'

    def test_cloud_without_user_falls_back_to_bearer(self):
        s = _make_session(url='https://redhat.atlassian.net', user=None, token='tok')
        headers = s._headers()
        # No user → can't do basic auth → falls back to Bearer
        assert 'Authorization' in headers

    def test_content_type_json(self):
        s = _make_session()
        assert s._headers()['Content-Type'] == 'application/json'


class TestJiraSessionSearch:
    def test_search_returns_issues(self):
        s = _make_session()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            'issues': [{'key': 'RHEL-100', 'fields': {'summary': 'test'}}]
        }
        with patch('requests.post', return_value=mock_resp):
            result = s.search('project=RHEL')
        assert result[0]['key'] == 'RHEL-100'

    def test_search_posts_to_v3(self):
        s = _make_session(url='https://redhat.atlassian.net')
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {'issues': []}
        with patch('requests.post', return_value=mock_resp) as mock_post:
            s.search('project=RHEL')
            url_called = mock_post.call_args[0][0]
            assert '/rest/api/3/search/jql' in url_called

    def test_search_empty_result(self):
        s = _make_session()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {'issues': []}
        with patch('requests.post', return_value=mock_resp):
            assert s.search('project=RHEL') == []


class TestJiraSessionCreate:
    def test_create_posts_to_v3(self):
        s = _make_session()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {'key': 'RHEL-200', 'id': '123'}
        with patch('requests.post', return_value=mock_resp) as mock_post:
            result = s.create({'project': {'key': 'RHEL'}, 'summary': 'test'})
            url_called = mock_post.call_args[0][0]
            assert '/rest/api/3/issue' in url_called
        assert result['key'] == 'RHEL-200'

    def test_create_raises_on_error(self):
        s = _make_session()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception('HTTP 400')
        with patch('requests.post', return_value=mock_resp):
            with pytest.raises(Exception):
                s.create({})


class TestJiraSessionGet:
    def test_get_returns_issue(self):
        s = _make_session()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {'key': 'RHEL-100', 'fields': {}}
        with patch('requests.get', return_value=mock_resp):
            result = s.get('RHEL-100')
        assert result['key'] == 'RHEL-100'

    def test_get_uses_v3_endpoint(self):
        s = _make_session(url='https://redhat.atlassian.net')
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {'key': 'RHEL-100', 'fields': {}}
        with patch('requests.get', return_value=mock_resp) as mock_get:
            s.get('RHEL-100')
            url_called = mock_get.call_args[0][0]
            assert '/rest/api/3/issue/RHEL-100' in url_called


# ══════════════════════════════════════════════════════════════════════════════
# issue_lookup
# ══════════════════════════════════════════════════════════════════════════════

class TestIssueLookup:
    def _session_with_search(self, issues):
        s = _make_session()
        s.search = MagicMock(return_value=issues)
        return s

    def test_found_returns_key(self):
        issue = {'key': 'RHEL-100', 'fields': {'summary': 'test'}}
        s = self._session_with_search([issue])
        key, obj = iss.issue_lookup(s, 'rhel-10.3', '2.88', 'ca-certificates', '2026')
        assert key == 'RHEL-100'
        assert obj is issue

    def test_not_found_returns_zero(self):
        s = self._session_with_search([])
        key, obj = iss.issue_lookup(s, 'rhel-10.3', '2.88', 'ca-certificates', '2026')
        assert key == '0'
        assert obj is None

    def test_multiple_results_returns_zero(self):
        issues = [{'key': 'RHEL-1', 'fields': {}}, {'key': 'RHEL-2', 'fields': {}}]
        s = self._session_with_search(issues)
        key, _ = iss.issue_lookup(s, 'rhel-10.3', '2.88', 'ca-certificates', '2026')
        assert key == '0'

    def test_zstream_appends_z_to_fixversion(self):
        s = _make_session()
        captured_jql = []
        def capture_search(jql, **kwargs):
            captured_jql.append(jql)
            return []
        s.search = capture_search
        iss.issue_lookup(s, 'rhel-9.6.0', '2.88', 'ca-certificates', '2026', zstream=True)
        assert 'rhel-9.6.z' in captured_jql[0]   # jira_fixversion strips .0, then .z added

    def test_quotes_component_in_jql(self):
        s = _make_session()
        captured = []
        def capture(jql, **kwargs):
            captured.append(jql)
            return []
        s.search = capture
        iss.issue_lookup(s, 'rhel-10.3', '2.88', 'ca-certificates', '2026')
        assert 'component="ca-certificates"' in captured[0]

    def test_quotes_fixversion_in_jql(self):
        s = _make_session()
        captured = []
        def capture(jql, **kwargs):
            captured.append(jql)
            return []
        s.search = capture
        iss.issue_lookup(s, 'rhel-10.3', '2.88', 'ca-certificates', '2026')
        assert 'fixVersion="rhel-10.3"' in captured[0]

    def test_error_returns_zero(self):
        import requests
        s = _make_session()
        err = requests.HTTPError('500')
        err.response = MagicMock(text='server error')
        s.search = MagicMock(side_effect=err)
        key, obj = iss.issue_lookup(s, 'rhel-10.3', '2.88', 'ca-certificates', '2026')
        assert key == '0'


# ══════════════════════════════════════════════════════════════════════════════
# issue_create
# ══════════════════════════════════════════════════════════════════════════════

class TestIssueCreate:
    def _session_with_create(self, key='RHEL-200'):
        s = _make_session()
        s.create = MagicMock(return_value={'key': key, 'id': '1'})
        return s

    def test_returns_key(self):
        s = self._session_with_create('RHEL-200')
        key, obj = iss.issue_create(s, 'rhel-10.3', '2.88', '3.125', '153', '1.5',
                                     'ca-certificates', False, '2026')
        assert key == 'RHEL-200'

    def test_zstream_appends_z(self):
        s = _make_session()
        captured = []
        def capture(fields):
            captured.append(fields)
            return {'key': 'RHEL-200', 'id': '1'}
        s.create = capture
        iss.issue_create(s, 'rhel-9.6.0', '2.88', '3.125', '153', '1.5',
                          'ca-certificates', True, '2026')
        fix_versions = captured[0]['fixVersions']
        assert any('.z' in fv['name'] for fv in fix_versions)

    def test_normalises_rhel_8_10(self):
        s = _make_session()
        captured = []
        def capture(fields):
            captured.append(fields)
            return {'key': 'RHEL-200', 'id': '1'}
        s.create = capture
        iss.issue_create(s, 'rhel-8.10.0', '2.88', '3.125', '153', '1.5',
                          'ca-certificates', True, '2026')
        fix_versions = captured[0]['fixVersions']
        # rhel-8.10.0 → rhel-8.10 (jira_fixversion) then .z → rhel-8.10.z
        assert fix_versions[0]['name'] == 'rhel-8.10.z'

    def test_project_is_rhel(self):
        s = _make_session()
        captured = []
        s.create = lambda f: (captured.append(f), {'key': 'RHEL-200', 'id': '1'})[1]
        iss.issue_create(s, 'rhel-10.3', '2.88', '3.125', '153', '1.5',
                          'ca-certificates', False, '2026')
        assert captured[0]['project']['key'] == 'RHEL'

    def test_create_error_returns_zero(self):
        import requests
        s = _make_session()
        err = requests.HTTPError('400')
        err.response = MagicMock(text='bad request')
        s.create = MagicMock(side_effect=err)
        key, obj = iss.issue_create(s, 'rhel-10.3', '2.88', '3.125', '153', '1.5',
                                     'ca-certificates', False, '2026')
        assert key == '0'
        assert obj is None

    def test_description_is_adf(self):
        s = _make_session()
        captured = []
        s.create = lambda f: (captured.append(f), {'key': 'RHEL-200', 'id': '1'})[1]
        iss.issue_create(s, 'rhel-10.3', '2.88', '3.125', '153', '1.5',
                          'ca-certificates', False, '2026')
        desc = captured[0]['description']
        assert isinstance(desc, dict)
        assert desc.get('type') == 'doc'

    def test_summary_contains_year_and_version(self):
        s = _make_session()
        captured = []
        s.create = lambda f: (captured.append(f), {'key': 'RHEL-200', 'id': '1'})[1]
        iss.issue_create(s, 'rhel-10.3', '2.88', '3.125', '153', '1.5',
                          'ca-certificates', False, '2026')
        assert '2026' in captured[0]['summary']
        assert '2.88' in captured[0]['summary']


# ══════════════════════════════════════════════════════════════════════════════
# has_clone_links
# ══════════════════════════════════════════════════════════════════════════════

class TestHasCloneLinks:
    def _session_with_issue(self, issue_fields):
        s = _make_session()
        s.get = MagicMock(return_value={'key': 'RHEL-100', 'fields': issue_fields})
        return s

    def test_detects_clone_link(self):
        fields = {
            'issuelinks': [{
                'type': {'id': '10120', 'name': 'Cloners', 'inward': 'is cloned by'},
                'outwardIssue': {'key': 'RHEL-101'}
            }]
        }
        s = self._session_with_issue(fields)
        assert iss.has_clone_links(s, 'RHEL-100') is True

    def test_no_links_returns_false(self):
        s = self._session_with_issue({'issuelinks': []})
        assert iss.has_clone_links(s, 'RHEL-100') is False

    def test_non_clone_link_returns_false(self):
        fields = {
            'issuelinks': [{
                'type': {'id': '10010', 'name': 'Blocks', 'inward': 'is blocked by'},
                'outwardIssue': {'key': 'RHEL-101'}
            }]
        }
        s = self._session_with_issue(fields)
        assert iss.has_clone_links(s, 'RHEL-100') is False

    def test_error_returns_false(self):
        s = _make_session()
        s.get = MagicMock(side_effect=Exception('network error'))
        assert iss.has_clone_links(s, 'RHEL-100') is False


# ══════════════════════════════════════════════════════════════════════════════
# issue_request_clone
# ══════════════════════════════════════════════════════════════════════════════

class TestIssueRequestClone:
    def test_dry_run_skips_api(self):
        s = _make_session()
        with patch('requests.put') as mock_put:
            iss.issue_request_clone(s, 'RHEL-100', dry_run=True)
            mock_put.assert_not_called()

    def test_puts_to_correct_field(self):
        s = _make_session(url='https://redhat.atlassian.net')
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        with patch('requests.put', return_value=mock_resp) as mock_put:
            result = iss.issue_request_clone(s, 'RHEL-100', dry_run=False)
            body = mock_put.call_args[1]['json']
            assert body['fields']['customfield_10941']['value'] == 'All Active Z-streams'
        assert result is True

    def test_non_204_returns_false(self):
        s = _make_session()
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = 'Bad Request'
        with patch('requests.put', return_value=mock_resp):
            assert iss.issue_request_clone(s, 'RHEL-100', dry_run=False) is False

    def test_accepts_dict_issue(self):
        s = _make_session()
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        with patch('requests.put', return_value=mock_resp):
            iss.issue_request_clone(s, {'key': 'RHEL-100'}, dry_run=False)


# ══════════════════════════════════════════════════════════════════════════════
# issue_get_state
# ══════════════════════════════════════════════════════════════════════════════

class TestIssueGetState:
    def test_from_dict(self):
        issue = {'fields': {'status': {'name': 'PLANNING'}}}
        assert iss.issue_get_state(issue) == 'PLANNING'

    def test_from_python_jira_object(self):
        issue = MagicMock()
        issue.fields.status = 'IN PROGRESS'
        assert iss.issue_get_state(issue) == 'IN PROGRESS'

    def test_missing_status(self):
        issue = {'fields': {}}
        result = iss.issue_get_state(issue)
        assert result == 'Unknown'


# ══════════════════════════════════════════════════════════════════════════════
# make_jira_client — URL resolution
# ══════════════════════════════════════════════════════════════════════════════

class TestMakeJiraClient:
    @patch('requests.get')
    @patch('jira.JIRA')
    def test_resolves_redirect_url(self, mock_jira, mock_get):
        mock_resp = MagicMock()
        mock_resp.url = 'https://redhat.atlassian.net/rest/api/3/serverInfo'
        mock_get.return_value = mock_resp
        mock_jira.return_value = MagicMock()

        session = iss.make_jira_client('https://issues.redhat.com', 'token',
                                        jira_user='u@h.com')
        # Should use the resolved atlassian URL
        assert 'atlassian.net' in session.url

    @patch('requests.get')
    @patch('jira.JIRA')
    def test_cloud_detected_from_resolved_url(self, mock_jira, mock_get):
        mock_resp = MagicMock()
        mock_resp.url = 'https://redhat.atlassian.net/rest/api/3/serverInfo'
        mock_get.return_value = mock_resp
        mock_jira.return_value = MagicMock()

        session = iss.make_jira_client('https://issues.redhat.com', 'token',
                                        jira_user='u@h.com')
        assert session.is_cloud is True

    @patch('requests.get')
    @patch('jira.JIRA')
    def test_returns_none_on_jira_error(self, mock_jira, mock_get):
        from jira import JIRAError
        mock_resp = MagicMock()
        mock_resp.url = 'https://redhat.atlassian.net/rest/api/3/serverInfo'
        mock_get.return_value = mock_resp
        mock_jira.side_effect = JIRAError('connect failed')

        session = iss.make_jira_client('https://issues.redhat.com', 'token')
        assert session is None
