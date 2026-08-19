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
from caupdate.tui import PipelineOutput
from caupdate.prereqs import check_prereqs
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

def _cryptosvc_headers():
    return {
        'Access-Token': cryptosvc_access_token,
        'PAT':          cryptosvc_pat,
        'Content-Type': 'application/json',
    }

def _cryptosvc_post(path, body):
    """POST to a cryptosvc endpoint with Kerberos + PAT auth."""
    url = cryptosvc_url.rstrip('/') + path
    r = requests.post(url, headers=_cryptosvc_headers(), json=body,
                      timeout=60, verify=ca_certs_file, auth=HTTPKerberosAuth())
    return r

def _triage_rhel_bug(bugnumber):
    """Triage a RHEL bug via cryptosvc, matching exactly what the browser does:
    1. POST update fields (priority/severity/regression) separately
    2. POST status='Planning' (capital P) to trigger transition + create_splits
    Only called on freshly created bugs."""
    if not (cryptosvc_url and cryptosvc_access_token and cryptosvc_pat):
        return
    if bugnumber in ('0', 'DRY-0'):
        return
    if DRY_RUN:
        print(f'  DRY_RUN: would triage {bugnumber} via cryptosvc')
        return

    # Step 1: update fields (separate request, no status)
    r = _cryptosvc_post('/jira/triage',
                        {'issueid': bugnumber,
                         'update': {'priority': 'Normal',
                                    'severity': 'Moderate',
                                    'regression': 'No'}})
    if r.status_code not in (200, 202):
        print(f'  WARNING: triage field update failed: {r.status_code}')

    # Step 2: status only — triggers transition + story points + create_splits
    r = _cryptosvc_post('/jira/triage',
                        {'issueid': bugnumber, 'status': 'Planning'})
    if r.status_code in (200, 202):
        print(f'  triaged {bugnumber} — [DEV]/[QE] splits created')
    else:
        print(f'  WARNING: triage status failed: {r.status_code} {r.text[:120]}')

def _find_dev_child(bugnumber):
    """Return the [DEV] CRYPTO split key linked to a RHEL bug, or None."""
    if not Jira or bugnumber in ('0', 'DRY-0'):
        return None
    try:
        issue = Jira.get(bugnumber)
        links = issue.get('fields', {}).get('issuelinks', [])
    except Exception as e:
        print(f'  WARNING: could not fetch issue links for {bugnumber}: {e}')
        return None
    for link in links:
        if link.get('type', {}).get('id') != '10120':
            continue
        linked = link.get('outwardIssue') or link.get('inwardIssue', {})
        linked_key = linked.get('key', '')
        if not linked_key.startswith('CRYPTO-'):
            continue
        try:
            li      = Jira.get(linked_key)
            summary = li.get('fields', {}).get('summary', '')
            if summary.startswith('[DEV]'):
                return linked_key
        except Exception:
            continue
    return None


def _set_split_sprints(bugnumber):
    """After triage, find [DEV]/[QE] splits linked to the RHEL bug and set
    their sprints via cryptosvc's sprint-add update. Uses dev_sprint and
    qe_sprint globals."""
    if not dev_sprint and not qe_sprint:
        return
    if not Jira or bugnumber in ('0', 'DRY-0'):
        return
    try:
        issue = Jira.get(bugnumber)
        links = issue.get('fields', {}).get('issuelinks', [])
    except Exception as e:
        print(f'  WARNING: could not fetch issue links for {bugnumber}: {e}')
        return

    for link in links:
        if link.get('type', {}).get('id') != '10120':  # split link type
            continue
        linked = link.get('outwardIssue') or link.get('inwardIssue', {})
        linked_key = linked.get('key', '')
        if not linked_key.startswith('CRYPTO-'):
            continue
        try:
            li      = Jira.get(linked_key)
            summary = li.get('fields', {}).get('summary', '')
        except Exception:
            continue

        if summary.startswith('[DEV]') and dev_sprint:
            sprint = dev_sprint
        elif summary.startswith('[QE]') and qe_sprint:
            sprint = qe_sprint
        else:
            continue

        if DRY_RUN:
            print(f'  DRY_RUN: would set sprint {sprint} on {linked_key}')
            continue
        r = _cryptosvc_post('/jira/triage',
                            {'issueid': linked_key,
                             'update': {'sprint-add': int(sprint)}})
        if r.status_code in (200, 202):
            print(f'  set sprint {sprint} on {linked_key} ({summary[:30]})')
        else:
            print(f'  WARNING: sprint set failed for {linked_key}: {r.status_code}')

def cryptosvc_create_errata(component, fixversion, bugs, description=''):
    """Call the existing cryptosvc /jira/errata/create endpoint.
    Returns the CRYPTO issue key string on success, None on failure."""
    import re as _re
    url  = cryptosvc_url.rstrip('/') + '/jira/errata/create'
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
    r = requests.post(url, headers=_cryptosvc_headers(), json=body, timeout=30,
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

def _handle_rhel(release, is_ga, use_zstream=False, is_sustaining=False):
    """Create/look up a RHEL Jira bug. Returns the bug key string.

    is_ga=True  — head release (true GA or head of z-stream-only major):
                  create bug, then request 'All Active Z-streams' clones.
    is_ga=False — other z-streams: look up the cloned bug, wait if not yet available.
    use_zstream — when True, the bug fixVersion uses the .z suffix (e.g. rhel-9.9.z).
    """
    if not Jira:
        return '0'

    if is_ga:
        bugnumber, issue = issue_lookup(Jira, release, ver, packages, year,
                                        zstream=use_zstream)
        if bugnumber == '0':
            bugnumber, issue = issue_create(
                Jira, release, ver, nss_ver, firefox_version, mcs_ver,
                packages, zstream=use_zstream, year=year)
            _triage_rhel_bug(bugnumber)
            _set_split_sprints(bugnumber)
        if bugnumber not in ('0', 'DRY-0'):
            if not has_clone_links(Jira, bugnumber):
                print(f'  no clone links — requesting z-stream clones for {release}')
                issue_request_clone(Jira, issue or bugnumber, dry_run=DRY_RUN)
            else:
                print(f'  clones already exist for {bugnumber}')
    else:
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

def _resolve_jira_account(email):
    """Resolve an email address to a Jira Cloud accountId."""
    if not Jira:
        return None
    try:
        r = requests.get(f'{Jira.url}/rest/api/3/user/search',
                         params={'query': email},
                         headers=Jira._headers(), timeout=30)
        users = r.json()
        if users:
            return users[0].get('accountId')
    except Exception as e:
        print(f'  WARNING: could not resolve {email} to accountId: {e}')
    return None

def _set_rhel_qa_contact(bugnumber):
    """Set the SE contact as QA contact (customfield_10470) on the RHEL bug."""
    if not (Jira and se_contact and bugnumber and bugnumber not in ('0', 'DRY-0')):
        return
    if DRY_RUN:
        print(f'  DRY_RUN: would set SE contact {se_contact} as QA on {bugnumber}')
        return
    account_id = _resolve_jira_account(se_contact)
    if not account_id:
        print(f'  WARNING: could not resolve SE contact {se_contact}')
        return
    payload = {'fields': {'customfield_10470': {'accountId': account_id}}}
    try:
        r = requests.put(f'{Jira.url}/rest/api/3/issue/{bugnumber}',
                         json=payload, headers=Jira._headers(), timeout=30)
        if r.status_code == 204:
            print(f'  set SE contact {se_contact} as QA on {bugnumber}')
        else:
            print(f'  WARNING: could not set QA on {bugnumber}: {r.status_code} {r.text[:120]}')
    except Exception as e:
        print(f'  WARNING: set QA on {bugnumber} failed: {e}')

def _set_crypto_parent(crypto_key):
    """Set crypto_epic_parent as the parent of a CRYPTO epic."""
    if not (Jira and crypto_epic_parent and crypto_key and crypto_key not in ('0', 'DRY-CRYPTO-0')):
        return
    if DRY_RUN:
        print(f'  DRY_RUN: would set parent {crypto_epic_parent} on {crypto_key}')
        return
    payload = {'fields': {'parent': {'key': crypto_epic_parent}}}
    try:
        r = requests.put(f'{Jira.url}/rest/api/3/issue/{crypto_key}',
                         json=payload, headers=Jira._headers(), timeout=30)
        if r.status_code == 204:
            print(f'  set parent {crypto_epic_parent} → {crypto_key}')
        else:
            print(f'  WARNING: could not set parent on {crypto_key}: {r.status_code} {r.text[:120]}')
    except Exception as e:
        print(f'  WARNING: set parent failed for {crypto_key}: {e}')

def _maybe_create_crypto_epic(release, bugnumber, is_zstream=False):
    if not (cryptosvc_url and cryptosvc_pat and cryptosvc_access_token):
        return None
    if bugnumber in ('0', 'DRY-0'):
        return None
    if release in crypto_map:
        print(f'  CRYPTO epic already exists: {crypto_map[release]}')
        return crypto_map[release]
    fixversion = jira_fixversion(release) + ('.z' if is_zstream else '')
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
        _set_crypto_parent(key)
    return key

# ── arg parsing ───────────────────────────────────────────────────────────────

_USAGE = """\
plan.py — Pre-pipeline planning step for ca-certificates updates.

Creates RHEL Jira bugs, requests z-stream clones, creates CRYPTO errata
epics via cryptosvc, and triages RHEL bugs (priority/severity/regression +
[DEV]/[QE] CRYPTO splits).  Writes meta/rhel.list and meta/fedora.list so
that build_combo.py and process.py can run without manual bug numbers.

Run plan.py first, then build_combo.py, then process.py.

Usage (pick one mode):
  ./plan.py --rhel  -f <firefox>               auto-discover active RHEL releases
  ./plan.py --fedora                            auto-discover Fedora releases

Options:
  -f <firefox>               Firefox version for the update (required for --rhel)
  -n <nss_version>           NSS version (auto-fetched from Mozilla if omitted)
  -s <mcs_version>           Microsoft code-signing version
  -v <ckbi_version>          Override CKBI version
  -o <email>                 Package owner e-mail (overrides config.cfg)
  -m <email>                 Manager e-mail (overrides config.cfg)
  --rhel                     Discover and plan all active RHEL releases
  --fedora                   Discover current/pending Fedora releases via Bodhi
  --dry-run                  Show what would happen without creating anything
  --resync                   Force refresh of the errata product-version cache
  --human                    Rich TUI output with live status table
  --loop                     Re-run every --interval seconds until all bugs exist
  --interval N               Loop sleep in seconds (default: 300)
  --crypto-epic-parent KEY   Set this CRYPTO epic as parent of new CRYPTO epics
  --dev-sprint ID            Set sprint on [DEV] CRYPTO tasks after triage
  --qe-sprint ID             Set sprint on [QE] CRYPTO tasks after triage

Required: valid Kerberos ticket (kinit) for the Errata Tool API.
Config:   config.cfg — jira_url, jira_api_key, jira_user,
                       cryptosvc_url, cryptosvc_access_token, cryptosvc_pat
"""

if '--help' in sys.argv or '-h' in sys.argv:
    print(_USAGE)
    sys.exit(0)

try:
    opts, _ = getopt.getopt(
        sys.argv[1:], 'f:n:s:v:o:m:', ['dry-run', 'resync', 'rhel', 'fedora',
                                        'crypto-epic-parent=',
                                        'dev-sprint=', 'qe-sprint=',
                                        'human', 'loop', 'interval='])
except getopt.GetoptError as err:
    print(err)
    print('Run with --help for usage information.')
    sys.exit(2)

mode            = None   # 'rhel' or 'fedora'
human           = False
loop_mode       = False
loop_interval   = 300
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
crypto_epic_parent     = None
se_contact             = None
dev_sprint             = None
qe_sprint              = None
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
    if key == 'crypto_epic_parent':       crypto_epic_parent = value
    if key == 'se_contact':               se_contact = value
    if key == 'dev_sprint':               dev_sprint = value
    if key == 'qe_sprint':               qe_sprint  = value
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
    elif opt == '--resync':              resync = True
    elif opt == '--crypto-epic-parent':  crypto_epic_parent = arg
    elif opt == '--dev-sprint':          dev_sprint = arg
    elif opt == '--qe-sprint':           qe_sprint  = arg
    elif opt == '--rhel':      mode = 'rhel'
    elif opt == '--fedora':    mode = 'fedora'
    elif opt == '--human':     human = True
    elif opt == '--loop':      loop_mode = True
    elif opt == '--interval':  loop_interval = int(arg)

if not DRY_RUN:
    check_prereqs(['kinit'], 'plan.py')

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
        errata_map, ga_list = load_errata_map(
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
existing_rhel = {}   # release → field list
crypto_map    = {}   # release → CRYPTO epic key
if os.path.exists(rhel_list):
    for line in open(rhel_list):
        parts = line.strip().split(':')
        if len(parts) >= 7:
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

subtitle = f'NSS {nss_ver} · CKBI {ver} · Firefox {firefox_version}'
out = PipelineOutput(human=human, title=f'plan.py --{mode}')
out.set_subtitle(subtitle)

if mode == 'rhel':
    out.set_columns(['Release', 'Type', 'Bug', 'CRYPTO', 'State'])
elif mode == 'fedora':
    out.set_columns(['Release', 'State'])

with out:
    out.log(f'Planning releases (mode={mode})  {subtitle}')

    if mode == 'rhel':
        discovered = discover_rhel_releases(errata_map, ga_list)
        if not discovered:
            out.log('WARNING: no active RHEL releases found in errata map')
        for item in discovered:
            release         = item['release']
            is_ga           = item['is_ga']
            use_zstream     = item['use_zstream']
            is_sustaining   = item['is_sustaining']
            if is_ga and not use_zstream:
                label = 'GA'
            elif is_ga and use_zstream:
                label = 'GA (z-stream)'
            else:
                label = 'z-stream'
            if is_sustaining:
                label += ' SE'

            out.update_row(release, [release, label, '…', '…', 'working'])
            out.log(f'{release}: {label}')

            bugnumber = _handle_rhel(release, is_ga, use_zstream, is_sustaining)
            out.log(f'  bug={bugnumber}')

            if is_sustaining:
                _set_rhel_qa_contact(bugnumber)
                crypto_key = ''
                crypto_dev  = ''
            else:
                crypto_key = _maybe_create_crypto_epic(release, bugnumber,
                                 is_zstream=not is_ga) or ''
                # Find the [DEV] child issue created by triage — store for process.py
                crypto_dev = _find_dev_child(bugnumber) or ''
                if crypto_dev:
                    out.log(f'  crypto_dev={crypto_dev}')

            if release in existing_rhel:
                prev = existing_rhel[release]
                # New format: release:branch:bug:errata:nvr:state:glmr:glup:crypto:dev
                merged_branch = prev[1] if len(prev) > 1 else ''
                merged_bug    = bugnumber if bugnumber != '0' else prev[2]
                merged_errata = prev[3] if len(prev) > 3 else '0'
                merged_nvr    = prev[4] if len(prev) > 4 else ''
                merged_state  = prev[5] if len(prev) > 5 else 'planned'
                merged_glmr   = prev[6] if len(prev) > 6 else ''
                merged_glup   = prev[7] if len(prev) > 7 else ''
                merged_crypto = crypto_key or (prev[8] if len(prev) > 8 else '')
                merged_dev    = crypto_dev or (prev[9] if len(prev) > 9 else '')
            else:
                merged_branch = ''
                merged_bug, merged_errata = bugnumber, '0'
                merged_nvr, merged_state  = '', 'planned'
                merged_glmr, merged_glup  = '', ''
                merged_crypto             = crypto_key
                merged_dev                = crypto_dev

            rhel_entries.append((release, merged_branch, merged_bug, merged_errata,
                                 merged_nvr, merged_state, merged_glmr,
                                 merged_glup, merged_crypto, merged_dev))
            out.update_row(release, [release, label, merged_bug,
                                     merged_crypto or '–', merged_state])

    elif mode == 'fedora':
        try:
            discovered = discover_fedora_releases()
        except Exception as e:
            if DRY_RUN:
                out.log(f'WARNING: Bodhi query failed ({e}); discovery empty')
                discovered = []
            else:
                raise
        if not discovered:
            out.log('WARNING: no active Fedora releases found via Bodhi')
        for release in discovered:
            out.log(f'{release}: fedora')
            fedora_entries.append((release, '0', '0', '', 'planned'))
            out.update_row(release, [release, 'planned'])

    # ── write meta files ──────────────────────────────────────────────────────

    if not DRY_RUN:
        with open(rhel_list, 'w') as f:
            for entry in rhel_entries:
                f.write(':'.join(entry) + '\n')
        with open(fedora_list, 'w') as f:
            for entry in fedora_entries:
                f.write(':'.join(entry) + '\n')

    if DRY_RUN:
        out.log('(dry run — no changes written)')

# ── loop mode ─────────────────────────────────────────────────────────────────

if loop_mode:
    import time as _time
    _all_done = (
        all(e[5] == 'planned' for e in rhel_entries)   # plan.py only reaches 'planned'
        or not rhel_entries
    ) and (
        all(e[4] == 'planned' for e in fedora_entries)
        or not fedora_entries
    )
    # For plan.py, loop is useful to retry releases that didn't get bugs yet
    # (z-stream clones pending). Check if any bug is still '0'.
    _missing = [e[0] for e in rhel_entries if e[2] == '0']
    if _missing:
        print(f'\nLoop mode: {len(_missing)} release(s) without bugs yet: '
              f'{", ".join(_missing)}')
        print(f'Sleeping {loop_interval}s then re-running…')
        _time.sleep(loop_interval)
        os.execv(sys.argv[0], sys.argv)
    else:
        print('\nAll releases have bugs — loop complete.')
