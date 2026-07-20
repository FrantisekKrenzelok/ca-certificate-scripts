#!/usr/bin/python3
# vim:set et sw=4:
#
# plan.py - Pre-pipeline planning step for ca-certificates updates.
#
# Creates RHEL Jira bugs and triggers CRYPTO errata epic creation via
# cryptosvc. Writes meta/rhel.list and meta/fedora.list so that
# build_combo.sh and process.py can run against pre-populated bug numbers.
#
# Usage (pick one mode):
#   ./plan.py -f 138 -n 3.114 -s 1.5 --rhel
#   ./plan.py -f 138 --fedora
#
# Options:
#   -f <firefox_version>   Firefox version for this update (required for --rhel)
#   -n <nss_version>       NSS version (e.g. 3.114)
#   -s <mcs_version>       Microsoft code-signing version
#   -v <ckbi_version>      CKBI/ca-certificates version override
#   -o <owner_email>       Override owner from config
#   -m <manager_email>     Override manager from config
#   --dry-run              Show what would happen without creating anything
#   --resync               Force refresh of errata cache

import os
import sys
import shutil
import getopt
import json
import datetime
import requests

from requests_kerberos import HTTPKerberosAuth

from caupdate.release import (
    release_get_major, safe_int,
    get_need_zstream_clone,
    discover_rhel_releases, discover_fedora_releases,
    load_errata_map, CA_CERTS_FILE,
)
from caupdate.issues import (
    issue_create, issue_lookup, issue_request_clone,
    make_jira_client,
)
from caupdate.versions import fetch_nss_versions, NSS_BASE_URL

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


def cryptosvc_create_errata(component, fixversion, bugs):
    """Call the existing cryptosvc /jira/errata/create endpoint."""
    url = cryptosvc_url.rstrip('/') + '/jira/errata/create'
    headers = {
        'Access-Token': cryptosvc_access_token,
        'PAT': cryptosvc_pat,
        'Content-Type': 'application/json',
    }
    body = {'component': component, 'fixversion': fixversion, 'bugs': bugs}
    if DRY_RUN:
        print(f'  DRY_RUN: POST {url} {body}')
        return True
    r = requests.post(url, headers=headers, json=body, timeout=30,
                      verify=ca_certs_file, auth=HTTPKerberosAuth())
    if r.status_code == 409:
        print(f'  CRYPTO errata epic already exists for {component}/{fixversion}')
        return True
    if r.status_code > 299:
        print(f'  cryptosvc errata create failed: {r.status_code} {r.text}')
        return False
    return True

# ── release processing ────────────────────────────────────────────────────────

def _handle_rhel(release, is_ga, latest_z_stream=False):
    """Create/look up a RHEL Jira bug. Returns the bug key string."""
    if not Jira:
        return '0'

    if is_ga:
        # True GA: create y-stream bug, then request z-stream clones
        bugnumber, issue = issue_lookup(Jira, release, ver, packages, year)
        if bugnumber == '0':
            bugnumber, issue = issue_create(
                Jira, release, ver, nss_ver, firefox_version, mcs_ver,
                packages, zstream=False, year=year)
            if issue is not None and safe_int(release_get_major(release)) > 8:
                print(f'  requesting z-stream clones for all active {release} z-streams')
                issue_request_clone(Jira, issue, dry_run=DRY_RUN)
    elif latest_z_stream:
        # z-stream-only major (e.g. RHEL 8): create z-stream bug directly
        bugnumber, _ = issue_lookup(Jira, release, ver, packages, year, zstream=True)
        if bugnumber == '0':
            bugnumber, _ = issue_create(
                Jira, release, ver, nss_ver, firefox_version, mcs_ver,
                packages, zstream=True, year=year)
    else:
        # Other z-streams: wait for clone from GA bug
        bugnumber, _ = issue_lookup(Jira, release, ver, packages, year, zstream=True)
        if bugnumber == '0':
            print(f'  clone not yet available — will retry on next run')

    return bugnumber

def _maybe_create_crypto_epic(release, bugnumber):
    if not (cryptosvc_url and cryptosvc_pat and cryptosvc_access_token):
        return
    if bugnumber in ('0', 'DRY-0'):
        return
    fixversion = release.replace('rhel-', '')
    print(f'  creating CRYPTO errata epic for {packages}/{fixversion}')
    cryptosvc_create_errata(packages, fixversion, [bugnumber])

# ── arg parsing ───────────────────────────────────────────────────────────────

try:
    opts, _ = getopt.getopt(
        sys.argv[1:], 'f:n:s:v:o:m:', ['dry-run', 'resync', 'rhel', 'fedora'])
except getopt.GetoptError as err:
    print(err)
    print('Usage: plan.py -f <firefox> [-n <nss_version>] [-s <mcs_version>] [--rhel | --fedora] [--dry-run] [--resync]')
    sys.exit(2)

mode            = None   # 'rhel' or 'fedora'
resync          = False
firefox_version = None
version         = None
nss_version     = None
mcs_version     = None
owner           = None
manager         = None
jira_api_key    = None
jira_user              = None
cryptosvc_url          = None
cryptosvc_access_token = None
cryptosvc_pat          = None
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
    if key == 'jira_user':               jira_user = value
    if key == 'errata_url':              errata_url_base = value
    if key == 'version':                  version = value
    if key == 'firefox':                  firefox_version = value
    if key == 'cryptosvc_url':            cryptosvc_url = value
    if key == 'cryptosvc_access_token':   cryptosvc_access_token = value
    if key == 'cryptosvc_pat':            cryptosvc_pat = value
    if key == 'dry_run':
        DRY_RUN = value.lower() == 'true'

for opt, arg in opts:
    if opt == '-f':          firefox_version = arg
    elif opt == '-n':        nss_version = arg
    elif opt == '-s':        mcs_version = arg
    elif opt == '-v':        version = arg
    elif opt == '-o':        owner = arg
    elif opt == '-m':        manager = arg
    elif opt == '--dry-run': DRY_RUN = True
    elif opt == '--resync':  resync = True
    elif opt == '--rhel':    mode = 'rhel'
    elif opt == '--fedora':  mode = 'fedora'

if mode is None:
    print('Specify a mode: --rhel or --fedora')
    sys.exit(1)

if firefox_version is None and mode == 'rhel':
    print('Firefox version required for --rhel. Use -f <version>.')
    sys.exit(2)
firefox_version = firefox_version or 'unknown'

year     = datetime.date.today().strftime('%Y')
packages = 'ca-certificates'

# Auto-fetch NSS/CKBI versions from Mozilla if not supplied on CLI/config
if mode == 'rhel' and (nss_version is None or version is None):
    try:
        auto_nss, auto_ckbi = fetch_nss_versions()
        nss_version = nss_version or auto_nss
        version     = version     or auto_ckbi
    except Exception as e:
        print(f'WARNING: could not fetch NSS versions from Mozilla ({e})')
        print('         Use -n <nss_version> and -v <ckbi_version> to set them manually.')

ver     = version     or config.get('version',      'unknown')
nss_ver = nss_version or config.get('nss_version',  'unknown')
mcs_ver = mcs_version or config.get('mcs_version',  'unknown')

# ── errata map (RHEL mode only) ───────────────────────────────────────────────

errata_map = {}
ga_list    = []

if mode == 'rhel':
    try:
        errata_map, ga_list, _ = load_errata_map(
            errata_url_base, errata_cache_file, ca_certs_file, force_resync=resync)
    except Exception as e:
        if DRY_RUN:
            print(f'WARNING: could not load errata map ({e}); discovery will be empty')
        else:
            raise

# ── Jira client (RHEL mode only) ─────────────────────────────────────────────

Jira = None
if mode == 'rhel' and jira_api_key and not DRY_RUN:
    Jira = make_jira_client(jira_url_base, jira_api_key, jira_user=jira_user)

# ── wipe and recreate meta/ ───────────────────────────────────────────────────

if not DRY_RUN:
    if os.path.exists(meta_dir):
        shutil.rmtree(meta_dir)
    os.makedirs(meta_dir)
    for fname, val in [(ckbiver_file, ver),
                       (nssver_file,  nss_ver),
                       (mcsver_file,  mcs_ver),
                       (firefox_info, firefox_version)]:
        with open(fname, 'w') as f:
            f.write(val)
else:
    print('DRY_RUN: would wipe and recreate meta/')

# ── discover and process ──────────────────────────────────────────────────────

rhel_entries   = []
fedora_entries = []

print(f'\n=== Planning releases (mode={mode}) ===\n')

if mode == 'rhel':
    discovered = discover_rhel_releases(errata_map, ga_list)
    if not discovered:
        print('WARNING: no active RHEL releases found in errata map')
    for item in discovered:
        release       = item['release']
        is_ga         = item['is_ga']
        latest_z_stream = item['latest_z_stream']
        if is_ga:
            label = 'GA'
        elif latest_z_stream:
            label = 'z-stream (direct, no GA for this major)'
        else:
            label = 'z-stream'
        print(f'{release}: {label}')
        bugnumber = _handle_rhel(release, is_ga, latest_z_stream)
        print(f'  bug={bugnumber}')
        _maybe_create_crypto_epic(release, bugnumber)
        rhel_entries.append((release, packages, bugnumber, '0', '', 'planned', '', ''))

elif mode == 'fedora':
    try:
        discovered = discover_fedora_releases()
    except Exception as e:
        if DRY_RUN:
            print(f'WARNING: Bodhi query failed ({e}); discovery will be empty')
            discovered = []
        else:
            raise
    if not discovered:
        print('WARNING: no active Fedora releases found via Bodhi')
    for release in discovered:
        print(f'{release}: fedora')
        fedora_entries.append((release, packages, '0', '0', '', 'planned'))

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
