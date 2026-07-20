#!/usr/bin/python3
# vim:set et sw=4:
#
# fix_se_contacts.py - Retroactively set the SE contact as QA contact
# (customfield_10470) on the RHEL bugs for all sustaining engineering
# releases listed in meta/rhel.list.
#
# Usage:
#   ./fix_se_contacts.py [--dry-run] [--se-contact email@redhat.com]

import sys
import getopt
import requests

config_file = './config.cfg'
rhel_list   = './meta/rhel.list'

from caupdate.issues import make_jira_client
from caupdate.release import discover_rhel_releases, get_ga_list, load_errata_map, CA_CERTS_FILE

DRY_RUN        = False
jira_url_base  = 'https://issues.redhat.com'
jira_api_key   = None
jira_user      = None
se_contact     = None
errata_url     = 'https://errata.devel.redhat.com'
errata_cache   = './errata_cache'

try:
    opts, _ = getopt.getopt(sys.argv[1:], '', ['dry-run', 'se-contact='])
except getopt.GetoptError as err:
    print(err)
    print('Usage: fix_se_contacts.py [--se-contact email] [--dry-run]')
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
    if key == 'se_contact':    se_contact    = value
    if key == 'errata_url':    errata_url    = value

for opt, arg in opts:
    if opt == '--dry-run':     DRY_RUN    = True
    if opt == '--se-contact':  se_contact = arg

if not se_contact:
    print('SE contact required. Use --se-contact or set se_contact in config.cfg')
    sys.exit(1)

# ── connect ───────────────────────────────────────────────────────────────────

Jira = make_jira_client(jira_url_base, jira_api_key, jira_user=jira_user)
if not Jira:
    print('Failed to connect to Jira')
    sys.exit(1)

# ── identify SE releases ──────────────────────────────────────────────────────

errata_map, ga_list, _ = load_errata_map(errata_url, errata_cache, CA_CERTS_FILE)
se_releases = {r['release'] for r in discover_rhel_releases(errata_map, ga_list)
               if r['is_sustaining']}
print(f'SE releases: {sorted(se_releases)}\n')

# ── resolve SE contact accountId ──────────────────────────────────────────────

r = requests.get(f'{Jira.url}/rest/api/3/user/search',
                 params={'query': se_contact},
                 headers=Jira._headers(), timeout=30)
users = r.json()
if not users:
    print(f'Could not resolve {se_contact} to a Jira accountId')
    sys.exit(1)
account_id = users[0]['accountId']
print(f'Resolved {se_contact} → {account_id}\n')

# ── read rhel.list, find SE releases with RHEL bugs ───────────────────────────

payload = {'fields': {'customfield_10470': {'accountId': account_id}}}

try:
    for line in open(rhel_list):
        parts = line.strip().split(':')
        if len(parts) < 3:
            continue
        release   = parts[0]
        bugnumber = parts[2]
        if release not in se_releases or not bugnumber or bugnumber == '0':
            continue
        print(f'{release}: {bugnumber}', end=' ... ')
        if DRY_RUN:
            print(f'DRY_RUN: would set QA={se_contact}')
            continue
        try:
            resp = requests.put(f'{Jira.url}/rest/api/3/issue/{bugnumber}',
                                json=payload, headers=Jira._headers(), timeout=30)
            print('OK' if resp.status_code == 204
                  else f'FAILED {resp.status_code}: {resp.text[:80]}')
        except Exception as e:
            print(f'ERROR: {e}')
except FileNotFoundError:
    print(f'{rhel_list} not found')
    sys.exit(1)

print('\nDone.')
