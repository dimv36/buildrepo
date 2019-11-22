#!/bin/bash

cd `dirname $0`

source environment
source runtime-environment

function help()
{
	echo $0 "<dsc>"
}

function exec_cmd_or_fail()
{
	args=$1
	bash -c "$args"
	retcode=$?
	if [[ "$retcode" -ne "0" ]]
	then
		echo "$args failed"
		exit $retcode
	fi
}

function build_package()
{
	bash -c "dpkg-buildpackage"
	retcode=$?
	if [[ "retcode" -ne "0" ]]
	then
		echo "RETCODE $retcode"
		case "$retcode" in
			"1")
				;;
			"25")
				;;
			*)
				exit $retcode
		esac
	fi
}

##############################
#         main               #
##############################
if [[ "$#" -ne "1" ]]
then
	help
	exit 0
fi

IN_CHROOT=`echo $container`
if [[ "x$IN_CHROOT" == "x" ]]
then
	echo "$0 must be run under chroot"
	exit 1
fi

DSCFILE=$1

# Determine dirname of DSCFILE
pushd `dirname $DSCFILE` &> /dev/null

# Chown to BUILDUSER
chmod u+rw *
chown -R $BUILDUSER:root ./

DSCFILE=`basename $DSCFILE`
BUILDDIR="${DSCFILE%.*}"_build

# Check, if BUILDDIR exists
if [[ -d "$BUILDDIR" ]]
then
	rm -fr $BUILDDIR &> /dev/null
fi

# Update apt cache
exec_cmd_or_fail "apt-get update"

# Next, extract sources
exec_cmd_or_fail "sudo -u $BUILDUSER dpkg-source -x $DSCFILE $BUILDDIR"

# Enter to BUILDDIR
pushd $BUILDDIR &> /dev/null

# Install build-depends via apt
exec_cmd_or_fail "apt-get build-dep ./"

# Try to build package
exec_cmd_or_fail "sudo -u $BUILDUSER DEB_BUILD_OPTIONS=\"$DEB_BUILD_OPTIONS\" dpkg-buildpackage"

popd &> /dev/null # BUILDDIR

rm -fr $BUILDDIR &> /dev/null

# Move binaries into repo
mv *.deb ../repo

popd &> /dev/null # Dir with sources

# Generate packages list
dpkg-scanpackages --multiversion 2>/dev/null repo > repo/Packages

exit 0
