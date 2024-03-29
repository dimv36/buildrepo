#!/bin/bash

cd `dirname $0`

source environment


function help()
{
	echo $0 "build <dsc>"
	echo $0 "refresh <repo-path>"
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


function refresh_repo()
{
	REPO=$1
	dpkg-scanpackages --multiversion 2>/dev/null $REPO > $REPO/Packages
}


function exec_init_scripts()
{
	INIT_SCRIPTS_DIR="/srv/init"
	if [[ -d "$INIT_SCRIPTS_DIR" ]]
	then
		for script in `ls -1 "$INIT_SCRIPTS_DIR"`
		do
			fullpath="$INIT_SCRIPTS_DIR/$script"
			echo "INIT: Executing \"$fullpath\" ..."
			/bin/bash "$fullpath"
			err=$?
			if [[ "$?" -ne "0" ]]
			then
				echo "INIT: Script \"$fullpath\" failed with code $err"
			else
				echo "INIT: Script \"$fullpath\" succeeded"
			fi
		done
	fi
	return 0
}


function cmd_build
{
	exec_init_scripts

	source runtime-environment

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
	exec_cmd_or_fail "sudo -u $BUILDUSER DEB_BUILD_OPTIONS=\"$DEB_BUILD_OPTIONS\" DEB_BUILD_PROFILES=\"$DEB_BUILD_PROFILES\" dpkg-buildpackage"

	popd &> /dev/null # BUILDDIR

	rm -fr $BUILDDIR &> /dev/null

	# Move binaries into repo
	mv *.deb ../repo

	popd &> /dev/null # Dir with sources

	# Generate packages list
	refresh_repo repo

	exit 0
}


function cmd_refresh
{
	REPO_PATH=$1

	pushd $REPO_PATH &> /dev/null

	# Move to parent dir
	cd ..
	BASENAME=`basename $REPO_PATH`

	# Generate packages list
	refresh_repo $BASENAME

	popd &> /dev/null
}


##############################
#         main               #
##############################

IN_CHROOT=`echo $container`
if [[ "x$IN_CHROOT" == "x" ]]
then
	echo "$0 must be run under chroot"
	exit 1
fi

if [[ "$#" -ne "2" ]]
then
	help
	exit 0
fi

command=$1

case "$command" in
	build)
		cmd_build $2
	;;
	refresh)
		cmd_refresh $2
	;;
	*)
		help
		exit 0
	;;
esac
