#!/usr/bin/python3
# vim:set et sw=4:
#
# plan.py - Pre-pipeline planning step for ca-certificates updates.
#
# Creates RHEL Jira bugs and triggers CRYPTO errata epic creation via
# cryptosvc. Writes meta/rhel.list and meta/fedora.list so that
# build_combo.sh and process.py can run against pre-populated bug numbers.
#
# Usage:
#   ./plan.py -f <firefox_version> releases...
#   ./plan.py -f 138 rhel-9.6.0 rhel-9.4.0 rhel-8.10.0 f43 rawhide
#
# Options:
#   -f <firefox_version>   Firefox version for this update (required)
#   -v <ckbi_version>      Override CKBI version
#   -o <owner_email>       Override owner email from config
#   -m <manager_email>     Override manager email from config
#   --dry-run              Print what would be done without creating anything
#   --resync               Force refresh of errata cache

import os
import sys
import shutil
import getopt
import json
import datetime
import requests

from jwcrypto.common import json_encode
from jwcrypto import jwk, jwe

from caupdate.release import (
    release_get_major, safe_int,
    get_need_zstream_clone,
    load_errata_map, CA_CERTS_FILE,
)
from caupdate.issues import (
    issue_create, issue_lookup, issue_request_clone,
    make_jira_client,
)

DRY_RUN = False

meta_dir          = './meta'
rhel_list         = './meta/rhel.list'
fedora_list       = './meta/fedora.list'
ckbiver_file      = './meta/ckbiversion.txt'
nssver_file       = './meta/nssversion.txt'
mcsver_file       = './meta/mcsversion.txt'
firefox_info      = './meta/firefox_info.txt'
config_file       = './config.cfg'
errata_cache_file = './errata_cache'
errata_url_base   = 'https://errata.devel.redhat.com'
jira_url_base     = 'https://issues.redhat.com'
ca_certs_file     = CA_CERTS_FILE

# ── cryptosvc helpers ─────────────────────────────────────────────────────────

def make_pat(pat_key_json, jira_user, jira_api_key):
    """Encrypt Jira credentials as a JWE PAT for use with cryptosvc."""
    key = jwk.JWK(**json.loads(pat_key_json))
    token = jwe.JWE(
        json_encode({'user': jira_user, 'apikey': jira_api_key}),
        json_encode({'alg': 'A256KW', 'enc': 'A256CBC-HS512'}))
    token.add_recipient(key)
    return token.serialize(compact=True)

def cryptosvc_create_errata(cryptosvc_url, access_token, pat, component, fixversion, bugs):
    """Call the existing cryptosvc /jira/errata/create endpoint."""
    url = cryptosvc_url.rstrip('/') + '/jira/errata/create'
    headers = {
        'Access-Token': access_token,
        'PAT': pat,
        'Content-Type': 'application/json',
    }
    body = {'component': component, 'fixversion': fixversion, 'bugs': bugs}
    if DRY_RUN:
        print(f'  DRY_RUN: POST {url} {body}')
        return True
    r = requests.post(url, headers=headers, json=body, timeout=30,
                      verify=ca_certs_file)
    if r.status_code == 409:
        print(f'  CRYPTO errata epic already exists for {component}/{fixversion}')
        return True
    if r.status_code > 299:
        print(f'  cryptosvc errata create failed: {r.status_code} {r.text}')
        return False
    return True

# ── main ──────────────────────────────────────────────────────────────────────

try:
    opts, release_args = getopt.getopt(
        sys.argv[1:], 'f:v:o:m:', ['dry-run', 'resync'])
except getopt.GetoptError as err:
    print(err)
    print('Usage: plan.py [-f firefox] [-v ckbi_version] [-o owner] [-m manager]'
          ' [--dry-run] [--resync] releases...')
    sys.exit(2)

resync          = False
firefox_version = None
version         = None
owner           = None
manager         = None
jira_api_key    = None
jira_user       = None
cryptosvc_url          = None
cryptosvc_access_token = None
cryptosvc_pat_key      = None
config = {}

for config_line in open(config_file, 'r'):
    line = config_line.strip()
    if not line or line.startswith('#'):
        continue
    key, value = line.split(':', 1)
    value = value.strip()
    config[key] = value
    if key == 'owner':                    owner = value
    if key == 'manager':                  manager = value
    if key == 'jira_url':                 jira_url_base = value
    if key == 'jira_api_key':             jira_api_key = value
    if key == 'jira_user':                jira_user = value
    if key == 'errata_url':               errata_url_base = value
    if key == 'version':                  version = value
    if key == 'firefox':                  firefox_version = value
    if key == 'cryptosvc_url':            cryptosvc_url = value
    if key == 'cryptosvc_access_token':   cryptosvc_access_token = value
    if key == 'cryptosvc_pat_key':        cryptosvc_pat_key = value
    if key == 'dry_run':
        DRY_RUN = value.lower() == 'true'

for opt, arg in opts:
    if opt == '-f':          firefox_version = arg
    elif opt == '-v':        version = arg
    elif opt == '-o':        owner = arg
    elif opt == '-m':        manager = arg
    elif opt == '--dry-run': DRY_RUN = True
    elif opt == '--resync':  resync = True

if not release_args:
    print('No releases specified.')
    sys.exit(1)

if firefox_version is None:
    print('Firefox version required. Use -f <version>.')
    sys.exit(2)

year = datetime.date.today().strftime('%Y')

# ── errata map ────────────────────────────────────────────────────────────────

errata_map, ga_list, _ = load_errata_map(
    errata_url_base, errata_cache_file, ca_certs_file, force_resync=resync)

# ── Jira client ───────────────────────────────────────────────────────────────

Jira = None
if jira_api_key:
    Jira = make_jira_client(jira_url_base, jira_api_key)

# ── cryptosvc PAT ─────────────────────────────────────────────────────────────

cryptosvc_pat = None
if cryptosvc_url and cryptosvc_pat_key and jira_user and jira_api_key:
    try:
        cryptosvc_pat = make_pat(cryptosvc_pat_key, jira_user, jira_api_key)
    except Exception as e:
        print(f'WARNING: Could not generate cryptosvc PAT: {e}')

# ── wipe and recreate meta/ ───────────────────────────────────────────────────

if not DRY_RUN:
    if os.path.exists(meta_dir):
        shutil.rmtree(meta_dir)
    os.makedirs(meta_dir)
    for fname, val in [(ckbiver_file, version or 'unknown'),
                       (nssver_file,  config.get('nss_version', 'unknown')),
                       (mcsver_file,  config.get('mcs_version', 'unknown')),
                       (firefox_info, firefox_version)]:
        with open(fname, 'w') as f:
            f.write(val)
else:
    print('DRY_RUN: would wipe and recreate meta/')
    os.makedirs(meta_dir, exist_ok=True)

# ── process releases ──────────────────────────────────────────────────────────

packages = 'ca-certificates'
rhel_entries   = []
fedora_entries = []

print('\n=== Planning releases ===\n')

for release in release_args:
    if release.startswith('f') or release == 'rawhide':
        print(f'{release}: fedora (no bug needed)')
        fedora_entries.append((release, packages, '0', '0', '', 'planned'))
        continue

    major   = safe_int(release_get_major(release))
    zstream = get_need_zstream_clone(release, ga_list)

    print(f'{release}: major={major} zstream={zstream}')

    bugnumber = '0'
    if Jira:
        if zstream:
            bugnumber, _ = issue_lookup(Jira, release, version or 'unknown',
                                        packages, year, zstream=True)
            if bugnumber == '0':
                print(f'  no cloned z-stream bug yet — will wait')
        else:
            bugnumber, _ = issue_lookup(Jira, release, version or 'unknown',
                                        packages, year)
            if bugnumber == '0':
                bugnumber, _ = issue_create(
                    Jira, release, version or 'unknown',
                    config.get('nss_version', 'unknown'),
                    firefox_version,
                    config.get('mcs_version', 'unknown'),
                    packages, zstream=False, year=year)
                if bugnumber not in ('0', 'DRY-0') and major > 8:
                    issue_request_clone(Jira, release, version or 'unknown',
                                        packages, year)

    print(f'  bug={bugnumber}')

    # trigger CRYPTO errata epic creation via cryptosvc
    if cryptosvc_url and cryptosvc_pat and cryptosvc_access_token \
            and bugnumber not in ('0', 'DRY-0'):
        fixversion = release.replace('rhel-', '')
        print(f'  creating CRYPTO errata epic for {packages}/{fixversion}')
        cryptosvc_create_errata(
            cryptosvc_url, cryptosvc_access_token, cryptosvc_pat,
            packages, fixversion, [bugnumber])

    rhel_entries.append((release, packages, bugnumber, '0', '', 'planned', '', ''))

# ── write meta files ──────────────────────────────────────────────────────────

if not DRY_RUN:
    with open(rhel_list, 'w') as f:
        for entry in rhel_entries:
            f.write(':'.join(entry) + '\n')
    with open(fedora_list, 'w') as f:
        for entry in fedora_entries:
            f.write(':'.join(entry) + '\n')

# ── summary ───────────────────────────────────────────────────────────────────

print('\n=== Summary ===\n')
print(f'{"Release":<25} {"Bug":<15} {"State"}')
print('-' * 50)
for entry in rhel_entries:
    print(f'{entry[0]:<25} {entry[2]:<15} {entry[5]}')
for entry in fedora_entries:
    print(f'{entry[0]:<25} {"(none)":<15} {entry[5]}')

if DRY_RUN:
    print('\n(dry run — no changes written)')
