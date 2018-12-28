#!/usr/bin/python3

import logging
import re
import json
import gettext
import os
import gzip
import apt
from os import getuid, mkdir, devnull, chdir, listdir, remove
from os.path import curdir, abspath, exists, basename, isdir
from shutil import rmtree, copyfile
from argparse import ArgumentParser
from subprocess import check_call, check_output, CalledProcessError, Popen
from tempfile import mkdtemp
from copy import deepcopy

DEFAULT_REPO_DIR = abspath('%s/../build' % curdir)
REPO_FILE_NAME = '/etc/apt/sources.list.d/build-repo.list'
COMMAND_INIT = 'init'
COMMAND_BUILD = 'build'
COMMAND_MAKE_REPO = 'make-repo'
COMMAND_MAKE_PACKAGE_CACHE = 'make-package-cache'
DEVNULL = open(devnull, 'wb')

WHITE_SPACE = ' '
EMPTY_SPACE = ''
END_OF_LINE = '\n'

DEB_RE = '^(?P<name>[\w\-\.]+)_(?P<version>[\w\.\-\~\+]+)_(?P<arch>[\w]+)\.deb$'
DSC_FULL_RE = '^(?P<name>[\w\-\.\+]+)_(?P<version>[\w\.\-\~\+]+)\.dsc$'
DSC_RE = '^%s_(?P<version>[\w\.\-\~\+]+)\.dsc$'
DSC_PACKAGE_VERSION_RE = '^(?P<name>[\w\-\.\+]+)\s\((?P<version>[<>=\w\:\s\.\-~\+]+)\)$'
DSC_CONTENT_RE = '^(?P<tag>[\w\-]+):\s(?P<value>[\w\.\s\(\)\-\,\~\+\=|\[\]\<\>\+:@/!]+)$'
DSC_CONTENT_MULTI_RE = '^(?P<tag>[\w\-]+):\s?$'
STANDART_BUILD_OPTIONS_TEMPLATE = 'DEB_BUILD_OPTIONS="nocheck parallel=%d"'
DPKG_IGNORED_CODES = [1]

BUILD_USER = 'builder'

# В deb пакете таким макросом помечаются пакеты, необходимые определенным платформам.
SUPPORTED_PLATFORMS = ['amd64', 'linux-any']
CURRENT_ARCH = 'amd64'

# Ключи кэша
DIRECTIVE_CACHE_NAME = 'cache_name'
DIRECTIVE_CACHE_TYPE = 'cache_type'
DIRECTIVE_CACHE_VERSION = 'version'
DIRECTIVE_CACHE_PACKAGES = 'packages'
DIRECTIVE_CACHE_PACKAGES_PACKAGE_NAME = 'name'
DIRECTIVE_CACHE_PACKAGES_PACKAGE_VERSION = 'version'
DIRECTIVE_CACHE_PACKAGES_PACKAGE_PROVIDES = 'provides'

# Тэги наличия хэша в dsc файле
DSC_MESSAGE_BEGIN = '-----BEGIN PGP SIGNED MESSAGE-----'
DSC_PGP_SIGNATURE_BEGIN = '-----BEGIN PGP SIGNATURE-----'
DSC_PGP_SIGNATURE_END = '-----END PGP SIGNATURE-----'

# gettext
_ = gettext.gettext


def check_root_access():
    if not getuid() == 0:
        logging.error(_('Must be run as superuser'))
        exit(1)


def exit_with_error(error):
    logging.critical(error)
    exit(1)


def fix_re(reg_exp):
    if '++' in reg_exp:
        return reg_exp.replace('++', '\++')
    return reg_exp

# ###########################################################################################################################
# Парсинг зависимостей


class OrderedObject(object):
    def __eq__(self, other):
        return type(self) is type(other) and self._key() == other._key()

    def __lt__(self, other):
        return isinstance(other, OrderedObject) and self._key() < other._key()

    def __hash__(self):
        return hash(self.__class__) ^ hash(self._key())

    def _key(self):
        raise NotImplementedError()


def parse_versioned_or_simple_dep(depstring):
    """
    Разбор зависимости (простой или версионной)
    @param depstring: str -- строка, содержащая зависимость
    @returns: Relationship or VersionedRelationship or None, если зависимость не под текущую платформу
    """
    # 1. Определяем наличие тега архитектуры
    parsed = depstring
    if '[' in parsed:
        start = parsed.index('[')
        end = parsed.index(']')
        archrow = parsed[start + 1: end]
        parsed = parsed[:start - 1]
        arches = re.split('\s', archrow)
        dep_enabled = False
        for pl in SUPPORTED_PLATFORMS:
            # TODO: обработать знак инверсии зависимости '!'
            if pl in arches:
                dep_enabled = True
                break
        # Зависимость не требуется для текущей платформы
        if not dep_enabled:
            return None
    # 2. Определяем тип зависимости
    versioned = re.match(DSC_PACKAGE_VERSION_RE, parsed)
    if versioned:
        name = versioned.group('name')
        version = versioned.group('version')
        operator, version = re.split('\s', version)
        return VersionedRelationship(name, operator, version)
    else:
        return Relationship(parsed)


def parse_depends(relationships):
    parsed_deps = []
    deps = re.split('\s*,\s*', relationships)
    for dep in deps:
        if '|' in dep:
            alt_deps = re.split('\s*\|\s*', dep)
            alt1 = parse_versioned_or_simple_dep(alt_deps[0])
            alt2 = parse_versioned_or_simple_dep(alt_deps[1])
            if alt1 and alt2:
                parsed_deps.append(AlternativeRelationship(alt1, alt2))
            elif alt1 is None:
                parsed_deps.append(alt2)
            elif alt2 is None:
                parsed_deps.append(alt1)
        else:
            pdep = parse_versioned_or_simple_dep(dep)
            if pdep:
                parsed_deps.append(pdep)
    return parsed_deps


class Relationship(OrderedObject):
    def __init__(self, name):
        self.name = name

    @property
    def names(self):
        return set([self.name])

    def matches(self, name, version=None):
        return True if self.name == name else None

    def __repr__(self):
        return '%s' % (', '.join([
            '%s' % self.name
        ]))

    def _key(self):
        return (self.name,)


class VersionedRelationship(Relationship):
    def __init__(self, name, operator, version):
        self.name = name
        self.operator = operator
        self.version = version

    def matches(self, name, version=None):
        if self.name == name:
            if version:
                Debhelper.compare_versions(version, self.operator, self.version)
            else:
                return False

    def __repr__(self):
        return '%s %s %s' % (self.name, self.operator, self.version)

    def _key(self):
        return (self.name, self.operator, self.version)


class AlternativeRelationship(Relationship):
    def __init__(self, *relationships):
        self.relationships = tuple(relationships)

    @property
    def names(self):
        names = set()
        for relationship in self.relationships:
            names |= relationship.names
        return names

    def matches(self, name, version=None):
        matches = None
        for alternative in self.relationships:
            alternative_matches = alternative.matches(name, version)
            if alternative_matches is True:
                return True
            elif alternative_matches is False:
                # Keep looking for a match but return False if we don't find one.
                matches = False
        return matches

    def __repr__(self):
        return '%s' % (' | '.join(repr(r) for r in self.relationships))

    def _key(self):
        return self.relationships


class RelationshipSet(OrderedObject):
    def __init__(self, *relationships):
        self.relationships = tuple(relationships)

    @property
    def names(self):
        names = set()
        for relationship in self.relationships:
            names |= relationship.names
        return names

    def matches(self, name, version=None):
        results = [r.matches(name, version) for r in self.relationships]
        matches = [r for r in results if r is not None]
        return all(matches) if matches else None

    def __repr__(self, pretty=False, indent=0):
        prefix = '('
        indent += len(prefix)
        delimiter = ',\n%s' % (' ' * indent) if pretty else ', '
        return prefix + delimiter.join(repr(r) for r in self.relationships) + ')'

    def _key(self):
        return self.relationships

    def __iter__(self):
        return iter(self.relationships)

# ###########################################################################################################################


class Debhelper:
    __DSC_BUILD_DEPENDS_LIKE_DIRECTIVES = ['Build-Depends', 'Build-Depends-Indep']
    __DSC_FILES_DIRECTIVE = 'Files'
    __DSC_BUILD_CONFICTS = 'Build-Conflicts'
    __CONTROL_VERSION_DIRECTIVE = 'Version: '
    __CONTROL_DEPENDS_LIKE_DIRECTIVES = ['Pre-Depends', 'Depends', 'Python-Depends']
    __CONTROL_SOURCE_DIRECTIVE = 'Source:'
    __CACHE_REVERSE_PROVIDES_DIRECTIVE = 'Reverse Provides:'

    """
    Класс для запуска Debian утилит
    """

    @staticmethod
    def run_command_with_output(command, ):
        return check_output(command, shell=True, stderr=DEVNULL).decode().rstrip(END_OF_LINE)

    @staticmethod
    def run_command(command, need_output=False):
        if not need_output:
            check_call(command, shell=True, stderr=DEVNULL, stdout=DEVNULL)
        else:
            check_call(command, shell=True)

    @staticmethod
    def check_is_dpkg_dev_installed():
        command = 'dpkg --list | grep dpkg-dev'
        try:
            Debhelper.run_command(command)
        except CalledProcessError:
            exit_with_error(_('Package \'dpkg-dev\' is required for building'))

    @staticmethod
    def get_packages_list():
        command = 'dpkg --list | grep ii'
        try:
            output = Debhelper.run_command_with_output(command)
            packages = []
            for line in output.split(END_OF_LINE):
                try:
                    data = [d for d in line.split(WHITE_SPACE) if d][1]
                    packages.append(data)
                except IndexError:
                    pass
            return packages
        except CalledProcessError:
            exit_with_error(_('Error package list getting by the command \'%s\'') % command)

    @staticmethod
    def generate_packages_list(repodirpath, ignore_errors=False):
        # Вызываем dpkg-scanpackages
        CURDIR = curdir
        repo_name = basename(repodirpath)
        chdir(abspath('%s/..' % repodirpath))
        command = 'dpkg-scanpackages %s/ > %s/Packages' % (repo_name, repo_name)
        try:
            Debhelper.run_command(command)
        except CalledProcessError as error:
            if not ignore_errors:
                exit_with_error(_('Error package list generation by command \'%s\'') % command, error)
        # Вызываем apt-get update
        command = 'apt-get update'
        try:
            Debhelper.run_command(command)
        except CalledProcessError as error:
            if not ignore_errors:
                exit_with_error(_('Error updating package list by command \'%s\'' % command))
        finally:
            chdir(CURDIR)
        logging.info(_('Repository \'%s\' was updated') % repo_name)

    @staticmethod
    def remove_package(package_name, ignore_errors=True):
        command = 'apt-get purge %s -y' % package_name
        try:
            Debhelper.run_command(command)
        except CalledProcessError as error:
            if not ignore_errors:
                exit_with_error(_('Error while package \'%s\' deleting: %s') % (package_name, error))

    @staticmethod
    def get_source_files_to_extract(dscfilepath):
        lines = Debhelper.get_lines(dscfilepath)
        found_files_line = False
        files = [basename(dscfilepath)]
        for line in lines:
            if line.startswith(Debhelper.__DSC_FILES_DIRECTIVE) and not found_files_line:
                found_files_line = True
            elif found_files_line and (len(line) == 0 or not line.startswith(' ')):
                break
            elif found_files_line:
                tokens = [t for t in line.split(' ') if not t.isspace()]
                files.append(tokens[-1])
        if not found_files_line:
            exit_with_error(_('Uncorrect dsc file \'%s\'') % dscfilepath)
        return files

    @staticmethod
    def extract_sources(tmpdirpath, package_name):
        logging.info(_('Unpacking sources \'%s\'...') % package_name)
        command = 'dpkg-source -x *.dsc && chown -R %s.%s *' % (BUILD_USER, BUILD_USER)
        CURDIR = abspath(curdir)
        chdir(tmpdirpath)
        try:
            Debhelper.run_command(command)
        except CalledProcessError as error:
            exit_with_error(_('Error unpacking sources with command \'%s\': %s') % (command, error))
        finally:
            chdir(CURDIR)

    @staticmethod
    def get_build_dir(tmpdirpath):
        for file_name in listdir(tmpdirpath):
            path = '%s/%s' % (tmpdirpath, file_name)
            if isdir(path):
                return path

    @staticmethod
    def get_lines(filepath):
        try:
            return [l.rstrip(END_OF_LINE) for l in open(filepath, mode='r').readlines() if l.endswith(END_OF_LINE)]
        except Exception:
            exit_with_error(_('Failed to open file \'%s\'') % filepath)

    @staticmethod
    def parse_dscfile(dscfilepath):
        lines = Debhelper.get_lines(dscfilepath)
        oldtag = str()
        values = []
        result = {}
        # Определяем наличие хэша PGP
        if DSC_MESSAGE_BEGIN in lines:
            # Если есть, то отрезаем первые три строки (заголовок, имя хэша и пустая строка)
            lines = lines[3:]
            index_pgp_start = lines.index(DSC_PGP_SIGNATURE_BEGIN)
            index_pgp_end = lines.index(DSC_PGP_SIGNATURE_END)
            if not (index_pgp_start == 0 and index_pgp_end == 0):
                # Дополнительно учитываем пустую строку перед DSC_SIGNATURE_BEGIN
                lines = lines[:index_pgp_start - 1]
            else:
                exit_with_error(_('Uncorrect syntax of dsc file \'%s\'') % dscfilepath)
        for l in lines:
            match_row = re.match(DSC_CONTENT_RE, l)
            match_multi_row = re.match(DSC_CONTENT_MULTI_RE, l)
            if match_row:
                tag = match_row.group('tag')
                value = match_row.group('value')
                result[tag] = value
            elif match_multi_row:
                tag = match_multi_row.group('tag')
                if not oldtag == tag:
                    result[oldtag] = values
                    oldtag = str()
                    values = []
                oldtag = tag
            else:
                values.append(l.lstrip(WHITE_SPACE) if l.startswith(WHITE_SPACE) else l)
        if len(tag) and len(values):
            result[tag] = values
        return result

    @staticmethod
    def get_build_depends(dscfilepath):
        data = Debhelper.parse_dscfile(dscfilepath)
        result = str()
        for d in Debhelper.__DSC_BUILD_DEPENDS_LIKE_DIRECTIVES:
            if d in data.keys():
                if not len(result) == 0:
                    result = '%s, %s' % (result, data[d])
                else:
                    result = data[d]
        return result

    @staticmethod
    def get_build_conficts(dscfilepath):
        data = Debhelper.parse_dscfile(dscfilepath)
        build_conflicts = data.get(Debhelper.__DSC_BUILD_CONFICTS, None)
        if build_conflicts:
            build_conflicts = build_conflicts.replace(' ', '')
            if ',' in build_conflicts:
                return build_conflicts.split(',')
            else:
                return [build_conflicts]
        return []

    @staticmethod
    def install_build_depends(tmpdirpath, package):
        try:
            dsc_file = next(f for f in listdir(tmpdirpath) if re.match(DSC_FULL_RE, f))
        except AttributeError:
            exit_with_error(_('dsc file does not exist'))
        build_deps_row = Debhelper.get_build_depends('%s/%s' % (tmpdirpath, dsc_file))
        parsed_deps = parse_depends(build_deps_row)
        logging.debug(_('Depends for package \'%s\': %s') % (package, parsed_deps))
        # Удаляем все сборочные конфликты, если они есть
        for conf in Debhelper.get_build_conficts('%s/%s' % (tmpdirpath, dsc_file)):
            try:
                Debhelper.remove_package(conf)
            except:
                pass
        for d in parsed_deps:
            # Альтернативные зависимости?
            if isinstance(d, AlternativeRelationship):
                statuses = set()
                for r in d.relationships:
                    status = True
                    command = 'apt-get install -y --force-yes --no-install-recommends %s' % r.name
                    try:
                        Debhelper.run_command(command)
                    except CalledProcessError:
                        status = False
                    finally:
                        # Определяем, поставилась ли хотя бы одна из зависимостей?
                        message = _('Installing alternative dependency \'%s\' for \'%s\' ... %s') % (
                                  r.name,
                                  package,
                                  _('success') if status else _('ERROR'))
                        logging.info(message)
                        statuses.add(status)
                # Если не поставилась ни одна из альтернативных зависимостей -- выходим
                if True not in statuses:
                    exit_with_error(_('Could not resolve alternative depends for \'%s\'') % package)
            # Обычная или версионная зависимость?
            else:
                status = True
                try:
                    command = 'apt-get install -y --force-yes --no-install-recommends %s' % d.name
                    Debhelper.run_command(command)
                except CalledProcessError:
                    status = False
                finally:
                    if isinstance(d, VersionedRelationship) and status:
                        status = Debhelper.check_installed_package_version(d)
                    message = _('Installing dependency \'%s\' for \'%s\' ... %s') % (
                              str(d),
                              package,
                              _('success') if status else _('ERROR'))
                    logging.info(message)
                    if not status:
                        exit_with_error(_('Could not resolve depends for \'%s\'') % package)

    @staticmethod
    def build_package(tmpdirpath, logdir, jobs, options):
        CURDIR = curdir
        dirpath = Debhelper.get_build_dir(tmpdirpath)
        chdir(dirpath)
        options = options % jobs if options is not None else str()
        log_file = '%s/%s.log' % (logdir, basename(dirpath))
        command = 'sudo -u %s %s dpkg-buildpackage' \
                  % (BUILD_USER, options)
        logging.info(_('Package building \'%s\'') % basename(dirpath))
        try:
            logstream = open(log_file, mode='w')
        except OSError as e:
            exit_with_error(_('Error opening logfile: %s') % e)
        logstream.write('\n\nCommand: %s' % command)
        proc = Popen(command, stdout=logstream, stderr=logstream, universal_newlines=True, shell=True)
        proc.communicate()
        logstream.write('\nReturncode: %d' % proc.returncode)
        logstream.close()
        returncode = proc.returncode
        if returncode:
            if returncode not in DPKG_IGNORED_CODES:
                rmtree(tmpdirpath)
                chdir(CURDIR)
                exit_with_error(_('Package \'%s\' build is failed: Command \'%s\' return exit code %d') % (
                    basename(dirpath),
                    command,
                    returncode))
        chdir(CURDIR)

    @staticmethod
    def copy_debs(tmpdirpath, repopath):
        # Определяем список собранных deb-пакетов
        debs = [f for f in listdir(tmpdirpath) if f.endswith('.deb')]
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
    def get_binary_depends(repodirpath, deb_package):
        # Создаем временную директорию для распаковки deb-пакета
        tmpdirpath = mkdtemp(prefix='buildrepo')
        # Копируем deb-пакет во временную директорию
        Debhelper.copy_files(repodirpath, tmpdirpath, [deb_package])
        command = 'dpkg-deb -R %s/%s %s' % (repodirpath, deb_package, tmpdirpath)
        try:
            Debhelper.run_command(command)
        except CalledProcessError:
            exit_with_error(_('Failed to unpack file \'%s\'') % deb_package)
            rmtree(tmpdirpath)
        control_file_path = '%s/DEBIAN/control' % tmpdirpath
        if not exists(control_file_path):
            rmtree(tmpdirpath)
            exit_with_error(_('File \'%s\' does not exist') % deb_package)
        lines = Debhelper.get_lines(control_file_path)
        depends = []
        for l in lines:
            for d in Debhelper.__CONTROL_DEPENDS_LIKE_DIRECTIVES:
                reg = '^%s:\s' % d
                if re.match(reg, l):
                    tokens = [t.strip() for t in re.split(reg, l) if t and not t.isspace()]
                    if len(tokens) == 0 or len(tokens) > 1:
                        exit_with_error(_('Error getting binary depends for \'%s\': expression: \'%s\'')
                                        % (deb_package, l))
                    else:
                        depends += parse_depends(tokens[0])
        rmtree(tmpdirpath)
        return depends

    @staticmethod
    def get_binary_version(repodirpath, deb_package):
        # Создаем временную директорию для распаковки deb-пакета
        tmpdirpath = mkdtemp(prefix='buildrepo')
        # Копируем deb-пакет во временную директорию
        Debhelper.copy_files(repodirpath, tmpdirpath, [deb_package])
        command = 'dpkg-deb -R %s/%s %s' % (repodirpath, deb_package, tmpdirpath)
        try:
            Debhelper.run_command(command)
        except CalledProcessError:
            exit_with_error(_('Failed to unpack file \'%s\'') % deb_package)
            rmtree(tmpdirpath)
        control_file_path = '%s/DEBIAN/control' % tmpdirpath
        if not exists(control_file_path):
            rmtree(tmpdirpath)
            exit_with_error(_('File \'%s\' does not exist') % deb_package)
        lines = Debhelper.get_lines(control_file_path)
        for l in lines:
            if l.startswith(Debhelper.__CONTROL_VERSION_DIRECTIVE):
                version = l.split(Debhelper.__CONTROL_VERSION_DIRECTIVE)[1]
                version = version.rstrip(END_OF_LINE) if version.endswith(END_OF_LINE) else version
        rmtree(tmpdirpath)
        return version

    @staticmethod
    def copy_files(srcdir, dstdir, files):
        for f in files:
            src = '%s/%s' % (srcdir, f)
            dst = '%s/%s' % (dstdir, f)
            logging.debug(_('Copying file \'%s\' to \'%s\'') % (src, dst))
            try:
                copyfile(src, dst)
            except IOError:
                exit_with_error(_('File \'%s\' does not exist') % src)

    @staticmethod
    def compare_versions(left, operator, right):
        command = 'dpkg --compare-versions %s \'%s\' %s' % (left, operator, right)
        try:
            Debhelper.run_command(command)
        except CalledProcessError:
            return False
        return True

    @staticmethod
    def get_provider_package_name(package_name):
        command = 'apt-cache showpkg %s' % package_name
        try:
            output = Debhelper.run_command_with_output(command)
        except CalledProcessError:
            exit_with_error(_('Could not get information for package \'%s\'') % package_name)
        found_provides_directive = False
        packages = []
        for l in output.split(END_OF_LINE):
            if found_provides_directive:
                if WHITE_SPACE in l:
                    packages.append(l.split(WHITE_SPACE)[0])
            if l.startswith(Debhelper.__CACHE_REVERSE_PROVIDES_DIRECTIVE):
                found_provides_directive = True
        return package_name if len(packages) == 0 else packages

    @staticmethod
    def check_installed_package_version(package):
        command = 'dpkg --list | grep %s | awk \'{print $2,$3}\'' % package.name
        try:
            output = Debhelper.run_command_with_output(command)
        except CalledProcessError:
            exit_with_error(_('Could not get information for package \'%s\'') % package.name)
        installed_package_version = None
        arch = None
        for line in output.split('\n'):
            try:
                pkgname, version = line.split(' ')
                if ':' in pkgname:
                    pkgname, arch = pkgname.split(':')
            except Exception as e:
                exit_with_error(e)
            if pkgname == package.name and (arch is None or arch == CURRENT_ARCH):
                installed_package_version = version
                break
        # Если не установлен пакет, зависимость не установлена
        if not installed_package_version:
            return False
        command = 'dpkg --compare-versions %s \'%s\' %s' % (installed_package_version, package.operator, package.version)
        try:
            Debhelper.run_command(command)
        except CalledProcessError:
            return False
        return True


class Configuration:
    def __init__(self, root):
        self.root = root
        self.srcdirpath = '%s/src' % root
        self.repodirpath = '%s/repo' % root
        self.datadirpath = '%s/data' % root
        self.logdirpath = '%s/logs' % root
        self.debsdirpath = '%s/debs' % root
        self.cachedirpath = '%s/cache' % root
        self.frepodirpath = '%s/frepo' % root
        self.frepodevdirpath = '%s/frepodev' % root
        self.packageslistpath = '%s/packageslist.txt' % self.datadirpath

    @staticmethod
    def init_logger(root):
        if not exists(root):
            mkdir(root)
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


class RepoInitializer:
    """
    Класс выполняет подготовку при инициализации репозитория
    """
    def __init__(self, root):
        if not exists(root):
            mkdir(root)
        self.__conf = Configuration(root)

    def __init_build_dirs(self):
        """
        Создает директории в корневой директории
        """
        def make_dir(directory):
            if exists(directory):
                rmtree(directory)
            mkdir(directory)
            logging.debug(_('Creating directory \'%s\'') % directory)

        for _directory in [self.__conf.srcdirpath, self.__conf.datadirpath,
                           self.__conf.repodirpath, self.__conf.logdirpath,
                           self.__conf.debsdirpath, self.__conf.cachedirpath,
                           self.__conf.frepodirpath, self.__conf.frepodevdirpath]:
            make_dir(_directory)

    def __init_packages_list(self):
        """
        Записывает в файл список пакетов системы
        """
        packagelist_file = open(self.__conf.packageslistpath, mode='w')
        packagelist_file.writelines([line + END_OF_LINE for line in Debhelper.get_packages_list()])
        packagelist_file.close()
        logging.info(_('Creating package list of system in file \'%s\'') % self.__conf.packageslistpath)

    def __init_repo(self, repo_filename, repo_path):
        repo_file = open(repo_filename, mode='w')
        content = 'deb "file://%s" %s/' % (abspath('%s/..' % repo_path),
                                           basename(repo_path))
        repo_file.write(content)
        repo_file.close()
        logging.info(_('Repo file \'%s\' is created with content: \'%s\'') % (repo_filename, content))
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
        self.__init_repo(REPO_FILE_NAME, self.__conf.repodirpath)


class Builder:
    class PackageData:
        def __init__(self, name, version=None, build_not_required=False, options=None):
            self.name = name
            self.version = version
            self.build_not_required = build_not_required
            self.options = options

        def __repr__(self):
            return '%s: %s %s' % (self.name, self.version, self.build_not_required)

    class Scenario:
        __NAME_TAG = '# Name:'
        __COMMENT_TAG = '#'
        __BUILD_NOT_REQUIRED_TAG = '-'
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
                    if line.endswith(END_OF_LINE):
                        line = line.rstrip(END_OF_LINE)
                    if not line:
                        continue
                    if line.startswith(self.__NAME_TAG):
                        name = line.split(self.__NAME_TAG)[1]
                        name = name.replace(WHITE_SPACE, EMPTY_SPACE)
                        self.name = name
                    elif line.startswith(self.__COMMENT_TAG):
                        continue
                    else:
                        tokens = [e for e in line.split(WHITE_SPACE) if not e.isspace()]
                        build_not_required = False
                        if tokens[0].startswith(self.__BUILD_NOT_REQUIRED_TAG):
                            tokens[0] = tokens[0].lstrip('-')
                            build_not_required = True
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
                                options = t.replace(self.__BUILD_OPTIONS_TAG, EMPTY_SPACE)
                                if options == self.__BUILD_OPTIONS_NONE:
                                    options = None
                        package_data = Builder.PackageData(name, version, build_not_required, options)
                        self.packages.append(package_data)
                if self.name is None:
                    exit_with_error(_('Scenario name is missing in file \'%s\'') % self.scenario_path)
            logging.info(_('Following packages will be built: \n%s') %
                         END_OF_LINE.join([p.name for p in self.packages]))

    """
    Класс выполняет сборку пакетов
    """
    def __init__(self, repodirpath, scenario_path, clean, jobs):
        if not exists(scenario_path):
            exit_with_error(_('File \'%s\' does not exist') % scenario_path)
        self.__conf = Configuration(repodirpath)
        self.__clean = clean
        self.__jobs = jobs
        self.__scenario = self.Scenario(scenario_path)

    def __make_clean(self):
        logging.info(_('Package cleaning before rebuilding...'))
        init_packages_list = [p.rstrip(END_OF_LINE)
                              for p in open(self.__conf.packageslistpath, mode='r').readlines()
                              if p.endswith(END_OF_LINE)]
        current_package_list = Debhelper.get_packages_list()
        diff = [item for item in current_package_list if item not in init_packages_list]
        if diff:
            for package in diff:
                logging.debug(_('Removing package \'%s\'') % package)
                Debhelper.remove_package(package)

    def __make_build(self):
        def copy_files_to_builddir(package_data, tmpdirpath):
            reg_dsc = DSC_RE % package_data.name
            reg_dsc = fix_re(reg_dsc)
            dsc_files = [f for f in listdir(self.__conf.srcdirpath) if re.search(reg_dsc, f)]
            versions = [re.match(reg_dsc, v).group('version') for v in dsc_files]
            if not len(versions) == 1 and package_data.version is None:
                if len(versions) == 0:
                    exit_with_error(_('Could not find source files of package \'%s\'') % package_data.name)
                exit_with_error(_('There are %d versions of package \'%s\': %s') % (
                    len(versions),
                    package_data.name,
                    ', '.join(versions)))
            # Определяем файлы для копирования
            try:
                files = Debhelper.get_source_files_to_extract('%s/%s' % (self.__conf.srcdirpath, dsc_files[0]))
            except IndexError:
                exit_with_error(_('Failed determine files to copy \'%s\'') % package_data.name)
            # Копируем файлы во временную директорию
            Debhelper.copy_files(self.__conf.srcdirpath, tmpdirpath, files)

        def copy_debs_files_to_repodir(package_data):
            version = package_data.version
            files = []
            search = None
            if version is None:
                search = package_data.name
            else:
                search = '%s_%s' % (package_data.name, package_data.version)
            files = [file_name for file_name in listdir(self.__conf.debsdirpath)
                     if file_name.startswith(search) and file_name.endswith('.deb')]
            Debhelper.copy_files(self.__conf.debsdirpath, self.__conf.repodirpath, files)

        logging.info(_('Executing scenario \'%s\' ...') % self.__scenario.name)
        for package_data in self.__scenario.packages:
            if not package_data.build_not_required:
                tmpdirpath = mkdtemp(prefix='buildrepo')
                Debhelper.run_command('chown -R %s.%s %s' % (BUILD_USER, BUILD_USER, tmpdirpath))
                logging.debug(_('Creating temparary directory \'%s\'') % tmpdirpath)
                # Копируем исходники из src во временную директорию
                copy_files_to_builddir(package_data, tmpdirpath)
                # Распаковываем пакет
                Debhelper.extract_sources(tmpdirpath, package_data.name)
                # Определяем зависимости
                Debhelper.install_build_depends(tmpdirpath, package_data.name)
                # Запускаем сборку
                Debhelper.build_package(tmpdirpath, self.__conf.logdirpath, self.__jobs, package_data.options)
                # Копируем *.deb в репозиторий
                Debhelper.copy_debs(tmpdirpath, self.__conf.repodirpath)
                # Удаляем временную директорию
                rmtree(tmpdirpath)
            else:
                # Копируем уже собранные файлы в репозиторий
                copy_debs_files_to_repodir(package_data)
            # Обновляем репозиторий
            Debhelper.generate_packages_list(self.__conf.repodirpath)

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


class RepoMaker2:
    class DependencyFinder:
        def __init__(self, package, caches, target=True):
            self.deps = list()
            aptcache = apt.Cache()
            self.__caches = caches
            self.__package = aptcache.get(package)
            if self.__package is None:
                exit_with_error(_('Package %s does not exists') % package)
            self.deps.append((self.__package.name, self.__package.versions[0],
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
                    package_ver = package.versions[0].version
                    item = (p.name, package, package_ver, self.__get_package_repository(package))
                    if item not in s:
                        s.append(item)
                        self.__deps_recurse(s, package)

    def __init__(self, repodirpath, white_list_path):
        if not exists(white_list_path):
            exit_with_error(_('File \'%s\' does not exist') % white_list_path)
        self.__conf = Configuration(repodirpath)
        self.__white_list = white_list_path
        self.__packages = {}
        self.__caches = []
        self.__build_cache_of_builded_packages()
        self.__load_caches()
        self.__parse_white_list()

    def __parse_white_list(self):
        i = 1
        last_section = None
        for line in open(self.__white_list, mode='r').readlines():
            i += 1
            if line.startswith('#') or line == END_OF_LINE:
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
        maker = PackageCacheMaker(self.__conf.root,
                                  self.__conf.repodirpath,
                                  'builded',
                                  PackageType.PACKAGE_BUILDED)
        maker.run(is_builded=True)

    def __load_caches(self):
        files = [f for f in listdir(self.__conf.cachedirpath) if f.endswith('.cache')]
        if len(files) <= 1:
            exit_with_error(_('No one cache is created'))
        for f in files:
            path = '%s/%s' % (self.__conf.cachedirpath, f)
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

    def __get_depends_for_package(self, package, target=False):
        depfinder = self.DependencyFinder(package, self.__caches, target)
        return depfinder.deps

    def run(self):
        # Подготовка к созданию репозитория - очистка директорий
        for directory in [self.__conf.frepodirpath, self.__conf.frepodevdirpath]:
            logging.debug(_('Clearing %s') % directory)
            for file in listdir(directory):
                remove('%s/%s' % (directory, file))
        for required in self.__packages['target']:
            deps = self.__get_depends_for_package(required, True)
            for dep in deps:
                print(dep)


class RepoMaker:
    def __init__(self, repodirpath, white_list_path):
        if not exists(white_list_path):
            exit_with_error(_('File \'%s\' does not exist') % white_list_path)
        self.__conf = Configuration(repodirpath)
        self.__required_packages = []
        self.__available_packages = []
        self.__cache = []
        self.__white_list = white_list_path
        self.__load_available_packages()
        self.__load_cache()
        self.__parse_white_list()

    def __load_available_packages(self):
        logging.info(_('Loading info for builded packages ...'))
        maker = PackageCacheMaker(self.__conf.root,
                                  self.__conf.repodirpath,
                                  'target',
                                  PackageType.PACKAGE_FROM_TARGET)
        self.__available_packages = maker.run()

    def __parse_white_list(self):
        for line in open(self.__white_list, mode='r').readlines():
            if line.startswith('#') or line == END_OF_LINE:
                continue
            self.__required_packages.append(line.rstrip(END_OF_LINE))

    def __load_cache(self):
        files = [f for f in listdir(self.__conf.cachedirpath) if f.endswith('.cache')]
        if len(files) <= 1:
            exit_with_error(_('No one cache is loaded'))
        for f in files:
            path = '%s/%s' % (self.__conf.cachedirpath, f)
            with open(path, mode='r') as json_data:
                self.__cache.append(json.load(json_data))

    def __get_package_destination(self, dep):
        def find_package_name(_package, _packages):
            for p in _packages:
                if p == _package or (_packages[p] is not None and _package in _packages[p]):
                    return p
            return None

        def get_package_data(_package, _packages):
            for p in _packages:
                if p[DIRECTIVE_CACHE_PACKAGES_PACKAGE_NAME] == _package:
                    return p
            return None

        def find_in_repo(dep, packages, status_if_found, cache_name):
            package_names = {p[DIRECTIVE_CACHE_PACKAGES_PACKAGE_NAME]: p[DIRECTIVE_CACHE_PACKAGES_PACKAGE_PROVIDES]
                             for p in packages}
            # Отдельно обрабатываем альтернативные зависимости
            if isinstance(dep, AlternativeRelationship):
                statuses = []
                for r in dep.relationships:
                    current_status = {'package': r.name,
                                      'status': PackageType.PACKAGE_NOT_FOUND,
                                      'dependency': r}
                    provider_package = find_package_name(r.name, package_names)
                    if provider_package:
                        logging.debug(_('Alternative dependency \'%s\' is found in cache \'%s\'') %
                                       (r.name, cache_name))
                        package = get_package_data(provider_package, packages)
                        current_status['package'] = package[DIRECTIVE_CACHE_PACKAGES_PACKAGE_NAME]
                        if isinstance(r, VersionedRelationship):
                            if Debhelper.compare_versions(package[DIRECTIVE_CACHE_PACKAGES_PACKAGE_VERSION],
                                                          r.operator, r.version):
                                current_status['status'] = status_if_found
                        elif isinstance(r, Relationship):
                            current_status['status'] = status_if_found
                    else:
                        logging.debug(_('Alternative dependency \'%s\' is not found in cache \'%s\'') % (
                            r.name, cache_name))
                    statuses.append(current_status)
                # Анализируем полученные статусы
                best = None
                for s in statuses:
                    if statuses.index(s) == 0:
                        best = s
                    else:
                        if best['status'] >= s['status']:
                            best = s
                best['package'] = Debhelper.get_provider_package_name(best['dependency'].name)
                return best
            else:
                provider_package = find_package_name(dep.name, package_names)
                if provider_package:
                    packages = [p for p in packages if p[DIRECTIVE_CACHE_PACKAGES_PACKAGE_NAME] == dep.name]
                    if len(packages) == 0:
                        packages = Debhelper.get_provider_package_name(dep.name)
                    if len(packages) > 1:
                        packages = sorted(packages, reverse=True)
                    package = packages[0]
                    # В зависимости от типа зависимости определяем наличие пакета
                    if isinstance(dep, VersionedRelationship):
                        # Зависимость с указанием версии -- выполняем сравнение
                        if Debhelper.compare_versions(package[DIRECTIVE_CACHE_PACKAGES_PACKAGE_VERSION],
                                                      dep.operator,
                                                      dep.version):
                            return {'package': dep.name,
                                    'status': status_if_found,
                                    'dependency': dep}
                        else:
                            return {'package': dep.name,
                                    'status': PackageType.PACKAGE_NOT_FOUND,
                                    'dependency': dep}
                    elif isinstance(dep, Relationship):
                        # Простая зависимость -- указано лишь её название
                        return {'package': dep.name,
                                'status': status_if_found,
                                'dependency': dep}
                return {'package': dep,
                        'status': PackageType.PACKAGE_NOT_FOUND,
                        'dependency': dep}

        def print_status(dep_name, status):
            _from = str()
            if status == PackageType.PACKAGE_FROM_MAIN:
                _from = _('in main cache')
            elif status == PackageType.PACKAGE_FROM_TARGET:
                _from = _('in package of built package list')
            elif status == PackageType.PACKAGE_FROM_NON_MAIN:
                _from = _('in devel package list')
            elif status == PackageType.PACKAGE_NOT_FOUND:
                _from = _('not found')
            logging.debug(_('Package \'%s\' %s') % (dep_name, _from))

        # Ищем в основных кэшах
        main_caches = [c for c in self.__cache if c[DIRECTIVE_CACHE_TYPE] == PackageType.PACKAGE_FROM_MAIN]
        non_main_caches = [c for c in self.__cache if c[DIRECTIVE_CACHE_TYPE] == PackageType.PACKAGE_FROM_NON_MAIN]
        for c in main_caches:
            result = find_in_repo(dep,
                                  c[DIRECTIVE_CACHE_PACKAGES],
                                  PackageType.PACKAGE_FROM_MAIN,
                                  c[DIRECTIVE_CACHE_NAME])
        # Не нашли? Смотрим в собранных пакетах
        if result['status'] == PackageType.PACKAGE_NOT_FOUND:
            result = find_in_repo(dep,
                                  self.__available_packages[DIRECTIVE_CACHE_PACKAGES],
                                  PackageType.PACKAGE_FROM_TARGET,
                                  self.__available_packages[DIRECTIVE_CACHE_NAME])
        # Не нашли? Смотрим в вспомогательных кэшах
        if result['status'] == PackageType.PACKAGE_NOT_FOUND:
            for c in non_main_caches:
                result = find_in_repo(dep,
                                      c[DIRECTIVE_CACHE_PACKAGES],
                                      PackageType.PACKAGE_FROM_NON_MAIN,
                                      c[DIRECTIVE_CACHE_NAME])
        # Выводим статус поиска
        print_status(result['package'], result['status'])
        return result

    def __clean(self):
        for p in listdir(self.__conf.frepodirpath):
            remove('%s/%s' % (self.__conf.frepodirpath, p))

    def run(self):
        def check_depends(package):
            def remove_dependency(dep, deps):
                for d in deps:
                    if isinstance(d, AlternativeRelationship):
                        for r in d.relationships:
                            if r == dep:
                                deps.remove(d)
                                return
                    else:
                        if d == dep:
                            deps.remove(d)
                            return
                exit_with_error(_('Dependency \'%s\' is not found') % dep)

            depends = Debhelper.get_binary_depends(self.__conf.repodirpath, package)
            all_depends = deepcopy(depends)
            package_name = re.match(DEB_RE, package).group('name')
            packages_to_add = []
            logging.info(_('Dependencies of \'%s\': %s') % (package_name, ', '.join(repr(r) for r in depends)))
            while len(depends):
                cur_dep = depends[0]
                result = self.__get_package_destination(cur_dep)
                # Пакет из основного репозитория
                if result['status'] == PackageType.PACKAGE_FROM_MAIN:
                    remove_dependency(result['dependency'], depends)
                # Нашли зависимость в уже собранных пакетах?
                # Удаляем текущую зависимость.
                # Определяем зависимости для зависимости, включаем их в depends,
                # Включаем зависимость в список пакетов для добавления
                elif result['status'] == PackageType.PACKAGE_FROM_TARGET:
                    remove_dependency(result['dependency'], depends)
                    dpackage = [p for p in listdir(self.__conf.repodirpath)
                                if (re.match(DEB_RE, p) and re.match(DEB_RE, p).group('name') == result['package'])]
                    if len(dpackage) == 0:
                        continue
                    elif len(dpackage) > 1:
                        # В репозитории оказались больше 1 пакета с разными версиями,
                        # берем тот, версия у которого версия будет старше.
                        dpackage = sorted(dpackage, reverse=True)
                    dpackage = dpackage[0]
                    if not dpackage:
                        exit_with_error(_('Failed to find deb file \'%s\'' % result['package']))
                    new_depends = Debhelper.get_binary_depends(self.__conf.repodirpath, dpackage)
                    # Удаляем все записимости, когда-то присутствующие в новых зависимостях
                    new_depends = [d for d in new_depends if d not in all_depends]
                    logging.debug(_('Additional dependencies of \'%s\': %s') % (dpackage, new_depends))
                    depends = depends + deepcopy(new_depends)
                    all_depends = all_depends + deepcopy(new_depends)
                    if dpackage not in packages_to_add:
                        packages_to_add.append(dpackage)
                # Нашли пакет в кэше в вспомогательном (диске разработки)
                elif result['status'] == PackageType.PACKAGE_FROM_NON_MAIN:
                    exit_with_error(_('Dependency of \'%s\' \'%s\' is not on main repository') % (
                                    package_name, result['package']))
                elif result['status'] == PackageType.PACKAGE_NOT_FOUND:
                    exit_with_error(_('Could not resolve dependencies for package \'%s\': %s') % (
                                    package_name, result['package']))
                else:
                    exit_with_error('UNEXPECTED status: %s' % result)
            return packages_to_add

        # Очистка всех существующих файлов в директории
        self.__clean()
        for deb in self.__required_packages:
            packages = [p for p in listdir(self.__conf.repodirpath)
                        if (re.match(DEB_RE, p) and re.match(DEB_RE, p).group('name') == deb)]
            # В репозитории оказались больше 1 пакета с разными версиями,
            # берем тот, версия у которого версия будет старше.
            if len(packages) == 0:
                exit_with_error(_('No one binary package with name \'%s\' is found') % deb)
            if len(packages) > 1:
                packages = sorted(packages, reverse=True)
            package = packages[0]
            if package:
                ddeb = check_depends(package)
                Debhelper.copy_files(self.__conf.repodirpath, self.__conf.frepodirpath, [package])
                # Копируем зависимости для пакета
                if ddeb:
                    logging.debug(_('Copying dependencies for package \'%s\': %s') % (package, ddeb))
                    Debhelper.copy_files(self.__conf.repodirpath, self.__conf.frepodirpath, ddeb)
            else:
                exit_with_error(_('Package with name \'%s\' does not exist') % deb)
        Debhelper.generate_packages_list(self.__conf.frepodirpath)


class PackageCacheMaker:
    __DIRECTIVE_PACKAGE = 'Package: '
    __DIRECTIVE_VERSION = 'Version: '
    __DIRECTIVE_PROVIDES = 'Provides: '
    __DIRECTIVE_PROVIDES_MANY = ', '
    __DIRECTIVE_DESCRIPTION_ENDS = ''

    def __init__(self, repodirpath, mount_point, name, cache_type):
        if not exists(mount_point):
            exit_with_error(_('Path \'%s\' does not exist') % mount_point)
        self.__conf = Configuration(repodirpath)
        self.__name = name
        self.__mount_point = mount_point
        self.__cache_type = cache_type

    def run(self, is_builded=False):
        if not is_builded:
            packages_path = Debhelper.find_packages_files(self.__mount_point)
        else:
            packages_path = ['%s/Packages' % self.__mount_point]
        cache_file_path = '%s/%s.cache' % (self.__conf.cachedirpath, self.__name)
        result = {DIRECTIVE_CACHE_NAME: self.__name,
                  DIRECTIVE_CACHE_TYPE: self.__cache_type}
        packages = []
        for path in packages_path:
            try:
                with gzip.open(path, mode='rb') as gfile:
                    content = gfile.read().decode('utf-8', 'ignore')
                    lines = content.split(END_OF_LINE)
            except OSError:
                with open(path, mode='r') as fp:
                    lines = [line.rstrip('\n') for line in fp.readlines()]
            version = str()
            package_name = str()
            version = str()
            provides = None
            for line in lines:
                if line.startswith(self.__DIRECTIVE_PACKAGE):
                    package_name = line.split(self.__DIRECTIVE_PACKAGE)[1]
                elif line.startswith(self.__DIRECTIVE_VERSION):
                    version = line.split(self.__DIRECTIVE_VERSION)[1]
                elif line.startswith(self.__DIRECTIVE_PROVIDES):
                    provides_line = line.split(self.__DIRECTIVE_PROVIDES)[1]
                    if self.__DIRECTIVE_PROVIDES_MANY in provides_line:
                        provides = provides_line.split(self.__DIRECTIVE_PROVIDES_MANY)
                    else:
                        provides = [provides_line]
                elif line == self.__DIRECTIVE_DESCRIPTION_ENDS:
                    data = {
                        DIRECTIVE_CACHE_PACKAGES_PACKAGE_NAME: package_name,
                        DIRECTIVE_CACHE_PACKAGES_PACKAGE_VERSION: version,
                        DIRECTIVE_CACHE_PACKAGES_PACKAGE_PROVIDES: provides
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
    parser = ArgumentParser()
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
        root = abspath(args.path)
    except AttributeError:
        parser.print_help()
        exit(1)
    Configuration.init_logger(root)
    # Проверяем наличие прав суперпользователя
    check_root_access()
    # Проверяем наличие пакета dpkg-dev, необходимого для сборки
    Debhelper.check_is_dpkg_dev_installed()
    try:
        if args.command == COMMAND_INIT:
            initializer = RepoInitializer(root)
            initializer.run()
        elif args.command == COMMAND_BUILD:
            builder = Builder(root, abspath(args.source_list), args.clean, args.jobs)
            builder.run()
        elif args.command == COMMAND_MAKE_REPO:
            repomaker = RepoMaker2(root, abspath(args.white_list))
            repomaker.run()
        elif args.command == COMMAND_MAKE_PACKAGE_CACHE:
            cache_type = PackageType.PACKAGE_FROM_OS_REPO if args.primary else PackageType.PACKAGE_FROM_OS_DEV_REPO
            cachemaker = PackageCacheMaker(root, abspath(args.mount_path), args.name, cache_type)
            cachemaker.run()
        else:
            parser.print_help()
    except KeyboardInterrupt:
        logging.info(_('Exit on user\'s query'))
