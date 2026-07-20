#!/usr/bin/python3
# vim:set et sw=4:
#
# fix_crypto_parent.py - Retroactively set the parent epic on all CRYPTO
# epics listed in meta/rhel.list.
#
# Usage:
#   ./fix_crypto_parent.py [--parent CRYPTO-19515] [--dry-run]
#
# Reads meta/rhel.list (field 9 = CRYPTO key), connects to Jira, and
# sets the parent field on each CRYPTO epic that doesn't already have one.

import sys
import getopt
import requests

config_file = './config.cfg'
rhel_list   = './meta/rhel.list'

from caupdate.issues import make_jira_client

# ── config ────────────────────────────────────────────────────────────────────

DRY_RUN        = False
jira_url_base  = 'https://issues.redhat.com'
jira_api_key   = None
jira_user      = None
parent_key     = None
config         = {}

try:
    opts, _ = getopt.getopt(sys.argv[1:], '', ['dry-run', 'parent='])
except getopt.GetoptError as err:
    print(err)
    print('Usage: fix_crypto_parent.py [--parent CRYPTO-XXXXX] [--dry-run]')
    sys.exit(2)

for config_line in open(config_file):
    line = config_line.strip()
    if not line or line.startswith('#'):
        continue
    key, value = line.split(':', 1)
    value = value.strip()
    config[key] = value
    if key == 'jira_url':            jira_url_base = value
    if key == 'jira_api_key':        jira_api_key  = value
    if key == 'jira_user':           jira_user     = value
    if key == 'crypto_epic_parent':  parent_key    = value

for opt, arg in opts:
    if opt == '--dry-run': DRY_RUN   = True
    if opt == '--parent':  parent_key = arg

if not parent_key:
    print('Parent key required. Use --parent CRYPTO-XXXXX or set crypto_epic_parent in config.cfg')
    sys.exit(1)

# ── connect ───────────────────────────────────────────────────────────────────

Jira = None
if not DRY_RUN:
    Jira = make_jira_client(jira_url_base, jira_api_key, jira_user=jira_user)
    if not Jira:
        print('Failed to connect to Jira')
        sys.exit(1)

# ── read rhel.list and collect CRYPTO keys ────────────────────────────────────

crypto_keys = []
try:
    for line in open(rhel_list):
        parts = line.strip().split(':')
        if len(parts) >= 9 and parts[8].startswith('CRYPTO-'):
            release    = parts[0]
            crypto_key = parts[8]
            crypto_keys.append((release, crypto_key))
except FileNotFoundError:
    print(f'{rhel_list} not found')
    sys.exit(1)

if not crypto_keys:
    print('No CRYPTO keys found in rhel.list')
    sys.exit(0)

print(f'Setting parent {parent_key} on {len(crypto_keys)} CRYPTO epics\n')

# ── set parent ────────────────────────────────────────────────────────────────

payload = {'fields': {'parent': {'key': parent_key}}}

for release, crypto_key in crypto_keys:
    print(f'{release}: {crypto_key}', end=' ... ')
    if DRY_RUN:
        print(f'DRY_RUN: would set parent {parent_key}')
        continue
    try:
        r = requests.put(
            f'{Jira.url}/rest/api/3/issue/{crypto_key}',
            json=payload, headers=Jira._headers(), timeout=30)
        if r.status_code == 204:
            print('OK')
        else:
            print(f'FAILED {r.status_code}: {r.text[:120]}')
    except Exception as e:
        print(f'ERROR: {e}')

print('\nDone.')
