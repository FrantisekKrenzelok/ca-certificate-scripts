#!/bin/sh
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
baseurl="https://hg.mozilla.org/releases/mozilla-release/raw-file/default/security/nss/lib"
release_type="RTM"
release="3_43"
verbose=1

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

addpatch()
{
   SPEC=$1
   PATCH=$2
   PATCH_ORIG=$3
   CERT_LOG=$4
   nss_version=$5
   ckbi_version=$6
   new_version=$7

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
	echo $line | grep "^%patch" > /dev/null
        if [ $? -eq 0 -a ${inSetup} -le 1 ]; then
	    inSetup=1;
	    echo "$line";
	    continue;
	fi
	if [[ ! ${line} =~ [^[:space:]] ]]; then
	    if [ $inPatches -eq 1 ]; then
		patchnum=`expr ${maxpatch} + 1`
		echo "# Update certdata.txt to version $ckbi_version"
		echo "Patch${patchnum}: ${PATCH}"
		inPatches=2
	    fi
	    if [ $inSetup -eq 1 ]; then
		echo "%patch${patchnum} -p1 -b ${PATCH_ORIG}"
		inSetup=2
            fi
	    echo "$line"
            continue;
         fi
    fi
# update the version if we've supplied it, otherwise not the version for the log
    echo $line | grep "^Version: " > /dev/null
    if [ $? -eq 0 ]; then
	if [ -z ${new_version} ]; then
	    echo "$line"
	    version=`echo $line | sed -e 's;^Version: ;;'`
	else
	    version=${new_version}
            echo "Version: ${version}"
            echo "New Version: ${version}" 1>&2
        fi
        continue
    fi
# update the release
    echo $line | grep "^Release: " > /dev/null
    if [ $?  -eq 0 ]; then
	if [ -z ${new_version} ]; then
	    release=`echo $line | sed -e 's;^Release: ;;'`
            # this magic strips the leading number and increments it, so 5.1
            # becomes 6
            release=$(expr ${release%%[^0-9]*} + 1)
	else
	    release=1
	fi
        echo "Release: ${release}%{?dist}"
        continue
    fi
    echo $line | grep "^%changelog" > /dev/null
    if [ $?  -eq 0 ]; then
        echo "$line"
	mklog ${version}-${release}
        echo "Update to CKBI ${ckbi_version} from NSS ${nss_version}"
	cat ${CERT_LOG}
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

# update an openssl build
openssl_update()
{
   OPENSSLPACKAGEDIR=$1
   CERTDATA=$2
   NSSCKBI=$3
   nss_version=$4
   ckbi_version=$5
   SCRATCH=$6
   RELEASE=$7

   # make sure the generated certdata file exists
   if [ ! -f ${CERTDATA} ]; then
	echo "!!!Skipping openssl build for ${RELEASE}. no certdata.txt generated"
        return 1
   fi
   if [ ! -d ${OPENSSLPACKAGEDIR} ]; then
	echo "!!!Skipping openssl build for ${RELEASE}. no git repository found"
        return 1
   fi
   if [ ! -d ${PACKAGES}/ca-certificates/rhel-6.6 ]; then
	echo "!!!Skipping openssl build for ${RELEASE}. Couldn't find rhel-6.6 ca-certificates"
        return 1
   fi

   # first copy the rhel-6.6 ca-certificates branch to the scratch directory
   rm -rf ${SCRATCH}
   mkdir -p ${SCRATCH}
   echo ">>> fetching rhel-6.6 ca-certificates"
   (cd ${PACKAGES}/ca-certificates ; tar cf - rhel-6.6) | (cd ${SCRATCH} ; tar xf -)
   cd ${SCRATCH}/rhel-6.6
   # remove the java BuildRequires line
   cat ca-certificates.spec | while IFS= read -r line
   do
      echo $line | grep "^BuildRequires:.*java-.*-openjdk" > /dev/null
      if [ $? -eq 0 ]; then
	echo "#${line}"
        continue
      fi
      echo $line | grep "^BuildRequires:.*asciidoc" > /dev/null
      if [ $? -eq 0 ]; then
	echo "#${line}"
        continue
      fi
      echo "$line"
   done > /tmp/tmp.spec.$$
   mv /tmp/tmp.spec.$$ ca-certificates.spec
   # fetch or modifed certdata.txt
   cp ${CERTDATA} .
   # do a build. This has a side effect of generating a bunch of .crt files
   echo ">>> build ca-certificates.spec with the new spec"
   rhpkg -q local 2>&1 | grep -v "Certificate was added to keystore"
   if [ ! -d ca-certificates ]; then
	echo "!!!Skipping openssl build for ${RELEASE}. rhpkg local failed"
        return 1
   fi
   # run or magic scripts to create our new bundle
   echo ">>> use the ca-certificates build to create an openssl bundle."
   cd ca-certificates
   cp ${SCRIPT_LOC}/doit.sh .
   cp ${SCRIPT_LOC}/sort-bundle.py .
   ./doit.sh -q
   cp ${OPENSSLPACKAGEDIR}/ca-bundle.crt ./old-ca-bundle.crt
   python sort-bundle.py
   # check out output
   echo ">>> verify against the old bundle."
   diff ./old-ca-bundle.crt sorted-new > /dev/null 
   if [ $? -eq 0 ]; then
	echo "Skipping openssl build for ${RELEASE}. ca-bundle.crt is already up to date";
	return 0
   fi
   ${SCRIPT_LOC}/check_certs.sh ./old-ca-bundle.crt sorted-new -b | sed -e 's;       Subject: ;;' > ${SCRATCH}/cert_log
   # copy our new bundle
   cp sorted-new ${OPENSSLPACKAGEDIR}/ca-bundle.crt
   # update our spec file
   cd ${OPENSSLPACKAGEDIR}
   echo ">>> update openssl.spec"
   addpatch openssl.spec NONE  empty ${SCRATCH}/cert_log ${nss_version} ${ckbi_version}
   if [ ${verbose} -eq 1 ]; then
      git --no-pager diff openssl.spec
   fi
   git add openssl.spec ca-bundle.crt
   if [ ${verbose} -eq 1 ]; then
       git status
   fi
   return 0
}

# update an NSS build
nss_update()
{
   NSSPACKAGEDIR=$1
   CERTDATA=$2
   NSSCKBI=$3
   nss_version=$4
   ckbi_version=$5
   SCRATCH=$6
   RELEASE=$7

   # make sure the generated certdata file exists
   if [ ! -f ${CERTDATA} ]; then
	echo "!!!Skipping nss build for ${RELEASE}. no certdata.txt generated"
        return 1
   fi
   if [ ! -d ${NSSPACKAGEDIR} ]; then
	echo "!!!Skipping nss build for ${RELEASE}. no git repository found"
        return 1
   fi

   # create an NSS build directory
   rm -rf ${SCRATCH}
   mkdir -p ${SCRATCH}/SPECS
   mkdir -p ${SCRATCH}/SOURCES
   mkdir -p ${SCRATCH}/BUILD
   cd ${NSSPACKAGEDIR}
   echo "Fetch and extract the current nss build"
   rhpkg -q srpm
   rpm -i --define="%_topdir ${SCRATCH}" nss-*.src.rpm
   cd ${SCRATCH}/SPECS
   rpmbuild -bp nss.spec --define="%_topdir ${SCRATCH}" --quiet --nodeps
   if [ $? -ne 0 ]; then
	echo "!!!Skipping nss build for ${RELEASE}. rpmbuild -bp failed"
        return 1
   fi
   cd ${SCRATCH}/BUILD/nss*
   if [ $? -ne 0 ]; then
	echo "!!!Skipping nss build for ${RELEASE}. Couldn't find build directory"
        return 1
   fi
   ${SCRIPT_LOC}/check_certs.sh ./nss/lib/ckfw/builtins/certdata.txt $CERTDATA > ${SCRATCH}/cert_log
   diff ./nss/lib/ckfw/builtins/certdata.txt ${CERTDATA} > /dev/null 
   if [ $? -eq 0 ]; then
	echo "Skipping nss build for ${RELEASE}. certdata is already up to date";
	return 0
   fi
   echo ">>> generating patch file nss-${RELEASE}-ca-${ckbi_version}.patch"
   mv ./nss/lib/ckfw/builtins/certdata.txt ./nss/lib/ckfw/builtins/certdata.txt.ca-${ckbi_version}
   mv ./nss/lib/ckfw/builtins/nssckbi.h ./nss/lib/ckfw/builtins/nssckbi.h.ca-${ckbi_version}
   cp ${CERTDATA} ./nss/lib/ckfw/builtins/
   cp ${NSSCKBI} ./nss/lib/ckfw/builtins/
   gendiff . .ca-${ckbi_version} > ${SCRATCH}/SOURCES/nss-${RELEASE}-ca-${ckbi_version}.patch
   cd ${SCRATCH}/SPECS
   echo ">>> update nss.spec"
   addpatch nss.spec nss-${RELEASE}-ca-${ckbi_version}.patch .ca-${ckbi_version} ${SCRATCH}/cert_log ${nss_version} ${ckbi_version}
   echo ">>> verify updated nss.spec"
   rpmbuild -bp nss.spec --define="%_topdir ${SCRATCH}" --quiet --nodeps
   if [ $? -ne 0 ]; then
	echo "!!!Skipping nss build for ${RELEASE}. spec file update failed"
	return 1
   fi
   cp ${SCRATCH}/SPECS/nss.spec ${NSSPACKAGEDIR}/
   cp ${SCRATCH}/SPECS/checkin.log ${NSSPACKAGEDIR}/
   cp ${SCRATCH}/SOURCES/nss-${RELEASE}-ca-${ckbi_version}.patch ${NSSPACKAGEDIR}/
   cd ${NSSPACKAGEDIR}
   if [ ${verbose} -eq 1 ]; then
   	git --no-pager diff nss.spec
   fi
   git add nss.spec nss-${RELEASE}-ca-${ckbi_version}.patch
   if [ ${verbose} -eq 1 ]; then
       git status
   fi
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

   if [ ! -f ${CERTDATA} ]; then
	echo "!!!Skipping ca-certificates build for ${RELEASE}. no certdata.txt generated"
        return 1
   fi
   if [ ! -d ${CACERTSPACKAGEDIR} ]; then
	echo "!!!Skipping ca-certificates build for ${RELEASE}. no git repository found"
        return 1
   fi
   mkdir -p ${SCRATCH}
   # create an NSS build directory
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
   addpatch ca-certificates.spec NONE  empty ${SCRATCH}/cert_log ${nss_version} ${ckbi_version} ${year}.${ckbi_version}
   cp ${CERTDATA} .
   if [ ${verbose} -eq 1 ]; then
   	git --no-pager diff ca-certificates.spec 
   fi
   git add ca-certificate.spec certdata.txt
   if [ ${verbose} -eq 1 ]; then
       git status
   fi
   return 0
}

trap finish EXIT
#
# Parse the arguments
#
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
	certdatadir=`echo $i | sed -e 's;-f;;'`
        if [ "${certdatadir}" = "" ]; then
           shift
           certdatadir=$1
        fi
        ;;
    rhel-5*) RHEL5="${RHEL5} $1";;
    rhel-6*) RHEL6="${RHEL6} $1";;
    rhel-8*) RHEL8="${RHEL8} $1";;
    rhel-7*) if [ $1 \< "rhel-7.5" ]; then
	       RHEL7o="${RHEL7o} $1"
	    fi
            if [ $1 = "rhel-7.5" ]; then
	       RHEL7="${RHEL7} $1"
	    fi
            if [ $1 \> "rhel-7.5" ]; then
	       RHEL7="${RHEL7} $1"
	    fi
	    ;;
    *)
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

# reset the directory structure
echo "******************************************************************"
echo "*                   Setting up directories                       *"
echo "******************************************************************"
rm -rf ${PACKAGES} ${MODIFIED} ${CACERTS}
mkdir -p ${MODIFIED}/rhel7_4/ca-certificates
mkdir -p ${MODIFIED}/rhel7_4/nss
mkdir -p ${MODIFIED}/rhel7_5/ca-certificates
mkdir -p ${MODIFIED}/rhel7_5/nss
mkdir -p ${MODIFIED}/rhel8/ca-certificates
mkdir -p ${MODIFIED}/rhel6_10/ca-certificates
mkdir -p ${MODIFIED}/rhel6_10/nss
mkdir -p ${MODIFIED}/rhel6_7/ca-certificates
mkdir -p ${MODIFIED}/rhel6_7/nss
mkdir -p ${MODIFIED}/rhel5/nss
mkdir -p ${MODIFIED}/rhel5/openssl
mkdir -p ${PACKAGES}
mkdir -p ${CACERTS}

#fetch everthing we need. First certdata and nssckbi
echo "******************************************************************"
echo "*                   Fetching Sources                             *"
echo "******************************************************************"
if [ -z "${certdatadir}" ]; then
    nss_version=`wget -q ${baseurl}/nss/nss.h -O - | grep "NSS_VERSION" | awk '{print $3}' | sed -e "s;\";;g" `
    if [ "${nss_version}" = "" ]; then
        echo "Didn't find nss version from ${baseurl}"
        exit 1;
    fi
    cd ${CACERTS}
    wget -q ${baseurl}/ckfw/builtins/nssckbi.h -O nssckbi.h
    if [ $? -ne 0 ]; then
       echo fetching nssckbi.h from ${baseurl} failed!
       exit 1;
    fi
    wget -q ${baseurl}/ckfw/builtins/certdata.txt -O certdata.txt
    if [ $? -ne 0 ]; then
       echo fetching certdata.txt from ${baseurl} failed!
       exit 1;
    fi
else
    cd ${certdatadir}
    nss_version=`grep "NSS_VERSION" nss.h | awk '{print $3}' | sed -e "s;\";;g" `
    if [ "${nss_version}" = "" ]; then
        echo "Didn't find nss version from ${baseurl}"
        exit 1;
    fi
    cp nssckbi.h $(CACERTS)
    if [ ! -f nssckbi.h ]; then
       echo copying nssckbi.h from ${certdatadir} failed!
       exit 1;
    fi
    cp certdata.txt $(CACERTS)
    if [ $? -ne 0 ]; then
       echo copying certdata.txt from ${certdata} failed!
       exit 1;
    fi
    cd ${CACERTS}
fi
ckbi_version=`grep "NSS_BUILTINS_LIBRARY_VERSION " nssckbi.h | awk '{print $NF}' | sed -e "s;\";;g" `

# now fetch the relevant builds
cd ${PACKAGES}
echo ">> fetching ca-certificates"
rhpkg -q clone -B ca-certificates
echo ">> fetching nss"
rhpkg -q clone -B nss
echo ">> fetching openssl"
rhpkg -q clone -B openssl

# modify certdata.txt
cd ${SCRIPT_LOC}
echo "******************************************************************"
echo "*          Modifying certdata.txt for releases                   *"
echo "******************************************************************"
./certdata-upstream-to-certdata-rhel.py --input ${CACERTS}/certdata.txt --output ${MODIFIED}/rhel7_5/ca-certificates/certdata.txt
./certdata-upstream-to-certdata-rhel.py --input ${CACERTS}/certdata.txt --output ${MODIFIED}/rhel8/ca-certificates/certdata.txt
./certdata-upstream-to-certdata-rhel.py --input ${CACERTS}/certdata.txt --output ${MODIFIED}/rhel7_5/nss/certdata.txt --without-legacy-choice
./certdata-upstream-to-certdata-rhel.py --input ${CACERTS}/certdata.txt --output ${MODIFIED}/rhel7_4/ca-certificates/certdata.txt --add-legacy-codesign
./certdata-upstream-to-certdata-rhel.py --input ${CACERTS}/certdata.txt --output ${MODIFIED}/rhel7_4/nss/certdata.txt --add-legacy-codesign --without-legacy-choice
./certdata-upstream-to-certdata-rhel.py --input ${CACERTS}/certdata.txt --output ${MODIFIED}/rhel6_10/ca-certificates/certdata.txt --add-legacy-1024bit --add-legacy-codesign
./certdata-upstream-to-certdata-rhel.py --input ${CACERTS}/certdata.txt --output ${MODIFIED}/rhel6_10/nss/certdata.txt --add-legacy-codesign --without-legacy-choice --without-ca-policy-attribute
./certdata-upstream-to-certdata-rhel.py --input ${CACERTS}/certdata.txt --output ${MODIFIED}/rhel6_7/ca-certificates/certdata.txt --add-legacy-1024bit --add-legacy-codesign --without-ca-policy-attribute
./certdata-upstream-to-certdata-rhel.py --input ${CACERTS}/certdata.txt --output ${MODIFIED}/rhel6_7/nss/certdata.txt --add-legacy-codesign --without-legacy-choice --without-ca-policy-attribute
./certdata-upstream-to-certdata-rhel.py --input ${CACERTS}/certdata.txt --output ${MODIFIED}/rhel5/nss/certdata.txt --add-legacy-codesign --without-legacy-choice --without-ca-policy-attribute
./certdata-upstream-to-certdata-rhel.py --input ${CACERTS}/certdata.txt --output ${MODIFIED}/rhel5/openssl/certdata.txt --add-legacy-1024bit --without-legacy-choice --without-ca-policy-attribute

# update the relevant builds
echo "******************************************************************"
echo "*          Updating RHEL packages                                *"
echo "******************************************************************"
errors=0
for i in ${RHEL5}
do
   echo "************************** openssl $i ****************************"
   openssl_update ${PACKAGES}/openssl/$i ${MODIFIED}/rhel5/openssl/certdata.txt ${CACERTS}/nssckbi.h $nss_version $ckbi_version ${SCRATCH} $i
   errors=$(expr $errors + $?)
   echo "**************************** nss $i ******************************"
   nss_update ${PACKAGES}/nss/$i ${MODIFIED}/rhel5/nss/certdata.txt ${CACERTS}/nssckbi.h $nss_version $ckbi_version ${SCRATCH} $i
   errors=$(expr $errors + $?)
done
for i in ${RHEL6}
do
   echo "**************************** nss $i ******************************"
   nss_update ${PACKAGES}/nss/$i ${MODIFIED}/rhel6_10/nss/certdata.txt ${CACERTS}/nssckbi.h $nss_version $ckbi_version ${SCRATCH} $i
   errors=$(expr $errors + $?)
   echo "********************** ca-certificaes $i *************************"
   cacertificates_update ${PACKAGES}/ca-certificates/$i ${MODIFIED}/rhel6_10/ca-certificates/certdata.txt ${CACERTS}/nssckbi.h $nss_version $ckbi_version ${SCRATCH} $i
   errors=$(expr $errors + $?)
done
for i in ${RHEL7o}
do
   echo "**************************** nss $i ******************************"
   nss_update ${PACKAGES}/nss/$i ${MODIFIED}/rhel7_4/nss/certdata.txt ${CACERTS}/nssckbi.h $nss_version $ckbi_version ${SCRATCH} $i
   errors=$(expr $errors + $?)
   echo "********************** ca-certificaes $i *************************"
   cacertificates_update ${PACKAGES}/ca-certificates/$i ${MODIFIED}/rhel7_4/ca-certificates/certdata.txt ${CACERTS}/nssckbi.h $nss_version $ckbi_version ${SCRATCH} $i
   errors=$(expr $errors + $?)
done
for i in ${RHEL7}
do
   echo "**************************** nss $i ******************************"
   nss_update ${PACKAGES}/nss/$i ${MODIFIED}/rhel7_5/nss/certdata.txt ${CACERTS}/nssckbi.h $nss_version $ckbi_version ${SCRATCH} $i
   errors=$(expr $errors + $?)
   echo "********************** ca-certificaes $i *************************"
   cacertificates_update ${PACKAGES}/ca-certificates/$i ${MODIFIED}/rhel7_5/ca-certificates/certdata.txt ${CACERTS}/nssckbi.h $nss_version $ckbi_version ${SCRATCH} $i
   errors=$(expr $errors + $?)
done
for i in ${RHEL8}
do
   echo "********************** ca-certificaes $i *************************"
   cacertificates_update ${PACKAGES}/ca-certificates/$i ${MODIFIED}/rhel7_5/ca-certificates/certdata.txt ${CACERTS}/nssckbi.h $nss_version $ckbi_version ${SCRATCH} $i
   errors=$(expr $errors + $?)
done
echo "Finished updates with ${errors} errors"
cd ${SCRIPT_LOC}
echo "The following directories are ready for checkin:"
find packages -name checkin.log -print | sed -e 's;/checkin.log;;'
