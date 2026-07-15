#!/bin/bash
#
# Automatically update the selected builds with the new certdata.txt
#
# update a spec file with a new patch


#  globals
SCRIPT_LOC=$(pwd)
CACERTS=${SCRIPT_LOC}/cacerts
PACKAGES=${SCRIPT_LOC}/packages
MODIFIED=${SCRIPT_LOC}/modified
SCRATCH=${SCRIPT_LOC}/scratch.$$
META_DATA=${SCRIPT_LOC}/meta
baseurl="https://hg.mozilla.org/releases/mozilla-release/raw-file/default/security/nss/lib"
release_type="RTM"
release="3_114"
verbose=1
CURRENT_RELEASES="rawhide $(./process.py --get-ga)"
CENTOS_CACERTS_FORK=$(./process.py --getconfig centos_fork)
RHEL_CACERTS=0
FEDORA_CACERTS=0
PRUNE_DATE='NEVER'
MERGE_OBJECT_SIGN=1
RHEL_LIST=${META_DATA}/rhel.list
FEDORA_LIST=${META_DATA}/fedora.list

finish() {
    rm -rf ${SCRATCH}
    return;
}

mklog()
{
    vr=$1
    export LANG=C
    log_date=`date +"%a %b %d %Y"`

    # user name from the environment, fallback to git, fallback to the current user
    username=`whoami`
    name=${NAME}
    if [ "${name}" = "" ]; then
       name=`git config user.name`
    fi
    if [ "${name}" = "" ]; then
       name=`getent passwd $username`
    fi
    email=${EMAIL}
    if [ "${email}" = "" ]; then
       email=`git config user.email`
    fi
    if [ "${email}" = "" ]; then
       email=$username@`hostname`
    fi
    echo "*${log_date} ${name} <$email> - ${vr}"
}

bumprelease()
{
    release=$1
    reset_release=$2
    #strip any characters from the end
    release=${release%%[^.0-9]*}

    # if we are not updating ca, certficates,
    # bump to the next full release number
    # (5.1 -> 6, 7->8 etc.)
    if [ -z ${reset_release} ]; then
       expr ${release%%[^0-9]*} + 1
    else
       # if we are updating ca-certicates package,
       # preserve the minor release number, so
       # 80.0 -> 80.1, 81->82 etc.
       base=${release%.*}
       bump=${release##*.}
       if [ ${bump} == ${base} ]; then
          expr ${base} + 1
       else
          bump=$(expr ${bump} + 1)
          echo "${base}.${bump}"
       fi
    fi
}

addpatch()
{
   SPEC=$1
   PATCH=$2
   PATCH_ORIG=$3
   CERT_LOG=$4
   nss_version=$5
   ckbi_version=$6
   new_version=$7
   restart_release=$8

   inPatches=0
   inSetup=0
   maxpatch=0
   patchnum=0
   cat ${SPEC} | while IFS= read -r line
   do
#
# handle the actual patch. We add our patch at the end of the patches in
# the patch section, and at the end of the %patches in the  %setup
#
    if [ "${PATCH}" != "NONE" ]; then
	echo $line | grep "^Patch.*: " > /dev/null
        # Find the first patch source block, and find the next patch number
	if [ $? -eq 0 -a ${inPatches} -le 1 ]; then
	    lpatchnum=`echo ${line} | sed -e 's;^Patch;;'`
	    lpatchnum=${lpatchnum%%[^0-9]*}
	    if [ ${lpatchnum} -gt ${maxpatch} ]; then
		maxpatch=${lpatchnum}
	    fi
	    inPatches=1;
	    echo "$line";
            continue;
	fi
        # Find the first patch setup block
	echo $line | grep "^%patch" > /dev/null
        if [ $? -eq 0 -a ${inSetup} -le 1 ]; then
	    inSetup=1;
	    echo "$line";
	    continue;
	fi
        # handle the end of the block
	if [[ ! ${line} =~ [^[:space:]] ]]; then
            # add the new patch source
	    if [ $inPatches -eq 1 ]; then
		patchnum=`expr ${maxpatch} + 1`
		echo "# Update certdata.txt to version $ckbi_version"
		echo "Patch${patchnum}: ${PATCH}"
		inPatches=2
	    fi
            # add the new patch
	    if [ $inSetup -eq 1 ]; then
		echo "%patch${patchnum} -p1 -b ${PATCH_ORIG}"
		inSetup=2
            fi
	    echo "$line"
            continue;
         fi
    fi
# fetch and symbols likely used in the version number, currently only NSS
    echo $line | grep "^%global" > /dev/null
    if [ $? -eq 0 ]; then
	read glob sym value <<< "$line"
        case "${sym}" in
        "nss_version") glob_nss_version=$value;;
        *) ;;
        esac
        echo "$line"
        continue;
   fi
# update the version if we've supplied it, otherwise not the version for the log
    echo $line | grep "^Version: " > /dev/null
    if [ $? -eq 0 ]; then
        # if we have a new version number, replace it,
        # if not remember the old one for our log
	if [ -z ${new_version} ]; then
	    echo "$line"
	    version=`echo $line | sed -e 's;^Version: ;;'`
	    version=`echo $version | sed -e 's;%{nss_version};'${glob_nss_version}';g'`
	else
	    oldversion=`echo $line | sed -e 's;^Version: ;;'`
	    version=${new_version}
            echo "Version: ${version}"
            echo "Old Version: ${oldversion}" 1>&2
            echo "New Version: ${version}" 1>&2
        fi
        continue
    fi
# update the release
    echo $line | grep "^Release: " > /dev/null
    if [ $?  -eq 0 ]; then
        # we bump the release number if 1) we are updating a non-ca-certificate
        # package (like openssl or nss), or 2) we are updating an existing
        # ca-certificate package with the same version number.
	if [ -z ${new_version} ] || [ "${new_version}" = "${oldversion}" ]; then
	    release=`echo $line | sed -e 's;^Release: ;;'`
            release=$(bumprelease ${release} ${restart_release})
	else
	    release=${restart_release}
	fi
        echo "Release: ${release}%{?dist}"
        continue
    fi
    echo $line | grep "^%changelog" > /dev/null
    if [ $?  -eq 0 ]; then
        echo "$line"
	mklog ${version}-${release}
        echo "- Update to CKBI ${ckbi_version} from NSS ${nss_version}"
	cat ${CERT_LOG} | sed -e 's;^;- ;'
        echo ""
        continue
    fi
    echo "$line"
    done > /tmp/tmp.spec.$$
    cp /tmp/tmp.spec.$$ ${SPEC}
    rm /tmp/tmp.spec.$$
    echo "Update to CKBI ${ckbi_version} from NSS ${nss_version}" > checkin.log
    cat ${CERT_LOG} >> checkin.log
    return 0
}

# update a CA-cert build
cacertificates_update()
{
   CACERTSPACKAGEDIR=$1
   CERTDATA=$2
   NSSCKBI=$3
   nss_version=$4
   ckbi_version=$5
   SCRATCH=$6
   RELEASE=$7
   RESTART_RELEASE_Z=$8
   RESTART_RELEASE_BASE=$9

   if [ ! -f ${CERTDATA} ]; then
	echo "!!!Skipping ca-certificates build for ${RELEASE}. no certdata.txt generated"
        return 1
   fi
   if [ ! -d ${CACERTSPACKAGEDIR} ]; then
	echo "!!!Skipping ca-certificates build for ${RELEASE}. no git repository found"
        return 1
   fi
   if  echo ${CURRENT_RELEASES} | grep $RELEASE ; then
      restart_release=${RESTART_RELEASE_BASE}
   else
      restart_release=${RESTART_RELEASE_Z}
   fi
   mkdir -p ${SCRATCH}
   # analyze ca certificate differences
   cd ${CACERTSPACKAGEDIR}
   ${SCRIPT_LOC}/check_certs.sh certdata.txt ${CERTDATA} > ${SCRATCH}/cert_log
   diff certdata.txt ${CERTDATA} > /dev/null
   if [ $? -eq 0 ]; then
	echo "Skipping ca-certificates build for ${RELEASE}. certdata is already up to date";
	return 0;
   fi
   echo ">>> update ca-certificates.spec file"
   export LANG=C
   year=`date +"%Y"`
   addpatch ca-certificates.spec NONE  empty ${SCRATCH}/cert_log ${nss_version} ${ckbi_version} ${year}.${ckbi_version} ${restart_release}
   cp ${CERTDATA} .
   cp ${NSSCKBI} .
   if [ ${verbose} -eq 1 ]; then
   	git --no-pager diff ca-certificates.spec
   fi
   git add ca-certificates.spec nssckbi.h certdata.txt
   if [ ${verbose} -eq 1 ]; then
       git status
   fi
   return 0
}

trap finish EXIT
#
# Parse the arguments
#
rootPath=$(pwd)
while [ -n "$1" ]; do
   case $1 in
   "-q")
        # currently only affects git diff
        verbose=0
        ;;
   "-d")
        baseurl="https://hg.mozilla.org/projects/nss/raw-file/default/lib"
        ;;
   -t*)
        release_type=`echo $1 | sed -e 's;-t;;'`
        if [ "${release_type}" = "" ]; then
           shift
           release_type=$1
        fi
        baseurl="https://hg.mozilla.org/projects/nss/raw-file/NSS_${release}_${release_type}/lib"
        ;;
   -n*)
        release=`echo $1 | sed -e 's;-n;;'`
        if [ "${release}" = "" ]; then
           shift
           release=$1
        fi
        release=`echo ${release} | sed -e 's;\\.;_;g'`
        baseurl="https://hg.mozilla.org/projects/nss/raw-file/NSS_${release}_${release_type}/lib"
        ;;
   -f*)
	certdatadir=`echo $1 | sed -e 's;-f;;'`
        if [ "${certdatadir}" = "" ]; then
           shift
           certdatadir=$1
        fi
        ;;
    -p*)
	PRUNE_DATE=`echo $1 | sed -e 's;-p;;'`
        if [ "${PRUNE_DATE}" = "" ]; then
           shift
           PRUNE_DATE=$1
        fi
        ;;
    rhel-8*) RHEL8="${RHEL8} $1"; RHEL_CACERTS=1;;
    rhel-9*) RHEL9="${RHEL9} $1"; RHEL_CACERTS=1;;
    rhel-10*) RHEL10="${RHEL10} $1"; RHEL_CACERTS=1;;
     f*|rawhide)
            FEDORA="${FEDORA} $1"; FEDORA_CACERTS=1;;
    *)
        echo "unknown command $1"
        echo "usage: $0 [-r] [-t nss_type] [-n nss_release] [-f] rhel_releases"
        echo "-d               use the development tip rather than the latest release"
        echo "-n nss_release   fetch a specific nss release (default latest)"
        echo "-t nss_type      type of nss release to fetch (RTM,BETA1,BETA2)"
        echo "-f cert_datadir  fetch certdata.txt, nssckbi.h, and nss.h from a directory"
        exit 1
        ;;
    esac
    shift
done

CENTOS_LIST=()
# reset the directory structure
echo "******************************************************************"
echo "*                   Setting up directories                       *"
echo "******************************************************************"
rm -rf ${PACKAGES} ${MODIFIED} ${CACERTS} ${META_DATA}
mkdir -p ${PACKAGES}
mkdir -p ${CACERTS}
mkdir -p ${META_DATA}
if [ -n "${RHEL8}" ]; then
    mkdir -p ${MODIFIED}/rhel8/ca-certificates
    CENTOS_LIST+=( "8" )
fi
if [ -n "${RHEL9}" ]; then
    mkdir -p ${MODIFIED}/rhel9/ca-certificates
    CENTOS_LIST+=( "9" )
fi
if [ -n "${RHEL10}" ]; then
    mkdir -p ${MODIFIED}/rhel10/ca-certificates
    CENTOS_LIST+=( "10" )
fi
if [ -n "${FEDORA}" ]; then
    mkdir -p ${MODIFIED}/fedora/ca-certificates
    mkdir -p ${PACKAGES}/fedora
fi
touch ${RHEL_LIST}
touch ${FEDORA_LIST}

if [[ ${#CENTOS_LIST[@]} -gt 0 ]]; then
    mkdir -p ${PACKAGES}/centos
    mkdir -p ${PACKAGES}/centos-fork/ca-certificates
fi

#fetch everthing we need. First certdata and nssckbi
echo "******************************************************************"
echo "*                   Fetching Sources                             *"
echo "******************************************************************"
if [ -z "${certdatadir}" ]; then
    echo "fetching source data from mozilla:"
    echo " baseurl:${baseurl}"
    echo " target:${CACERTS}"
    cd ${CACERTS}
    echo ">> fetching nss/nss.h"
    wget -q ${baseurl}/nss/nss.h -O nss.h
    if [ $? -ne 0 ]; then
       echo fetching nss.h from ${baseurl} failed!
       exit 1;
    fi
    echo ">> fetching ckfw/buildtins/nssckbi.h"
    wget -q ${baseurl}/ckfw/builtins/nssckbi.h -O nssckbi.h
    if [ $? -ne 0 ]; then
       echo fetching nssckbi.h from ${baseurl} failed!
       exit 1;
    fi
    echo ">> fetching ckfw/builtins/certdata.txt"
    wget -q ${baseurl}/ckfw/builtins/certdata.txt -O certdata.txt.orig
    if [ $? -ne 0 ]; then
       echo fetching certdata.txt from ${baseurl} failed!
       exit 1;
    fi
    if [ ${MERGE_OBJECT_SIGN} -ne 0 ]; then
        echo ">> fetching signed objects certs"
        sign_obj_cas="microsoft_sign_obj_ca.pem"
        ${rootPath}/fetch_objsign.sh -n -o "${sign_obj_cas}"
        python3 ${rootPath}/mergepem2certdata.py -c "certdata.txt.orig" -p "${sign_obj_cas}" -o "certdata.txt" -t "CKA_TRUST_CODE_SIGNING" -l "Microsoft Code Signing Only Certificate" -x "${PRUNE_DATE}"
    else
        # add date prune script here?
        cp certdata.txt.orig certdata.txt
    fi

else
    echo "copying source data from directory"
    echo " nsource:${certdatadir}"
    echo " target:${CACERTS}"
    cd ${certdatadir}
    echo ">> copying ${certdatadir}/nss.h"
    cp nss.h ${CACERTS}
    if [ ! -f nss.h ]; then
       echo copying nss.h from ${certdatadir} failed!
       exit 1;
    fi
    echo "copying ${certdatadir}/nssckbi.h"
    cp nssckbi.h ${CACERTS}
    if [ ! -f nssckbi.h ]; then
       echo copying nssckbi.h from ${certdatadir} failed!
       exit 1;
    fi
    echo "copying ${certdatadir}/certdata.txt"
    cp certdata.txt ${CACERTS}
    if [ $? -ne 0 ]; then
       echo copying certdata.txt from ${certdatadir} failed!
       exit 1;
    fi
    cd ${CACERTS}
fi
nss_version=`grep "NSS_VERSION" nss.h | awk '{print $3}' | sed -e "s;\";;g" `
ckbi_version=`grep "NSS_BUILTINS_LIBRARY_VERSION " nssckbi.h | awk '{print $NF}' | sed -e "s;\";;g" `

if [ -f codesign-release.txt ]; then
    mcs_version=$(cat codesign-release.txt)
    if [[ $mcs_version != "unknown" ]]; then
        ckbi_version="${ckbi_version}_${mcs_version}"
    fi
fi
echo ${nss_version} > ${META_DATA}/nssversion.txt
echo ${mcs_version} > ${META_DATA}/mcsversion.txt
echo ${ckbi_version} > ${META_DATA}/ckbiversion.txt

# now fetch the relevant builds
cd ${PACKAGES}
if [ ${RHEL_CACERTS} -eq 1 ]; then
    echo ">> fetching rhel ca-certificates"
    rhpkg -q clone -B ca-certificates
    echo ">> fetching centos ca-certificates"

    # first fetch the centos stream directory
    pushd centos

        centpkg -q clone -B ca-certificates

        # Fetch upstream git url
        pushd ca-certificates/c8s
        CA_UPSTREAM=$(git config --get remote.origin.url)
        popd

    popd
    # now fetch the centos fork
    echo "Cloning fork, CA_UPSTREAM=${CA_UPSTREAM} CENTOS_CACERTS_FORK=${CENTOS_CACERTS_FORK}"
    pushd centos-fork/ca-certificates/
    for version in "${CENTOS_LIST[@]}"; do
        BRANCH_NAME="c${version}s"

        echo "Cloning ${BRANCH_NAME} from ${CENTOS_CACERTS_FORK}"
        git clone -c url."git@gitlab.com:".insteadOf="https://gitlab.com/" ${CENTOS_CACERTS_FORK} -b ${BRANCH_NAME} ${BRANCH_NAME}

        if [ ! -d "$BRANCH_NAME" ]; then
            echo "Folder $BRANCH_NAME not found"
            continue
        fi

        pushd ${BRANCH_NAME}
            # make sure the fork is up to date
            git remote add upstream ${CA_UPSTREAM}
            git fetch upstream
            git pull upstream ${BRANCH_NAME}
            git push origin ${BRANCH_NAME}

            # create the branch for the pull request
            git checkout -b ${BRANCH_NAME} origin/${BRANCH_NAME}
            git branch -u upstream/${BRANCH_NAME}
        popd
    done
    popd
fi

if [ ${FEDORA_CACERTS} -eq 1 ]; then
    echo ">> fetching fedora ca-certificates"
    (cd fedora; fedpkg -q clone -B ca-certificates)
fi


# modify certdata.txt
cd ${SCRIPT_LOC}
echo "******************************************************************"
echo "*          Modifying certdata.txt for releases                   *"
echo "******************************************************************"
if [ -n "${FEDORA}" ]; then
     echo " - Creating FEDORA certdata.txt fedora=${FEDORA} "
    ./certdata-upstream-to-certdata-rhel.py --input ${CACERTS}/certdata.txt --output ${MODIFIED}/fedora/ca-certificates/certdata.txt
fi
if [ -n "${RHEL10}" ]; then
     echo " - Creating RHEL 10 certdata.txt rhel10=${RHEL10} "
    ./certdata-upstream-to-certdata-rhel.py --input ${CACERTS}/certdata.txt --output ${MODIFIED}/rhel10/ca-certificates/certdata.txt
fi
if [ -n "${RHEL9}" ]; then
     echo " - Creating RHEL 9 certdata.txt rhel9=${RHEL9} "
    ./certdata-upstream-to-certdata-rhel.py --input ${CACERTS}/certdata.txt --output ${MODIFIED}/rhel9/ca-certificates/certdata.txt
fi
if [ -n "${RHEL8}" ]; then
     echo " - Creating RHEL 8 certdata.txt rhel8=${RHEL8} "
    ./certdata-upstream-to-certdata-rhel.py --input ${CACERTS}/certdata.txt --output ${MODIFIED}/rhel8/ca-certificates/certdata.txt
fi

# update the relevant builds
echo "******************************************************************"
echo "*          Updating RHEL packages                                *"
echo "******************************************************************"
errors=0
for i in ${RHEL8}
do
   echo "********************** ca-certificates $i *************************"
   cacertificates_update ${PACKAGES}/ca-certificates/$i ${MODIFIED}/rhel8/ca-certificates/certdata.txt ${CACERTS}/nssckbi.h $nss_version $ckbi_version ${SCRATCH} $i "80.0" "81"
   errors=$(expr $errors + $?)
   echo $i:ca-certificates:0:0::staged:: >> ${RHEL_LIST}
done
for i in ${RHEL9}
do
   echo "********************** ca-certificates $i *************************"
   if  echo ${CURRENT_RELEASES} | grep $i ; then
      cacertificates_update ${PACKAGES}/centos-fork/ca-certificates/c9s ${MODIFIED}/rhel9/ca-certificates/certdata.txt ${CACERTS}/nssckbi.h $nss_version $ckbi_version ${SCRATCH} $i "90.0" "91"
   else
      echo "CURRENT_RELEASES=\"${CURRENT_RELEASES}\" THIS_RELEASE=$i"
      cacertificates_update ${PACKAGES}/ca-certificates/$i ${MODIFIED}/rhel9/ca-certificates/certdata.txt ${CACERTS}/nssckbi.h $nss_version $ckbi_version ${SCRATCH} $i "90.0" "91"
   fi
   errors=$(expr $errors + $?)
   echo $i:ca-certificates:0:0::staged:: >> ${RHEL_LIST}
done
for i in ${RHEL10}
do
   echo "********************** ca-certificates $i *************************"
   if  echo ${CURRENT_RELEASES} | grep $i ; then
      cacertificates_update ${PACKAGES}/centos-fork/ca-certificates/c10s ${MODIFIED}/rhel10/ca-certificates/certdata.txt ${CACERTS}/nssckbi.h $nss_version $ckbi_version ${SCRATCH} $i "100.0" "101"
   else
      echo "CURRENT_RELEASES=\"${CURRENT_RELEASES}\" THIS_RELEASE=$i"
      cacertificates_update ${PACKAGES}/ca-certificates/$i ${MODIFIED}/rhel10/ca-certificates/certdata.txt ${CACERTS}/nssckbi.h $nss_version $ckbi_version ${SCRATCH} $i "100.0" "101"
   fi
   errors=$(expr $errors + $?)
   echo $i:ca-certificates:0:0::staged:: >> ${RHEL_LIST}
done
for i in ${FEDORA}
do
   echo "********************** ca-certificates $i *************************"
   cacertificates_update ${PACKAGES}/fedora/ca-certificates/$i ${MODIFIED}/fedora/ca-certificates/certdata.txt ${CACERTS}/nssckbi.h $nss_version $ckbi_version ${SCRATCH} $i "1.0" "2"
   errors=$(expr $errors + $?)
   echo $i:ca-certificates:0:0::staged >> ${FEDORA_LIST}
done
echo "Finished updates for ca-certificates ${cki_version} from NSS ${nss_version} with ${errors} errors"
cd ${SCRIPT_LOC}
echo "The following directories are ready for checkin:"
find packages -name checkin.log -print | sed -e 's;/checkin.log;;'
