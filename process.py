#!/usr/bin/python3
# vim:set et sw=4:
#
# certdata2pem.py - splits certdata.txt into multiple files
#
# Copyright (C) 2009 Philipp Kern <pkern@debian.org>
# Copyright (C) 2013 Kai Engert <kaie@redhat.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301,
# USA.

import os
import subprocess
import sys
import getopt
import requests
import json
import git
import datetime
import gitlab
import re

from requests_kerberos import HTTPKerberosAuth
from jira import JIRAError
from caupdate.tui import PipelineOutput
from caupdate.release_config import uses_centos_stream, version_parts as _version_parts, centos_branch as _centos_branch
from caupdate.prereqs import check_prereqs
from caupdate.release import (
    release_get_major, safe_int,
    release_is_centos_stream,
    errata_get_release_info,
    load_errata_map, CA_CERTS_FILE,
)
from caupdate.issues import (
    issue_get_state, issue_change_state, issue_get,
    issue_update_versions,
    make_jira_client,
    bug_summary_short, bug_summary, bug_description,
)

DRY_RUN = False # Used alongside staging environments for development

rhel_list='./meta/rhel.list'
fedora_list='./meta/fedora.list'
ckbiver_file='./meta/ckbiversion.txt'
nssver_file='./meta/nssversion.txt'
mcsver_file='./meta/mcsversion.txt'
firefox_info='./meta/firefox_info.txt'
config_file='./config.cfg'
release_id_file='./release_id'
errata_cache_file='./errata_cache'
errata_url_base='https://errata.devel.redhat.com'
brew_url_base='https://brewweb.engineering.redhat.com/brew'
koji_url_base='https://koji.fedoraproject.org/koji'
jira_url_base='https://issues.redhat.com'
glab_url_base='https://gitlab.com/'
ca_certs_file=CA_CERTS_FILE
distro=None



# define differences between rhel and
# fedora releases
#
packages_dir = {
    "rhel":"./packages/rhel/",
    "fedora":"./packages/fedora/",
    "centos":"./packages/centos"
}
build_info_tool = {
    "rhel":"brew",
    "fedora":"koji",
    "centos":"koji -p stream"
}
package_tool = {
    "rhel":"rhpkg",
    "fedora":"fedpkg",
    "centos":"centpkg"
}

ga_list = []
errata_map = {}
config = {}
Jira = None

def _centos_fork_dir(release) -> str:
    """Return the centos-fork worktree path for this release (e.g. ./packages/centos-fork/c10s)."""
    major = safe_int(release_get_major(release))
    return f'./packages/centos-fork/{_centos_branch(major)}'


def _rhel_branch(release) -> str:
    """Convert a release string to its dist-git branch name (mirrors build_combo._distgit_branch)."""
    import re as _re
    m = _re.match(r'^rhel-(\d+)\.(\d+)\.(\d+)$', release)
    if m and _version_parts(int(m.group(1))) == 2:
        return f'rhel-{m.group(1)}.{m.group(2)}'
    return release


def _release_dir(release) -> str:
    """Return the worktree directory name for a release.
    Uses entry['branch'] when available (set by build_combo.py), falling back
    to _rhel_branch().  For dist_branch releases (e.g. rhel-8.10 → rhel-8-main)
    build_combo creates the worktree under the dist_branch name directly.
    """
    branch = (rhel_packages.get(release, {}).get('branch', '')
              or fedora_packages.get(release, {}).get('branch', ''))
    return branch if branch else _rhel_branch(release)


# handle package location differences for centos stream vs direct RHEL
def get_git_packages_dir(distro, package, release):
    if distro == 'centos':
        return _centos_fork_dir(release)
    return packages_dir[distro] + _release_dir(release)


def get_build_packages_dir(distro, package, release):
    if distro == 'centos':
        return _centos_fork_dir(release)
    return packages_dir[distro] + _release_dir(release)


# Release helpers and errata-map functions are in caupdate.release.
def _release_is_centos_stream(release):
    """True if this release flows through the centos-fork (GA of a centos_stream major).

    Uses the same structural check as build_combo.py: if uses_centos_stream is
    True for the major AND no dedicated RHEL worktree exists for this release,
    it is the current GA going through centos-fork.  This avoids depending on
    the errata-map ga_list which may be stale or missing new majors.
    """
    major = safe_int(release_get_major(release))
    if not uses_centos_stream(major):
        return False
    return not os.path.isdir(packages_dir['rhel'] + _rhel_branch(release))


#
# mapping functions to map release
# to errata strings
#
def release_map(release) :
    if not release in errata_map:
       return None
    return errata_map[release]['name']

def numeric_release_map(release) :
    if not release in errata_map:
       return 0
    return errata_map[release]['id']

def release_description_map(release):
    if not release in errata_map:
       return None
    return errata_map[release]['description']

def release_get_release_id(release):
    if not release in errata_map:
       return None
    return errata_map[release]['release_id']

package_description_map= {
    "ca-certificates":"The ca-certificates package contains a set of Certificate Authority (CA) certificates chosen by the Mozilla Foundation for use with the Internet Public Key Infrastructure (PKI).",
}

# constants
owner=None
manager=None
qe=None
se_contact=None
firefox_version=None
jira_api_key=None
jira_user=None
Jira=None
GLab=None
glab_api_key=None
CentOSFork=None
centos_fork=None

solution="Before applying this update, make sure all previously released errata relevant to your system have been applied.\n\nFor details on how to apply this update, refer to:\n\nhttps://access.redhat.com/articles/11258"
description_base="Bug Fix(es) and Enhancement(s):\n\n* Update ca-certificates package in %s to CA trust list version (%s) %s from Firefox %s (bug %s)\n"
synopsis="%s bug fix and enhancement update"
topic_base="An update for %s %s now available for %s."
checkin_log="checkin.log"

def _resolve_jira_account(email):
    """Resolve an email to a Jira Cloud accountId."""
    if not (Jira and email):
        return None
    try:
        r = requests.get(f'{Jira.url}/rest/api/3/user/search',
                         params={'query': email}, headers=Jira._headers(), timeout=30)
        if r.status_code == 200:
            data = r.json()
            if data:
                return data[0].get('accountId')
    except Exception as e:
        print(f'  WARNING: could not resolve account for {email}: {e}')
    return None


_PRELIM_TESTING_FIELD  = 'customfield_10879'
_PRELIM_TESTING_REQUESTED_ID = '20445'  # option id for "Requested"

def _set_preliminary_testing_requested(bugnumber, release):
    """Set the 'Preliminary Testing' field to 'Requested' on the Jira bug."""
    if not bugnumber or bugnumber in ('0',) or not Jira:
        return
    try:
        r = requests.get(f'{Jira.url}/rest/api/3/issue/{bugnumber}',
                         params={'fields': _PRELIM_TESTING_FIELD},
                         headers=Jira._headers(), timeout=30)
        if r.status_code == 200:
            current = (r.json().get('fields', {})
                               .get(_PRELIM_TESTING_FIELD) or {})
            if current.get('id') == _PRELIM_TESTING_REQUESTED_ID:
                return  # already set
    except Exception:
        pass
    payload = {'fields': {_PRELIM_TESTING_FIELD: {'id': _PRELIM_TESTING_REQUESTED_ID}}}
    try:
        r = requests.put(f'{Jira.url}/rest/api/3/issue/{bugnumber}',
                         json=payload, headers=Jira._headers(), timeout=30)
        if r.status_code == 204:
            _log(release, 'Preliminary Testing → Requested')
        else:
            print(f'  WARNING: could not set Preliminary Testing on {bugnumber}: '
                  f'{r.status_code} {r.text[:120]}')
    except Exception as e:
        print(f'  WARNING: set Preliminary Testing on {bugnumber} failed: {e}')


def _set_qa_contact(bugnumber, release):
    """Set QA contact on a RHEL bug: se_contact for sustaining, qe for regular releases."""
    from caupdate.release import is_sustaining_release
    pv_name = errata_map.get(release, {}).get('name', '') if errata_map.get(release) else ''
    is_sustaining = is_sustaining_release(pv_name)
    contact_email = se_contact if is_sustaining else qe
    if not contact_email or not bugnumber or bugnumber in ('0',):
        return
    account_id = _resolve_jira_account(contact_email)
    if not account_id:
        print(f'  WARNING: could not resolve QA contact {contact_email}')
        return
    try:
        r = requests.get(f'{Jira.url}/rest/api/3/issue/{bugnumber}',
                         params={'fields': 'customfield_10470'},
                         headers=Jira._headers(), timeout=30)
        if r.status_code == 200:
            current = (r.json().get('fields', {})
                               .get('customfield_10470') or {})
            if current.get('accountId') == account_id:
                return  # already set correctly
    except Exception:
        pass  # if the check fails, attempt the update anyway
    payload = {'fields': {'customfield_10470': {'accountId': account_id}}}
    try:
        r = requests.put(f'{Jira.url}/rest/api/3/issue/{bugnumber}',
                         json=payload, headers=Jira._headers(), timeout=30)
        if r.status_code == 204:
            print(f'  [{release}] QA contact set to {contact_email} ({"SE" if is_sustaining else "QE"})')
        else:
            print(f'  WARNING: could not set QA on {bugnumber}: {r.status_code} {r.text[:120]}')
    except Exception as e:
        print(f'  WARNING: set QA on {bugnumber} failed: {e}')


# Wrappers for issue functions to inject the year global.
def _issue_get(bugnumber):
    return issue_get(Jira, bugnumber)

def _issue_get_state(issue):
    return issue_get_state(issue)

def _issue_change_state(issue, state):
    key = issue if isinstance(issue, str) else issue.get('key', issue)
    return issue_change_state(Jira, key, state)

#
#    Errata helper function
#
# create a new errata and attach the bug returns the errata number
def _ga_product_name(z_name):
    """Convert a z-stream errata product name to the GA equivalent.
    'RHEL-10.3.Z' → 'RHEL-10.3.GA'
    'RHEL-9.9.0.Z.MAIN' → 'RHEL-9.9.0.GA'
    """
    import re as _re
    return _re.sub(r'\.Z(?:\.[A-Z]+)*$', '.GA', z_name or '')


def _ga_release_id(ga_name):
    """Look up the numeric release_id for a product version from the errata API."""
    headers = {'Content-type': 'application/json', 'Accept': 'application/json'}
    try:
        r = requests.get(errata_url_base + '/api/v1/releases',
                         params={'filter[name]': ga_name},
                         headers=headers, auth=HTTPKerberosAuth(),
                         verify=ca_certs_file, timeout=30)
        if r.status_code <= 299:
            data = r.json().get('data', [])
            if data:
                rid = data[0].get('id') or data[0].get('attributes', {}).get('id')
                return int(rid) if rid is not None else None
        else:
            print(f'  WARNING: release lookup failed {r.status_code} for {ga_name!r}')
    except Exception as e:
        print(f'  WARNING: could not look up release_id for {ga_name}: {e}')
    return None


def _release_name_by_id(release_id):
    """Return the Errata Tool release short name for a numeric release_id."""
    headers = {'Content-type': 'application/json', 'Accept': 'application/json'}
    try:
        r = requests.get(f'{errata_url_base}/api/v1/releases/{release_id}',
                         headers=headers, auth=HTTPKerberosAuth(),
                         verify=ca_certs_file, timeout=30)
        if r.status_code <= 299:
            data = r.json()
            return (data.get('data', {}).get('attributes', {}).get('name')
                    or data.get('name'))
    except Exception as e:
        print(f'  WARNING: could not fetch release name for id={release_id}: {e}')
    return None


def errata_create(release, version, firefox_version, packages, year, bugnumber,
                  force_ga=False) :
    release_name=release_map(release)
    if force_ga and release_name:
        release_name = _ga_product_name(release_name)
        print(f'  using GA product version: {release_name}')
    if release_name == None :
        print("Can't find product version for release %s, skipping errata create"%release)
        return 0
    release_description=release_description_map(release)
    advisory= dict()
    packages_list=packages.split(',')
    # handle singular and plural verbs, adjust the packages to english
    verb='is'
    package_names=packages
    if len(packages_list) != 1 :
       verb='are'
       # replace just the last occurance of , with ' and ' and add a space to
       # the rest of the commas
       package_names=packages[::-1].replace(',',' and ',1)[::-1].replace(',',', ')
    #build the description
    description=''
    for package in packages_list :
       description=description+package_description_map[package]+'\n\n'
    description=description+description_base%(release_name,year,version,firefox_version,bugnumber)
    product = 'RHEL'
    #now build the advisory
    advisory['errata_type']='RHBA'
    advisory['security_impact']='None'
    advisory['solution']=solution;
    advisory['description']=description
    advisory['manager_email']=manager
    advisory['package_owner_email']=owner
    advisory['synopsis']=synopsis%package_names
    advisory['topic']=topic_base%(package_names,verb,release_description)
    advisory['idsfixed']=bugnumber
    errata= {}
    errata['product']=product
    # Resolve release_id: try by product-version name first, fall back to cache.
    # Always cast to int — JSON:API returns IDs as strings and the Errata Tool
    # ignores a string release_id (treats it as nil → "Couldn't find Release without an ID").
    _rel_id = _ga_release_id(release_name) or release_get_release_id(release)
    if _rel_id:
        try:
            _rel_id = int(_rel_id)
        except (TypeError, ValueError):
            _rel_id = None
    if _rel_id:
        errata['release_id'] = _rel_id
        _rel_name = _release_name_by_id(_rel_id)
        errata['release'] = _rel_name or release_name
    else:
        errata['release'] = release_name
    errata['advisory']=advisory
    print("----------Creating errata for "+release.strip())
    print(f"  release={errata.get('release')!r}  release_id={errata.get('release_id')!r}")
    headers= { 'Content-type':'application/json', 'Accept':'application/json' }
    url=errata_url_base+'/api/v1/erratum'
    r = requests.post(url, headers=headers, json=errata,
                     auth=HTTPKerberosAuth(),
                     verify=ca_certs_file)
    if r.status_code <= 299 :
        return r.json()['errata']['rhba']['id']
    print('errata create status=%d'%r.status_code)
    print('returned text=',r.text)
    return 0

def errata_get_all_pages(url, paste, request_type):
    from caupdate.release import _errata_get_all_pages
    return _errata_get_all_pages(url, paste, request_type, ca_certs_file)

def errata_lookup(release, version, firefox_version, packages, force_ga=False) :
    headers= { 'Content-type':'application/json', 'Accept':'application/json' }
    packages_list=packages.split(',')
    if force_ga:
        ga_name = _ga_product_name(release_map(release) or '')
        release_id = _ga_release_id(ga_name)
        if not release_id:
            print(f"couldn't find GA release id for: {ga_name}")
            return 0
    else:
        release_id = release_get_release_id(release)
        if release_id == None:
            print("couldn't find release id for release: " + release)
            return 0
    search_params="/api/v1/erratum/search?show_state_NEW_FILES=1&show_state_QE=1&product[]=16&release[]=%s&synopsis_text=%s"%(release_id,packages_list[0])
    url=errata_url_base + search_params
    r = requests.get(url, headers=headers,
                     auth=HTTPKerberosAuth(),
                     verify=ca_certs_file)
    if r.status_code > 299 :
        print('errata lookup status=%d'%r.status_code)
        print('text=',r.text)
        return 0
    data=r.json()['data']
    if len(data) == 0 :
        print("errata for %s (%d) %s not found"%(release,numeric_release_map(release),packages_list[0]))
        return 0
    return int(data[0]['id'])

# return the nvr of the attached builds
def errata_get_bugs(errata) :
    headers= { 'Content-type':'application/json', 'Accept':'application/json' }
    url=errata_url_base+"/api/v1/erratum/%d"%errata
    r = requests.get(url, headers=headers,
                     auth=HTTPKerberosAuth(),
                     verify=ca_certs_file)
    if r.status_code >  299 :
        print('errata get builds status=%d'%r.status_code)
        print('text=',r.text)
        return []
    data = r.json()
    if not data:
        return []
    bugs = []
    # Legacy Bugzilla bugs
    for bug in data.get('bugs', {}).get('bugs', []):
        bugs.append(bug['bug']['id'])
    # Jira issues (post-migration) — keyed by issue key e.g. RHEL-212568
    for issue in data.get('jira_issues', {}).get('jira_issues', []):
        bugs.append(issue['jira_issue']['key'])
    return bugs

# return the nvr of the attached builds
def errata_get_builds(errata, release, force_ga=False) :
    headers= { 'Content-type':'application/json', 'Accept':'application/json' }
    url=errata_url_base+"/api/v1/erratum/%d/builds"%errata
    r = requests.get(url, headers=headers,
                     auth=HTTPKerberosAuth(),
                     verify=ca_certs_file)
    if r.status_code >  299 :
        print('errata get builds status=%d'%r.status_code)
        print('text=',r.text)
        return []
    data = r.json()
    if len(data) == 0 :
        return []
    pv = release_map(release)
    if force_ga and pv:
        pv = _ga_product_name(pv)
    # Fall back to scanning all product versions if exact key not found
    if pv not in data:
        pv = next((k for k in data if k.endswith('.GA') or k == release_map(release)), None)
    if not pv or pv not in data:
        return []
    builds = []
    for builditem in data[pv]['builds'] :
        builds +=  list(builditem.keys())
    return builds

def errata_has_bug(errata, bug) :
    if not errata:
        return False   # no errata yet — bug not attached
    bugs = errata_get_bugs(errata)
    for this_bug in bugs :
        if str(bug) == str(this_bug) :
            return True
    return False

# return True if errata has all the builds attached
def errata_has_builds(errata, release, builds, force_ga=False) :
    if not errata:
        return False   # no errata yet — builds not attached
    nvrlist = errata_get_builds(errata, release, force_ga=force_ga)
    for build in builds.split(',') :
        if not build in nvrlist :
            return False
    return True

def errata_resync_bug(errata, bug) :
    if not errata:
        return
    request= []
    request.append(bug)
    headers= { 'Content-type':'application/json', 'Accept':'application/json' }
    url=errata_url_base+"/api/v1/erratum/%d/bug/refresh"%errata
    r = requests.post(url, headers=headers, json=request,
                     auth=HTTPKerberosAuth(),
                     verify=ca_certs_file)
    if r.status_code <= 299 :
        return
    print('errata resync bug status=%d'%r.status_code)
    print('text=',r.text)
    return

# add a bug to the errata
def errata_add_bug(errata, bug, resync) :
    if not errata:
        return
    if errata_has_bug(errata, bug) :
        return
    if (resync) :
        errata_resync_bug(errata,bug)
    request= {}
    request['bug'] = bug
    headers= { 'Content-type':'application/json', 'Accept':'application/json' }
    url=errata_url_base+"/api/v1/erratum/%d/add_bug"%errata
    r = requests.post(url, headers=headers, json=request,
                     auth=HTTPKerberosAuth(),
                     verify=ca_certs_file)
    if r.status_code <= 299 :
        return
    print('errata add bug status=%d'%r.status_code)
    print('text=',r.text)
    return

# add builds to the errata
def errata_add_builds(errata, release, builds, force_ga=False) :
    if not errata:
        return
    nvr = errata_get_builds(errata, release)
    pv = release_map(release)
    if force_ga and pv:
        pv = _ga_product_name(pv)
    request= []
    # only add builds we haven't successfully added yet
    for build in builds.split(',') :
        if not build in nvr :
            entry = dict()
            entry['product_version']=pv
            entry['build']=build
            request.append(entry)
    # if they are all already added, don't send an empty request
    if len(request) == 0 :
        return 0
    headers= { 'Content-type':'application/json', 'Accept':'application/json' }
    url=errata_url_base+"/api/v1/erratum/%d/add_builds"%errata
    r = requests.post(url, headers=headers, json=request,
                     auth=HTTPKerberosAuth(),
                     verify=ca_certs_file)
    if r.status_code <= 299 or r.status_code == 401:
        return
    print('errata add builds status=%d'%r.status_code)
    print('text=',r.text)
    return

def errata_get_release_info():
    from caupdate.release import errata_get_release_info as _eri
    return _eri(errata_url_base, ca_certs_file)

def errata_merge_rpm_status(status, status2) :
    # first, state of PASSED has lowest priority
    # STATUSs are PASSED, WAIVED, INFO, FAILED, RUNING, PENDING
    # in reverse order of precedence
    if status == 'PASSED':
        return status2
    if status2 == 'PASSED':
        return status
    # if they are equal, return them
    if status == status2 :
        return status
    # 'Pending' has the highest precedence
    if status == 'PENDING' or status2 == 'PENDING' :
        return 'PENDING'
    # 'Running' has the highest precedence
    if status == 'RUNNING' or status2 == 'RUNNING' :
        return 'RUNNING'
    # 'Failed' is next
    if status == 'FAILED' or status2 == 'FAILED' :
        return 'FAILED'
    # now we know that 1) state != state2, and neither
    # is equal to 'Passed', 'Pending', 'Running' or 'Failed'
    # One must be 'Info' and the other 'Waived', 'Info'
    # has precedence
    return 'INFO'

def errata_get_rpm_state(erratanumber, builds) :
    headers= { 'Content-type':'application/json', 'Accept':'application/json' }
    params="/api/v1/external_tests?filter[errata_id]=%d&filter[test_type]=rpmdiff"%erratanumber
    url=errata_url_base + params
    data = errata_get_all_pages(url,"&","get rpm state")
    if data == None :
        return "PASSED"
    current_status = "PASSED"
    for rpmdiff in data :
        relationships = rpmdiff['relationships']
        if relationships['brew_build']['nvr'] in builds :
            status = rpmdiff['attributes']['status']
            if 'superseded_by' in relationships:
                status = relationships['status']
            current_status = errata_merge_rpm_status(status, current_status)
    return current_status
    
def errata_get_state(erratanumber) :
    headers= { 'Content-type':'application/json', 'Accept':'application/json' }
    url=errata_url_base+"/api/v1/erratum/%d"%erratanumber
    r = requests.get(url, headers=headers,
                     auth=HTTPKerberosAuth(),
                     verify=ca_certs_file)
    if r.status_code >  299 :
        print('errata get builds status=%d'%r.status_code)
        print('text=',r.text)
        return 'UNKNOWN'
    if len(r.json()) == 0 :
        return 'UNKNOWN'
    errata=r.json()
    if not 'errata' in errata :
        return 'UNKNOWN'
    if 'rhba' in errata['errata'] :
        return errata['errata']['rhba']['status']
    elif 'rhea' in errata['errata'] :
        return errata['errata']['rhea']['status']
    elif 'rhsa' in errata['errata'] :
        return errata['errata']['rhsa']['status']
    return 'UNKNOWN'

_errata_user_id_cache = {}

def _errata_user_id(email):
    """Return the numeric errata user ID for an email, cached."""
    if email in _errata_user_id_cache:
        return _errata_user_id_cache[email]
    try:
        r = requests.get(f'{errata_url_base}/api/v1/user/{email}',
                         headers={'Accept': 'application/json'},
                         auth=HTTPKerberosAuth(), verify=ca_certs_file, timeout=30)
        if r.status_code == 200:
            uid = r.json().get('id')
            if uid:
                _errata_user_id_cache[email] = uid
                return uid
    except Exception as e:
        print(f'  WARNING: could not fetch errata user id for {email}: {e}')
    return None


def _set_errata_qa(erratanumber, release):
    """Set QA owner on an errata via form POST to /errata/change_owner/{id}."""
    if not erratanumber:
        return
    from caupdate.release import is_sustaining_release as _is_se
    pv_name = errata_map.get(release, {}).get('name', '') if errata_map.get(release) else ''
    contact = se_contact if _is_se(pv_name) else qe
    if not contact:
        return
    new_uid = _errata_user_id(contact)
    if not new_uid:
        print(f'  WARNING: could not resolve errata user id for {contact}')
        return
    # Fetch current assigned_to_id to pass as old_qe_user_id (required by change_owner)
    old_uid = 3000002  # default unassigned
    try:
        r0 = requests.get(f'{errata_url_base}/api/v1/erratum/{erratanumber}',
                          headers={'Accept': 'application/json'},
                          auth=HTTPKerberosAuth(), verify=ca_certs_file, timeout=30)
        if r0.status_code == 200:
            old_uid = r0.json().get('errata', {}).get('rhba', {}).get('assigned_to_id', old_uid) or old_uid
    except Exception:
        pass
    if old_uid == new_uid:
        return  # already correct
    try:
        # POST with params in URL — the action executes despite the redirect response
        requests.post(
            f'{errata_url_base}/errata/change_owner/{erratanumber}'
            f'?new_qe_user_id={new_uid}&old_qe_user_id={old_uid}'
            f'&new_qe_group_id=114&old_qe_group_id=114&commit=Change',
            auth=HTTPKerberosAuth(), verify=ca_certs_file, timeout=30,
            allow_redirects=True)
        print(f'  errata {erratanumber}: QA owner → {contact} (id={new_uid})')
    except Exception as e:
        print(f'  WARNING: errata QA set failed {erratanumber}: {e}')


def errata_set_state(erratanumber,newstate) :
    if not erratanumber:
        return 'UNKNOWN'
    request= {}
    request['new_state'] = newstate
    headers= { 'Content-type':'application/json', 'Accept':'application/json' }
    url=errata_url_base+"/api/v1/erratum/%d/change_state"%erratanumber
    r = requests.post(url, headers=headers, json=request,
                     auth=HTTPKerberosAuth(),
                     verify=ca_certs_file)
    if r.status_code <= 299 :
        return errata_get_state(erratanumber)
    print('errata change state to %s status=%d'%(newstate,r.status_code))
    print('text=',r.text)
    return 'UNKNOWN'

#
#    git helper functions
#
def git_files_exist(diff):
    for cfile in diff.iter_change_type('M'):
        return True
    for cfile in diff.iter_change_type('A'):
        if (cfile != checkin_log):
            return True
    for cfile in diff.iter_change_type('D'):
        return True
    for cfile in diff.iter_change_type('T'):
        return True
    return False

def git_ensure_branch(repo):
    """If HEAD is detached, check out the branch whose name matches the worktree dir."""
    try:
        repo.active_branch
    except TypeError:
        branch_name = os.path.basename(repo.working_dir)
        print(f'WARNING: detached HEAD in {repo.working_dir} — checking out {branch_name}')
        repo.git.checkout(branch_name)

def git_repo_state(repo):
    git_ensure_branch(repo)
    index = repo.index
    commit = repo.head.commit
    origin = repo.remotes.origin
    branch = repo.active_branch

    # staged means changes need committing
    if git_files_exist(index.diff(None)) :
        return 'staged'
    if git_files_exist(index.diff(commit)) :
        return 'staged'
    # committed mean changes are committed, but not pushed
    if not branch.name in origin.refs :
        return 'committed'
    if git_files_exist(commit.diff(origin.refs[branch.name])) :
        return 'committed'
    return 'pushed'

def git_get_state(release, package, bugnumber):
    directory = get_git_packages_dir(distro,package,release)
    try:
        repo = git.Repo(directory)
    except Exception:
        print("repo: "+directory+" doesn't exists")
        return None
    git_ensure_branch(repo)
    return git_repo_state(repo)

def commit_and_push_tests():
    """Commit and push staged changes in packages/tests/ before RHEL builds.

    Reads meta/tests_state.txt written by build_combo.py.  Skips if already
    pushed this cycle.  Updates the state file to 'pushed' on success.
    Aborts with a warning if the tests NSS version doesn't match meta/nssversion.txt.
    """
    tests_state_file = './meta/tests_state.txt'
    tests_dir = './packages/tests'

    # Read current state
    state_info = {}
    try:
        for line in open(tests_state_file).read().splitlines():
            if '=' in line:
                k, _, v = line.partition('=')
                state_info[k.strip()] = v.strip()
    except FileNotFoundError:
        print('  tests: no meta/tests_state.txt — nothing to commit')
        return

    # Version alignment check — tests must match the packages being built
    tests_nss = state_info.get('nss', '')
    try:
        pkg_nss = open(nssver_file).read().strip()
    except FileNotFoundError:
        pkg_nss = ''
    if tests_nss and pkg_nss and tests_nss != pkg_nss:
        print(f'WARNING: tests are for NSS {tests_nss} but packages are NSS {pkg_nss} '
              f'— run build_combo.py first to re-generate the tests update',
              file=sys.stderr)
        return

    if state_info.get('state') == 'pushed':
        print(f'  tests: already pushed for NSS {state_info.get("nss", "?")} — skipping')
        return

    if state_info.get('state') != 'staged':
        print(f'  tests: state={state_info.get("state", "unknown")} — skipping')
        return

    try:
        repo = git.Repo(tests_dir)
    except Exception:
        print(f'  tests: {tests_dir} not found — skipping')
        return

    git_ensure_branch(repo)

    staged = repo.index.diff(repo.head.commit)
    if not staged:
        print('  tests: nothing staged to commit')
        # Still mark pushed so we don't re-check every cycle
        _update_tests_state(tests_state_file, state_info, 'pushed')
        return

    nss  = state_info.get('nss', open(nssver_file).read().strip()
                           if os.path.exists(nssver_file) else '')
    ckbi = state_info.get('ckbi', open(ckbiver_file).read().strip()
                           if os.path.exists(ckbiver_file) else '')
    message = f'Update for NSS {nss} / CKBI {ckbi}\n'

    print(f'  tests: committing — {message.strip()}')
    repo.index.commit(message)
    print('  tests: committed')

    if DRY_RUN:
        print('  tests: DRY_RUN — skipping push')
        _update_tests_state(tests_state_file, state_info, 'committed')
        return

    repo.remotes.origin.push()
    print('  tests: pushed')
    _update_tests_state(tests_state_file, state_info, 'pushed')


def _update_tests_state(path, state_info, new_state):
    state_info['state'] = new_state
    with open(path, 'w') as f:
        for k, v in state_info.items():
            f.write(f'{k}={v}\n')


def git_checkin(release, package, bugnumber):
    gitdir=get_git_packages_dir(distro,package,release)
    repo = git.Repo(gitdir)
    index = repo.index
    # first put all the files in 'staged'
    diff = index.diff(None)
    for cfile in diff.iter_change_type('M'):
        print("Adding modified file",cfile.b_path)
        index.add([cfile.b_path])
    for cfile in diff.iter_change_type('A'):
        if cfile != checkin_log :
            print("Adding new file",cfile)
            index.add(cfile.b_path)
    for cfile in diff.iter_change_type('D'):
        print("Adding removed file",cfile.a_path)
        index.remove([cfile.a_path])
    for cfile in diff.iter_change_type('T'):
        print("Adding moved file",cfile.b_path)
        index.add([cfile.b_path])
    # now build the log message.
    f=open("%s/%s"%(gitdir,checkin_log),"r")
    headline = f.readline() + '\n'
    message = f.read()
    f.close()
    if bugnumber != "-1" :
        message = headline + "Resolves: %s\n\n"%bugnumber + message
    #do the checkin
    print("checking in:",gitdir)
    index.commit(message)
    print("checked in:",gitdir)
    return git_repo_state(repo)

def git_push(release, package, bugnumber):
    gitdir=get_git_packages_dir(distro,package,release)
    repo = git.Repo(gitdir)
    print("repo.remotes.origin", repo.remotes.origin)

    # Ensure we're on the correct branch before pushing
    git_ensure_branch(repo)

    if DRY_RUN :
        print("DRY_RUN: git would push to %s"%repo.remotes.origin.url)
        return 'pushed'

    if distro == 'centos':
        # Push current branch explicitly by name to avoid GitLab rejecting HEAD
        local_branch = repo.active_branch.name
        repo.remotes.origin.push(f'HEAD:{local_branch}')
    else:
        # Use the stored branch name (handles rhel-8.10 → rhel-8-main)
        remote_branch = (rhel_packages.get(release, {}).get('branch', '')
                         or fedora_packages.get(release, {}).get('branch', '')
                         or repo.active_branch.name)
        # For releases that share a remote branch (e.g. rhel-8.10 → rhel-8-main),
        # pull first to avoid non-fast-forward rejection from concurrent pushes.
        if remote_branch != repo.active_branch.name:
            try:
                repo.remotes.origin.fetch(remote_branch)
                repo.git.rebase(f'origin/{remote_branch}')
            except Exception as e:
                print(f'  WARNING: rebase from origin/{remote_branch} failed: {e}')
        repo.remotes.origin.push(f'HEAD:{remote_branch}')
    return git_repo_state(repo)

def git_pull(gitdir):
    repo = git.Repo(gitdir)
    repo.remotes.origin.pull()
    return git_repo_state(repo)

#
#    GitLab
#

def gitlab_src_from_fork(project):
    if project is None:
        print("ERROR: GitLab fork project is None — check glab_api_key and centos_fork in config.cfg")
        return None
    if project.forked_from_project:
        source_project_id = project.forked_from_project['id']
        source_project = GLab.projects.get(source_project_id)
        print(f"Source Project: {source_project.web_url}")
        return source_project
    else:
        print("The project is not a fork.")
        return None

def gitlab_create_mr(repo_fork, repo_target, bugnumber, branch='main'):
    arguments = {
        'source_branch': branch,
        'target_branch': branch,
        'target_project_id' : repo_target.id,
        'assignee_id' : GLab.user.id,
        'title': (bug_summary_short % year),
        'description' : ("Resolves: %s\n\n" % bugnumber),
    }

    mr = repo_fork.mergerequests.create(arguments)

    # mr.iid from the fork API is the IID within the FORK project.
    # The upstream project assigns a different IID — extract it from web_url
    # which always points to the upstream (target) project.
    # e.g. https://gitlab.com/redhat/centos-stream/rpms/ca-certificates/-/merge_requests/51
    try:
        upstream_iid = int(mr.web_url.rstrip('/').rsplit('/', 1)[-1])
        upstream_mr = repo_target.mergerequests.get(upstream_iid)
        upstream_mr.merge(merge_when_pipeline_succeeds=True)
        print(f"Set automerge on MR !{upstream_iid}  {mr.web_url}")
    except Exception as e:
        print(f"WARNING: could not set automerge on MR {mr.web_url}: {e}")

    return mr

def gitlab_get_mr(project, iid):
    try:
        mr = project.mergerequests.get(int(iid))
    except (gitlab.exceptions.GitlabGetError, gitlab.exceptions.GitlabParsingError) as e:
        print(f'Error getting merge request !{iid}: {e}')
        return None
    return mr

def gitlab_get_mr_ci_status(mr):
    """Return the status of the latest pipeline on an MR.

    Returns one of:
      'pending'  — pipeline queued or running (wait longer)
      'passed'   — CI succeeded
      'failed'   — CI failed (needs manual intervention)
      'none'     — no pipeline found yet
    """
    if mr is None:
        return 'none'
    try:
        pipelines = mr.pipelines.list(get_all=False, per_page=1)
        if not pipelines:
            return 'none'
        status = pipelines[0].status
        # GitLab pipeline statuses:
        # created, waiting_for_resource, preparing, pending, running,
        # success, failed, canceled, skipped, manual, scheduled
        if status == 'success':
            return 'passed'
        if status in ('failed', 'canceled'):
            return 'failed'
        return 'pending'   # running / created / waiting / etc.
    except Exception as e:
        print(f'WARNING: could not get pipeline status for MR !{mr.iid}: {e}')
        return 'none'

#
#    local utility functions
#
# do all the packages have builds in the nvrlist
def builds_complete(nvrlist, package='ca-certificates') :
    for nvr in nvrlist.split(',') :
        if nvr.startswith(package) :
            return True
    return False

def add_nvr(nvrlist, nvr) :
    if nvr == None or nvr == '' :
       return nvrlist
    if nvrlist == '' :
        return nvr
    nlist=nvrlist.split(',')
    nlist.append(nvr)
    return ','.join(nlist)

# todo use brew rest api?
def rhel_build_nvr_for_centos(release, package):
    """Find the RHEL GA build NVR created by centos stream promotion.

    When a centos MR is merged, Brew creates two builds: one for CentOS Stream
    and one tagged for the RHEL GA release.  This queries 'brew latest-build'
    with the RHEL candidate tag to find the RHEL one.

    release: e.g. 'rhel-9.9.0' or 'rhel-10.3'
    Returns: NVR string or '' if not found yet.
    """
    import re as _re
    m = _re.match(r'^rhel-(\d+)\.(\d+)', release)
    if not m:
        return ''
    major, minor = m.group(1), m.group(2)
    # Brew tag format: rhel-9.9.0-candidate or rhel-10.3-candidate
    if _version_parts(int(major)) == 3:
        tag = f'rhel-{major}.{minor}.0-candidate'
    else:
        tag = f'rhel-{major}.{minor}-candidate'
    out = subprocess.Popen(
        f'brew latest-build {tag} {package}',
        shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        close_fds=True)
    response = out.communicate()[0].decode('utf-8').strip()
    # Output: "Build                                    Tag               Built by"
    #         "ca-certificates-2026.2.90_v9.0.316-90.0.el9_9  rhel-9.9.0-candidate  ..."
    lines = [l for l in response.splitlines() if l.startswith(package)]
    if not lines:
        return ''
    nvr = lines[0].split()[0]
    return nvr



def build_state(nvr) :
    out=subprocess.Popen("%s buildinfo %s"%(build_info_tool[distro],nvr),shell=True, stdin=None,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,close_fds=True)
    brew_response = out.communicate()[0].decode("utf-8").split('\n')
    if len(brew_response) == 0 :
        return 'Nobuilds'
    if brew_response[0].startswith('No such build:') :
        return 'Nobuilds'
    complete=False
    tag=False
    gating=True
    for line in brew_response :
        line = line.strip()
        if line.startswith('State: ') :
            state = line.replace('State: ','')
            if state == 'COMPLETE' :
                complete=True
            elif state == 'BUILDING' :
                return 'Building'
            elif state == 'CANCELED' :
                return 'Nobuilds'
            elif state == 'FAILED' :
                return 'Failed'
            else :
                return 'Nobuilds'
        if line.startswith('Tags: ') :
            tag=True
            if distro == 'fedora' or line.find('-candidate') != -1 :
                gating=False
        if complete and tag :
            if gating :
                return 'Gating'
            return 'Complete'
    return 'NoBuilds'

#
# merge the different states from 2 different builds
# we return the state that is least further along
# than the other states.
def merge_state(state, state2) :
    # first, states of Complete or None have lowest priority
    if state == None or state == 'Complete':
        return state2
    if state2 == None or state2 == 'Complete':
        return state
    # if they are equal, return them
    if state == state2 :
        return state
    # 'Failed' has the highest precedence
    if state == 'Failed' or state2 == 'Failed' :
        return 'Failed'
    # 'Nobuilds' is next
    if state == 'Nobuilds' or state2 == 'Nobuilds' :
        return 'Nobuilds'
    # now we know that 1) state != state2, and neither
    # is equal to None, 'Complete', 'Failed', or 'Nobuilds'
    # One must be 'Gating' and the other 'Building', 'Building'
    # has precedence
    return 'Building'

def build_nvr(release,package):
    packagedir=get_git_packages_dir(distro,package,release)
    if not os.path.exists(packagedir):
        return None
    if not release in build_nvr.cache :
        build_nvr.cache[release]= {}
    if package in build_nvr.cache[release] :
        return build_nvr.cache[release][package]

    pushd=os.getcwd()
    os.chdir(packagedir)

    stream=os.popen("%s verrel"%package_tool[distro])
    nvr = stream.read().strip()
    os.chdir(pushd)
    if nvr:   # don't cache empty results — retry next cycle if verrel failed
        build_nvr.cache[release][package]=nvr
    return nvr
build_nvr.cache = {}

def build_status(release,package):
    nvr = build_nvr(release,package)
    return build_state(nvr)

# todo use brew rest api?
def build_get_info(release, package) :
    nvr = build_nvr(release, package)
    state = build_state(nvr)

    out=subprocess.Popen("%s buildinfo %s"%(build_info_tool[distro],nvr),shell=True, stdin=None,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,close_fds=True)
    brew_response = out.communicate()[0].decode("utf-8").split('\n')
    if len(brew_response) == 0 :
        return ( '', nvr, state )
    if brew_response[0].startswith('No such build:') :
        return ( '', nvr, state )
    for line in brew_response :
        line = line.strip()
        if line.startswith('Task: ') :
            components=line.split()
            return (components[1], nvr, state)
    return ( '', nvr, state )

# todo use brew rest api?
def build(release,package):
    nvr = build_nvr(release,package)
    if nvr == None :
        print("buildir doesn't exist");
        return ''
    state = build_state(nvr)
    if state in ('Complete', 'Building', 'Gating') :
        return nvr   # NVR is known — keep it regardless of build progress
    # Nobuilds or Failed — submit the build
    packagedir=get_build_packages_dir(distro,package,release)

    pushd=os.getcwd()
    os.chdir(packagedir)
    os.system("%s build --nowait"%package_tool[distro])
    os.chdir(pushd)
    return nvr   # return NVR even if not yet Complete; next cycle checks state


#######################################################
#
# argument parsing and configuration initialization
#
#######################################################
_USAGE = """\
process.py — Advance the ca-certificates release lifecycle.

Reads meta/rhel.list and meta/fedora.list (written by plan.py) and
drives each release through: git checkin → push → (CentOS Stream MR) →
Brew/Koji build → Errata advisory creation and attachment.

The script is idempotent: re-run it as each step completes.
Use --loop to run continuously until all releases reach 'complete'.

Usage:
  ./process.py [options]

Options:
  -r rhel.list      Override default meta/rhel.list path
  -o email          Package owner e-mail (overrides config.cfg)
  -m email          Manager e-mail (overrides config.cfg)
  -q email          QE contact e-mail (overrides config.cfg)
  -v version        CKBI version string (overrides meta/ckbiversion.txt)
  -f firefox        Firefox version string (overrides meta/firefox_info.txt)
  -y year           Override year (default: current year)
  -e url            Errata Tool base URL
  -j url            Jira base URL
  -l url            GitLab base URL
  --resync          Force refresh of the errata product-version cache
  --get-ga          Print current GA releases and exit
  --getconfig key   Print a config.cfg value and exit
  --dry-run         Skip git push; log other actions normally
  --loop            Re-run every --interval seconds until all complete
  --interval N      Loop sleep interval in seconds (default: 300)
  --human           Rich TUI output with live status table

Required tools: git, rhpkg (RHEL builds), fedpkg (Fedora builds),
centpkg (CentOS Stream builds), brew (RHEL build status),
koji (Fedora/CentOS build status), kinit (Kerberos ticket).
"""

if '--help' in sys.argv or '-h' in sys.argv:
    print(_USAGE)
    sys.exit(0)

try:
    opts, args = getopt.getopt(sys.argv[1:],"r:o:m:q:v:f:y:e:j:l:",["resync","get-ga","getconfig=","dry-run","loop","interval=","human"])
except getopt.GetoptError as err:
    print(err)
    print('Run with --help for usage information.')
    sys.exit(2)

resync=False
get_ga=False
human=False
loop_mode=False
loop_interval=300   # seconds between passes in loop mode (default 5 min)
try:
    f = open(ckbiver_file, "r")
    version=f.read().strip()
    f.close()
except :
    version=None

try:
    f = open(nssver_file, "r")
    nss_version=f.read().strip()
    f.close()
except :
    nss_version=None

try:
    f = open(mcsver_file, "r")
    mcs_version=f.read().strip()
    f.close()
except :
    mcs_version=None

try:
    f = open(firefox_info, "r")
    firefox_version=f.read().strip()
    f.close()
except :
    firefox_version=None

year=datetime.date.today().strftime("%Y")

for config_line in open(config_file, 'r'):
    if config_line[0] == '#': continue
    ( key, value) = config_line.strip().split(':',1)
    config[key]=value.strip()
    if key == 'manager':
       manager = value.strip()
    if key == 'owner':
       owner = value.strip()
    if key == 'qe':
       qe = value.strip()
    if key == 'se_contact':
       se_contact = value.strip()
    if key == 'version':
       version = value.strip()
    if key == 'firefox':
       firefox_version = value.strip()
    if key == 'errata_url':
       errata_url_base = value.strip()
    if key == 'centos_fork':
       centos_fork = value.strip()
    if key == 'jira_url':
       jira_url_base = value.strip()
    if key == 'jira_api_key':
       jira_api_key = value.strip()
    if key == 'jira_user':
       jira_user = value.strip()
    if key == 'glab_url_base':
       glab_url_base = value.strip()
    if key == 'glab_api_key':
       glab_api_key = value.strip()
    if key == 'dry_run':
       DRY_RUN = True if value.strip().lower() == 'true' else False

for opt, arg in opts:
    if opt == '-r' :
        rhel_list = arg
    elif opt == '-o' :
        owner = arg
    elif opt == '-m' :
        manager = arg
    elif opt == '-v' :
        version = arg
    elif opt == '-f' :
        firefox_version = arg
    elif opt == '-y' :
        year = arg
    elif opt == '-e' :
        errata_url_base = arg
    elif opt == '-j' :
        jira_url_base = arg
    elif opt == '-l' :
        glab_url_base = arg
    elif opt == '--resync' :
        resync = True
    elif opt == '--get-ga' :
        get_ga = True
    elif opt == '--dry-run':
        DRY_RUN = True
    elif opt == '--loop':
        loop_mode = True
    elif opt == '--interval':
        loop_interval = int(arg)
    elif opt == '--human':
        human = True
    elif opt == '--getconfig' :
        if not arg in config:
            print("No arg found");
            sys.exit(3)
        else:
            print(config[arg]);
        sys.exit(0)

check_prereqs(['git', 'kerberos', 'rhpkg', 'fedpkg', 'centpkg', 'brew', 'koji'],
              'process.py')

if jira_api_key is not None:
    Jira = make_jira_client(jira_url_base, jira_api_key, jira_user=jira_user)

if glab_api_key != None:
    try:
        GLab = gitlab.Gitlab(url=glab_url_base, private_token=glab_api_key)
        GLab.auth()
    except gitlab.exceptions.GitlabError as e:
        print(e);
        exit(1)
else:
    print("No GitLab api provided")
    exit(1)

if GLab != None and centos_fork != None:
    import re as _re
    _m = _re.match(r'^git@[^:]+:(.+?)(?:\.git)?$', centos_fork)
    if not _m:
        _m = _re.match(r'^https?://[^/]+/(.+?)(?:\.git)?$', centos_fork)
    _fork_path = _m.group(1) if _m else centos_fork.replace(glab_url_base, '')
    CentOSFork = GLab.projects.get(_fork_path)
    if(CentOSFork is None):
        print(f'WARNING: could not load GitLab fork project {centos_fork!r}: {e}')
        exit(1)

errata_map, ga_list = load_errata_map(
    errata_url_base, errata_cache_file, ca_certs_file, force_resync=resync)

if get_ga :
    for i in ga_list :
        print(i,end=' ')
    print('')
    sys.exit(0)

if firefox_version == None :
    print("No firefox_info file ("+firefox_info+") be sure to include -f option to specify the related firefox version on first call")
    sys.exit(2)

if not os.path.exists(firefox_info) :
    f = open(firefox_info, "w")
    f.write(firefox_version)
    f.close()

rhel_packages = {}
fedora_packages = {}
_out = PipelineOutput(human=human, title='process.py', mode='process')

#######################################################
#
# read in our status files
#
#######################################################
for rhel_entry in open(rhel_list, 'r'):
    fields = rhel_entry.strip().split(':')
    if not fields or not fields[0]:
        continue
    (release, branch, bugnumber, erratanumber, nvr, state, glmr, glupstream) = fields[:8]
    entry = dict()
    entry['branch']      = branch
    entry['bugnumber']   = bugnumber
    entry['erratanumber']= int(erratanumber)
    entry['nvr']         = nvr
    entry['state']       = state
    entry['glmr']        = glmr
    entry['glupstream']  = glupstream
    entry['crypto']      = fields[8] if len(fields) > 8 else ''
    entry['crypto_dev']  = fields[9] if len(fields) > 9 else ''
    rhel_packages[release] = entry
    print(f'  {release}: state={state} bug={bugnumber}')

for fedora_entry in open(fedora_list, 'r'):
    line = fedora_entry.strip()
    if not line:
        continue
    (release, bugnumber, erratanumber, nvr, state) = line.split(':')
    entry = dict()
    entry['bugnumber']   = bugnumber
    entry['erratanumber']= int(erratanumber)
    entry['nvr']         = nvr
    entry['state']       = state
    fedora_packages[release] = entry
    print(f'  {release}: state={state}')

#######################################################
#
# logic to try to advance the release to the next possible
# level.
#
#######################################################
CENTOS_TERMINAL = {'complete'}

# States where the GA Brew build exists and is done — z-streams can start
# once all centos releases reach at least this point.
CENTOS_BUILDS_DONE = {
    'builds complete', 'needs errata', 'need builds attached',
    'needs bugs attached', 'errata ready QE',
    'complete', 'centos ci failed',
}
package = 'ca-certificates'

def _log(release, msg):
    line = f'[{release}] {msg}'
    if human:
        _out.log(line)
    else:
        print(line)


def _advance_crypto_dev(entry, release, target_state):
    """Transition the [DEV] CRYPTO child issue to target_state if not already there.

    Valid states: 'Backlog' → 'In Progress' → 'Closed'
    Uses issue_change_state which calls the Jira transition API.
    """
    key = entry.get('crypto_dev', '')
    if not key or not Jira:
        return
    try:
        current = issue_get_state({'key': key,
                                   'fields': {'status': {'name': ''}}})
        # Re-fetch to get actual state
        raw = Jira.get(key) if hasattr(Jira, 'get') else None
        if raw:
            current = raw.get('fields', {}).get('status', {}).get('name', '')
        if current == target_state:
            return
        issue_change_state(Jira, key, target_state)
        _log(release, f'[DEV] {key}: {current} → {target_state}')
    except Exception as e:
        _log(release, f'WARNING: could not advance {key} to {target_state}: {e}')


def _process_release(release, entry, distro_name):
    global distro
    distro = distro_name
    _planned_as_centos = (distro_name == 'centos')   # set by plan.py / Pass 1, never changes
    _log(release, f'processing  state={entry["state"]}  distro={distro_name}')
    if entry['state'] == 'complete':
        _log(release, 'already complete — skipping')
        return

    bugnumber = entry['bugnumber']
    issue = None
    centosUpstream = None
    glmr = entry['glmr']

    if bugnumber == '0':
        _log(release, 'no bug number — run plan.py first')
        entry['state'] = 'need bug'
        return
    _log(release, f'bug={bugnumber}')
    issue = _issue_get(bugnumber)

    git_state = 'pushed'   # default; overwritten by the git block below if reached

    # For centos releases with an MR already open/merged, skip all git operations.
    # Once glmr is set the fork push has already happened; we only need to check
    # MR state and (if merged) find the RHEL GA build for errata.
    # If NVR is already known the MR is long merged — skip straight to errata.
    if _planned_as_centos and builds_complete(entry['nvr']):
        _log(release, 'centos build already found — switching to rhel errata path')
        distro_name = 'rhel'
        distro = 'rhel'
        all_builds_pushed = True

    elif distro_name == 'centos' and glmr:
        _log(release, f'centos MR exists ({glmr}) — checking MR state before git ops')
        centosUpstream = gitlab_src_from_fork(CentOSFork)
        if centosUpstream is None:
            return
        entry['glupstream'] = centosUpstream.id
        mr = gitlab_get_mr(centosUpstream, glmr)
        if mr is None:
            _log(release, 'MR not found — skipping')
            return
        if mr.state == 'merged':
            _log(release, f'MR !{glmr} merged — switching to rhel errata path')
            if not builds_complete(entry['nvr']):
                rhel_nvr = rhel_build_nvr_for_centos(release, package)
                if rhel_nvr:
                    entry['nvr'] = add_nvr(entry['nvr'], rhel_nvr)
                    _log(release, f'RHEL GA build nvr={rhel_nvr}')
                else:
                    _log(release, 'RHEL GA build not yet visible in Brew — will retry')
                    return
            else:
                _log(release, f'nvr already known: {entry["nvr"]}')
            distro_name = 'rhel'
            distro = 'rhel'
            all_builds_pushed = True
        else:
            ci_status = gitlab_get_mr_ci_status(mr)
            _log(release, f'MR !{mr.iid}: state={mr.state}  CI={ci_status}  {mr.web_url}')
            entry['state'] = {
                'failed': 'centos ci failed',
                'passed': 'waiting centos merge',
            }.get(ci_status, 'waiting centos ci')
            return

    else:
        all_builds_pushed = True
        _log(release, 'checking git state')
        git_state = git_get_state(release, package, bugnumber)
        if git_state is None:
            _log(release, 'git repo not found — skipping')
            return
        _log(release, f'git={git_state}')
        if git_state == 'staged':
            _log(release, 'checking in')
            git_state = git_checkin(release, package, bugnumber)
            _log(release, f'git={git_state}')
        if git_state == 'committed':
            _log(release, 'pushing')
            git_state = git_push(release, package, bugnumber)
            _log(release, f'git={git_state}')
        if git_state != 'pushed':
            all_builds_pushed = False
    if git_state == 'pushed' and not builds_complete(entry['nvr']):
        if distro_name == 'centos':
            # No existing MR yet — create one (first push case)
            if centosUpstream is None:
                centosUpstream = gitlab_src_from_fork(CentOSFork)
                if centosUpstream is None:
                    return
                entry['glupstream'] = centosUpstream.id
            cb = _centos_branch(safe_int(release_get_major(release)))
            _log(release, f'creating MR for branch {cb}')
            mr = gitlab_create_mr(CentOSFork, centosUpstream,
                                  bugnumber, branch=cb)
            upstream_iid = int(mr.web_url.rstrip('/').rsplit('/', 1)[-1])
            entry['glmr'] = upstream_iid
            entry['state'] = 'waiting centos ci'
            _log(release, f'MR !{upstream_iid} created  {mr.web_url}')
            return
        _log(release, 'triggering build')
        nvr = build(release, package)
        entry['nvr'] = add_nvr(entry['nvr'], nvr)
        _log(release, f'nvr={entry["nvr"]}')

    builds = entry['nvr']
    erratanumber = entry['erratanumber']
    # NVR presence means build was submitted; Complete means it's done in Brew
    _brew_state = build_state(builds) if builds_complete(builds) else 'Nobuilds'
    all_builds_complete = (_brew_state == 'Complete')
    if not all_builds_pushed:
        if distro_name == 'centos':
            # Push to the fork failed — changes are committed locally but not yet
            # on the remote fork.  Leave state as 'committed' so next cycle retries.
            _log(release, 'push to centos-fork failed — changes committed locally, will retry')
            return
        entry['state'] = 'builds need push'
    elif not all_builds_complete:
        entry['state'] = {
            'Nobuilds': 'builds not started',
            'Failed':   'builds failed',
            'Building': 'builds in progress',
            'Gating':   'builds in gating',
            'Complete': 'builds complete, state error',
        }.get(_brew_state, 'builds in an unknown state')
    elif erratanumber == 0:
        entry['state'] = 'needs errata'
    else:
        entry['state'] = 'builds complete'
    _log(release, f'build state → {entry["state"]}')

    _log(release, 'handling errata')
    bug_state = _issue_get_state(issue)
    _log(release, f'bug state={bug_state}')
    bug_resync = False
    if all_builds_pushed:
        bug_resync = True
        _set_qa_contact(bugnumber, release)
        _advance_crypto_dev(entry, release, 'In Progress')
        # Update summary/description with the actual build versions — these may
        # differ from what plan.py used when it created the issue weeks earlier.
        _is_zstream = not _planned_as_centos  # GA (centos) releases don't get .z suffix
        issue_update_versions(
            Jira, bugnumber,
            version=version, nss_version=nss_version,
            firefox_version=firefox_version, mcs_version=mcs_version,
            release=release, zstream=_is_zstream, year=year)
    if not all_builds_complete:
        return

    _force_ga = _planned_as_centos
    if erratanumber == 0:
        erratanumber = errata_lookup(release, version, firefox_version, package,
                                     force_ga=_force_ga)
    # errata_create disabled — errata are now auto-created by the Errata Tool
    # if erratanumber == 0:
    #     _log(release, f'no existing errata found — creating one (force_ga={_force_ga})')
    #     erratanumber = errata_create(release, version, firefox_version,
    #                                  package, year, bugnumber,
    #                                  force_ga=_force_ga)
    if erratanumber == 0:
        _log(release, 'no errata yet — will retry next cycle')
        return

    _log(release, f'errata={erratanumber}')
    entry['erratanumber'] = erratanumber

    # 1. QA contact on errata
    _set_errata_qa(erratanumber, release)

    # 2. Attach RHEL Jira bug if not already present
    if bug_state == 'IN PROGRESS' and not errata_has_bug(erratanumber, bugnumber):
        _log(release, f'attaching bug {bugnumber} to errata {erratanumber}')
        errata_add_bug(erratanumber, bugnumber, bug_resync)

    # 3. Attach builds if not already present
    if all_builds_complete and not errata_has_builds(erratanumber, release, builds,
                                                      force_ga=_force_ga):
        errata_state = errata_get_state(erratanumber)
        _log(release, f'attaching builds to errata {erratanumber}  (errata state={errata_state})')
        if errata_state == 'QE':
            errata_state = errata_set_state(erratanumber, 'NEW_FILES')
            _log(release, f'errata → {errata_state}')
        errata_add_builds(erratanumber, release, builds, force_ga=_force_ga)

    # 4. Check all three conditions: build attached, bug attached, QA set
    _build_ok = all_builds_complete and errata_has_builds(erratanumber, release, builds,
                                                          force_ga=_force_ga)
    _bug_ok   = errata_has_bug(erratanumber, bugnumber)
    if not all_builds_complete:
        entry['state'] = 'need builds attached'
    elif not _build_ok:
        entry['state'] = 'need builds attached'
    elif not _bug_ok:
        entry['state'] = 'needs bugs attached'
    else:
        entry['state'] = 'errata ready QE'
        rpm_state = errata_get_rpm_state(erratanumber, entry['nvr'])
        _log(release, f'rpm diff state={rpm_state}')
        if rpm_state in ('PASSED', 'WAIVED', 'INFO'):
            _set_preliminary_testing_requested(bugnumber, release)
            _advance_crypto_dev(entry, release, 'Closed')
            entry['state'] = 'complete'
    _log(release, f'→ {entry["state"]}')

def _save_state():
    """Write current in-memory state to rhel.list and fedora.list immediately."""
    with open(rhel_list, 'w') as f:
        for r, e in rhel_packages.items():
            f.write('%s:%s:%s:%d:%s:%s:%s:%s:%s:%s\n' % (
                r, e.get('branch', ''),
                e['bugnumber'], e['erratanumber'],
                e['nvr'], e['state'],
                e['glmr'], e['glupstream'],
                e.get('crypto', ''), e.get('crypto_dev', '')))
    with open(fedora_list, 'w') as f:
        for r, e in fedora_packages.items():
            f.write('%s:%s:%d:%s:%s\n' % (
                r, e['bugnumber'], e['erratanumber'],
                e['nvr'], e['state']))


def _print_status():
    """Log final status for each release (progress bars remain visible in TUI)."""
    for r, e in rhel_packages.items():
        errata_str = str(e['erratanumber']) if e['erratanumber'] != 0 else '–'
        _out.log(f"  {r}: state='{e['state']}' bug={e['bugnumber']} errata={errata_str}")
        if e['bugnumber'] not in ('0', ''):
            _out.log(f"    {jira_url_base}/show_bug.cgi?id={e['bugnumber']}")
        if e['erratanumber']:
            _out.log(f"    {errata_url_base}/advisory/{e['erratanumber']}")
        build_states = {'builds in progress', 'builds in gating', 'builds not started'}
        if e['nvr'] and e['state'] in build_states:
            try:
                task, nvr_b, state_b = build_get_info(r, package)
                if task:
                    _out.log(f"    {brew_url_base}/taskinfo?taskID={task} ({nvr_b},{state_b})")
            except Exception as ex:
                _out.log(f"    [build info unavailable: {ex}]")
    for r, e in fedora_packages.items():
        _out.log(f"  {r}: state='{e['state']}'")


def _run_release(release, entry, distro_name_or_fn):
    """Run a release processor with error isolation.

    distro_name_or_fn: a distro string ('rhel'/'centos') for RHEL releases,
    or a callable(release, entry) for Fedora.
    Failures are logged but never propagate; state is saved after every attempt.
    """
    try:
        if callable(distro_name_or_fn):
            distro_name_or_fn(release, entry)
        else:
            _process_release(release, entry, distro_name_or_fn)
    except Exception as e:
        import traceback
        print(f'\n[{release}] UNHANDLED ERROR: {e}', file=sys.stderr)
        traceback.print_exc()
    finally:
        _save_state()
        # Update the progress bar for this release
        _out.update_release(release, entry)


def _process_fedora_release(release, entry):
    """Advance a single Fedora release: git checkin → push → build."""
    print(f'\n[{release}] Processing Fedora release')
    if entry['state'] == 'complete':
        print(f'[{release}] already complete')
        return
    all_builds_pushed = True
    print(f'[{release}] checking git tree status')
    git_state = git_get_state(release, package, '-1')
    print(f'[{release}] git state: {git_state}')
    if git_state == 'staged':
        git_state = git_checkin(release, package, '-1')
    if git_state == 'committed':
        print(f'[{release}] pushing')
        git_state = git_push(release, package, '-1')
    if git_state != 'pushed':
        all_builds_pushed = False
    if git_state == 'pushed' and not builds_complete(entry['nvr']):
        nvr = build(release, package)
        entry['nvr'] = add_nvr(entry['nvr'], nvr)
    all_builds_complete = builds_complete(entry['nvr'])
    if not all_builds_pushed:
        entry['state'] = 'builds need push'
    elif not all_builds_complete:
        state = build_status(release, package)
        entry['state'] = {
            'Nobuilds': 'builds not started',
            'Failed':   'builds failed',
            'Building': 'builds in progress',
            'Complete': 'builds complete, state error',
        }.get(state, 'builds in an unknown state')
    else:
        entry['state'] = 'complete'
    print(f'[{release}] → {entry["state"]}')


# Initialise TUI progress bars with current state of all releases
_out.initialize_releases(rhel_packages)

with _out:
 try:
    # ── Commit and push tests repo before any RHEL builds ────────────────────
    _out.log("=== Tests repo ===")
    commit_and_push_tests()

    # ── Pass 1: CentOS-stream (GA) releases ──────────────────────────────────
    print("\n=== Pass 1: CentOS-stream releases ===")
    for release in rhel_packages:
        if not _release_is_centos_stream(release):
            continue
        _run_release(release, rhel_packages[release], 'centos')

    # Build a per-major map of centos-stream settled state:
    # c8s only gates rhel-8.x z-streams, c9s gates rhel-9.x, etc.
    _centos_settled_by_major = {}
    for r in rhel_packages:
        if not _release_is_centos_stream(r):
            continue
        major = safe_int(release_get_major(r))
        done = rhel_packages[r]['state'] in CENTOS_BUILDS_DONE
        _centos_settled_by_major[major] = (
            _centos_settled_by_major.get(major, True) and done)

    # ── Pass 2: RHEL z-stream releases ───────────────────────────────────────
    _out.log("=== Pass 2: RHEL z-stream releases ===")
    for release in rhel_packages:
        if _release_is_centos_stream(release):
            continue
        major = safe_int(release_get_major(release))
        if not _centos_settled_by_major.get(major, True):
            _out.log(f"  [{release}] waiting for centos major {major} build — skipping")
            continue
        _run_release(release, rhel_packages[release], 'rhel')

    # ── Fedora releases ───────────────────────────────────────────────────────
    _out.log("=== Fedora releases ===")
    for release in fedora_packages:
        _run_release(release, fedora_packages[release],
                     _process_fedora_release)

 except (EOFError, KeyboardInterrupt) as _sig:
    _why = 'Ctrl+D' if isinstance(_sig, EOFError) else 'Ctrl+C'
    _out.log(f'{_why} received — saving state and exiting')
    _save_state()
    _print_status()
    sys.exit(0)

#######################################################
#
# Loop mode: re-exec if not all releases are terminal
#
#######################################################
if loop_mode:
    import time as _time

    _TERMINAL_STATES = {'complete', 'centos ci failed'}
    _all_rhel_done   = all(e['state'] in _TERMINAL_STATES
                           for e in rhel_packages.values())
    _all_fedora_done = all(e['state'] == 'complete'
                           for e in fedora_packages.values())

    if _all_rhel_done and _all_fedora_done:
        _out.log("All releases complete — exiting loop.")
    else:
        _pending = [r for r, e in rhel_packages.items()
                    if e['state'] not in _TERMINAL_STATES]
        _pending += [r for r, e in fedora_packages.items()
                     if e['state'] != 'complete']
        _out.log(f"Loop: {len(_pending)} pending — {', '.join(_pending)}")
        try:
            for _remaining in range(loop_interval, 0, -1):
                _out.set_subtitle(f"next pass in {_remaining}s")
                _time.sleep(1)
        except (KeyboardInterrupt, EOFError):
            _out.set_subtitle("")
            _out.log("Loop interrupted — exiting.")
            sys.exit(0)
        _out.set_subtitle("")
        os.execv(sys.argv[0], sys.argv)
