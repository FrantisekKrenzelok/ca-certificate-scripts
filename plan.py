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
    issue_create, issue_lookup, issue_request_clone, has_clone_links,
    make_jira_client, jira_fixversion, bug_summary_short,
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


def cryptosvc_create_errata(component, fixversion, bugs, description=''):
    """Call the existing cryptosvc /jira/errata/create endpoint.
    Returns the CRYPTO issue key string on success, None on failure."""
    import re as _re
    url = cryptosvc_url.rstrip('/') + '/jira/errata/create'
    headers = {
        'Access-Token': cryptosvc_access_token,
        'PAT': cryptosvc_pat,
        'Content-Type': 'application/json',
    }
    body = {
        'component':   component,
        'fixversion':  fixversion,
        'bugs':        bugs,
        'description': description,
        'override':    False,
    }
    if DRY_RUN:
        print(f'  DRY_RUN: POST {url} {body}')
        return 'DRY-CRYPTO-0'
    r = requests.post(url, headers=headers, json=body, timeout=30,
                      verify=ca_certs_file, auth=HTTPKerberosAuth())
    if r.status_code == 409:
        print(f'  CRYPTO errata epic already exists for {component}/{fixversion}')
        return None
    if r.status_code > 299:
        print(f'  cryptosvc errata create failed: {r.status_code} {r.text[:200]}')
        return None
    m = _re.search(r'id="div-(CRYPTO-\d+)"', r.text)
    if m:
        return m.group(1)
    print(f'  WARNING: could not parse CRYPTO key from response')
    return None

# ── release processing ────────────────────────────────────────────────────────

def _handle_rhel(release, is_ga, latest_z_stream=False):
    """Create/look up a RHEL Jira bug. Returns the bug key string."""
    if not Jira:
        return '0'

    if is_ga:
        # True GA: create y-stream bug, then request z-stream clones
        major = safe_int(release_get_major(release))
        bugnumber, issue = issue_lookup(Jira, release, ver, packages, year)
        if bugnumber == '0':
            bugnumber, issue = issue_create(
                Jira, release, ver, nss_ver, firefox_version, mcs_ver,
                packages, zstream=False, year=year)
        # Request clones if the GA bug has none yet (covers first creation
        # and the case where a previous clone request failed)
        if bugnumber not in ('0', 'DRY-0') and major > 8:
            if not has_clone_links(Jira, bugnumber):
                print(f'  no clone links found — requesting z-stream clones for {release}')
                issue_request_clone(Jira, issue or bugnumber, dry_run=DRY_RUN)
            else:
                print(f'  clones already exist for {bugnumber}')
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

def _get_epic_from_rhel_bug(bugnumber):
    """Read the CRYPTO epic key from the RHEL bug's EPICLINK field (customfield_10014).
    cryptosvc sets this on the RHEL bug when it creates the CRYPTO epic."""
    if not Jira or bugnumber in ('0', 'DRY-0'):
        return None
    try:
        issue = Jira.get(bugnumber)
        epic_key = issue.get('fields', {}).get('customfield_10014')
        if epic_key and str(epic_key).startswith('CRYPTO-'):
            return str(epic_key)
    except Exception as e:
        print(f'  WARNING: could not read epic link on {bugnumber}: {e}')
    return None

def _maybe_create_crypto_epic(release, bugnumber, is_zstream=False):
    if not (cryptosvc_url and cryptosvc_pat and cryptosvc_access_token):
        return None
    if bugnumber in ('0', 'DRY-0'):
        return None
    if release in crypto_map:
        print(f'  CRYPTO epic already exists: {crypto_map[release]}')
        return crypto_map[release]
    # Check the RHEL bug's epic link before calling cryptosvc
    existing = _get_epic_from_rhel_bug(bugnumber)
    if existing:
        print(f'  CRYPTO epic found via RHEL bug link: {existing}')
        crypto_map[release] = existing
        return existing
    description = (bug_summary_short % year) + (
        f' version {ver} from NSS {nss_ver} for Firefox {firefox_version}'
        f' and Microsoft {mcs_ver}')
    print(f'  creating CRYPTO errata epic for {packages}/{fixversion}')
    key = cryptosvc_create_errata(packages, fixversion, [bugnumber], description)
    if key:
        print(f'  CRYPTO epic: {key}')
        crypto_map[release] = key
    return key

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

# ── load existing meta state ──────────────────────────────────────────────────

# Load existing rhel.list so we can merge rather than overwrite.
# The user is responsible for wiping rhel.list when starting a fresh cycle.
existing_rhel = {}   # release → 9-tuple of field strings
crypto_map    = {}   # release → CRYPTO key
if os.path.exists(rhel_list):
    for line in open(rhel_list):
        parts = line.strip().split(':')
        if len(parts) >= 8:
            existing_rhel[parts[0]] = parts
            if len(parts) >= 9 and parts[8]:
                crypto_map[parts[0]] = parts[8]

if not DRY_RUN:
    os.makedirs(meta_dir, exist_ok=True)
    for fname, val in [(ckbiver_file, ver),
                       (nssver_file,  nss_ver),
                       (mcsver_file,  mcs_ver),
                       (firefox_info, firefox_version)]:
        with open(fname, 'w') as f:
            f.write(val)
else:
    print('DRY_RUN: meta/ preserved, version files would be updated')

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
        crypto_key = _maybe_create_crypto_epic(release, bugnumber, is_zstream=not is_ga) or ''

        # Merge with existing rhel.list entry, preserving pipeline progress
        if release in existing_rhel:
            prev = existing_rhel[release]
            # Only update bugnumber and crypto if newly obtained
            merged_bug    = bugnumber if bugnumber != '0' else prev[2]
            merged_errata = prev[3]
            merged_nvr    = prev[4]
            merged_state  = prev[5]
            merged_glmr   = prev[6]
            merged_glup   = prev[7]
            merged_crypto = crypto_key or (prev[8] if len(prev) > 8 else '')
        else:
            merged_bug, merged_errata = bugnumber, '0'
            merged_nvr, merged_state  = '', 'planned'
            merged_glmr, merged_glup  = '', ''
            merged_crypto             = crypto_key

        rhel_entries.append((release, packages, merged_bug, merged_errata,
                             merged_nvr, merged_state, merged_glmr,
                             merged_glup, merged_crypto))

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
print(f'{"Release":<25} {"Bug":<15} {"CRYPTO":<15} {"State"}')
print('-' * 62)
for entry in rhel_entries:
    crypto = crypto_map.get(entry[0], '')
    print(f'{entry[0]:<25} {entry[2]:<15} {crypto:<15} {entry[5]}')
for entry in fedora_entries:
    print(f'{entry[0]:<25} {"(none)":<15} {"":15} {entry[5]}')

if DRY_RUN:
    print('\n(dry run — no changes written)')
