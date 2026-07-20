#!/usr/bin/python3
# vim:set et sw=4:
#
# plan.py - Pre-pipeline planning step for ca-certificates updates.
#
# Creates RHEL Jira bugs for each release and triggers CRYPTO errata epic
# creation via cryptosvc. Writes meta/rhel.list and meta/fedora.list so
# that build_combo.sh and process.py can run against pre-populated bug numbers.
#
# Usage:
#   ./plan.py -f <firefox_version> releases...
#   ./plan.py -f 138 rhel-9.6.0 rhel-9.4.0 rhel-8.10.0 f43 rawhide
#
# Options:
#   -f <firefox_version>   Firefox version associated with this update (required)
#   -v <ckbi_version>      Override CKBI version (default: read from meta/)
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
import re
import requests

import jira as jiralib
from jira import JIRAError

from jwcrypto.common import json_encode
from jwcrypto import jwk, jwe

DRY_RUN = False

meta_dir        = './meta'
rhel_list       = './meta/rhel.list'
fedora_list     = './meta/fedora.list'
ckbiver_file    = './meta/ckbiversion.txt'
nssver_file     = './meta/nssversion.txt'
mcsver_file     = './meta/mcsversion.txt'
firefox_info    = './meta/firefox_info.txt'
config_file     = './config.cfg'
errata_cache_file = './errata_cache'
errata_url_base = 'https://errata.devel.redhat.com'
jira_url_base   = 'https://issues.redhat.com'

bug_summary_short = 'Annual %s ca-certificates update'
bug_summary = bug_summary_short + ' version %s from NSS %s for Firefox %s and Microsoft %s [%s]'
bug_description = 'Update CA certificates to version %s from NSS %s and Microsoft %s for our annual CA certificate update.'

JIRA_PROJ      = 'RHEL'
JIRA_ISSUE_TYPE = 'Bug'
CRYPTO_PROJ    = 'CRYPTO'
EPIC_NAME_FIELD = 'customfield_10011'
EPIC_LINK_FIELD = 'customfield_10014'

# ── release helpers (mirrored from process.py) ────────────────────────────────

def release_get_major(release):
    comp = release.split('-')
    if len(comp) != 2:
        return None
    version = comp[1].split('.')
    if len(version) < 2:
        return None
    return version[0]

def safe_int(a):
    try:
        return int(a)
    except (ValueError, TypeError):
        return 0

def get_need_zstream_clone(release, ga_list):
    if safe_int(release_get_major(release)) < 8:
        return False
    release = re.sub(r'^rhel-(\d+\.\d+)$', r'rhel-\1.0', release)
    return release not in ga_list

def get_ga_list(errata_map):
    l_ga_list = []
    last_ga = None
    last_major = 0
    for release in errata_map.keys():
        current_major = release_get_major(release)
        if last_major != current_major:
            if last_ga is not None:
                l_ga_list.append(last_ga)
            last_major = current_major
        last_ga = release
    if last_ga is not None:
        l_ga_list.append(last_ga)
    return l_ga_list

# ── errata map (mirrored from process.py) ─────────────────────────────────────

from requests_kerberos import HTTPKerberosAuth
from functools import cmp_to_key

ca_certs_file = '/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem'

def errata_get_all_pages(url, paste, request_type):
    headers = {'Content-type': 'application/json', 'Accept': 'application/json'}
    r = requests.get(url, headers=headers, auth=HTTPKerberosAuth(), verify=ca_certs_file)
    if r.status_code > 299:
        print('errata %s status=%d' % (request_type, r.status_code))
        return None
    data = r.json()['data']
    if 'page' in r.json():
        page = r.json()['page']
        num_pages = page['total_pages']
        if num_pages != 1:
            for i in range(2, num_pages + 1):
                url_page = "%s%spage[number]=%d" % (url, paste, i)
                r = requests.get(url_page, headers=headers, auth=HTTPKerberosAuth(), verify=ca_certs_file)
                if r.status_code > 299:
                    return None
                data = data + r.json()['data']
    return data

def errata_candidate_to_release(brew_tag):
    lists = brew_tag.split('-')
    if len(lists) < 1:
        return 'empty'
    rhel_type = lists[0].lower()
    if len(lists) < 2:
        return rhel_type
    return "%s-%s" % (rhel_type, lists[1])

def splitnumeric(string):
    numeric = ''
    pos = len(string)
    for i in range(0, pos - 1):
        if not string[i].isnumeric():
            pos = i
            break
        numeric = numeric + string[i]
    return (numeric, string[pos:])

def errata_nvrcmp(rel1, rel2):
    comp1 = rel1.split('-')
    comp2 = rel2.split('-')
    if len(comp1) == 0:
        return 0 if len(comp2) == 0 else -1
    if len(comp2) == 0:
        return 1
    if comp1[0] < comp2[0]:
        return -1
    if comp1[0] > comp2[0]:
        return 1
    if len(comp1) == 1:
        return 0 if len(comp2) == 1 else -1
    if len(comp2) == 1:
        return 1
    ver1 = comp1[1].split('.')
    ver2 = comp2[1].split('.')
    for i in range(0, min(len(ver1), len(ver2))):
        if ver1[i] == ver2[i]:
            continue
        if not ver1[i].isnumeric() or not ver2[i].isnumeric():
            (v1n, v1rest) = splitnumeric(ver1[i])
            (v2n, v2rest) = splitnumeric(ver2[i])
            if v1n < v2n:
                return -1
            if v1n > v2n:
                return 1
            return -1 if v1rest < v2rest else 1
        if int(ver1[i]) < int(ver2[i]):
            return -1
        return 1
    if len(ver1) < len(ver2):
        return -1
    if len(ver1) > len(ver2):
        return 1
    if len(comp1) == 2:
        return 0 if len(comp2) == 2 else -1
    if len(comp2) == 2:
        return 1
    for i in range(0, min(len(comp1), len(comp2))):
        if comp1[i] < comp2[i]:
            return -1
        if comp1[i] > comp2[i]:
            return 1
    if len(comp1) < len(comp2):
        return -1
    if len(comp1) > len(comp2):
        return 1
    return 0

def errata_get_release_info():
    headers = {'Content-type': 'application/json', 'Accept': 'application/json'}

    release_ids = {}
    data = errata_get_all_pages(errata_url_base + '/api/v1/releases', '?', "")
    if data is None:
        return {}
    for item in data:
        release_ids[item.get("attributes").get("name")] = item.get('id')

    data = errata_get_all_pages(
        errata_url_base + '/api/v1/products/16/product_versions', '?', "release_info")
    if data is None:
        return {}

    product_version_list = {}
    out_of_life_list = {}
    releases = []
    maps = {}

    for pv in data:
        info = {}
        attrs = pv['attributes']
        name = attrs['name']
        info['name'] = name
        info['description'] = attrs['description']
        info['id'] = pv['id']
        if name not in release_ids:
            continue
        info['release_id'] = release_ids[name]
        brew = attrs['default_brew_tag']
        if brew is None:
            continue
        release = errata_candidate_to_release(brew)
        if release not in releases:
            releases.append(release)
        if attrs['enabled']:
            product_version_list.setdefault(release, []).append(info)
        else:
            out_of_life_list.setdefault(release, []).append(info)

    sorted_releases = sorted(releases, key=cmp_to_key(errata_nvrcmp))
    ga = None
    for release in sorted_releases:
        if release in product_version_list:
            for pv in product_version_list[release]:
                if pv['name'].endswith('.GA'):
                    ga = release
    for release in sorted_releases:
        if release in product_version_list:
            best = None
            for version in product_version_list[release]:
                if best is None or _is_better(best, version, release == ga):
                    best = version
            maps[release] = best
    return maps

def _is_better(best, compare, isga):
    bestname = best['name']
    if bestname.endswith(".GA"):
        return not isga
    comparename = compare['name']
    if comparename.endswith(".GA"):
        return isga
    if bestname.endswith(".MAIN+EUS"):
        return False
    if comparename.endswith(".MAIN+EUS"):
        return True
    order = {".EUS": 10, "-EUS": 9, ".Z": 8, ".AUS": 7, "-AUS": 6,
             ".TUS": 5, "-TUS": 4, ".E4S": 3, "-E4S": 2}
    bord = next((v for k, v in order.items() if bestname.endswith(k)), 0)
    cord = next((v for k, v in order.items() if comparename.endswith(k)), 0)
    return bord < cord

# ── Jira helpers ──────────────────────────────────────────────────────────────

def issue_lookup(Jira, release, version, packages, zstream=False):
    package = packages.split(',')[0]
    summary = bug_summary_short % year
    lookup_release = release
    if zstream:
        lookup_release += '.z'
    jql = (f'project={JIRA_PROJ} AND issuetype={JIRA_ISSUE_TYPE} AND '
           f'component={package} AND summary~"{summary}" AND fixVersion={lookup_release}')
    print(jql)
    try:
        issues = Jira.search_issues(jql)
    except JIRAError as e:
        print(e)
        return '0', None
    if not issues or len(issues) != 1:
        print(f'Found {len(issues) if issues else 0} issues matching {summary}')
        return '0', None
    return issues[0].key, issues[0]

def issue_create(Jira, release, version, nss_version, firefox_version, mcs_version, packages, zstream):
    package = packages.split(',')[0]
    if release == 'rhel-8.10.0':
        release = 'rhel-8.10'
    if zstream:
        release += '.z'
    fields = {
        'project': {'key': JIRA_PROJ},
        'issuetype': {'name': JIRA_ISSUE_TYPE},
        'summary': bug_summary % (year, version, nss_version, firefox_version, mcs_version, release),
        'description': bug_description % (version, nss_version, mcs_version),
        'fixVersions': [{'name': release}],
        'components': [{'name': package}],
        'priority': {'name': 'Minor'},
        'security': {'name': 'Red Hat Employee'},
        'labels': ['Triaged', 'Rebase'],
    }
    if DRY_RUN:
        print(f'DRY_RUN: would create RHEL bug for {release}')
        return 'DRY-0', None
    try:
        new_issue = Jira.create_issue(fields=fields)
        return new_issue.key, new_issue
    except JIRAError as e:
        print(f'Issue creation failed: {e}')
        return '0', None

def issue_request_clone(Jira, release, version, packages):
    _, issue = issue_lookup(Jira, release, version, packages)
    if issue is None:
        return False
    try:
        issue.update({'customfield_12323242': {'id': '33996'}})
    except JIRAError as e:
        print(e)
    return True

# ── cryptosvc PAT generation ───────────────────────────────────────────────────

def make_pat(pat_key_json, jira_user, jira_api_key):
    key = jwk.JWK(**json.loads(pat_key_json))
    token = jwe.JWE(
        json_encode({'user': jira_user, 'apikey': jira_api_key}),
        json_encode({'alg': 'A256KW', 'enc': 'A256CBC-HS512'}))
    token.add_recipient(key)
    return token.serialize(compact=True)

# ── cryptosvc errata epic creation ────────────────────────────────────────────

def cryptosvc_create_errata(cryptosvc_url, access_token, pat, component, fixversion, bugs):
    url = cryptosvc_url.rstrip('/') + '/jira/errata/create'
    headers = {
        'Access-Token': access_token,
        'PAT': pat,
        'Content-Type': 'application/json',
    }
    body = {
        'component': component,
        'fixversion': fixversion,
        'bugs': bugs,
    }
    if DRY_RUN:
        print(f'DRY_RUN: would POST {url} component={component} fixversion={fixversion} bugs={bugs}')
        return True
    r = requests.post(url, headers=headers, json=body, timeout=30,
                      verify=ca_certs_file)
    if r.status_code == 409:
        print(f'  CRYPTO errata epic already exists for {component}/{fixversion}, skipping')
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
    print('Usage: plan.py [-f firefox_version] [-v ckbi_version] [-o owner] [-m manager] [--dry-run] releases...')
    sys.exit(2)

resync = False
firefox_version = None
version = None
owner = None
manager = None
jira_api_key = None
jira_user = None
cryptosvc_url = None
cryptosvc_access_token = None
cryptosvc_pat_key = None
config = {}

for config_line in open(config_file, 'r'):
    line = config_line.strip()
    if not line or line.startswith('#'):
        continue
    key, value = line.split(':', 1)
    value = value.strip()
    config[key] = value
    if key == 'owner':           owner = value
    if key == 'manager':         manager = value
    if key == 'jira_url':        jira_url_base = value
    if key == 'jira_api_key':    jira_api_key = value
    if key == 'jira_user':       jira_user = value
    if key == 'errata_url':      errata_url_base = value
    if key == 'version':         version = value
    if key == 'firefox':         firefox_version = value
    if key == 'cryptosvc_url':           cryptosvc_url = value
    if key == 'cryptosvc_access_token':  cryptosvc_access_token = value
    if key == 'cryptosvc_pat_key':       cryptosvc_pat_key = value
    if key == 'dry_run':
        DRY_RUN = value.lower() == 'true'

for opt, arg in opts:
    if opt == '-f':        firefox_version = arg
    elif opt == '-v':      version = arg
    elif opt == '-o':      owner = arg
    elif opt == '-m':      manager = arg
    elif opt == '--dry-run':  DRY_RUN = True
    elif opt == '--resync':   resync = True

if not release_args:
    print('No releases specified.')
    sys.exit(1)

if firefox_version is None:
    print('Firefox version required. Use -f <version>.')
    sys.exit(2)

year = datetime.date.today().strftime('%Y')

# ── errata map ────────────────────────────────────────────────────────────────

errata_map = {}
if not resync:
    try:
        with open(errata_cache_file, 'r') as f:
            valid = datetime.datetime.strptime(f.readline().strip(), '%Y-%m-%d')
            delta = datetime.date.today() - valid.date()
            if delta > datetime.timedelta(days=30):
                resync = True
            else:
                errata_map = json.loads(f.read())
    except Exception:
        resync = True

if resync:
    errata_map = errata_get_release_info()
    with open(errata_cache_file, 'w') as f:
        f.write(datetime.date.today().strftime('%Y-%m-%d') + '\n')
        f.write(json.dumps(errata_map, indent=1))

ga_list = get_ga_list(errata_map)

# ── Jira client ───────────────────────────────────────────────────────────────

Jira = None
if jira_api_key:
    base_options = {'server': jira_url_base, 'verify': True}
    constructor_args = {'options': base_options, 'token_auth': jira_api_key}
    if 'stage' in jira_url_base:
        constructor_args['proxies'] = {
            'http': 'http://squid.corp.redhat.com:3128',
            'https': 'http://squid.corp.redhat.com:3128',
        }
    try:
        Jira = jiralib.JIRA(**constructor_args)
    except JIRAError as e:
        print(f'JIRA connection failed: {e}')
        sys.exit(1)

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
else:
    print('DRY_RUN: would wipe and recreate meta/')
    os.makedirs(meta_dir, exist_ok=True)

# ── read version files (if they existed before the wipe, from CLI or config) ──

nss_version = config.get('nss_version', 'unknown')
mcs_version = config.get('mcs_version', 'unknown')

if version is None:
    print('WARNING: CKBI version unknown; use -v or add version: to config.cfg')
    version = 'unknown'

# write version files so build_combo.sh and process.py can read them
if not DRY_RUN:
    for fname, val in [(ckbiver_file, version), (nssver_file, nss_version),
                       (mcsver_file, mcs_version), (firefox_info, firefox_version)]:
        with open(fname, 'w') as f:
            f.write(val)

# ── process releases ──────────────────────────────────────────────────────────

rhel_entries = []
fedora_entries = []
packages = 'ca-certificates'

print('\n=== Planning releases ===\n')

for release in release_args:
    # Fedora / rawhide — no Jira bug needed
    if release.startswith('f') or release == 'rawhide':
        print(f'{release}: fedora (no bug)')
        fedora_entries.append((release, packages, '0', '0', '', 'planned'))
        continue

    # RHEL release
    major = safe_int(release_get_major(release))
    zstream = get_need_zstream_clone(release, ga_list)

    print(f'{release}: looking up RHEL bug (zstream={zstream})')

    bugnumber = '0'
    if Jira:
        if zstream:
            bugnumber, _ = issue_lookup(Jira, release, version, packages, zstream=True)
            if bugnumber == '0':
                print(f'  no cloned z-stream bug yet for {release}')
        else:
            bugnumber, _ = issue_lookup(Jira, release, version, packages)
            if bugnumber == '0':
                bugnumber, _ = issue_create(
                    Jira, release, version, nss_version, firefox_version,
                    mcs_version, packages, zstream=False)
                if bugnumber != '0' and bugnumber != 'DRY-0' and major > 8:
                    issue_request_clone(Jira, release, version, packages)

    print(f'  bug={bugnumber}')

    # trigger CRYPTO errata epic creation via cryptosvc
    if (cryptosvc_url and cryptosvc_pat and cryptosvc_access_token
            and bugnumber not in ('0', 'DRY-0')):
        fixversion = release.replace('rhel-', '')
        print(f'  creating CRYPTO errata epic for {packages}/{fixversion}')
        cryptosvc_create_errata(
            cryptosvc_url, cryptosvc_access_token, cryptosvc_pat,
            packages, fixversion, [bugnumber])
    elif DRY_RUN and bugnumber == 'DRY-0':
        fixversion = release.replace('rhel-', '')
        cryptosvc_create_errata(
            cryptosvc_url or 'https://example.com',
            cryptosvc_access_token or 'token',
            cryptosvc_pat or 'pat',
            packages, fixversion, ['DRY-0'])

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
