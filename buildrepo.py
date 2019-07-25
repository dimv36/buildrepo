#!/usr/bin/python3

import sys
import logging
import re
import json
import gettext
import os
import gzip
import apt
import apt.debfile
import glob
import apt_pkg
import pwd
import shutil
import argparse
import subprocess
import tempfile
import platform
import atexit
import time
import datetime
import configparser

CURDIR = os.path.abspath(os.path.curdir)
DEFAULT_BUILD_DIR = os.path.abspath(os.path.join(CURDIR, 'build'))
DEFAULT_CONF = os.path.join(CURDIR, 'buildrepo.conf')
COMMAND_INIT = 'init'
COMMAND_BUILD = 'build'
COMMAND_MAKE_REPO = 'make-repo'
COMMAND_MAKE_PACKAGE_CACHE = 'make-package-cache'
COMMAND_REMOVE_SOURCES = 'remove-sources'
DEVNULL = open(os.devnull, 'wb')

DEB_RE = '^(?P<name>[\w\-\.\+]+)_(?P<version>[\w\.\-\~\+]+)_(?P<arch>[\w]+)\.deb$'
DSC_FULL_RE = '^(?P<name>[\w\-\.\+]+)_(?P<version>[\w\.\-\~\+]+)\.dsc$'
DSC_RE = '^%s_(?P<version>[\w\.\-\~\+]+)\.dsc$'
STANDART_BUILD_OPTIONS_TEMPLATE = 'DEB_BUILD_OPTIONS="nocheck parallel=%d"'
DPKG_IGNORED_CODES = [1, 25]

REQUIRED_PACKAGES = ['dpkg-dev', 'fakeroot', 'reprepro', 'genisoimage']

BUILD_USER = 'builder'

# Ключи кэша
DIRECTIVE_CACHE_NAME = 'cache_name'
DIRECTIVE_CACHE_TYPE = 'cache_type'
DIRECTIVE_CACHE_VERSION = 'version'
DIRECTIVE_CACHE_PACKAGES = 'packages'
DIRECTIVE_CACHE_PACKAGES_PACKAGE_NAME = 'name'
DIRECTIVE_CACHE_PACKAGES_PACKAGE_VERSION = 'version'

# gettext
_ = gettext.gettext

# apt cache
cache = apt.Cache()
# apt init
apt_pkg.init()
apt_pkg.config.set('Acquire::AllowInsecureRepositories', 'true')


def check_root_access():
    if not os.getuid() == 0:
        logging.error(_('Must be run as superuser'))
        exit(1)


def exit_with_error(error):
    logging.critical(error)
    exit(1)


def fix_re(reg_exp):
    if '++' in reg_exp:
        return reg_exp.replace('++', '\++')
    return reg_exp


def fix_package_version(pver):
    # TODO: Version hack
    if ':' in pver:
        pver = pver.split(':')[-1]
    return pver


class Debhelper:
    """
    Класс для запуска Debian утилит
    """

    @staticmethod
    def run_command_with_output(command):
        return subprocess.check_output(command, shell=True, stderr=DEVNULL).decode().rstrip('\n')

    @staticmethod
    def run_command(command, need_output=False):
        if not need_output:
            subprocess.check_call(command, shell=True, stderr=DEVNULL, stdout=DEVNULL)
        else:
            subprocess.check_call(command, shell=True)

    @staticmethod
    def base_init():
        global cache
        # Проверяем пакеты, которые должны быть уже установлены
        for pname in REQUIRED_PACKAGES:
            package = cache.get(pname)
            if not package:
                exit_with_error(_('Could not get package %s from cache') % pname)
            if not package.is_installed:
                try:
                    logging.info(_('Installing required package %s ...') % pname)
                    package.mark_install()
                    cache.commit()
                    cache.clear()
                except Exception as e:
                    exit_with_error(_('Failed to install required package %s: %s') % (pname, e))
        # Проверяем наличие учетной записи пользователя,
        # от имени которого будет выполяться сборка
        try:
            pwd.getpwnam(BUILD_USER)
        except KeyError:
            logging.info(_('Creating user %s ...') % BUILD_USER)
            try:
                Debhelper.run_command('useradd %s' % BUILD_USER)
            except Exception as e:
                exit_with_error(_('Failed to add user %s') % BUILD_USER)

    @staticmethod
    def get_packages_list():
        return [p.name for p in cache if p.installed]

    @staticmethod
    def generate_packages_list(repodirpath, ignore_errors=False):
        logging.info(_('Generating packages list ...'))
        # Вызываем dpkg-scanpackages
        repo_name = os.path.basename(repodirpath)
        os.chdir(os.path.abspath('%s/..' % repodirpath))
        command = 'dpkg-scanpackages %s/ > %s/Packages' % (repo_name, repo_name)
        try:
            Debhelper.run_command(command)
        except subprocess.CalledProcessError as error:
            if not ignore_errors:
                exit_with_error(_('Error package list generation by command %s') % command, error)
        # Вызываем apt-get update
        command = 'apt-get update'
        try:
            Debhelper.run_command(command)
        except subprocess.CalledProcessError as error:
            if not ignore_errors:
                exit_with_error(_('Error updating package list by command %s' % command))
        finally:
            os.chdir(CURDIR)
        cache.open()
        logging.info(_('Repository %s was updated') % repo_name)

    @staticmethod
    def extract_sources(tmpdirpath, package_name):
        logging.info(_('Unpacking sources %s ...') % package_name)
        command = 'sudo -u %s dpkg-source -x *.dsc' % BUILD_USER
        os.chdir(tmpdirpath)
        try:
            Debhelper.run_command(command)
        except subprocess.CalledProcessError as error:
            exit_with_error(_('Error unpacking sources with command %s: %s') % (command, error))
        finally:
            os.chdir(CURDIR)

    @staticmethod
    def get_build_dir(tmpdirpath):
        for file_name in os.listdir(tmpdirpath):
            path = os.path.join(tmpdirpath, file_name)
            if os.path.isdir(path):
                return path

    @staticmethod
    def install_build_depends(tmpdirpath, pkgname):
        def check_package_version(depname, op, version):
            pkgs = [cache.get(depname) or cache.get_providing_packages(depname, include_nonvirtual=True)]
            if not len(pkgs):
                return False
            if len(op) and len(version):
                return any(pkg.installed and apt_pkg.check_dep(pkg.installed.version, op, version) for pkg in pkgs)
            return True

        def install_package_or_providing(ptuple, builded_package, critical=True):
            pname, version, op = ptuple
            real_pkg = cache.get(pname, None)
            if not real_pkg:
                packages = cache.get_providing_packages(pname)
            else:
                packages = [real_pkg]
            if not len(packages):
                if critical:
                    exit_with_error(_('Failed to get package %s from cache') % pname)
                else:
                    logging.info(_('Failed to get package %s from cache') % pname)
                    return False, None
            for pdep in packages:
                if pdep.is_installed:
                    if len(version) and not apt_pkg.check_dep(pdep.installed.version, op, version):
                        try:
                            pdep.mark_install()
                            cache.commit()
                        except apt_pkg.Error:
                            pass
                        finally:
                            cache.open()
                        if not check_package_version(pdep.name, op, version):
                            logging.error(_('For package %s building %s (version %s) is required, '
                                            'but installed %s') % (builded_package, pname, version,
                                                                   pdep.installed.version))
                            return False, None
                        else:
                            logging.info(_('Dependency %s (%s %s) installed') % (pdep.name, op, version))
                            return True, None
                    logging.info(_('Package %s already installed') % pname)
                    return True, None
                if cache.is_virtual_package(pdep.name):
                    logging.info(_('Package %s is virtual, provided by %s ...') % (pname, pdep.name))
                logging.info(_('Installing dependency %s ...') % pdep.name)
                try:
                    pdep.mark_install()
                    cache.commit()
                except apt_pkg.Error:
                    pass
                finally:
                    cache.open()
                # Проверяем, установлен ли пакет
                dep = [cache.get(pdep.name)] or cache.get_providing_packages(pdep.name, include_nonvirtual=True)
                if not len(dep):
                    return False, pdep.name
                dep = dep[0]
                if len(op) and len(version):
                    req_version = check_package_version(dep.name, op, version)
                    if not req_version:
                        if dep.installed:
                            installed_ver = 'installed %s' % dep.installed.version
                        else:
                            installed_ver = '%s is not not installed' % dep.name
                        logging.error(_('For package %s building %s (version %s) is required, '
                                        '%s') % (builded_package, pname, version,
                                                 installed_ver))
                    return req_version, pdep.name
                return True if dep.installed else False, dep.name
            # Не должно дойти сюда
            return False, None

        def form_depends(depends):
            depstrings = []
            for dep in depends:
                depstr = ' | '.join(['%s (%s %s)' % (p[0], p[2], p[1])
                                    if len(p[1]) else p[0] for p in dep])
                depstrings.append(depstr)
            return ', '.join(depstrings)

        try:
            dscfilepath = next(f for f in os.listdir(tmpdirpath) if re.match(DSC_FULL_RE, f))
        except AttributeError:
            exit_with_error(_('dsc file does not exist'))
        dscfile = apt.debfile.DscSrcPackage(filename=os.path.join(tmpdirpath, dscfilepath))
        # Проверка по конфиликтам
        if len(dscfile.conflicts):
            logging.info(_('Found Build conflicts for package %s: %s') % (pkgname, form_depends(dscfile.conflicts)))
            for conflict in dscfile.conflicts:
                c_name = conflict[0][0]
                try:
                    dep = cache.get(c_name)
                    if not dep:
                        continue
                    dep.mark_delete()
                    cache.commit()
                except Exception as e:
                    exit_with_error(e)
                finally:
                    cache.open()
        logging.info(_('Build depends for package %s: %s') % (pkgname, form_depends(dscfile.depends)))
        for dep in dscfile.depends:
            cache.open()
            if len(dep) == 1:
                # Обыкновенная зависимость
                res, installed_package = install_package_or_providing(dep[0], pkgname)
                if not res:
                    exit_with_error(_('Failed to install package %s') % (
                                    installed_package if installed_package else pkgname))
            else:
                # Альтернативные зависимости
                alt_installed = False
                depstr = ' | '.join([' '.join([p[0], '(', p[2], p[1], ')'])
                                    if len(p[1]) else p[0] for p in dep])
                logging.info(_('Processing alternative dependencies %s ...') % depstr)
                for alt in dep:
                    is_installed, installed_pkg = install_package_or_providing(alt, pkgname, critical=False)
                    alt_installed = alt_installed or is_installed
                    if alt_installed:
                        break
                if not alt_installed:
                    exit_with_error(_('Could not resolve alternative depends %s for package %s') % (
                                    depstr, pkgname))

    @staticmethod
    def build_package(tmpdirpath, logdir, jobs, options):
        dirpath = Debhelper.get_build_dir(tmpdirpath)
        os.chdir(dirpath)
        options = options % jobs if options is not None else str()
        log_file = '%s/%s.log' % (logdir, os.path.basename(dirpath))
        command = 'sudo -u %s %s dpkg-buildpackage' \
                  % (BUILD_USER, options)
        logging.info(_('Package building %s ...') % os.path.basename(dirpath))
        try:
            logstream = open(log_file, mode='w')
        except OSError as e:
            exit_with_error(_('Error opening logfile: %s') % e)
        logstream.write('\n\nCommand: %s' % command)
        start = datetime.datetime.now()
        proc = subprocess.Popen(command, stdout=logstream, stderr=logstream,
                                universal_newlines=True, shell=True)
        proc.communicate()
        end = datetime.datetime.now() - start
        logstream.write('\nReturncode: %d' % proc.returncode)
        logstream.write('\nBuilding time: %s\n' % time.strftime('%H:%M:%S', time.gmtime(end.seconds)))
        logstream.close()
        returncode = proc.returncode
        if returncode:
            if returncode not in DPKG_IGNORED_CODES:
                exit_with_error(_('Package %s building is failed: Command %s return exit code %d') % (
                    os.path.basename(dirpath),
                    command,
                    returncode))
        os.chdir(CURDIR)

    @staticmethod
    def copy_debs(tmpdirpath, repopath):
        # Определяем список собранных deb-пакетов
        debs = [f for f in os.listdir(tmpdirpath) if f.endswith('.deb')]
        Debhelper.copy_files(tmpdirpath, repopath, debs)

    @staticmethod
    def find_packages_files(mount_point, package_file='Packages.gz'):
        if not os.path.exists(mount_point):
            exit_with_error(_('Path %s does not exists') % mount_point)
        distrs_path = os.path.join(mount_point, 'dists')
        result = []
        for root, dirs, files in os.walk(distrs_path):
            if package_file in files:
                result.append(os.path.join(root, package_file))
        return result

    @staticmethod
    def copy_files(srcdir, dstdir, files):
        for f in files:
            src = os.path.join(srcdir, f)
            dst = os.path.join(dstdir, f)
            logging.debug(_('Copying file %s to %s') % (src, dst))
            try:
                shutil.copyfile(src, dst)
            except IOError:
                exit_with_error(_('File %s does not exist') % src)

    @staticmethod
    def get_sources_filelist(conf, package=None, dscfile=None):
        dscfilepath = str()
        if package:
            candidate = package.versions[0]
            package_name, package_ver = candidate.source_name, fix_package_version(candidate.source_version)
            dscfilepath = '%s/%s_%s.dsc' % (conf.srcdirpath, package_name, package_ver)
        else:
            dscfilepath = os.path.join(conf.srcdirpath, dscfile)
        try:
            dscfile = apt.debfile.DscSrcPackage(filename=dscfilepath)
        except apt_pkg.Error as e:
            exit_with_error(e)
        filelist = [os.path.join(conf.srcdirpath, f) for f in dscfile.filelist]
        filelist = [dscfilepath] + filelist
        return tuple(item for item in filelist)


class TemporaryDirManager(object):
    def __init__(self, prefix='buildrepo'):
        self.__dirs = []
        self.__prefix = prefix

    def dirs(self):
        return self.__dirs

    def create(self):
        directory = tempfile.mkdtemp(prefix=self.__prefix)
        try:
            shutil.chown(directory, user=BUILD_USER, group=BUILD_USER)
        except Exception as e:
            logging.warn(_('Failed to change owner for %s: %s') % (directory, e))
        self.__dirs.append(directory)
        return directory

tmpdirmanager = TemporaryDirManager()


class Configuration:
    _instance = None
    _inited = False

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = object.__new__(cls)
        return cls._instance

    def __init__(self, conf_path):
        if not os.path.exists(conf_path) or not os.path.isfile(conf_path):
            exit_with_error(_('Configuration does not found on %s') % conf_path)
        self.conf_path = conf_path
        self.parser = configparser.ConfigParser()
        self.parser.read(conf_path)
        self.root = self.parser.get('common', 'build-root', fallback=DEFAULT_BUILD_DIR)
        self.reponame = self.parser.get('common', 'repo-name', fallback=None)
        self.repoversion = self.parser.get('common', 'repo-version', fallback=None)
        self.__base_init()
        if not self.reponame:
            exit_with_error(_('Repository name is missing in %s') % conf_path)
        if not self.repoversion:
            exit_with_error(_('Repository version is missing in %s') % conf_path)

    def __safe_mkdir(self, directory):
        try:
            if not os.path.exists(directory):
                os.mkdir(directory)
        except Exception as e:
            exit_with_error(_('Failed to create directory %s: %s') % (directory, e))

    def __base_init(self):
        if self._inited:
            return
        self.__safe_mkdir(self.root)
        for subdir in ['src', 'repo', 'data', 'logs',
                       'cache', 'fsrc', 'frepo', 'frepodev', 'iso']:
            setattr(self, '%sdirpath' % subdir, os.path.join(self.root, subdir))
        self.packageslistpath = os.path.join(self.datadirpath, 'packageslist.txt')
        self.__init_logger()
        self._inited = True

    def __init_logger(self):
        self.__safe_mkdir(self.logsdirpath)
        logging.basicConfig(level=logging.DEBUG,
                            format='%(asctime)-8s %(levelname)-8s %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S',
                            filename='%s/buildrepo.log' % self.root,
                            filemode='a')
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        formatter = logging.Formatter('%(levelname)-8s: %(message)s')
        console.setFormatter(formatter)
        logging.getLogger('').addHandler(console)
        # Проверяем наличие прав суперпользователя
        check_root_access()
        logging.info('*****' * 6)
        logging.info(_('Running {} ...').format(' '.join(sys.argv)))
        logging.info(_('Using build-root as {}').format(self.root))
        logging.info('*****' * 6)


class BaseCommand:
    cmd = None

    def __init__(self, conf):
        if not os.path.exists(os.path.dirname(conf)):
            os.makedirs(os.path.dirname(conf))
        self._conf = Configuration(conf)
        Debhelper.base_init()

    def run(self):
        raise NotImplementedError()


class RepoInitializer(BaseCommand):
    cmd = COMMAND_INIT
    """
    Класс выполняет подготовку при инициализации репозитория
    """
    def __init__(self, conf_path):
        super().__init__(conf_path)

    def __init_build_dirs(self):
        """
        Создает директории в корневой директории
        """
        def make_dir(directory):
            if os.path.exists(directory):
                shutil.rmtree(directory)
            os.mkdir(directory)
            logging.debug(_('Creating directory %s') % directory)

        for _directory in [self._conf.srcdirpath, self._conf.datadirpath,
                           self._conf.repodirpath, self._conf.logsdirpath,
                           self._conf.cachedirpath, self._conf.fsrcdirpath,
                           self._conf.frepodirpath, self._conf.frepodevdirpath,
                           self._conf.isodirpath]:
            make_dir(_directory)

    def __init_packages_list(self):
        """
        Записывает в файл список пакетов системы
        """
        packagelist_file = open(self._conf.packageslistpath, mode='w')
        packagelist_file.writelines([line + '\n' for line in Debhelper.get_packages_list()])
        packagelist_file.close()
        logging.info(_('Creating package list of system in file %s') % self._conf.packageslistpath)

    def __init_repo(self):
        repofile = '/etc/apt/sources.list.d/%s.list' % self._conf.reponame
        repo_path = self._conf.repodirpath
        with open(repofile, mode='w') as fp:
            content = 'deb "file://%s" %s/' % (os.path.abspath('%s/..' % repo_path),
                                               os.path.basename(repo_path))
            fp.write(content)
        logging.info(_('Repo file %s is created with content: %s') % (repofile, content))
        Debhelper.generate_packages_list(repo_path, ignore_errors=True)

    def run(self):
        """
        Основная функция для инициализации репозитория
        """
        # Создаем директории
        self.__init_build_dirs()
        # Инициализируем список пакетов
        self.__init_packages_list()
        # Инициализация репозитория
        self.__init_repo()


class Builder(BaseCommand):
    cmd = COMMAND_BUILD

    class PackageData:
        def __init__(self, name, version=None, options=None):
            self.name = name
            self.version = version
            self.options = options

        def __repr__(self):
            return '%s: %s %s' % (self.name, self.version)

    class Scenario:
        __NAME_TAG = '# name:'
        __COMMENT_TAG = '#'
        __BUILD_OPTIONS_TAG = 'options='
        __BUILD_VERSION = 'version='
        __BUILD_OPTIONS_NONE = 'None'

        def __init__(self, scenario_path):
            self.scenario_path = scenario_path
            self.packages = []
            self.__parse_scenario()

        def __parse_scenario(self):
            with open(self.scenario_path, mode='r') as scenario:
                for line in scenario.readlines():
                    if line.endswith('\n'):
                        line = line.rstrip('\n')
                    if not line or line.startswith(self.__COMMENT_TAG):
                        continue
                    else:
                        tokens = [e for e in line.split(' ') if not e.isspace()]
                        # Нашли и пакет, и версию
                        name = str()
                        version = None
                        options = STANDART_BUILD_OPTIONS_TEMPLATE
                        for t in tokens:
                            if tokens.index(t) == 0:
                                name = t
                            elif t.startswith(self.__BUILD_VERSION):
                                version = t.split(self.__BUILD_VERSION)[0]
                            elif t.startswith(self.__BUILD_OPTIONS_TAG):
                                options = t.replace(self.__BUILD_OPTIONS_TAG, '')
                                if options == self.__BUILD_OPTIONS_NONE:
                                    options = None
                        package_data = Builder.PackageData(name, version, options)
                        self.packages.append(package_data)
            logging.info(_('Following packages will be built: \n%s') %
                         '\n'.join([p.name for p in self.packages]))

    """
    Класс выполняет сборку пакетов
    """
    def __init__(self, conf_path):
        super().__init__(conf_path)
        scenario_path = self._conf.parser.get(Builder.cmd, 'source-list', fallback=None)
        if not scenario_path:
            exit_with_error(_('Source list does not specified in %s') % self._conf.conf_path)
        elif not os.path.exists(scenario_path):
            exit_with_error(_('File %s does not exist') % scenario_path)
        self.__clean = self._conf.parser.getboolean(Builder.cmd, 'clean', fallback=False)
        self.__jobs = self._conf.parser.getint(Builder.cmd, 'jobs', fallback=2)
        self.__force_rebuild = self._conf.parser.getboolean(Builder.cmd, 'force-rebuild', fallback=False)
        self.__scenario = self.Scenario(scenario_path)

    def __make_clean(self):
        logging.info(_('Package cleaning before building...'))
        init_packages_list = [p.rstrip('\n')
                              for p in open(self._conf.packageslistpath, mode='r').readlines()
                              if p.endswith('\n')]
        current_package_list = Debhelper.get_packages_list()
        diff = [item for item in current_package_list if item not in init_packages_list]
        if len(diff):
            for package in diff:
                logging.debug(_('Removing package %s') % package)
                p = cache.get(package)
                if not p:
                    exit_with_error(_('Failed to get package %s from cache') % package)
                p.mark_delete()
            try:
                cache.commit()
            except Exception as e:
                exit_with_error(_('Failed to remove packages: %s') % e)

    def __make_build(self):
        def copy_files_to_builddir(dscfilepath, tmpdirpath):
            try:
                files = Debhelper.get_sources_filelist(self._conf, dscfile=dscfilepath)
            except IndexError:
                exit_with_error(_('Failed determine files to copy from %s') % os.path.basename(dscfilepath))
            # Копируем файлы во временную директорию
            for file in files:
                dst = os.path.join(tmpdirpath, os.path.basename(file))
                try:
                    shutil.copyfile(file, dst)
                    shutil.chown(dst, user=BUILD_USER, group=BUILD_USER)
                except Exception as e:
                    exit_with_error(e)

        def check_is_building_required(package_data):
            reg_dsc = DSC_RE % package_data.name
            reg_dsc = fix_re(reg_dsc)
            dsc_files = [f for f in os.listdir(self._conf.srcdirpath) if re.search(reg_dsc, f)]
            versions = [re.match(reg_dsc, v).group('version') for v in dsc_files]
            if not len(versions) == 1 and package_data.version is None:
                if len(versions) == 0:
                    exit_with_error(_('Could not find source files of package %s') % package_data.name)
                exit_with_error(_('There are %d versions of package %s: %s') % (
                    len(versions),
                    package_data.name,
                    ', '.join(versions)))
            dscfilepath = os.path.join(self._conf.srcdirpath, dsc_files[0])
            try:
                dscfile = apt.debfile.DscSrcPackage(filename=dscfilepath)
            except apt_pkg.Error as e:
                exit_with_error(e)
            version = dscfile['Version']
            for binary in dscfile.binaries:
                package = cache.get(binary)
                if not package or not package.candidate.version == version:
                    logging.debug(_('Source package %s will be builded because missing binaries') % package_data.name)
                    return (dscfilepath, True)
            if not self.__force_rebuild:
                logging.info(_('Package %s already builded, spipped') % package_data.name)
                return (dscfilepath, False)
            else:
                logging.info(_('Rebuilding package %s') % package_data.name)
                return (dscfilepath, True)

        def copy_debs_files_to_repodir(package_data):
            version = package_data.version
            files = []
            search = None
            if version is None:
                search = package_data.name
            else:
                search = '%s_%s' % (package_data.name, package_data.version)
            files = [file_name for file_name in os.listdir(self._conf.debsdirpath)
                     if file_name.startswith(search) and file_name.endswith('.deb')]
            Debhelper.copy_files(self._conf.debsdirpath, self._conf.repodirpath, files)

        logging.info(_('Building packages from %s ...') % self.__scenario.scenario_path)
        for package_data in self.__scenario.packages:
            dscfile, need_building = check_is_building_required(package_data)
            if need_building:
                tmpdirpath = tmpdirmanager.create()
                Debhelper.run_command('chown -R %s.%s %s' % (BUILD_USER, BUILD_USER, tmpdirpath))
                # Копируем исходники из src во временную директорию
                copy_files_to_builddir(dscfile, tmpdirpath)
                # Распаковываем пакет
                Debhelper.extract_sources(tmpdirpath, package_data.name)
                # Определяем зависимости
                Debhelper.install_build_depends(tmpdirpath, package_data.name)
                # Запускаем сборку
                Debhelper.build_package(tmpdirpath, self._conf.logsdirpath, self.__jobs, package_data.options)
                # Копируем *.deb в репозиторий
                Debhelper.copy_debs(tmpdirpath, self._conf.repodirpath)
                # Обновляем репозиторий
                Debhelper.generate_packages_list(self._conf.repodirpath)
                # Обновляем кэш
                cache.update()

    def run(self):
        if self.__clean:
            self.__make_clean()
        self.__make_build()


class PackageType:
    (PACKAGE_BUILDED,
     PACKAGE_FROM_OS_REPO,
     PACKAGE_FROM_OS_DEV_REPO,
     PACKAGE_NOT_FOUND) = range(0, 4)


class RepoMaker(BaseCommand):
    cmd = COMMAND_MAKE_REPO

    class IsoRepositoryMaker:
        def __init__(self, name, version, is_dev):
            self.__directory = tmpdirmanager.create()
            self.__name = name
            self.__codename = self.get_codename()
            self.__version = version
            self.__is_dev = is_dev
            self.__base_init()

        def __get_arch(self):
            if platform.machine() == 'x86_64':
                return 'amd64'
            exit_with_error('Unexpected machine: %s' % platform.machine())

        @staticmethod
        def get_codename():
            release = Debhelper.run_command_with_output('lsb_release -c -s')
            return release

        def __base_init(self):
            conf_directory = os.path.join(self.__directory, 'conf')
            if not os.path.exists(conf_directory):
                os.mkdir(conf_directory)
            with open(os.path.join(conf_directory, 'distributions'), mode='w') as fp:
                fp.write('Codename: %s\n' % self.__name)
                fp.write('Version: %s\n' % self.__version)
                fp.write('Description: %s repository\n' % self.__name)
                fp.write('Architectures: %s\n' % self.__get_arch())
                fp.writelines(['Components: main contrib non-free\n',
                               'DebIndices: Packages Release . .gz .bz2\n',
                               'Contents: . .gz .bz2\n'])
            try:
                os.chdir(self.__directory)
                Debhelper.run_command('reprepro export')
            except subprocess.CalledProcessError:
                exit_with_error(_('Reprepro initialization failed'))
            finally:
                os.chdir(CURDIR)
            disk_directory = os.path.join(self.__directory, '.disk')
            if not os.path.exists(disk_directory):
                os.mkdir(disk_directory)
            with open(os.path.join(disk_directory, 'info'), mode='w') as fp:
                fp.write('%s %s (%s) - %s DVD\n' % ('%s-devel' % self.__name
                                                    if self.__is_dev else self.__name,
                                                    self.__version,
                                                    self.__codename,
                                                    self.__get_arch()))

        def mkiso(self, conf):
            try:
                os.chdir(self.__directory)
                logging.info(_('Creating repository for %s via reprepro ...') % (
                             '%s-dev' % self.__name if self.__is_dev else self.__name))
                packagedir = conf.frepodevdirpath if self.__is_dev else conf.frepodirpath
                Debhelper.run_command('reprepro includedeb %s %s/*.deb' % (self.__name, packagedir))
                # Удаление ненужных директорий
                for directory in ['db', 'conf']:
                    shutil.rmtree(directory)
                now = datetime.datetime.now().strftime('%Y-%m-%d')
                isoname = '%s_%s_%s_%s.iso' % (self.__name, self.__version, self.__codename, now)
                if self.__is_dev:
                    isoname = 'devel-%s' % isoname
                isopath = os.path.join(conf.isodirpath, isoname)
                label = '%s %s (%s) %s' % (self.__name, self.__version, self.__codename, self.__get_arch())
                os.chdir(os.path.join(self.__directory, '..'))
                logging.info(_('Building iso %s for %s ...') % (isopath, self.__name))
                Debhelper.run_command('genisoimage -r -J -o %s -V "%s" %s' % (isopath,
                                                                              label,
                                                                              self.__directory))
            except Exception as e:
                exit_with_error(_('Failed to make iso: %s') % e)
            finally:
                os.chdir(CURDIR)

    class DependencyFinder:
        def __init__(self, package, caches, conf, exclude_rules=None, black_list=[]):
            self.deps = list()
            self.__caches = caches
            self.__package = cache.get(package)
            self.__exclude_rules = exclude_rules
            self.__black_list = black_list
            self.__conf = conf
            if self.__package is None:
                exit_with_error(_('Package %s does not exists') % package)
            self.deps.append((self.__package.name, self.__package,
                              self.__get_package_repository(self.__package, self.__package.name)))
            self.__deps_recurse(self.deps, self.__package)

        def __get_package_repository(self, package, required_by):
            package_name, package_ver = package.name, package.versions[0].version
            for cache in self.__caches:
                cache_name = cache[DIRECTIVE_CACHE_NAME]
                for p in cache[DIRECTIVE_CACHE_PACKAGES]:
                    if p['name'] == package_name and p['version'] == package_ver:
                        logging.debug(_('%s -> %s(%s) found in %s repo') % (required_by,
                                                                            package_name,
                                                                            package_ver,
                                                                            cache_name))
                        return cache[DIRECTIVE_CACHE_TYPE]
            return PackageType.PACKAGE_NOT_FOUND

        def __process_exclude_filters(self, s, p):
            if self.__exclude_rules is not None:
                candidate = p.versions[0]
                package_name, package_ver = candidate.source_name, fix_package_version(candidate.source_version)
                dscfilepath = '%s/%s_%s.dsc' % (self.__conf.srcdirpath, package_name, package_ver)
                try:
                    dscfile = apt.debfile.DscSrcPackage(filename=dscfilepath)
                    for binary in dscfile.binaries:
                        skip_package = False
                        for exl in self.__exclude_rules:
                            if binary.endswith(exl):
                                skip_package = True
                                break
                        if not skip_package:
                            paddition = cache.get(binary)
                            if not paddition:
                                exit_with_error(_('Package %s does not exists') % paddition)
                            # Black list
                            if binary not in self.__black_list:
                                item = (paddition.name, paddition,
                                        self.__get_package_repository(paddition, paddition))
                                if item not in s:
                                    s.append(item)
                                    self.__deps_recurse(s, paddition)
                            else:
                                logging.info(_('Package %s skipped because blacklist rule') % binary)
                        else:
                            logging.debug(_('Package %s skipped because \'%s\' rule') % (
                                binary, ' '.join(self.__exclude_rules)))
                except apt_pkg.Error as e:
                    exit_with_error(e)

        def __deps_recurse(self, s, p):
            deps = p.candidate.get_dependencies('Depends')
            pre_deps = p.candidate.get_dependencies('PreDepends')
            all_deps = deps + pre_deps
            pdest = None
            for i in all_deps:
                dp = i.target_versions
                if len(dp) > 0:
                    package = dp[0].package
                    pdest = self.__get_package_repository(package, p.name)
                    item = (p.name, package, pdest)
                    if item not in s:
                        s.append(item)
                        self.__deps_recurse(s, package)
                else:
                    # Пакета нет в кэше
                    depname = str(i).split(':')[1].strip()
                    pdest = PackageType.PACKAGE_NOT_FOUND
                    s.append((p.name, depname, PackageType.PACKAGE_NOT_FOUND,))
            if pdest == PackageType.PACKAGE_BUILDED:
                self.__process_exclude_filters(s, p)

    _DEFAULT_DEV_PACKAGES_SUFFIXES = ['dbg', 'dbgsym', 'doc', 'dev']

    def __init__(self, conf_path):
        super().__init__(conf_path)
        self.__white_list_path = self._conf.parser.get(RepoMaker.cmd, 'white-list', fallback=None)
        self.__no_create_iso = self._conf.parser.getboolean(RepoMaker.cmd, 'no-create-iso', fallback=False)
        self.__dev_packages_suffixes = self._conf.parser.get(RepoMaker.cmd, 'dev-package-suffixes',
                                                             fallback=RepoMaker._DEFAULT_DEV_PACKAGES_SUFFIXES)
        if not self.__white_list_path:
            exit_with_error(_('White list does not specified in %s') % self._conf.conf_path)
        if not os.path.exists(self.__white_list_path):
            exit_with_error(_('File %s does not exist') % self.__white_list_path)
        if isinstance(self.__dev_packages_suffixes, str):
            self.__dev_packages_suffixes = [item.strip() for item in self.__dev_packages_suffixes.split(',')]
        logging.info(_('Using %s rule for packages for 2nd disk') % ', '.join(self.__dev_packages_suffixes))
        self.__packages = {}
        self.__caches = []
        self.__sources = {}
        self.__build_cache_of_builded_packages()
        self.__load_caches()
        self.__parse_white_list()

    def __parse_white_list(self):
        i = 1
        last_section = None
        for line in open(self.__white_list_path, mode='r').readlines():
            i += 1
            if line.startswith('#') or line == '\n':
                continue
            line = line.rstrip('\n')
            if line.startswith('[') and line.endswith(']'):
                last_section = line[1:-1]
                self.__packages[last_section] = []
            else:
                if last_section is None:
                    exit_with_error(_('Got package at line %d, '
                                      'but section expected') % i)
                packages = self.__packages.get(last_section)
                if line in packages:
                    logging.warn(_('Package %s already in %s, skipped'), line, last_section)
                    continue
                packages.append(line)
                self.__packages[last_section] = packages
        if 'target' not in self.__packages:
            exit_with_error(_('White list for target repository is empty'))
        # Проверка на пересечение
        all_pkgs = set()
        for section, packages in self.__packages.items():
            if not len(all_pkgs):
                all_pkgs = set(packages)
                continue
            if (all_pkgs & set(packages)):
                exit_with_error(_('Intersection is found in lists'))

    def __build_cache_of_builded_packages(self):
        logging.info(_('Build cache for builded packages ...'))
        maker = PackageCacheMaker(self._conf.conf_path)
        maker.run(mount_path=self._conf.repodirpath,
                  name='builded',
                  primary=False,
                  is_builded=True)

    def __load_caches(self):
        files = [f for f in os.listdir(self._conf.cachedirpath) if f.endswith('.cache')]
        if len(files) <= 1:
            exit_with_error(_('No one cache is created'))
        for f in files:
            path = os.path.join(self._conf.cachedirpath, f)
            with open(path, mode='r') as json_data:
                self.__caches.append(json.load(json_data))
        os_repo_exists = any([cache[DIRECTIVE_CACHE_TYPE] == PackageType.PACKAGE_FROM_OS_REPO
                             for cache in self.__caches])
        if not os_repo_exists:
            exit_with_error(_('Cache for OS repo is needed'))
        os_dev_repo_exists = any([cache[DIRECTIVE_CACHE_TYPE] == PackageType.PACKAGE_FROM_OS_DEV_REPO
                                 for cache in self.__caches])
        if not os_dev_repo_exists:
            exit_with_error(_('Cache for OS dev repo is needed'))
        self.__caches = sorted(self.__caches, key=lambda c: c[DIRECTIVE_CACHE_TYPE])

    def __get_depends_for_package(self, package, exclude_rules=None, black_list=None):
        depfinder = self.DependencyFinder(package,
                                          self.__caches,
                                          self._conf,
                                          exclude_rules, black_list)
        return depfinder.deps

    def run(self):
        def get_deb_dev_to_copy(pkgs):
            filenames = set()
            for pkg in pkgs:
                p = pkg[1]
                if self._conf.repodirpath in p.versions[0].uris[0]:
                    filenames.add(p.versions[0].filename)
            return filenames

        # Подготовка к созданию репозитория - очистка директорий
        for directory in [self._conf.frepodirpath,
                          self._conf.frepodevdirpath,
                          self._conf.fsrcdirpath]:
            logging.debug(_('Clearing %s') % directory)
            for file in os.listdir(directory):
                os.remove(os.path.join(directory, file))
        logging.info(_('Processing target repository ...'))
        # Анализ пакетов основного репозитория
        target_builded_deps = set()
        sources = dict()
        for required in self.__packages['target']:
            logging.info(_('Processing %s ...') % required)
            deps = self.__get_depends_for_package(required,
                                                  exclude_rules=self.__dev_packages_suffixes,
                                                  black_list=self.__packages.get('target-dev', []))
            unresolve = [d for d in deps if d[2] == PackageType.PACKAGE_NOT_FOUND]
            deps_in_dev = [d for d in deps if d[2] == PackageType.PACKAGE_FROM_OS_DEV_REPO]
            if len(unresolve):
                for p in unresolve:
                    pkg = p[1]
                    if isinstance(pkg, str):
                        depstr = pkg
                    else:
                        depstr = _('%s version %s') % (pkg.name, pkg.versions[0].version)
                    logging.error(_('Could not resolve %s for %s: %s') %
                                   ('dependency' if p[0] == required else 'subdependency',
                                    required, depstr))
                exit_with_error(_('Could not resolve dependencies'))
            if len(deps_in_dev):
                for p in deps_in_dev:
                    pkg = p[1]
                    package_name, package_ver = pkg.name, pkg.versions[0].version
                    logging.error(_('%s %s (%s) for %s is founded in os-dev repo') %
                                   ('Dependency' if p[0] == required else 'Subdependency',
                                    package_name, package_ver, p[0]))
                exit_with_error(_('Could not resolve dependencies'))
            target_deps = [d for d in deps if d[2] == PackageType.PACKAGE_BUILDED]
            for p in target_deps:
                package = p[1]
                if package.name in sources.keys():
                    continue
                package_sources = Debhelper.get_sources_filelist(self._conf, package=package)
                sources[package.name] = package_sources
            files_to_copy = set()
            # Hack:
            # Поскольку в собираемом репозитории может быть пакет с той же версией,
            # что и в DEV репозиториии ОС, то нам требуется определить путь к пакету
            # из собираемого репозитория.
            for item in target_deps:
                p = item[1]
                for version in p.versions:
                    if self._conf.repodirpath in version.uri:
                        files_to_copy.add(version.filename)
                        break
            target_builded_deps.update(files_to_copy)
            logging.debug(_('Copying dependencies for package %s: %s') % (required, files_to_copy))
            for f in files_to_copy:
                src = os.path.join(self._conf.root, f)
                dst = os.path.join(self._conf.frepodirpath, os.path.basename(f))
                try:
                    logging.debug(_('Copying %s to %s') % (src, dst))
                    shutil.copyfile(src, dst)
                except Exception as e:
                    exit_with_error(e)
        logging.info(_('Processing dev repository ...'))
        # Определяем репозиторий со средствами разработки -
        # все пакеты из сборочного репозитория за вычетом всех, указанных в target
        dev_packages = []
        for f in os.listdir(self._conf.repodirpath):
            m = re.match(DEB_RE, f)
            if m:
                package_name = m.group('name')
                dev_packages.append(package_name)
        dev_packages = sorted([p for p in set(dev_packages) - set(self.__packages['target'])])
        for devpkg in dev_packages:
            logging.info(_('Processing %s ...') % devpkg)
            deps = self.__get_depends_for_package(devpkg)
            unresolve = [d for d in deps if d[2] == PackageType.PACKAGE_NOT_FOUND]
            if len(unresolve):
                for p in unresolve:
                    pkg = p[1]
                    if isinstance(pkg, str):
                        depstr = pkg
                    else:
                        depstr = _('%s version %s') % (pkg.name, pkg.versions[0].version)
                    logging.error(_('Could not resolve %s for %s: %s') %
                                   ('dependency' if p[0] == required else 'subdependency',
                                    devpkg, depstr))
                exit_with_error(_('Could not resolve dependencies'))
            builded = [d for d in deps if d[2] == PackageType.PACKAGE_BUILDED]
            files_to_copy = get_deb_dev_to_copy(builded)
            intersection = files_to_copy & target_builded_deps
            # Исключаем пересечения с основным репозиторием
            files_to_copy -= intersection
            for package in [p[1] for p in builded]:
                if package.name in sources.keys():
                    continue
                package_sources = Debhelper.get_sources_filelist(self._conf, package)
                sources[package.name] = package_sources
            logging.debug(_('Copying dependencies for package %s: %s') % (devpkg, files_to_copy))
            for f in files_to_copy:
                src = os.path.join(self._conf.root, f)
                dst = os.path.join(self._conf.frepodevdirpath, os.path.basename(f))
                try:
                    logging.debug(_('Copying %s to %s') % (src, dst))
                    shutil.copyfile(src, dst)
                except Exception as e:
                    exit_with_error(e)
        # Копируем исходники для разрешенных репозиториев
        # Обращаем ключи словаря
        reversed_sources = {}
        for key, value in sources.items():
            if value not in reversed_sources.keys():
                reversed_sources[value] = [key]
            else:
                reversed_sources[value].append(key)
        for sourcelist, packages in reversed_sources.items():
            packages = list(set(packages))
            logging.info(_('Copying sources for package(s) %s ...') % ', '.join(packages))
            for source in sourcelist:
                dst = os.path.join(self._conf.fsrcdirpath, os.path.basename(source))
                try:
                    logging.debug(_('Copying %s to %s') % (source, dst))
                    shutil.copyfile(source, dst)
                except Exception as e:
                    exit_with_error(e)
        if self.__no_create_iso:
            return
        # Создаем репозиторий (main и dev)
        for is_dev in (False, True):
            iso_maker = self.IsoRepositoryMaker(self._conf.reponame, self._conf.repoversion, is_dev)
            iso_maker.mkiso(self._conf)
        # Формируем образ диска с исходниками
        tmpdir = tmpdirmanager.create()
        try:
            # Копируем исходники
            shutil.copytree(self._conf.fsrcdirpath, os.path.join(tmpdir, 'src'))
            # Копируем списки и текущий скрипт
            script_dir = os.path.dirname(sys.argv[0])
            for file in os.listdir(script_dir):
                if os.path.isfile(file):
                    shutil.copyfile(os.path.join(script_dir, file),
                                    os.path.join(tmpdir, file))
            os.chdir(os.path.join(tmpdir, '..'))
            now = datetime.datetime.now().strftime('%Y-%m-%d')
            isoname = 'sources-%s_%s_%s_%s.iso' % (self._conf.reponame, self._conf.repoversion,
                                                   self.IsoRepositoryMaker.get_codename(), now)
            isopath = os.path.join(self._conf.isodirpath, isoname)
            label = '%s %s (sources)' % (self._conf.reponame, self._conf.repoversion)
            logging.info(_('Building sources iso %s for %s ...') % (isopath, self._conf.reponame))
            Debhelper.run_command('genisoimage -r -J -o %s -V "%s" %s' % (isopath,
                                                                          label,
                                                                          tmpdir))
        except Exception as e:
            exit_with_error(_('Failed to create source iso: %s') % e)
        finally:
            os.chdir(CURDIR)


class PackageCacheMaker(BaseCommand):
    cmd = COMMAND_MAKE_PACKAGE_CACHE
    args = (
                ('--mount-path', {'required': True, 'help': _('Set path to repo\'s mount point')}),
                ('--name', {'required': True, 'help': _('Set package name of repo')}),
                ('--primary', {'required': False, 'default': False, 'action': 'store_true',
                 'help': _('Is primary repo?')})
           )
    __DIRECTIVE_PACKAGE = 'Package: '
    __DIRECTIVE_VERSION = 'Version: '
    __DIRECTIVE_DESCRIPTION_ENDS = ''

    def run(self, mount_path, name, primary, is_builded=False):
        if not is_builded:
            packages_path = Debhelper.find_packages_files(mount_path)
        else:
            packages_path = [os.path.join(mount_path, 'Packages')]
        if not len(packages_path):
            exit_with_error(_('Can\'t find any Packages files in %s') % mount_path)
        cache_file_path = '%s/%s.cache' % (self._conf.cachedirpath, name)
        if is_builded:
            cache_type = PackageType.PACKAGE_BUILDED
        elif primary:
            cache_type = PackageType.PACKAGE_FROM_OS_REPO
        else:
            cache_type = PackageType.PACKAGE_FROM_OS_DEV_REPO
        result = {DIRECTIVE_CACHE_NAME: name,
                  DIRECTIVE_CACHE_TYPE: cache_type}
        packages = []
        for path in packages_path:
            try:
                with gzip.open(path, mode='rb') as gfile:
                    content = gfile.read().decode('utf-8', 'ignore')
                    lines = content.split('\n')
            except OSError:
                with open(path, mode='r') as fp:
                    lines = [line.rstrip('\n') for line in fp.readlines()]
            version = str()
            package_name = str()
            version = str()
            for line in lines:
                if line.startswith(self.__DIRECTIVE_PACKAGE):
                    package_name = line.split(self.__DIRECTIVE_PACKAGE)[1]
                elif line.startswith(self.__DIRECTIVE_VERSION):
                    version = line.split(self.__DIRECTIVE_VERSION)[1]
                elif line == self.__DIRECTIVE_DESCRIPTION_ENDS:
                    data = {
                        DIRECTIVE_CACHE_PACKAGES_PACKAGE_NAME: package_name,
                        DIRECTIVE_CACHE_PACKAGES_PACKAGE_VERSION: version
                    }
                    packages.append(data)
        result[DIRECTIVE_CACHE_PACKAGES] = packages
        with open(cache_file_path, mode='w') as out:
            out.write(json.dumps(result, sort_keys=True, indent=4))
        return result


class RemoveSourceCmd(BaseCommand):
    cmd = COMMAND_REMOVE_SOURCES
    args = (
                ('--package', {'required': True, 'help': _('Source package name to be removed')}),
                ('--remove-orig', {'dest': 'remove_orig', 'action': 'store_true',
                 'default': False, 'help': _('Remove *.orig.tar.* source file, default: False')})
           )

    def run(self, package, remove_orig=False):
        expr = '%s/%s_*.dsc' % (self._conf.srcdirpath, package)
        sources = glob.glob(expr)
        if not len(sources):
            exit_with_error(_('No sources are found'))
        sys.stdout.write(_('The following sources are found:\n'))
        dscfiles = {num + 1: source for (num, source) in enumerate(sources)}
        for num, dsc in enumerate(sources):
            sys.stdout.write('%d\t%s\n' % (num + 1, os.path.basename(dsc)))
        while True:
            try:
                choice = int(input(_('\nChoose source to be removed:\n')))
            except ValueError:
                continue
            if choice not in dscfiles:
                continue
            dscfilepath = dscfiles.get(choice)
            break
        dscfilepath = os.path.join(self._conf.srcdirpath, dscfilepath)
        dscfile = apt.debfile.DscSrcPackage(filename=dscfilepath)
        sources = [dscfilepath] + [os.path.join(self._conf.srcdirpath, source)
                                   for source in dscfile.filelist]
        if not remove_orig:
            orig = None
            for source in sources:
                if re.match('.*\\.orig\\..*', source):
                    orig = source
                    break
            if orig:
                sources.remove(orig)
        binaries = []
        pver = dscfile['Version']
        for binary in dscfile.binaries:
            expr = '%s/%s_%s*deb' % (self._conf.repodirpath, binary, pver)
            binaries = binaries + glob.glob(expr)
        logging.info(_('The following sources will be removed: %s' % ', '.join(sources)))
        if len(binaries):
            logging.info(_('The following binaries will be removed: %s:' % ', '.join(binaries)))
        while True:
            answer = input(_('Do you want to continue? (yes/NO): '))
            if not len(answer) or answer == _('NO'):
                logging.info(_('Operation was cancelled by user'))
                exit(0)
            elif answer == _('yes'):
                break
        for f in sources + binaries:
            try:
                logging.info(_('Removing %s ...') % f)
                os.remove(f)
            except OSError as e:
                exit_with_error(_('Failed to remove file %s: %s' % (f, e)))
        Debhelper.generate_packages_list(self._conf.repodirpath)


def make_default_subparser(main_parser, command):
    parser = main_parser.add_parser(command)
    parser.add_argument('--config', required=False,
                        default=DEFAULT_CONF,
                        help=_('Buildrepo config path (default: %s)') % DEFAULT_CONF)
    return parser


def available_commands():
    """
    Возвращает словарь вида {CMD: (cls, tuple)}
    cmd -- имя команды, cls -- класс и tuple -- кортеж аргументов
    """
    import inspect

    def command_predicate(obj):
        if inspect.isclass(obj):
            cmd = getattr(obj, 'cmd', None)
            return cmd is not None
        return False

    return {tup[1].cmd: (tup[1], getattr(tup[1], 'args', None))
            for tup in inspect.getmembers(sys.modules[__name__], command_predicate)}

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='command')
    cmdmap = available_commands()

    for cmd in sorted(cmdmap.keys()):
        cls, cmdargs = cmdmap.get(cmd)
        subparser = make_default_subparser(subparsers, cmd)
        if cmdargs:
            for cmdarg in cmdargs:
                arg, kwargs = cmdarg
                subparser.add_argument(arg, **kwargs)

    args = parser.parse_args()
    try:
        conf = os.path.abspath(args.config)
    except AttributeError:
        parser.print_help()
        exit(1)
    try:
        def rm_tmp_dirs():
            for directory in tmpdirmanager.dirs():
                if os.path.exists(directory):
                    shutil.rmtree(directory)

        atexit.register(rm_tmp_dirs)
        cmdargs = {}
        for arg in dir(args):
            if (not arg.startswith('_') and arg not in ('command', 'config')):
                cmdargs[arg] = getattr(args, arg)
        cmdtuple = cmdmap.get(args.command)
        if cmdtuple:
            cls, *other = cmdtuple
            cls(args.config).run(**cmdargs)
        else:
            parser.print_help()
    except KeyboardInterrupt:
        logging.info(_('Exit on user\'s query'))
