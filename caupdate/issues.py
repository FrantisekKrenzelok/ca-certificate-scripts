"""
Jira issue helpers for the ca-certificates update pipeline.

Functions take all needed values as explicit parameters (no globals).
"""

from jira import JIRAError

JIRA_PROJ       = 'RHEL'
JIRA_ISSUE_TYPE = 'Bug'

bug_summary_short = 'Annual %s ca-certificates update'
bug_summary = (bug_summary_short +
               ' version %s from NSS %s for Firefox %s and Microsoft %s [%s]')
bug_description = ('Update CA certificates to version %s from NSS %s and '
                   'Microsoft %s for our annual CA certificate update.')

# ── issue CRUD ────────────────────────────────────────────────────────────────

def issue_create(Jira, release, version, nss_version, firefox_version,
                 mcs_version, packages, zstream, year):
    """Create a new RHEL Jira bug. Returns (key, issue) or ('0', None) on failure."""
    package = packages.split(',')[0]

    if release == 'rhel-8.10.0':   # Jira uses rhel-8.10, not rhel-8.10.0
        release = 'rhel-8.10'

    if zstream:
        release += '.z'

    fields = {
        'project':     {'key': JIRA_PROJ},
        'issuetype':   {'name': JIRA_ISSUE_TYPE},
        'summary':     bug_summary % (year, version, nss_version,
                                      firefox_version, mcs_version, release),
        'description': bug_description % (version, nss_version, mcs_version),
        'fixVersions': [{'name': release}],
        'components':  [{'name': package}],
        'priority':    {'name': 'Minor'},
        'security':    {'name': 'Red Hat Employee'},
        'labels':      ['Triaged', 'Rebase'],
    }

    try:
        new_issue = Jira.create_issue(fields=fields)
    except JIRAError as e:
        print(f"Issue couldn't be created: {e}")
        return '0', None
    return new_issue.key, new_issue

def issue_lookup(Jira, release, version, packages, year, zstream=False):
    """Look up an existing RHEL Jira bug. Returns (key, issue) or ('0', None)."""
    package = packages.split(',')[0]
    summary = bug_summary_short % year

    if zstream:
        release += '.z'

    jql = (f'project={JIRA_PROJ} AND issuetype={JIRA_ISSUE_TYPE} AND '
           f'component={package} AND summary~"{summary}" AND fixVersion={release}')
    print(jql)

    try:
        issues = Jira.search_issues(jql)
    except JIRAError as e:
        print(e)
        return '0', None

    if not issues:
        print(f'Found 0 issues matching {summary}')
        return '0', None
    if len(issues) != 1:
        print(f'Found {len(issues)} issues matching {summary}')
        return '0', None

    return issues[0].key, issues[0]

def issue_request_clone(Jira, release, version, packages, year, dry_run=False):
    """Request a z-stream bug clone for the given GA release bug."""
    _, issue = issue_lookup(Jira, release, version, packages, year)
    if issue is None:
        return False
    if dry_run:
        print(f'  DRY_RUN: would request z-stream clone for {issue.key}')
        return True
    try:
        issue.update({'customfield_12323242': {'id': '33996'}})
    except JIRAError as e:
        print(e)
    return True

def issue_get_state(issue):
    """Return the current status string of a Jira issue."""
    return str(issue.fields.status)

def issue_change_state(Jira, issue, state):
    """Transition issue to the named state. Returns the new state string."""
    try:
        Jira.transition_issue(issue, state)
    except JIRAError as e:
        print(f"Couldn't transition to {state}: {e}")
    issue = Jira.issue(issue.key)
    return issue_get_state(issue)

def issue_get(Jira, bugnumber):
    """Fetch a Jira issue by key. Returns None on failure."""
    try:
        return Jira.issue(bugnumber)
    except JIRAError as e:
        print(e)
        return None

def make_jira_client(jira_url_base, jira_api_key):
    """Initialise and return a python-jira JIRA client."""
    import jira as jiralib
    base_options = {'server': jira_url_base, 'verify': True}
    constructor_args = {'options': base_options, 'token_auth': jira_api_key}
    if 'stage' in jira_url_base:
        print('staging instance')
        constructor_args['proxies'] = {
            'http':  'http://squid.corp.redhat.com:3128',
            'https': 'http://squid.corp.redhat.com:3128',
        }
    try:
        return jiralib.JIRA(**constructor_args)
    except JIRAError as e:
        print(f'JIRA Error connecting to {jira_url_base}: {e}')
    except Exception as e:
        print(f'Unexpected error connecting to JIRA at {jira_url_base}: {e}')
    return None
