"""
Jira issue helpers for the ca-certificates update pipeline.

Uses the Jira Cloud v3 REST API directly for search and create (python-jira
still uses v2 for those, which was removed from Cloud in 2026).  Transitions
and field updates continue to go through python-jira.

Functions take all needed values as explicit parameters (no globals).
"""

import base64
import requests
from jira import JIRAError

JIRA_PROJ       = 'RHEL'
JIRA_ISSUE_TYPE = 'Bug'

bug_summary_short = 'Annual %s ca-certificates update'
bug_summary = (bug_summary_short +
               ' version %s from NSS %s for Firefox %s and Microsoft %s [%s]')
bug_description = ('Update CA certificates to version %s from NSS %s and '
                   'Microsoft %s for our annual CA certificate update.')

# ── Jira session ──────────────────────────────────────────────────────────────

class JiraSession:
    """
    Thin wrapper around a Jira connection.  Carries both the python-jira
    client (for transitions/updates) and raw credentials for v3 API calls.

    Jira Cloud (*.atlassian.net) requires Basic Auth with email:token.
    Jira Server/Data Center uses Bearer token auth.
    """
    def __init__(self, client, url, token, user=None):
        self.client = client
        self.url    = url.rstrip('/')
        self.token  = token
        self.user   = user
        self.is_cloud = 'atlassian.net' in self.url

    def _headers(self):
        if self.is_cloud and self.user:
            creds = base64.b64encode(f'{self.user}:{self.token}'.encode()).decode()
            auth  = f'Basic {creds}'
        else:
            auth = f'Bearer {self.token}'
        return {
            'Authorization': auth,
            'Content-Type':  'application/json',
            'Accept':        'application/json',
        }

    def search(self, jql, fields=None, max_results=50):
        """POST /rest/api/3/search/jql — returns raw issue dicts."""
        body = {'jql': jql, 'maxResults': max_results}
        if fields:
            body['fields'] = fields
        r = requests.post(
            f'{self.url}/rest/api/3/search/jql',
            json=body, headers=self._headers(), timeout=30)
        r.raise_for_status()
        return r.json().get('issues', [])

    def create(self, fields):
        """POST /rest/api/3/issue — returns the created issue dict."""
        r = requests.post(
            f'{self.url}/rest/api/3/issue',
            json={'fields': fields}, headers=self._headers(), timeout=30)
        r.raise_for_status()
        return r.json()

    def get(self, key):
        """GET /rest/api/3/issue/{key}"""
        r = requests.get(
            f'{self.url}/rest/api/3/issue/{key}',
            headers=self._headers(), timeout=30)
        r.raise_for_status()
        return r.json()

def make_jira_client(jira_url, jira_api_key, jira_user=None):
    """Initialise and return a JiraSession."""
    import jira as jiralib
    url = jira_url.rstrip('/')

    # Resolve the actual server URL — issues.redhat.com redirects to
    # redhat.atlassian.net and a raw requests.post() would follow that
    # redirect as GET, breaking issue creation.
    try:
        r = requests.get(
            f'{url}/rest/api/3/serverInfo',
            headers={'Authorization': f'Bearer {jira_api_key}',
                     'Accept': 'application/json'},
            timeout=30)
        actual_url = r.url.split('/rest/')[0].rstrip('/')
        if actual_url != url:
            print(f'Note: {url} → {actual_url}')
            url = actual_url
    except Exception as e:
        print(f'Warning: could not resolve Jira URL ({e}), using {url} as-is')

    is_cloud = 'atlassian.net' in url
    base_options = {'server': url, 'verify': True}
    if is_cloud and jira_user:
        # Jira Cloud: Basic Auth with email:api_token
        constructor_args = {'options': base_options,
                            'basic_auth': (jira_user, jira_api_key)}
    else:
        # Jira Server/Data Center: Bearer PAT
        constructor_args = {'options': base_options, 'token_auth': jira_api_key}

    if 'stage' in url:
        print('staging instance')
        constructor_args['proxies'] = {
            'http':  'http://squid.corp.redhat.com:3128',
            'https': 'http://squid.corp.redhat.com:3128',
        }
    try:
        client = jiralib.JIRA(**constructor_args)
        return JiraSession(client, url, jira_api_key, user=jira_user)
    except JIRAError as e:
        print(f'JIRA Error connecting to {url}: {e}')
    except Exception as e:
        print(f'Unexpected error connecting to JIRA at {url}: {e}')
    return None

# ── helpers ───────────────────────────────────────────────────────────────────

def _adf(text):
    """Wrap plain text in Atlassian Document Format for the v3 API."""
    return {
        'type': 'doc',
        'version': 1,
        'content': [{'type': 'paragraph',
                     'content': [{'type': 'text', 'text': text}]}],
    }

# ── issue CRUD ────────────────────────────────────────────────────────────────

def jira_fixversion(release):
    """
    Convert an errata-map release string (always rhel-X.Y.Z) to a Jira fixVersion.

    version_parts=2 majors (RHEL 10+, default):
      rhel-X.Y.Z → rhel-X.Y  (always strip .Z)

    version_parts=3 majors (RHEL 8/9, explicit):
      Uses jira_keep_zero_below_minor to decide whether to keep or strip the .0:
        minor < threshold → keep   (e.g. rhel-8.8.0.z, rhel-9.2.0.z)
        minor >= threshold → strip (e.g. rhel-9.6)
    """
    import re
    from caupdate.release_config import version_parts as _vp, jira_keep_zero
    m = re.match(r'^rhel-(\d+)\.(\d+)\.(\d+)$', release)
    if not m:
        return release
    major, minor = int(m.group(1)), int(m.group(2))
    if _vp(major) == 2:
        return f'rhel-{major}.{minor}'  # X.Y format for new-style majors
    if jira_keep_zero(major, minor):
        return release  # keep the .0 for old z-streams (e.g. rhel-8.8.0)
    return f'rhel-{major}.{minor}'  # strip the .0 for current stream

def issue_create(session, release, version, nss_version, firefox_version,
                 mcs_version, packages, zstream, year):
    """Create a new RHEL Jira bug. Returns (key, raw_dict) or ('0', None)."""
    package = packages.split(',')[0]
    release = jira_fixversion(release)  # rhel-X.Y.0 → rhel-X.Y

    if zstream:
        release += '.z'

    fields = {
        'project':           {'key': JIRA_PROJ},
        'issuetype':         {'name': JIRA_ISSUE_TYPE},
        'summary':           bug_summary % (year, version, nss_version,
                                            firefox_version, mcs_version, release),
        'description':       _adf(bug_description % (version, nss_version, mcs_version)),
        'fixVersions':       [{'name': release}],
        'components':        [{'name': package}],
        'priority':          {'name': 'Minor'},
        'security':          {'name': 'Red Hat Employee'},
        'labels':            ['Triaged', 'Rebase'],
        'customfield_10028': 0,  # story points
    }

    try:
        issue = session.create(fields)
    except requests.HTTPError as e:
        print(f"Issue couldn't be created: {e} — {e.response.text}")
        return '0', None
    return issue['key'], issue

def issue_lookup(session, release, version, packages, year, zstream=False):
    """Look up an existing RHEL Jira bug. Returns (key, raw_dict) or ('0', None)."""
    package = packages.split(',')[0]
    summary = bug_summary_short % year
    release = jira_fixversion(release)  # rhel-X.Y.0 → rhel-X.Y

    if zstream:
        release += '.z'

    jql = (f'project={JIRA_PROJ} AND issuetype={JIRA_ISSUE_TYPE} AND '
           f'component="{package}" AND summary~"{summary}" AND fixVersion="{release}"')
    print(jql)

    try:
        issues = session.search(jql, fields=['summary', 'key', 'status'])
    except requests.HTTPError as e:
        print(f'Search failed: {e} — {e.response.text}')
        return '0', None

    if not issues:
        print(f'Found 0 issues matching {summary}')
        return '0', None
    if len(issues) != 1:
        print(f'Found {len(issues)} issues matching {summary}')
        return '0', None

    return issues[0]['key'], issues[0]

def has_clone_links(session, issue_key):
    """Return True if the GA issue already has clone-type outward links (clones exist)."""
    try:
        issue = session.get(issue_key)
        for link in issue.get('fields', {}).get('issuelinks', []):
            t = link.get('type', {})
            if ('clone' in t.get('name', '').lower() or
                    'clone' in t.get('inward', '').lower()):
                return True
    except Exception as e:
        print(f'  WARNING: could not check clone links for {issue_key}: {e}')
    return False

def issue_update_versions(session, issue_or_key, version, nss_version,
                          firefox_version, mcs_version, release, zstream, year):
    """Update summary and description of an existing issue with actual build versions.

    Called from process.py after build_combo.py has determined the real NSS/CKBI
    versions, which may differ from what plan.py used at planning time.
    """
    key = issue_or_key if isinstance(issue_or_key, str) else issue_or_key.get('key', str(issue_or_key))
    fv  = jira_fixversion(release)
    if zstream:
        fv += '.z'
    payload = {
        'fields': {
            'summary':     bug_summary % (year, version, nss_version,
                                          firefox_version, mcs_version, fv),
            'description': _adf(bug_description % (version, nss_version, mcs_version)),
        }
    }
    try:
        r = requests.put(f'{session.url}/rest/api/3/issue/{key}',
                         json=payload, headers=session._headers(), timeout=30)
        if r.status_code == 204:
            print(f'  {key}: summary/description updated with NSS {nss_version} / {version}')
            return True
        print(f'  WARNING: version update on {key} returned {r.status_code}: {r.text}')
        return False
    except requests.RequestException as e:
        print(f'  WARNING: version update on {key} failed: {e}')
        return False


def issue_request_clone(session, issue_or_key, dry_run=False):
    """Request 'Clone for all active z-streams' on a GA issue via customfield_10941."""
    key = issue_or_key if isinstance(issue_or_key, str) else issue_or_key['key']
    if dry_run:
        print(f'  DRY_RUN: would request z-stream clone for {key}')
        return True
    payload = {'fields': {'customfield_10941': {'value': 'All Active Z-streams'}}}
    try:
        r = requests.put(f'{session.url}/rest/api/3/issue/{key}',
                         json=payload, headers=session._headers(), timeout=30)
        if r.status_code == 204:
            return True
        print(f'  WARNING: clone request on {key} returned {r.status_code}: {r.text}')
        return False
    except requests.RequestException as e:
        print(f'  WARNING: clone request on {key} failed: {e}')
        return False

def issue_get_state(issue_or_dict):
    """Return the current status string of a Jira issue (python-jira obj or raw dict)."""
    if isinstance(issue_or_dict, dict):
        return issue_or_dict.get('fields', {}).get('status', {}).get('name', 'Unknown')
    return str(issue_or_dict.fields.status)

def issue_change_state(session, issue_key, state):
    """Transition issue to the named state. Returns the new state string."""
    try:
        jira_issue = session.client.issue(issue_key)
        session.client.transition_issue(jira_issue, state)
        jira_issue = session.client.issue(issue_key)
        return str(jira_issue.fields.status)
    except JIRAError as e:
        print(f"Couldn't transition to {state}: {e}")
        return 'Unknown'

def issue_get(session, bugnumber):
    """Fetch a Jira issue by key. Returns raw dict or None."""
    try:
        return session.get(bugnumber)
    except requests.HTTPError as e:
        print(f'Failed to fetch {bugnumber}: {e}')
        return None
