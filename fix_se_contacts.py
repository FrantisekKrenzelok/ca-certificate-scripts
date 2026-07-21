#!/usr/bin/python3
# vim:set et sw=4:
#
# fix_se_contacts.py - Retroactively fix all RHEL bugs in meta/rhel.list:
#
#   1. Transition to "New" (reset state)
#   2. Call cryptosvc triage: sets priority/severity/regression and creates
#      [DEV]/[QE] CRYPTO splits
#   3. SE releases:  set se_contact as QA (customfield_10470) on RHEL bug
#      Active releases: set qe as assignee on [DEV]/[QE] sub-issues
#
# Usage:
#   ./fix_se_contacts.py [--dry-run] [--se-contact email] [--qe email]

import sys
import getopt
import requests
from requests_kerberos import HTTPKerberosAuth

config_file = './config.cfg'
rhel_list   = './meta/rhel.list'

from caupdate.issues import make_jira_client
from caupdate.release import (
    discover_rhel_releases, get_ga_list, load_errata_map, CA_CERTS_FILE
)

QA_SUMMARIES = ('Errata Workflow Checklist', 'Automated Testing')

DRY_RUN        = False
jira_url_base  = 'https://issues.redhat.com'
jira_api_key   = None
jira_user      = None
qe             = None
se_contact     = None
errata_url     = 'https://errata.devel.redhat.com'
errata_cache   = './errata_cache'
cryptosvc_url  = None
cryptosvc_token = None
cryptosvc_pat  = None
ca_certs_file  = CA_CERTS_FILE

try:
    opts, _ = getopt.getopt(sys.argv[1:], '', ['dry-run', 'se-contact=', 'qe='])
except getopt.GetoptError as err:
    print(err)
    print('Usage: fix_se_contacts.py [--se-contact email] [--qe email] [--dry-run]')
    sys.exit(2)

for config_line in open(config_file):
    line = config_line.strip()
    if not line or line.startswith('#'):
        continue
    key, value = line.split(':', 1)
    value = value.strip()
    if key == 'jira_url':               jira_url_base  = value
    if key == 'jira_api_key':           jira_api_key   = value
    if key == 'jira_user':              jira_user      = value
    if key == 'qe':                     qe             = value
    if key == 'se_contact':             se_contact     = value
    if key == 'errata_url':             errata_url     = value
    if key == 'cryptosvc_url':          cryptosvc_url  = value
    if key == 'cryptosvc_access_token': cryptosvc_token = value
    if key == 'cryptosvc_pat':          cryptosvc_pat  = value

for opt, arg in opts:
    if opt == '--dry-run':     DRY_RUN    = True
    if opt == '--se-contact':  se_contact = arg
    if opt == '--qe':          qe         = arg

if not se_contact:
    print('se_contact required. Use --se-contact or set se_contact in config.cfg')
    sys.exit(1)
if not qe:
    print('qe required. Use --qe or set qe in config.cfg')
    sys.exit(1)
if not cryptosvc_url or not cryptosvc_token or not cryptosvc_pat:
    print('cryptosvc_url, cryptosvc_access_token, cryptosvc_pat required in config.cfg')
    sys.exit(1)

# ── connect ───────────────────────────────────────────────────────────────────

Jira = make_jira_client(jira_url_base, jira_api_key, jira_user=jira_user)
if not Jira:
    print('Failed to connect to Jira')
    sys.exit(1)

# ── resolve email → accountId ─────────────────────────────────────────────────

def resolve(email):
    r = requests.get(f'{Jira.url}/rest/api/3/user/search',
                     params={'query': email},
                     headers=Jira._headers(), timeout=30)
    users = r.json()
    if not users:
        print(f'Could not resolve {email} to a Jira accountId')
        sys.exit(1)
    account_id = users[0]['accountId']
    print(f'  {email} → {account_id}')
    return account_id

print('Resolving contacts:')
se_account = resolve(se_contact)
qe_account = resolve(qe)
print()

# ── identify SE vs active releases ────────────────────────────────────────────

errata_map, ga_list, _ = load_errata_map(errata_url, errata_cache, CA_CERTS_FILE)
se_releases = {r['release'] for r in discover_rhel_releases(errata_map, ga_list)
               if r['is_sustaining']}

# ── helpers ───────────────────────────────────────────────────────────────────

def put_field(key, fields):
    if DRY_RUN:
        print(f'    DRY_RUN: PUT {key} {fields}')
        return True
    try:
        r = requests.put(f'{Jira.url}/rest/api/3/issue/{key}',
                         json={'fields': fields}, headers=Jira._headers(), timeout=30)
        if r.status_code == 204:
            return True
        print(f'    FAILED {r.status_code}: {r.text[:100]}')
    except Exception as e:
        print(f'    ERROR: {e}')
    return False

def transition_to_new(issue_key):
    """Find the transition that leads to 'New' status and apply it."""
    if DRY_RUN:
        print(f'    DRY_RUN: would transition {issue_key} to New')
        return True
    try:
        r = requests.get(f'{Jira.url}/rest/api/3/issue/{issue_key}/transitions',
                         headers=Jira._headers(), timeout=30)
        for t in r.json().get('transitions', []):
            if t['to']['name'].lower() in ('new', 'to do', 'open'):
                resp = requests.post(
                    f'{Jira.url}/rest/api/3/issue/{issue_key}/transitions',
                    json={'transition': {'id': t['id']}},
                    headers=Jira._headers(), timeout=30)
                return resp.status_code == 204
        print(f'    WARNING: no transition to New found for {issue_key}')
    except Exception as e:
        print(f'    ERROR: {e}')
    return False

SPLIT_LINK_TYPE_ID = '10120'  # "split from" / "split to" in Jira

def set_triage_fields(bugnumber):
    """Set priority/severity/regression directly on the RHEL bug."""
    return put_field(bugnumber, {
        'priority':          {'name': 'Normal'},
        'customfield_10840': {'value': 'Moderate'},  # severity
        'customfield_10623': {'value': 'No'},         # regression
    })

def transition_issue(issue_key, target_state):
    """Transition issue to the named target state."""
    if DRY_RUN:
        print(f'    DRY_RUN: would transition {issue_key} → {target_state}')
        return True
    try:
        r = requests.get(f'{Jira.url}/rest/api/3/issue/{issue_key}/transitions',
                         headers=Jira._headers(), timeout=30)
        for t in r.json().get('transitions', []):
            if t['to']['name'].lower() == target_state.lower():
                resp = requests.post(
                    f'{Jira.url}/rest/api/3/issue/{issue_key}/transitions',
                    json={'transition': {'id': t['id']}},
                    headers=Jira._headers(), timeout=30)
                return resp.status_code == 204
        print(f'    WARNING: no transition to "{target_state}" found')
    except Exception as e:
        print(f'    ERROR transitioning {issue_key}: {e}')
    return False

def create_splits(bugnumber):
    """Create [DEV] and [QE] CRYPTO task issues linked to the RHEL bug."""
    try:
        issue   = Jira.get(bugnumber)
        summary = issue.get('fields', {}).get('summary', bugnumber)
        comps   = [{'name': c['name']}
                   for c in issue.get('fields', {}).get('components', [])]
    except Exception as e:
        print(f'    WARNING: could not fetch {bugnumber}: {e}')
        return

    for prefix, desc in (('[DEV]', f'Development activities to resolve {bugnumber}'),
                         ('[QE]',  f'Quality assurance activities for {bugnumber}')):
        fields = {
            'project':   {'key': 'CRYPTO'},
            'issuetype': {'name': 'Task'},
            'summary':   f'{prefix} {summary}',
        }
        if comps:
            fields['components'] = comps
        try:
            result  = Jira.create(fields)
            new_key = result.get('key', '?')
            # Link back to RHEL bug with the split link type
            link_payload = {
                'type':         {'id': SPLIT_LINK_TYPE_ID},
                'inwardIssue':  {'key': bugnumber},
                'outwardIssue': {'key': new_key},
            }
            requests.post(f'{Jira.url}/rest/api/3/issueLink',
                          json=link_payload, headers=Jira._headers(), timeout=30)
            print(f'    created {new_key} ({prefix})')
        except Exception as e:
            print(f'    WARNING: could not create {prefix} split: {e}')

# ── process rhel.list ─────────────────────────────────────────────────────────

try:
    entries = [line.strip().split(':') for line in open(rhel_list) if line.strip()]
except FileNotFoundError:
    print(f'{rhel_list} not found')
    sys.exit(1)

for parts in entries:
    if len(parts) < 3:
        continue
    release    = parts[0]
    bugnumber  = parts[2]
    crypto_key = parts[8] if len(parts) > 8 else ''

    if not bugnumber or bugnumber == '0':
        print(f'{release}: no RHEL bug yet, skipping')
        continue

    print(f'\n{release}: {bugnumber}')

    # 1. Transition to New
    print(f'  transitioning to New ...', end=' ')
    if transition_to_new(bugnumber):
        print('OK')

    # 2. Set triage fields (priority/severity/regression) directly
    print(f'  setting triage fields ...', end=' ')
    if set_triage_fields(bugnumber):
        print('OK')

    # 3. Transition to PLANNING
    print(f'  transitioning to PLANNING ...', end=' ')
    if transition_issue(bugnumber, 'PLANNING'):
        print('OK')

    # 4. Zero story points
    print(f'  zeroing story points ...', end=' ')
    if put_field(bugnumber, {'customfield_10028': 0}):
        print('OK')

    # 5. Create [DEV]/[QE] CRYPTO splits directly
    print(f'  creating [DEV]/[QE] splits ...')
    create_splits(bugnumber)

    # 4. SE or active release specific handling
    if release in se_releases:
        print(f'  [SE] setting QA contact = {se_contact} ...', end=' ')
        if put_field(bugnumber, {'customfield_10470': {'accountId': se_account}}):
            print('OK')
    else:
        # Set qe as assignee on [DEV]/[QE] sub-issues in CRYPTO
        if not crypto_key or not crypto_key.startswith('CRYPTO-'):
            print(f'  no CRYPTO epic, skipping QE sub-issue assignment')
            continue
        jql = f'project=CRYPTO AND "Epic Link" = "{crypto_key}"'
        try:
            children = Jira.search(jql, fields=['key', 'summary'], max_results=20)
        except Exception as e:
            print(f'  WARNING: could not search CRYPTO children: {e}')
            continue
        for issue in children:
            key     = issue['key']
            summary = issue.get('fields', {}).get('summary', '')
            if summary not in QA_SUMMARIES:
                continue
            print(f'  assigning {qe} to {key} ({summary}) ...', end=' ')
            if put_field(key, {'assignee': {'accountId': qe_account}}):
                print('OK')

print('\nDone.')
