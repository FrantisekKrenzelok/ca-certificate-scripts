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
from caupdate.release import (
    release_get_major, safe_int,
    get_need_zstream_clone,
    release_is_centos_stream,
    errata_get_release_info,
    load_errata_map, CA_CERTS_FILE,
)
from caupdate.issues import (
    issue_create, issue_lookup, issue_request_clone,
    issue_get_state, issue_change_state, issue_get,
    make_jira_client,
    JIRA_PROJ, JIRA_ISSUE_TYPE,
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
    "rhel":"./packages/",
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

# handle package location differences for rhel9 centos stream
def get_git_packages_dir(distro,package,release) :
    if distro == 'centos' :
        return packages_dir[distro]+"-fork/%s"%package;
    return packages_dir[distro]+"%s/%s"%(package,release)

def get_build_packages_dir(distro,package,release) :
    if distro == 'centos' :
        return packages_dir[distro]+"/%s"%package;
    major = safe_int(release_get_major(release))
    if major > 9:
        return packages_dir[distro]+"%s/rhel%s-main"%(package,major)
    return packages_dir[distro]+"%s/%s"%(package,release)
# Release helpers and errata-map functions are in caupdate.release.
def _get_need_zstream_clone(release):
    return get_need_zstream_clone(release, ga_list)

def _release_is_centos_stream(release):
    return release_is_centos_stream(release, ga_list)


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

# Wrappers for issue functions to inject the year global.
def _issue_create(release, version, nss_version, firefox_version, mcs_version, packages, zstream):
    return issue_create(Jira, release, version, nss_version, firefox_version,
                        mcs_version, packages, zstream, year)

def _issue_lookup(release, version, packages, zstream=False):
    return issue_lookup(Jira, release, version, packages, year, zstream)

def _issue_request_clone(issue):
    return issue_request_clone(Jira, issue)

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
def errata_create(release, version, firefox_version, packages, year, bugnumber) :
    release_name=release_map(release)
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
    errata['release']=release_name
    errata['release_id']=release_get_release_id(release)
    errata['advisory']=advisory
    print("----------Creating errata for "+release.strip())
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

def errata_lookup(release, version, firefox_version, packages) :
    headers= { 'Content-type':'application/json', 'Accept':'application/json' }
    packages_list=packages.split(',')
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
    if len(r.json()) == 0 :
        return []
    errata=r.json()
    if not 'bugs' in errata :
        return []
    bug_list=errata['bugs']['bugs']
    bugs = []
    for bug in bug_list:
        bugs.append(bug['bug']['id'])
    return bugs

# return the nvr of the attached builds
def errata_get_builds(errata, release) :
    headers= { 'Content-type':'application/json', 'Accept':'application/json' }
    url=errata_url_base+"/api/v1/erratum/%d/builds"%errata
    r = requests.get(url, headers=headers,
                     auth=HTTPKerberosAuth(),
                     verify=ca_certs_file)
    if r.status_code >  299 :
        print('errata get builds status=%d'%r.status_code)
        print('text=',r.text)
        return []
    if len(r.json()) == 0 :
        return []
    builds = []
    for builditem in r.json()[release_map(release)]['builds'] :
        builds +=  list(builditem.keys())
    return builds

def errata_has_bug(errata, bug) :
    # errata of -1 means this distro doesn't use errata
    if errata == -1 :
        return True
    bugs = errata_get_bugs(errata)
    for this_bug in bugs :
        if str(bug) == str(this_bug) :
            return True
    return False

# return True if errata has all the builds attached
def errata_has_builds(errata, release, builds) :
    # errata of -1 means this distro doesn't use errata
    if errata == -1 :
        return True
    nvrlist = errata_get_builds(errata, release)
    for build in builds.split(',') :
        if not build in nvrlist :
            return False
    return True

def errata_resync_bug(errata, bug) :
    # errata of -1 means this distro doesn't use errata
    if errata == -1 :
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
    # errata of -1 means this distro doesn't use errata
    if errata == -1 :
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
def errata_add_builds(errata, release, builds) :
    # errata of -1 means this distro doesn't use errata
    if errata == -1 :
        return
    nvr = errata_get_builds(errata, release)
    request= []
    # only add builds we haven't successfully added yet
    for build in builds.split(',') :
        if not build in nvr :
            entry = dict()
            entry['product_version']=release_map(release)
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

def errata_set_state(erratanumber,newstate) :
    # errata of -1 means this distro doesn't use errata
    if erratanumber == -1 :
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

def git_repo_state(repo):
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
    return git_repo_state(repo)

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

    if DRY_RUN :
        print("DRY_RUN: git would push to %s"%repo.remotes.origin.url)
        return 'pushed'

    if distro == 'centos' :
        repo.remotes.origin.push("HEAD")
    else :
        repo.remotes.origin.push()
    return git_repo_state(repo)

def git_pull(gitdir):
    repo = git.Repo(gitdir)
    repo.remotes.origin.pull()
    return git_repo_state(repo)

#
#    GitLab
#

def gitlab_src_from_fork(project):
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

    try:
        mr.merge(merge_when_pipeline_succeeds=True)
        print(f"Set automerge on MR !{mr.iid}")
    except Exception as e:
        print(f"WARNING: could not set automerge on MR !{mr.iid}: {e}")

    return mr

def gitlab_get_mr(project, id):
    try:
        mr = project.mergerequests.get(id)
    except gitlab.exceptions.GitlabGetError as e:
        print(f'Error getting merge request: {e}')
        return None

    return mr

#
#    local utility functions
#
# do all the packages have builds in the nvrlist
def builds_complete(nvrlist,packages) :
    for package in packages.split(',') :
        found=False
        for nvr in nvrlist.split(',') :
            if nvr.startswith(package) :
                found=True
                break
        if not found :
            return False
    return True

def add_nvr(nvrlist, nvr) :
    if nvr == None or nvr == '' :
       return nvrlist
    if nvrlist == '' :
        return nvr
    nlist=nvrlist.split(',')
    nlist.append(nvr)
    return ','.join(nlist)

# todo use brew rest api?
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
    if state == 'Complete' :
        return nvr
    if state == 'Building' or state == 'Gating' :
        return ''
    # for failed, or nobuilds, time to make builds
    packagedir=get_build_packages_dir(distro,package,release)

    pushd=os.getcwd()
    os.chdir(packagedir)
    os.system("%s build --nowait"%package_tool[distro])
    os.chdir(pushd)
    state = build_state(nvr)
    if state == 'Complete' :
        return nvr
    # anything else, indicate the builds are not yet done
    return ''


#######################################################
#
# argument parsing and configuration initialization
#
#######################################################
try:
    opts, args = getopt.getopt(sys.argv[1:],"r:o:m:q:v:f:y:e:j:l:",["resync","get-ga","getconfig=","dry-run"])
except getopt.GetoptError as err:
    print(err)
    print(sys.argv[0] + ' [-r rhel.list] [-o owner.email] [-m manager.email] [-q qa.email] [-v ckbi.version] [-f firefox.version] [-y year] [-e errataurlbase] [-j jiraaurlbase]')
    sys.exit(2)

resync=False
get_ga=False
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
    ( key, value) = config_line.strip().split(':',1)
    config[key]=value.strip()
    if key == 'manager':
       manager = value.strip()
    if key == 'owner':
       owner = value.strip()
    if key == 'qe':
       qe = value.strip()
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
    if key == 'gitlab_url':
       glab_url_base = value.strip()
    if key == 'gitlab_api_key':
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
    elif opt == '--getconfig' :
        if not arg in config:
            print("No arg found");
            sys.exit(3)
        else:
            print(config[arg]);
        sys.exit(0)

if jira_api_key is not None:
    Jira = make_jira_client(jira_url_base, jira_api_key, jira_user=jira_user)

if glab_api_key != None:
    try:
        GLab = gitlab.Gitlab(url=glab_url_base, private_token=glab_api_key)
        GLab.auth()
    except gitlab.exceptions.GitlabError as e:
        print(e);

if GLab != None and centos_fork != None:
    CentOSFork = GLab.projects.get(centos_fork.replace(glab_url_base, ""))

errata_map, ga_list, _ = load_errata_map(
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

#######################################################
#
# read in our status files
#
#######################################################
for rhel_entry in open(rhel_list, 'r'):
    fields = rhel_entry.strip().split(':')
    (release, packages, bugnumber, erratanumber, nvr, state, glmr, glupstream) = fields[:8]
    entry=dict()
    print('release=',release,'packages=',packages,'bugnumber=',bugnumber,'erratanumber=',erratanumber,'nvr=',nvr,'state=',state)
    entry['packages']=packages
    entry['bugnumber']=bugnumber
    entry['erratanumber']=int(erratanumber)
    entry['nvr']=nvr
    entry['state']=state
    entry['glmr']=glmr
    entry['glupstream']=glupstream
    entry['crypto']=fields[8] if len(fields) > 8 else ''
    rhel_packages[release]=entry

for fedora_entry in open(fedora_list, 'r'):
    (release, packages, bugnumber, erratanumber, nvr, state) = fedora_entry.strip().split(':')
    entry=dict()
    print('release=',release,'packages=',packages,'bugnumber=',bugnumber,'erratanumber=',erratanumber,'nvr=',nvr,'state=',state)
    entry['packages']=packages
    entry['bugnumber']=bugnumber
    entry['erratanumber']=int(erratanumber)
    entry['nvr']=nvr
    entry['state']=state
    fedora_packages[release]=entry

#######################################################
#
# logic to try to advance the release to the next possible
# level.
#
#######################################################
distro='rhel'
for release in rhel_packages:
    if _release_is_centos_stream(release) :
        distro='centos'
    else :
        distro='rhel'
    entry=rhel_packages[release]
    print("Processing release <%s>:"%release)
    if entry['state'] == 'complete' :
        print("  * complete!")
        continue
    bugnumber=entry['bugnumber']
    packages=entry['packages']
    issue = None
    centosUpstream = None
    glmr = entry['glmr']
    glupstream = entry['glupstream']

    print("  * handling bugs")
    if bugnumber == "0" :
        # we need bug numbers so that we can commit our changes
        if _get_need_zstream_clone(release) :
            # lookup cloned bug number
            bugnumber,issue=_issue_lookup(release,version,packages,zstream=True)
            if bugnumber == "0" :
                print("    * waiting for clone")
                entry['state']='waiting bug clone'
                continue
            entry['bugnumber']=bugnumber
        else :
            # first lookup the bug to see if it has already been created
            bugnumber,issue=_issue_lookup(release,version,packages)
            if bugnumber == "0":
                # nope, create it now
                bugnumber,issue=_issue_create(release,version,nss_version,firefox_version,mcs_version,packages, False)

                if bugnumber == "0":
                    entry['state']='need bug'
                    continue

                # Request clone right away
                if safe_int(release_get_major(release)) > 8:
                    _issue_request_clone(issue)

            entry['bugnumber']=bugnumber
    print("      * bug=%s"%bugnumber)
    if issue == None :
        issue = _issue_get(bugnumber)
    # if we are here, we have our bug created for our release, we can check it in
    all_builds_pushed=True
    print("  * checking git tree status")
    for package in packages.split(',') :
        # make sure each package is checked in and built
        git_state = git_get_state(release, package, bugnumber)
        if git_state == None:
            continue
        print("      * package<%s> state=%s"%(package,git_state))
        if git_state == 'staged' :
              git_state = git_checkin(release, package, bugnumber)
        if git_state == 'committed':
              print('trying to push')
              git_state = git_push(release, package, bugnumber)
        if git_state != 'pushed' :
              all_builds_pushed=False
        if git_state == 'pushed' and not builds_complete(entry['nvr'],package) :
              # handle centos pull request here
              if (distro == "centos") :
                  mr = None
                  if centosUpstream is None:
                      # create a new merge request
                      centosUpstream = gitlab_src_from_fork(CentOSFork)
                      entry['glupstream'] = centosUpstream.id

                  if glmr is None:
                      mr = gitlab_create_mr(CentOSFork, centosUpstream,
                                            bugnumber, branch='main')
                      entry['glmr'] = mr.id
                  else:
                      mr = gitlab_get_mr(centosUpstream, glmr)

                  if (mr == None):
                      continue
                  elif (mr.state == "merged"):
                      git_pull(get_build_packages_dir(distro, package, release))
                  else:
                      print(f"Merge request status: {mr.state}");
                      entry['state'] = 'waiting centos merge'
                      continue

              nvr = build(release,package)
              entry['nvr'] = add_nvr(entry['nvr'],nvr)

    builds=entry['nvr']
    erratanumber=entry['erratanumber']
    if distro == 'centos' :
        erratanumber = -1
    all_builds_complete = builds_complete(builds, packages)
    print("  * setting up state")
    # update our state
    if not all_builds_pushed :
        entry['state'] = 'builds need push'
    elif not all_builds_complete :
        state = None
        for package in packages.split(',') :
            state = merge_state(state, build_status(release,package))
        if state == "Nobuilds" :
            entry['state'] = 'builds not started'
        elif state == "Failed" :
            entry['state'] = 'builds failed'
        elif state == "Building" :
            entry['state'] = 'builds in progress'
        elif state == "Gating" :
            entry['state'] = 'builds in gating'
        elif state == "Complete" :
            entry['state'] = 'builds complete, state error'
        else :
            entry['state'] = 'builds in an unknown state'
    elif erratanumber == 0 :
        entry['state'] = 'needs errata'
    else :
        entry['state'] = 'builds complete'
    print('  * handling errata')
    bug_state=_issue_get_state(issue)
    print("Bug state: "+bug_state)
    # once the builds are complete, put the bug in modified state
    bug_resync = False
    if bug_state == 'NEW' :
        bug_state = _issue_change_state(issue, 'PLANNING')
    if all_builds_pushed and (bug_state == 'NEW' or bug_state == 'PLANNING') :
        bug_resync = True
        bug_state = _issue_change_state(issue, 'IN PROGRESS')
    # and once our bug is modified, we can create the errata
    if erratanumber == 0 :
        erratanumber = errata_lookup(release, version, firefox_version, packages)

    if erratanumber == 0:
        continue;

    print("      * errata=%d"%erratanumber)
    entry['erratanumber'] = erratanumber
    # finally, once we have our errata and builds, attach them
    if (bug_state == 'IN PROGRESS') :
        if not errata_has_bug(erratanumber,bugnumber) :
            print("      * adding bug %s to  errata"%bugnumber)
            errata_add_bug(erratanumber, bugnumber, bug_resync)
    if all_builds_complete :
        entry['state'] = 'need builds attached'
        if  not errata_has_builds(erratanumber, release, builds):
            print("      * adding builds to errata")
            errata_state = errata_get_state(erratanumber)
            print("         - errata in state ",errata_state)
            # revert the errata to NEW_FILES if it's on QE
            if (errata_state == 'QE') :
                errata_state = errata_set_state(erratanumber,"NEW_FILES")
                print("         - errata in new state ",errata_state)
            errata_add_builds(erratanumber, release, builds)
        # finally, once the builds are build and attached to the errata, mark this release complete
        if errata_has_builds(erratanumber, release, builds):
            entry['state'] = "needs bugs attached"
            if  errata_has_bug(erratanumber, bugnumber):
                 rpm_state = errata_get_rpm_state(erratanumber, entry['nvr'])
                 entry['state'] = 'rpm diff state ' + rpm_state
                 if (rpm_state == 'PASSED' or rpm_state == 'WAIVED' or rpm_state == 'INFO') :
                     entry['state'] = 'need to set to QE'
                     errata_state = errata_get_state(erratanumber)
                     print("         - errata in state ",errata_state)
                     if (errata_state == 'NEW_FILES' or errata_state == 'UNKNOWN' ) :
                         errata_state = errata_set_state(erratanumber,"QE")
                         print("         - errata in new state ",errata_state)
                     if (errata_state == 'QE') :
                         entry['state'] = 'complete'

# fedora doesn't need bugs and errata, just git and builds
distro='fedora'
for release in fedora_packages:
    entry=fedora_packages[release]
    print("Processing release <%s>:"%release)
    if entry['state'] == 'complete' :
        print("  * complete!")
        continue
    bugnumber="-1"
    packages=entry['packages']
    all_builds_pushed=True
    print("  * checking git tree status")
    for package in packages.split(',') :
        # make sure each package is checked in and built
        git_state = git_get_state(release, package, bugnumber)
        print("      * package<%s> state=%s"%(package,git_state))
        if git_state == 'staged' :
              git_state = git_checkin(release, package, bugnumber)
        if git_state == 'committed':
              print('trying to push')
              git_state = git_push(release, package, bugnumber)
        if git_state != 'pushed' :
              all_builds_pushed=False
        if git_state == 'pushed' and not builds_complete(entry['nvr'],package) :
              nvr = build(release,package)
              entry['nvr'] = add_nvr(entry['nvr'],nvr)
    builds=entry['nvr']
    erratanumber=entry['erratanumber']
    all_builds_complete = builds_complete(builds, packages)
    print("  * setting up state")
    if not all_builds_pushed :
        entry['state'] = 'builds need push'
    elif not all_builds_complete :
        state = None
        for package in packages.split(',') :
            state = merge_state(state, build_status(release,package))
        if state == "Nobuilds" :
            entry['state'] = 'builds not started'
        elif state == "Failed" :
            entry['state'] = 'builds failed'
        elif state == "Building" :
            entry['state'] = 'builds in progress'
        elif state == "Complete" :
            entry['state'] = 'builds complete, state error'
        else :
            entry['state'] = 'builds in an unknown state'
    else :
        entry['state'] = 'complete'

#######################################################
#
# Update in our status files
#
#######################################################
print("Updating %s"%rhel_list)
f = open(rhel_list,"w")
for release in rhel_packages :
    entry = rhel_packages[release]
    bugnumber=entry['bugnumber']
    erratanumber=entry['erratanumber']
    packages=entry['packages']
    f.write("%s:%s:%s:%d:%s:%s:%s:%s:%s\n"%(release,packages,bugnumber,
            erratanumber,entry['nvr'],entry['state'],
            entry['glupstream'], entry['glmr'], entry.get('crypto','')))
f.close()
print("Updating %s"%fedora_list)
f = open(fedora_list,"w")
for release in fedora_packages :
    entry = fedora_packages[release]
    bugnumber=entry['bugnumber']
    erratanumber=entry['erratanumber']
    packages=entry['packages']
    f.write("%s:%s:%s:%d:%s:%s\n"%(release,packages,bugnumber,
            erratanumber,entry['nvr'],entry['state']))
f.close()


#######################################################
#
# print out in our current status
#
#######################################################
print("Current Status:")
distro='rhel'
for release in rhel_packages :
    entry = rhel_packages[release]
    bugnumber=entry['bugnumber']
    erratanumber=entry['erratanumber']
    packages=entry['packages']
    print("%s: state='%s' bug=%s errata=%d"%(release,entry['state'],bugnumber,erratanumber))
    if bugnumber != "0":
        print("    %s/show_bug.cgi?id=%s"%(jira_url_base,bugnumber))
    if erratanumber != 0:
        print("    %s/advisory/%d"%(errata_url_base,erratanumber))
    for package in packages.split(',') :
        (task, nvr, state) = build_get_info(release, package)
        if (task != '') :
            print("    %s/taskinfo?taskID=%s (%s,%s)"%(brew_url_base,task,nvr,state))

distro='fedora'
for release in fedora_packages :
    entry = fedora_packages[release]
    packages=entry['packages']
    print("%s: state='%s'"%(release,entry['state']))
    for package in packages.split(',') :
        (task, nvr, state) = build_get_info(release, package)
        if (task != '') :
            print("    %s/taskinfo?taskID=%s (%s,%s)"%(koji_url_base,task,nvr,state))
