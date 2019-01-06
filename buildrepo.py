#!/usr/bin/python3

import logging
import re
import json
import gettext
import os
import gzip
import apt
import apt.debfile
import apt_pkg
import pwd
import shutil
import argparse
import subprocess
import tempfile
import platform
import atexit
import datetime

CURDIR = os.path.abspath(os.path.curdir)
DEFAULT_REPO_DIR = os.path.abspath('%s/../build' % CURDIR)
REPO_FILE_NAME = '/etc/apt/sources.list.d/build-repo.list'
COMMAND_INIT = 'init'
COMMAND_BUILD = 'build'
COMMAND_MAKE_REPO = 'make-repo'
COMMAND_MAKE_PACKAGE_CACHE = 'make-package-cache'
DEVNULL = open(os.devnull, 'wb')

DEB_RE = '^(?P<name>[\w\-\.]+)_(?P<version>[\w\.\-\~\+]+)_(?P<arch>[\w]+)\.deb$'
DSC_FULL_RE = '^(?P<name>[\w\-\.\+]+)_(?P<version>[\w\.\-\~\+]+)\.dsc$'
DSC_RE = '^%s_(?P<version>[\w\.\-\~\+]+)\.dsc$'
STANDART_BUILD_OPTIONS_TEMPLATE = 'DEB_BUILD_OPTIONS="nocheck parallel=%d"'
DPKG_IGNORED_CODES = [1]

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


class Debhelper:
    """
    Класс для запуска Debian утилит
    """

    @staticmethod
    def run_command_with_output(command, ):
        return subprocess.check_output(command, shell=True, stderr=DEVNULL).decode().rstrip('\n')

    @staticmethod
    def run_command(command, need_output=False):
        if not need_output:
            subprocess.check_call(command, shell=True, stderr=DEVNULL, stdout=DEVNULL)
        else:
            subprocess.check_call(command, shell=True)

    @staticmethod
    def base_init():
        # Проверяем пакеты, которые должны быть уже установлены
        for pname in REQUIRED_PACKAGES:
            package = cache.get(pname)
            if not package:
                exit_with_error(_('Could not get package %s from cache') % pname)
            if not package.installed:
                try:
                    logging.info(_('Installing required package %s ...') % pname)
                    package.mark_install()
                    cache.commit()
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
            path = '%s/%s' % (tmpdirpath, file_name)
            if os.path.isdir(path):
                return path

    @staticmethod
    def install_build_depends(tmpdirpath, pkgname):
        def install_alt_depends(cache, depends):
            depstr = ' | '.join([' '.join([p[0], '(', p[2], p[1], ')'])
                                if len(p[1]) else p[0] for p in depends])
            for alt in depends:
                pname, version, op = alt
                pdep = cache.get(pname)
                if pdep is None:
                    exit_with_error(_('Failed to get package %s from cache') % pname)
                if pdep.is_installed:
                    if len(version) and not apt_pkg.check_dep(pdep.installed.version, op, version):
                        continue
                    logging.info(_('Package %s already installed') % pname)
                    return
                logging.info(_('Installing dependency %s ...') % pname)
                try:
                    pdep.mark_install()
                    cache.commit()
                    return
                except apt_pkg.Error:
                    continue
            # Формируем строку зависимостей
            exit_with_error(_('Could not resolve alternative depends %s for package %s') % (depstr, pkgname))

        try:
            dscfilepath = next(f for f in os.listdir(tmpdirpath) if re.match(DSC_FULL_RE, f))
        except AttributeError:
            exit_with_error(_('dsc file does not exist'))
        dscfile = apt.debfile.DscSrcPackage(filename=os.path.join(tmpdirpath, dscfilepath))
        cache.update()
        # TODO: Проверить по конфликтам
        for dep in dscfile.depends:
            # Обыкновенная зависимость
            try:
                if len(dep) == 1:
                    pname, version, op = dep[0]
                    pdep = cache.get(pname)
                    if pdep is None:
                        exit_with_error(_('Failed to get package %s from cache') % pname)
                    if pdep.is_installed:
                        if len(version):
                            if not apt_pkg.check_dep(pdep.installed.version, op, version):
                                exit_with_error(_('For package %s building requires %s (version %s), '
                                                  'but installed %s') % (pkgname, pname, version,
                                                                         pdep.installed.version))
                        logging.info(_('Package %s already installed') % pname)
                        continue
                    logging.info(_('Installing dependency %s ...') % pname)
                    pdep.mark_install()
                    cache.commit()
                else:
                    # Альтернативные зависимости
                    install_alt_depends(cache, dep)
            except Exception as e:
                exit_with_error(e)

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
        proc = subprocess.Popen(command, stdout=logstream, stderr=logstream,
                                universal_newlines=True, shell=True)
        proc.communicate()
        logstream.write('\nReturncode: %d' % proc.returncode)
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
            exit_with_error(_('Path %s does not exists'), mount_point)
        distrs_path = os.path.join(mount_point, 'dists')
        result = []
        for root, dirs, files in os.walk(distrs_path):
            if package_file in files:
                result.append(os.path.join(root, package_file))
        return result

    @staticmethod
    def copy_files(srcdir, dstdir, files):
        for f in files:
            src = '%s/%s' % (srcdir, f)
            dst = '%s/%s' % (dstdir, f)
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
            package_name, package_ver = candidate.source_name, candidate.source_version
            # TODO: Version hack
            if ':' in package_ver:
                package_ver = package_ver.split(':')[-1]
            dscfilepath = '%s/%s_%s.dsc' % (conf.srcdirpath, package_name, package_ver)
        else:
            dscfilepath = '%s/%s' % (conf.srcdirpath, dscfile)
        try:
            dscfile = apt.debfile.DscSrcPackage(filename=dscfilepath)
        except apt_pkg.Error as e:
            exit_with_error(e)
        filelist = ['%s/%s' % (conf.srcdirpath, f) for f in dscfile.filelist]
        filelist = [dscfilepath] + filelist
        return filelist


class TemporaryDirManager(object):
    def __init__(self, prefix='buildrepo'):
        self.__dirs = []
        self.__prefix = prefix

    # def __del__(self):
    #     for d in self.__dirs:
    #         if os.path.exists(d):
    #             shutil.rmtree(d)

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
    def __init__(self, root):
        self.root = root
        self.srcdirpath = '%s/src' % root
        self.repodirpath = '%s/repo' % root
        self.datadirpath = '%s/data' % root
        self.logdirpath = '%s/logs' % root
        self.cachedirpath = '%s/cache' % root
        self.fsrcdirpath = '%s/fsrc' % root
        self.frepodirpath = '%s/frepo' % root
        self.frepodevdirpath = '%s/frepodev' % root
        self.isodirpath = '%s/iso' % root
        self.packageslistpath = '%s/packageslist.txt' % self.datadirpath

    @staticmethod
    def init_logger(root):
        if not os.path.exists(root):
            os.mkdir(root)
        logging.basicConfig(level=logging.DEBUG,
                            format='%(asctime)s %(name)-12s %(message)s',
                            datefmt='%m-%d %H:%M',
                            filename='%s/buildrepo.log' % root,
                            filemode='a')
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        formatter = logging.Formatter('%(levelname)-8s: %(message)s')
        console.setFormatter(formatter)
        logging.getLogger('').addHandler(console)


class BaseCommand(object):
    def __init__(self, root):
        if not os.path.exists(root):
            os.makedirs(root)
        self._conf = Configuration(root)
        Debhelper.base_init()


class RepoInitializer(BaseCommand):
    """
    Класс выполняет подготовку при инициализации репозитория
    """
    def __init__(self, root):
        super().__init__(root)

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
                           self._conf.repodirpath, self._conf.logdirpath,
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

    def __init_repo(self, repo_filename, repo_path):
        repo_file = open(repo_filename, mode='w')
        content = 'deb "file://%s" %s/' % (os.path.abspath('%s/..' % repo_path),
                                           os.path.basename(repo_path))
        repo_file.write(content)
        repo_file.close()
        logging.info(_('Repo file %s is created with content: %s') % (repo_filename, content))
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
        self.__init_repo(REPO_FILE_NAME, self._conf.repodirpath)


class Builder(BaseCommand):
    class PackageData:
        def __init__(self, name, version=None, options=None):
            self.name = name
            self.version = version
            self.options = options

        def __repr__(self):
            return '%s: %s %s' % (self.name, self.version)

    class Scenario:
        __NAME_TAG = '# Name:'
        __COMMENT_TAG = '#'
        __BUILD_OPTIONS_TAG = 'options='
        __BUILD_VERSION = 'version='
        __BUILD_OPTIONS_NONE = 'None'

        def __init__(self, scenario_path):
            self.scenario_path = scenario_path
            self.name = None
            self.packages = []
            self.__parse_scenario()

        def __parse_scenario(self):
            with open(self.scenario_path, mode='r') as scenario:
                for line in scenario.readlines():
                    if line.endswith('\n'):
                        line = line.rstrip('\n')
                    if not line:
                        continue
                    if line.startswith(self.__NAME_TAG):
                        name = line.split(self.__NAME_TAG)[1]
                        name = name.replace(' ', '')
                        self.name = name
                    elif line.startswith(self.__COMMENT_TAG):
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
                if self.name is None:
                    exit_with_error(_('Scenario name is missing in file %s') % self.scenario_path)
            logging.info(_('Following packages will be built: \n%s') %
                         '\n'.join([p.name for p in self.packages]))

    """
    Класс выполняет сборку пакетов
    """
    def __init__(self, repodirpath, scenario_path, clean, jobs):
        super().__init__(repodirpath)
        if not os.path.exists(scenario_path):
            exit_with_error(_('File %s does not exist') % scenario_path)
        self.__clean = clean
        self.__jobs = jobs
        self.__scenario = self.Scenario(scenario_path)

    def __make_clean(self):
        logging.info(_('Package cleaning before rebuilding...'))
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
        def copy_files_to_builddir(package_data, tmpdirpath):
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
            # Определяем файлы для копирования
            try:
                files = Debhelper.get_sources_filelist(self._conf, dscfile=dsc_files[0])
            except IndexError:
                exit_with_error(_('Failed determine files to copy %s') % package_data.name)
            # Копируем файлы во временную директорию
            for file in files:
                dst = os.path.join(tmpdirpath, os.path.basename(file))
                try:
                    shutil.copyfile(file, dst)
                    shutil.chown(dst, user=BUILD_USER, group=BUILD_USER)
                except Exception as e:
                    exit_with_error(e)

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

        logging.info(_('Executing scenario %s ...') % self.__scenario.name)
        for package_data in self.__scenario.packages:
            tmpdirpath = tmpdirmanager.create()
            Debhelper.run_command('chown -R %s.%s %s' % (BUILD_USER, BUILD_USER, tmpdirpath))
            logging.debug(_('Creating temparary directory %s') % tmpdirpath)
            # Копируем исходники из src во временную директорию
            copy_files_to_builddir(package_data, tmpdirpath)
            # Распаковываем пакет
            Debhelper.extract_sources(tmpdirpath, package_data.name)
            # Определяем зависимости
            Debhelper.install_build_depends(tmpdirpath, package_data.name)
            # Запускаем сборку
            Debhelper.build_package(tmpdirpath, self._conf.logdirpath, self.__jobs, package_data.options)
            # Копируем *.deb в репозиторий
            Debhelper.copy_debs(tmpdirpath, self._conf.repodirpath)
            # Обновляем репозиторий
            Debhelper.generate_packages_list(self._conf.repodirpath)

    def run(self):
        if self.__clean:
            self.__make_clean()
        self.__make_build()


class PackageType:
    (PACKAGE_FROM_OS_REPO,
     PACKAGE_FROM_OS_DEV_REPO,
     PACKAGE_FROM_TARGET_REPO,
     PACKAGE_FROM_TARGET_DEV_REPO,
     PACKAGE_BUILDED,
     PACKAGE_NOT_FOUND) = range(0, 6)


class RepoMaker(BaseCommand):
    class IsoRepositoryMaker:
        def __init__(self, name, version, is_dev):
            self.__directory = tmpdirmanager.create()
            self.__name = name
            self.__version = version
            self.__version = version
            self.__is_dev = is_dev
            self.__base_init()

        def __get_arch(self):
            if platform.machine() == 'x86_64':
                return 'amd64'
            exit_with_error('Unexpected machine: %s' % platform.machine())

        def __get_codename(self):
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
                                                    self.__get_codename(),
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
                isoname = '%s_%s_%s.iso' % (self.__name, self.__version, now)
                if self.__is_dev:
                    isoname = 'devel-%s' % isoname
                isopath = os.path.join(conf.isodirpath, isoname)
                label = '%s %s (%s) %s' % (self.__name, self.__version, self.__get_codename(), self.__get_arch())
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
        def __init__(self, package, caches):
            self.deps = list()
            self.__caches = caches
            self.__package = cache.get(package)
            if self.__package is None:
                exit_with_error(_('Package %s does not exists') % package)
            self.deps.append((self.__package.name, self.__package,
                              self.__get_package_repository(self.__package)))
            self.__deps_recurse(self.deps, self.__package)

        def __get_package_repository(self, package):
            package_name, package_ver = package.name, package.versions[0].version
            for cache in self.__caches:
                cache_name = cache[DIRECTIVE_CACHE_NAME]
                for p in cache[DIRECTIVE_CACHE_PACKAGES]:
                    if p['name'] == package_name and p['version'] == package_ver:
                        logging.debug(_('Package %s(%s) founded in %s repo') % (package_name,
                                                                                package_ver, cache_name))
                        return cache[DIRECTIVE_CACHE_TYPE]

        def __deps_recurse(self, s, p):
            deps = p.candidate.get_dependencies('Depends')
            pre_deps = p.candidate.get_dependencies('PreDepends')
            all_deps = deps + pre_deps
            for i in all_deps:
                dp = i.target_versions
                if len(dp) > 0:
                    package = dp[0].package
                    item = (p.name, package, self.__get_package_repository(package))
                    if item not in s:
                        s.append(item)
                        self.__deps_recurse(s, package)

    def __init__(self, repodirpath, white_list_path, no_create_iso):
        super().__init__(repodirpath)
        if not os.path.exists(white_list_path):
            exit_with_error(_('File %s does not exist') % white_list_path)
        self.__white_list = white_list_path
        self.__no_create_iso = no_create_iso
        self.__packages = {}
        self.__caches = []
        self.__name = None
        self.__version = None
        self.__build_cache_of_builded_packages()
        self.__load_caches()
        self.__parse_white_list()

    def __parse_white_list(self):
        i = 1
        last_section = None
        for line in open(self.__white_list, mode='r').readlines():
            i += 1
            m = re.match(r'#\s?(?P<key>\w+)\s?:\s?(?P<value>[\w.\-]+)', line)
            if m:
                try:
                    setattr(self, '_%s__%s' % (self.__class__.__name__,
                                               m.group('key')), m.group('value'))
                    continue
                except AttributeError:
                    exit_with_error(_('Unexpected key %s in white list %s') % (m.group('key'),
                                                                               self.__white_list))
            elif line.startswith('#') or line == '\n':
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
        # Проверка на наличие полей name и version
        if self.__name is None or self.__version is None:
            exit_with_error(_('Name and version fields are required'))
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
        maker = PackageCacheMaker(self._conf.root,
                                  self._conf.repodirpath,
                                  'builded',
                                  PackageType.PACKAGE_BUILDED)
        maker.run(is_builded=True)

    def __load_caches(self):
        files = [f for f in os.listdir(self._conf.cachedirpath) if f.endswith('.cache')]
        if len(files) <= 1:
            exit_with_error(_('No one cache is created'))
        for f in files:
            path = '%s/%s' % (self._conf.cachedirpath, f)
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

    def __get_depends_for_package(self, package):
        depfinder = self.DependencyFinder(package, self.__caches)
        return depfinder.deps

    def run(self):
        # Подготовка к созданию репозитория - очистка директорий
        for directory in [self._conf.frepodirpath,
                          self._conf.frepodevdirpath,
                          self._conf.fsrcdirpath]:
            logging.debug(_('Clearing %s') % directory)
            for file in os.listdir(directory):
                os.remove('%s/%s' % (directory, file))
        logging.info(_('Processing target repository ...'))
        # Анализ пакетов основного репозитория
        target_builded_deps = set()
        sources = dict()
        for required in self.__packages['target']:
            logging.info(_('Processing %s ...') % required)
            deps = self.__get_depends_for_package(required)
            unresolve = [d for d in deps if d[2] == PackageType.PACKAGE_NOT_FOUND]
            deps_in_dev = [d for d in deps if d[2] == PackageType.PACKAGE_FROM_OS_DEV_REPO]
            if len(unresolve):
                for p in unresolve:
                    package_name, package_ver = p.name, p.versions[0].version
                    logging.error(_('Could not resolve %s for %s: %s version %s') %
                                   ('dependency' if p[0] == required else 'subdependency',
                                    required, package_name, package_ver))
                exit_with_error(_('Could not resolve dependencies'))
            if len(deps_in_dev):
                for p in deps_in_dev:
                    package_name, package_ver = p.name, p.versions[0].version
                    logging.error(_('%s %s(%s) for %s is founded in os-dev repo') %
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
            files_to_copy = set([p[1].versions[0].filename for p in target_deps])
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
        if self.__packages.get('target-dev', None) is None:
            for f in os.listdir(self._conf.repodirpath):
                m = re.match(DEB_RE, f)
                if m:
                    package_name = m.group('name')
                    dev_packages.append(package_name)
            dev_packages = sorted([p for p in set(dev_packages) - set(self.__packages['target'])])
        else:
            dev_packages = self.__packages['target-dev']
        for pkg in dev_packages:
            logging.info(_('Processing %s ...') % pkg)
            deps = self.__get_depends_for_package(pkg)
            unresolve = [d for d in deps if d[2] == PackageType.PACKAGE_NOT_FOUND]
            if len(unresolve):
                for p in unresolve:
                    package_name, package_ver = p.name, p.versions[0].version
                    logging.error(_('Could not resolve %s for %s: %s version %s') %
                                   ('dependency' if p[0] == required else 'subdependency',
                                    required, package_name, package_ver))
                exit_with_error(_('Could not resolve dependencies'))
            builded = [d for d in deps if d[2] == PackageType.PACKAGE_BUILDED]
            files_to_copy = set([p[1].versions[0].filename for p in builded])
            intersection = files_to_copy & target_builded_deps
            # Исключаем пересечения с основным репозиторием
            files_to_copy -= intersection
            for package in [p[1] for p in builded]:
                if package.name in sources.keys():
                    continue
                package_sources = Debhelper.get_sources_filelist(self._conf, package)
                sources[package.name] = package_sources
            logging.debug(_('Copying dependencies for package %s: %s') % (pkg, files_to_copy))
            for f in files_to_copy:
                src = os.path.join(self._conf.root, f)
                dst = os.path.join(self._conf.frepodevdirpath, os.path.basename(f))
                try:
                    logging.debug(_('Copying %s to %s') % (src, dst))
                    shutil.copyfile(src, dst)
                except Exception as e:
                    exit_with_error(e)
        # Копируем исходники для разрешенных репозиториев
        for package_name, sourcelist in sources.items():
            logging.info(_('Copying sources for package %s ...') % package_name)
            for source in sourcelist:
                dst = os.path.join(self._conf.fsrcdirpath, os.path.basename(source))
                try:
                    logging.debug(_('Copying %s to %s') % (src, dst))
                    shutil.copyfile(source, dst)
                except Exception as e:
                    exit_with_error(e)
        if self.__no_create_iso:
            return
        # Создаем репозиторий (main)
        for is_dev in (False, True):
            iso_maker = self.IsoRepositoryMaker(self.__name, self.__version, is_dev)
            iso_maker.mkiso(self._conf)


class PackageCacheMaker(BaseCommand):
    __DIRECTIVE_PACKAGE = 'Package: '
    __DIRECTIVE_VERSION = 'Version: '
    __DIRECTIVE_DESCRIPTION_ENDS = ''

    def __init__(self, repodirpath, mount_point, name, cache_type):
        super().__init__(repodirpath)
        if not os.path.exists(mount_point):
            exit_with_error(_('Path %s does not exist') % mount_point)
        self.__name = name
        self.__mount_point = mount_point
        self.__cache_type = cache_type

    def run(self, is_builded=False):
        if not is_builded:
            packages_path = Debhelper.find_packages_files(self.__mount_point)
        else:
            packages_path = ['%s/Packages' % self.__mount_point]
        cache_file_path = '%s/%s.cache' % (self._conf.cachedirpath, self.__name)
        result = {DIRECTIVE_CACHE_NAME: self.__name,
                  DIRECTIVE_CACHE_TYPE: self.__cache_type}
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


def make_default_subparser(main_parser, command):
    parser = main_parser.add_parser(command)
    parser.add_argument('--path', required=False,
                        default=DEFAULT_REPO_DIR,
                        help=_('Root directory for building (default: %s)') % DEFAULT_REPO_DIR)
    return parser


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='command')

    # init parser
    parser_init = make_default_subparser(subparsers, COMMAND_INIT)

    # build parser
    parser_build = make_default_subparser(subparsers, COMMAND_BUILD)
    parser_build.add_argument('--source-list', required=True,
                              help=_('Set path to scenario file'))
    parser_build.add_argument('--clean', required=False, action='store_true',
                              default=False,
                              help=_('Clear packages before building (default: False)'))
    parser_build.add_argument('--jobs', required=False, action='store', type=int,
                              default=2,
                              help=_('Jobs count for package building (default: 2)'))
    # make repo parser
    parser_make_repo = make_default_subparser(subparsers, COMMAND_MAKE_REPO)
    parser_make_repo.add_argument('--white-list', required=True,
                                  help=_('Set path to white list'))
    parser_make_repo.add_argument('--no-create-iso', required=False, action='store_true',
                                  help=_('Skip iso creation after binary depends resolving, default: False'))

    # make package cache parser
    parser_make_cache = make_default_subparser(subparsers, COMMAND_MAKE_PACKAGE_CACHE)
    parser_make_cache.add_argument('--mount-path', required=True,
                                   help=_('Set path to repo\'s mount point'))
    parser_make_cache.add_argument('--name', required=True,
                                   help=_('Set package name of repo'))
    parser_make_cache.add_argument('--primary', required=False,
                                   default=False, action='store_true',
                                   help=_('Is primary repo?'))
    args = parser.parse_args()
    root = None
    try:
        root = os.path.abspath(args.path)
    except AttributeError:
        parser.print_help()
        exit(1)
    Configuration.init_logger(root)
    # Проверяем наличие прав суперпользователя
    check_root_access()
    try:
        def rm_tmp_dirs():
            for directory in tmpdirmanager.dirs():
                if os.path.exists(directory):
                    shutil.rmtree(directory)

        atexit.register(rm_tmp_dirs)
        if args.command == COMMAND_INIT:
            initializer = RepoInitializer(root)
            initializer.run()
        elif args.command == COMMAND_BUILD:
            builder = Builder(root, os.path.abspath(args.source_list), args.clean, args.jobs)
            builder.run()
        elif args.command == COMMAND_MAKE_REPO:
            repomaker = RepoMaker(root, os.path.abspath(args.white_list), args.no_create_iso)
            repomaker.run()
        elif args.command == COMMAND_MAKE_PACKAGE_CACHE:
            cache_type = PackageType.PACKAGE_FROM_OS_REPO if args.primary else PackageType.PACKAGE_FROM_OS_DEV_REPO
            cachemaker = PackageCacheMaker(root, os.path.abspath(args.mount_path), args.name, cache_type)
            cachemaker.run()
        else:
            parser.print_help()
    except KeyboardInterrupt:
        logging.info(_('Exit on user\'s query'))
