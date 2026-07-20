"""
Release string helpers and Errata Tool release-map loading.

All functions that need lists derived from the errata map (ga_list,
latest_zstreams) take those as explicit parameters so they remain
side-effect-free and testable.
"""

import json
import re
import requests

from functools import cmp_to_key
from requests_kerberos import HTTPKerberosAuth

CA_CERTS_FILE = '/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem'

# ── release string helpers ────────────────────────────────────────────────────

def release_get_major(release):
    """Return the major version string from a release like 'rhel-9.6.0' → '9'."""
    comp = release.split('-')
    if len(comp) != 2:
        return None
    version = comp[1].split('.')
    if len(version) < 2:
        return None
    return version[0]

def safe_int(a):
    """Convert to int, returning 0 on failure."""
    try:
        return int(a)
    except (ValueError, TypeError):
        return 0

def get_need_zstream_clone(release, ga_list):
    """Return True if this release needs a z-stream bug clone from the y-stream."""
    if safe_int(release_get_major(release)) < 8:
        return False
    release = re.sub(r'^rhel-(\d+\.\d+)$', r'rhel-\1.0', release)
    return release not in ga_list

def is_latest_z_stream(release, latest_zstreams):
    """Return True if this is the latest z-stream for its major version."""
    release = re.sub(r'^rhel-(\d+\.\d+)$', r'rhel-\1.0', release)
    return release in latest_zstreams

def release_requires_build(release, latest_zstreams):
    """Return True if this release should trigger its own build."""
    if safe_int(release_get_major(release)) < 9:
        return True
    return is_latest_z_stream(release, latest_zstreams)

def release_is_centos_stream(release, ga_list):
    """Return True if this RHEL release is handled via CentOS Stream."""
    if safe_int(release_get_major(release)) < 8:
        return False
    return not get_need_zstream_clone(release, ga_list)

# ── errata map loading ────────────────────────────────────────────────────────

def _splitnumeric(string):
    numeric = ''
    pos = len(string)
    for i in range(0, pos - 1):
        if not string[i].isnumeric():
            pos = i
            break
        numeric += string[i]
    return (numeric, string[pos:])

def errata_nvrcmp(rel1, rel2):
    """Compare two RHEL release strings for sorting (rpm-style)."""
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
            v1n, v1rest = _splitnumeric(ver1[i])
            v2n, v2rest = _splitnumeric(ver2[i])
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

def _errata_get_version_order(version):
    order = {'.EUS': 10, '-EUS': 9, '.Z': 8, '.AUS': 7, '-AUS': 6,
             '.TUS': 5, '-TUS': 4, '.E4S': 3, '-E4S': 2}
    for suffix, score in order.items():
        if version.endswith(suffix):
            return score
    return 0

def _errata_is_better(best, compare, isga):
    if best is None:
        return True
    bestname = best['name']
    if bestname.endswith('.GA'):
        return not isga
    comparename = compare['name']
    if comparename.endswith('.GA'):
        return isga
    if bestname.endswith('.MAIN+EUS'):
        return False
    if comparename.endswith('.MAIN+EUS'):
        return True
    return _errata_get_version_order(bestname) < _errata_get_version_order(comparename)

def errata_candidate_to_release(brew_tag):
    lists = brew_tag.split('-')
    if len(lists) < 1:
        return 'empty'
    rhel_type = lists[0].lower()
    if len(lists) < 2:
        return rhel_type
    return '%s-%s' % (rhel_type, lists[1])

def _errata_get_all_pages(url, paste, request_type, ca_certs_file=CA_CERTS_FILE):
    headers = {'Content-type': 'application/json', 'Accept': 'application/json'}
    r = requests.get(url, headers=headers, auth=HTTPKerberosAuth(), verify=ca_certs_file)
    if r.status_code > 299:
        print('errata %s status=%d' % (request_type, r.status_code))
        print('text=', r.text)
        return None
    data = r.json()['data']
    if 'page' in r.json():
        num_pages = r.json()['page']['total_pages']
        if num_pages != 1:
            for i in range(2, num_pages + 1):
                url_page = '%s%spage[number]=%d' % (url, paste, i)
                r = requests.get(url_page, headers=headers,
                                 auth=HTTPKerberosAuth(), verify=ca_certs_file)
                if r.status_code > 299:
                    print('errata %s page %d status=%d' % (request_type, i, r.status_code))
                    print('text=', r.text)
                    return None
                data = data + r.json()['data']
    return data

def errata_get_release_info(errata_url_base, ca_certs_file=CA_CERTS_FILE):
    """Fetch the full errata product-version map from the Errata Tool."""
    # fetch release IDs
    data = _errata_get_all_pages(
        errata_url_base + '/api/v1/releases', '?', '', ca_certs_file)
    if data is None:
        return {}
    errata_release_ids = {
        item.get('attributes').get('name'): item.get('id') for item in data
    }

    # fetch product versions
    data = _errata_get_all_pages(
        errata_url_base + '/api/v1/products/16/product_versions',
        '?', 'release_info', ca_certs_file)
    if data is None:
        return {}

    product_version_list = {}
    releases = []
    maps = {}

    for pv in data:
        attrs = pv['attributes']
        name = attrs['name']
        if name not in errata_release_ids:
            print('%s not in errata_release_ids' % name)
            continue
        brew = attrs['default_brew_tag']
        if brew is None:
            print('brew tag is None for %s' % name)
            continue
        release = errata_candidate_to_release(brew)
        if release not in releases:
            print('adding release= %s' % release)
            releases.append(release)
        if attrs['enabled']:
            info = {
                'name': name,
                'description': attrs['description'],
                'id': pv['id'],
                'release_id': errata_release_ids[name],
            }
            product_version_list.setdefault(release, []).append(info)

    sorted_releases = sorted(releases, key=cmp_to_key(errata_nvrcmp))
    print('sorted_releases =', sorted_releases)

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
                if _errata_is_better(best, version, release == ga):
                    best = version
            maps[release] = best
            print('release=', release, 'map=', maps[release])

    return maps

def get_ga_list(errata_map):
    """Return the list of GA releases (one per major version)."""
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

def get_latest_zstreams(errata_map):
    """Return the latest z-stream release per major version."""
    l_zstream_list = []
    last_major = 0
    last_zstream = None
    for release in errata_map.keys():
        name = errata_map[release]['name'] if errata_map[release] else ''
        if '.Z' not in name:
            continue
        current_major = release_get_major(release)
        if last_major != current_major:
            if last_zstream is not None:
                l_zstream_list.append(last_zstream)
            last_major = current_major
        last_zstream = release
    if last_zstream is not None:
        l_zstream_list.append(last_zstream)
    return l_zstream_list

def discover_fedora_releases():
    """
    Query the Bodhi API for current and pending Fedora releases.
    Returns releases newest-first in build_combo.sh format:
      ['rawhide', 'f45', 'f44', 'f43']
    Rawhide is always prepended — it's never returned by Bodhi.
    """
    url = 'https://bodhi.fedoraproject.org/releases/'
    releases = []
    page = 1

    while True:
        r = requests.get(url, params=[('state', 'current'), ('state', 'pending'),
                                      ('page', page)], timeout=30)
        r.raise_for_status()
        data = r.json()
        for rel in data['releases']:
            name = rel['name']
            if re.match(r'^F[0-9]+$', name):
                releases.append(name.lower())          # F43 → f43
        if page >= data.get('pages', 1):
            break
        page += 1

    # sort descending (newest first), rawhide always last
    fedora_nums = sorted([r for r in releases if r != 'rawhide'],
                         key=lambda r: int(r[1:]), reverse=True)
    return ['rawhide'] + fedora_nums

def discover_rhel_releases(errata_map, ga_list, min_major=8):
    """
    Return all active RHEL releases from the errata map, grouped and sorted
    per major version, newest first.

    Returns a list of dicts:
      {'release': str, 'major': int, 'is_ga': bool, 'use_zstream': bool}

    is_ga=True, use_zstream=False — true GA (e.g. rhel-10.3): create y-stream bug,
                                    request 'All Active Z-streams' clones.
    is_ga=True, use_zstream=True  — head of z-stream-only major (e.g. rhel-8.10.0,
                                    rhel-9.9.0): create z-stream bug directly, then
                                    also request 'All Active Z-streams' clones.
    is_ga=False                   — other z-streams: wait for the cloned bug.

    A major is "z-stream-only" when none of its errata product versions end with
    '.GA' (e.g. 'RHEL-8.10.0.Z.MAIN+EUS'). The latest release of such a major
    is treated as the head and gets is_ga=True so the clone logic runs.
    """
    by_major = {}
    for release in errata_map.keys():
        if not release.startswith('rhel-'):
            continue
        major = safe_int(release_get_major(release))
        if major < min_major:
            continue
        by_major.setdefault(major, []).append(release)

    result = []
    for major in sorted(by_major.keys(), reverse=True):
        sorted_releases = sorted(
            by_major[major],
            key=lambda r: [safe_int(x) for x in r.split('-')[1].split('.')],
            reverse=True)

        # Determine if this major has a true GA by checking errata product names
        has_true_ga = any(
            (errata_map.get(r) or {}).get('name', '').endswith('.GA')
            for r in sorted_releases
        )

        for i, release in enumerate(sorted_releases):
            if has_true_ga:
                is_ga       = release in ga_list
                use_zstream = False
            else:
                # z-stream-only major: head release treated as GA for clone purposes
                is_ga       = (i == 0)
                use_zstream = True

            result.append({
                'release':     release,
                'major':       major,
                'is_ga':       is_ga,
                'use_zstream': use_zstream,
            })
    return result

def load_errata_map(errata_url_base, cache_file, ca_certs_file=CA_CERTS_FILE, force_resync=False):
    """
    Load errata map from cache, refreshing if stale (>30 days) or forced.
    Returns (errata_map, ga_list, latest_zstreams).
    """
    import datetime
    errata_map = {}
    resync = force_resync

    if not resync:
        try:
            with open(cache_file, 'r') as f:
                valid = datetime.datetime.strptime(f.readline().strip(), '%Y-%m-%d')
                delta = datetime.date.today() - valid.date()
                if delta > datetime.timedelta(days=30):
                    resync = True
                else:
                    errata_map = json.loads(f.read())
        except Exception:
            resync = True

    if resync:
        errata_map = errata_get_release_info(errata_url_base, ca_certs_file)
        with open(cache_file, 'w') as f:
            f.write(datetime.date.today().strftime('%Y-%m-%d') + '\n')
            f.write(json.dumps(errata_map, indent=1))

    return errata_map, get_ga_list(errata_map), get_latest_zstreams(errata_map)
