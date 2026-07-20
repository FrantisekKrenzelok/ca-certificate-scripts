"""
NSS version discovery — fetch nss.h and nssckbi.h from Mozilla and
extract the version strings exactly as build_combo.sh does.
"""

import re
import requests

NSS_BASE_URL = (
    'https://hg.mozilla.org/releases/mozilla-release'
    '/raw-file/default/security/nss/lib'
)

def fetch_nss_versions(base_url=NSS_BASE_URL, timeout=30):
    """
    Fetch nss.h and nssckbi.h from Mozilla and return (nss_version, ckbi_version).
    Mirrors the wget + grep/awk logic in build_combo.sh.
    """
    url_nss   = f'{base_url}/nss/nss.h'
    url_ckbi  = f'{base_url}/ckfw/builtins/nssckbi.h'

    print(f'Fetching {url_nss}')
    r = requests.get(url_nss, timeout=timeout)
    r.raise_for_status()
    # #define NSS_VERSION "3.114 Basic ECC"  or  "3.114"  → 3.114
    m = re.search(r'#define\s+NSS_VERSION\s+"([\d.]+)', r.text)
    nss_version = m.group(1) if m else 'unknown'

    print(f'Fetching {url_ckbi}')
    r = requests.get(url_ckbi, timeout=timeout)
    r.raise_for_status()
    # #define NSS_BUILTINS_LIBRARY_VERSION "2.80"  → "2.80"
    m = re.search(r'#define\s+NSS_BUILTINS_LIBRARY_VERSION\s+"([^"]+)"', r.text)
    ckbi_version = m.group(1) if m else 'unknown'

    return nss_version, ckbi_version
