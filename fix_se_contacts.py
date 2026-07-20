#!/usr/bin/python3
# vim:set et sw=4:
#
# fix_se_contacts.py - Fix QA contacts for the current update cycle:
#
#   SE releases  (E4S/AUS/TUS): set se_contact as QA (customfield_10470)
#                                on the RHEL bug.
#   Active releases:             set qe as assignee on the 'Errata Workflow
#                                Checklist' and 'Automated Testing' sub-issues
#                                of the CRYPTO epic.
#
# Usage:
#   ./fix_se_contacts.py [--dry-run]
#   ./fix_se_contacts.py [--se-contact email] [--qe email] [--dry-run]

import sys
import getopt
import requests

config_file = './config.cfg'
rhel_list   = './meta/rhel.list'

from caupdate.issues import make_jira_client
from caupdate.release import discover_rhel_releases, get_ga_list, load_errata_map, CA_CERTS_FILE

QA_SUMMARIES = ('Errata Workflow Checklist', 'Automated Testing')

DRY_RUN        = False
jira_url_base  = 'https://issues.redhat.com'
jira_api_key   = None
jira_user      = None
qe             = None
se_contact     = None
errata_url     = 'https://errata.devel.redhat.com'
errata_cache   = './errata_cache'

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
    if key == 'jira_url':      jira_url_base = value
    if key == 'jira_api_key':  jira_api_key  = value
    if key == 'jira_user':     jira_user     = value
    if key == 'qe':            qe            = value
    if key == 'se_contact':    se_contact    = value
    if key == 'errata_url':    errata_url    = value

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

def put_field(issue_key, fields):
    if DRY_RUN:
        print(f'    DRY_RUN: PUT {issue_key} {fields}')
        return True
    try:
        r = requests.put(f'{Jira.url}/rest/api/3/issue/{issue_key}',
                         json={'fields': fields}, headers=Jira._headers(), timeout=30)
        if r.status_code == 204:
            return True
        print(f'    FAILED {r.status_code}: {r.text[:100]}')
    except Exception as e:
        print(f'    ERROR: {e}')
    return False

# ── process rhel.list ─────────────────────────────────────────────────────────

try:
    entries = [line.strip().split(':') for line in open(rhel_list)
               if line.strip()]
except FileNotFoundError:
    print(f'{rhel_list} not found')
    sys.exit(1)

for parts in entries:
    if len(parts) < 3:
        continue
    release    = parts[0]
    bugnumber  = parts[2]
    crypto_key = parts[8] if len(parts) > 8 else ''

    if release in se_releases:
        # ── SE release: set SE contact as QA on the RHEL bug ─────────────────
        if not bugnumber or bugnumber == '0':
            print(f'{release}: no RHEL bug yet, skipping')
            continue
        print(f'{release} [SE]: setting QA={se_contact} on {bugnumber}')
        if put_field(bugnumber, {'customfield_10470': {'accountId': se_account}}):
            print(f'  OK')

    else:
        # ── Active release: set qe as assignee on CRYPTO QA sub-issues ───────
        if not crypto_key or not crypto_key.startswith('CRYPTO-'):
            print(f'{release}: no CRYPTO epic yet, skipping')
            continue
        print(f'{release}: setting assignee={qe} on QA sub-issues of {crypto_key}')
        summaries = '", "'.join(QA_SUMMARIES)
        jql = f'project=CRYPTO AND "Epic Link" = "{crypto_key}" AND summary in ("{summaries}")'
        try:
            sub_issues = Jira.search(jql, fields=['key', 'summary'], max_results=10)
        except Exception as e:
            print(f'  WARNING: search failed: {e}')
            continue
        if not sub_issues:
            print(f'  WARNING: no QA sub-issues found')
            continue
        for issue in sub_issues:
            key     = issue['key']
            summary = issue.get('fields', {}).get('summary', key)
            print(f'  {key} ({summary})', end=' ... ')
            if put_field(key, {'assignee': {'accountId': qe_account}}):
                print('OK')

print('\nDone.')
