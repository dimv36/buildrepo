#!/usr/bin/python3

import os
import sys
import logging
import re
import gettext
import apt.debfile
import apt_pkg
import glob
import shutil
import subprocess
import datetime
import configparser
import contextlib
import warnings

# gettext
_ = gettext.gettext

# Disable warnings
warnings.filterwarnings('ignore')


def exit_with_error(error):
    logging.critical(error)
    exit(1)


def form_dependency(dep):
    depname, depver, depop = dep
    if len(depver):
        return '{} ({} {})'.format(depname, depop, depver)
    return depname


@contextlib.contextmanager
def change_directory(newdirpath):
    curdir = os.path.abspath(os.path.curdir)
    if os.path.exists(newdirpath):
        os.chdir(newdirpath)
    yield
    os.chdir(curdir)


def run_command_log(cmdargs):
    proc = subprocess.Popen(cmdargs,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            universal_newlines=True)
    out, unused = proc.communicate()
    if proc.returncode:
        logging.error(_('Command {} execution failed with code {}').format(' '.join(cmdargs),
                                                                           proc.returncode))
        logging.error(_('Out was: \n{}'.format(out)))
    return proc.returncode == 0


def make_iso(isopath, target, label, tmpdir, sources_iso=False):
    with change_directory(os.path.join(tmpdir, '..')):
        what = 'iso' if not sources_iso else 'sources iso'
        logging.info(_('Building {} {} for {} ...').format(what, isopath, target))
        xorrisofs_bin = shutil.which('xorrisofs')
        if not xorrisofs_bin:
            exit_with_error(_('Failed to find {} binary').format('xorrisofs'))
        if not run_command_log([xorrisofs_bin, '-r', '-J', '-joliet-long',
                                '-o', isopath, tmpdir]):
            exit_with_error(_('Failed to create ISO image'))


class TemporaryDirManager:
    import tempfile
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = object.__new__(cls)
        return cls._instance

    def __init__(self, prefix='buildrepo', basedir=None):
        self.__dirs = []
        self.__prefix = prefix
        self.__basedir = basedir

    def set_basedir(self, basedir):
        self.__basedir = basedir

    def dirs(self):
        return self.__dirs

    def create(self):
        directory = self.tempfile.mkdtemp(prefix=self.__prefix, dir=self.__basedir)
        os.makedirs(directory, exist_ok=True)
        self.__dirs.append(directory)
        return directory


tmpdirmanager = TemporaryDirManager()


class Configuration:
    _instance = None
    _inited = False

    _CURDIR = os.getcwd()
    DEFAULT_BUILD_DIR = os.path.join(_CURDIR, 'build')
    DEFAULT_CONF = os.path.join(_CURDIR, 'buildrepo.conf')
    DEFAULT_CHROOT_SCRIPT = os.path.join(_CURDIR, 'chroot-helper.sh')

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = object.__new__(cls)
        return cls._instance

    def __init__(self, conf_path):
        if not os.path.exists(conf_path) or not os.path.isfile(conf_path):
            exit_with_error(_('Configuration does not found on {}').format(conf_path))
        self.conf_path = conf_path
        self.parser = configparser.ConfigParser()
        self.parser.read(conf_path)
        self.root = self.parser.get('common', 'build-root', fallback=self.DEFAULT_BUILD_DIR)
        self.reponame = self.parser.get('common', 'repo-name', fallback=None)
        self.repoversion = self.parser.get('common', 'repo-version', fallback=None)
        self.chroot_helper = self.parser.get('chroot', 'chroot-helper', fallback=self.DEFAULT_CHROOT_SCRIPT)
        self.distro = self.parser.get('chroot', 'distro', fallback=None)
        if not self.reponame:
            exit_with_error(_('Repository name is missing in {}').format(conf_path))
        elif not self.repoversion:
            exit_with_error(_('Repository version is missing in {}').format(conf_path))
        elif not self.distro:
            exit_with_error(_('Distro name is missing in {}').format(conf_path))
        self.__base_init()

    def __base_init(self):
        if self._inited:
            return
        for subdir in ['src', 'repo', 'logs', 'cache']:
            setattr(self, '{}dirpath'.format(subdir),
                    os.path.join(self.root, subdir, self.distro or '', self.reponame))
        for subdir in ['chroots', 'chrootsinst', 'tmp', 'iso']:
            setattr(self, '{}dirpath'.format(subdir), os.path.join(self.root, subdir))
        self.__init_logger()
        tmpdirmanager.set_basedir(self.tmpdirpath)
        self._inited = True

    def directories_created(self):
        dir_attrs = [attr for attr in dir(self) if attr.endswith('dirpath')]
        for attr in dir_attrs:
            val = getattr(self, attr)
            if not os.path.exists(val):
                return False
        return True

    def __init_logger(self):
        if os.path.exists(self.logsdirpath):
            logname = os.path.join(self.root, 'build-{}.log'.format(self.reponame))
            logging.basicConfig(level=logging.DEBUG,
                                format='%(asctime)-8s %(levelname)-8s %(message)s',
                                datefmt='%Y-%m-%d %H:%M:%S',
                                filename=logname,
                                filemode='a')
            console = logging.StreamHandler()
            console.setLevel(logging.INFO)
            formatter = logging.Formatter('%(levelname)-8s: %(message)s')
            console.setFormatter(formatter)
            logging.getLogger('').addHandler(console)
        else:
            logging.basicConfig(level=logging.INFO,
                                format='%(levelname)-8s: %(message)s')
        runmsg = _('Running {} ...').format(' '.join(sys.argv))
        build_root_msg = _('Using build-root as {}').format(self.root)
        msgmaxlen = max(len(runmsg), len(build_root_msg)) + 1
        logging.info('*' * msgmaxlen)
        logging.info(runmsg)
        logging.info(build_root_msg)
        logging.info('*' * msgmaxlen)


class ChrootDistributionInfo(dict):
    CHROOT_BUILDER_DEFAULT = 'builder'

    def __init__(self, conf):
        self.__parse_conf(conf)

    def __to_list(self, arg):
        if isinstance(arg, list):
            return arg
        elif isinstance(arg, str):
            return [a.strip() for a in arg.split(',')]

    def __parse_mirrors(self, mirrors):
        good_mirrors = []
        # Проверяем зеркала
        for mirror in mirrors:
            m = re.match(r'(?P<schema>\w+)://.*', mirror)
            if not m:
                logging.warning(_('Incorrect mirror: {}').format(mirror))
            else:
                # Проверяем схему
                schema = m.group('schema')
                if schema in ('file', 'ftp', 'http', 'https'):
                    good_mirrors.append(mirror)
                else:
                    logging.warning(_('Mirror with schema {} does not supported').format(schema))
        return good_mirrors

    def __parse_conf(self, conf):
        dists_parser = configparser.ConfigParser()
        dists_parser.read(conf.conf_path)
        items = {}
        mirrors = []
        for opt_name in sorted(dists_parser.options('chroot')):
            if re.match(r'mirror\d+', opt_name):
                mirrors.append(dists_parser.get('chroot', opt_name, fallback=''))
            elif opt_name == 'chroot-script':
                chroot_script = dists_parser.get('chroot', opt_name, fallback=None)
                if chroot_script:
                    chroot_script = os.path.abspath(chroot_script)
                items[opt_name] = chroot_script
            elif opt_name in ('components', 'debs'):
                val = self.__to_list(dists_parser.get('chroot', opt_name, fallback=[]))
                items[opt_name] = val
            elif opt_name == 'build-user':
                items[opt_name] = dists_parser.get('chroot', opt_name, fallback=None)
            elif opt_name == 'distro':
                items[opt_name] = dists_parser.get('chroot', opt_name, fallback=None)
        mirrors = self.__parse_mirrors(mirrors)
        if len(mirrors):
            items['mirrors'] = mirrors
            if not items.get('build-user'):
                items['build-user'] = self.CHROOT_BUILDER_DEFAULT
            self.update(items)
        else:
            exit_with_error(_('No one mirror is present'))


class NSPContainer:
    import tarfile
    import io

    _FIRST_MIRROR = 0
    DEFAULT_DIST_COMPONENTS = ['main', 'contrib', 'non-free']
    DEFAULT_USER_PACKAGES = []
    CHROOT_REQUIRED_DEBS = ['dpkg-dev', 'fakeroot', 'quilt', 'sudo']
    CHROOT_COMPRESSION = 'xz'

    class BuildLogger(io.FileIO):
        _ansi_escape = re.compile(r'(?:\x1B[@-_]|[\x80-\x9F])[0-?]*[ -/]*[@-~]')

        def write(self, data):
            data = self._ansi_escape.sub('', data)
            return super().write(data.encode('utf-8', errors='ignore'))

    def __init__(self, conf):
        self.__conf = conf
        self.__bind_directories = None
        self.__dist_info = ChrootDistributionInfo(self.__conf)
        self.__name = self.__dist_info.get('distro')

    @property
    def bind_directories(self):
        if not self.__bind_directories:
            # Сборочный репозиторий
            bind_directories = {self.__conf.repodirpath: ('/srv/repo', 'rw')}
            mirror_num = self._FIRST_MIRROR
            for mirror in self.__dist_info.get('mirrors'):
                if mirror.startswith('file://'):
                    src = mirror[7:]
                    dst = os.path.join('/srv', 'mirrors', 'mirror{}'.format(mirror_num))
                    bind_directories[src] = (dst, 'ro')
                    mirror_num += 1
            self.__bind_directories = bind_directories
        return self.__bind_directories

    def _exec_command_log(self, cmdargs, log_file, recreate_log=False):
        import time
        if log_file:
            mode = 'a' if os.path.exists(log_file) and not recreate_log else 'w'
            try:
                logstream = self.BuildLogger(log_file, mode='b' + mode)
            except OSError as e:
                exit_with_error(_('Error opening logfile: {}').format(e))
            if mode == 'a':
                logstream.write('\n')
                logstream.flush()
            logstream.write('Executing {} ...\n'.format(' '.join(cmdargs)))
            logstream.flush()
            start = datetime.datetime.now()
            with subprocess.Popen(cmdargs, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  universal_newlines=True) as proc:
                while True:
                    data = proc.stdout.readline()
                    if not data:
                        break
                    logstream.write(data)
            end = datetime.datetime.now() - start
            logstream.write('\nReturncode: {}'.format(proc.returncode))
            logstream.write('\nTime: {}\n'.format(time.strftime('%H:%M:%S', time.gmtime(end.seconds))))
            logstream.close()
        else:
            proc = subprocess.Popen(cmdargs, universal_newlines=True)
            proc.communicate()
        return proc.returncode

    def _exec_nspawn(self, cmdargs, container_path, log_file=None, recreate_log=False):
        nspawn_bin = shutil.which('systemd-nspawn')
        if not nspawn_bin:
            exit_with_error(_('systemd-nspawn does not found'))
        nspawn_args = [nspawn_bin, '-D', container_path,
                       '--hostname', self.__name, '-E', 'LC_ALL=C']
        for src, dstinfo in self.bind_directories.items():
            dst, mode = dstinfo
            if mode == 'ro':
                nspawn_args.append('--bind-ro={}:{}'.format(src, dst))
            elif mode == 'rw':
                nspawn_args.append('--bind={}:{}'.format(src, dst))
            else:
                logging.error(_('Incorrect bind mode: {}').format(mode))
        nspawn_args += cmdargs
        return self._exec_command_log(nspawn_args, log_file, recreate_log)

    @property
    def chroot_path(self):
        return os.path.join(self.__conf.chrootsdirpath, '{}.tar.{}'.format(
            self.__name, self.CHROOT_COMPRESSION))

    @property
    def hostname(self):
        return '{}_{}'.format(self.__conf.reponame, self.__name)

    @property
    def deploypath(self):
        return os.path.join(self.__conf.chrootsinstdirpath,
                            self.hostname)

    def exists(self):
        return os.path.isfile(self.chroot_path) and os.path.exists(self.chroot_path)

    def deployed(self):
        return os.path.isdir(self.deploypath) and os.path.exists(self.deploypath)

    @property
    def sources_dir(self):
        return os.path.join('/srv', 'build')

    @property
    def abs_sources_dir(self):
        return os.path.join(self.deploypath, self.sources_dir[1:])

    @property
    def name(self):
        return self.__name

    def deploy(self, recreate=False):
        if self.deployed() and recreate:
            # TODO:: Блокировки systemd-nspawn
            try:
                shutil.rmtree(self.deploypath)
            except Exception as e:
                exit_with_error(_('Failed to remove deploy path {}: {}').format(self.deploypath, e))
        elif self.deployed():
            return
        logging.info(_('Deploying {} to {} ...').format(self.__name, self.deploypath))
        with change_directory(self.__conf.chrootsinstdirpath):
            try:
                with self.tarfile.open(self.chroot_path,
                                       mode='r:{}'.format(self.CHROOT_COMPRESSION)) as tf:
                    tf.extractall()
                shutil.move(self.__name, self.hostname)
            except Exception as e:
                exit_with_error(_('Chroot deployment {} failed: {}').format(self.__name, e))

    def build_package(self, dsc_file_path, jobs):
        # В первую очередь генерируем environment file,
        # используемый скриптом сборки
        try:
            with open(os.path.join(self.deploypath, 'srv', 'runtime-environment'), mode='w') as fp:
                fp.write('export DEB_BUILD_OPTIONS="nocheck parallel={}"\n'.format(jobs))
        except RuntimeError:
            raise RuntimeError(_('Runtime environment file generation failure'))
        # Файл создали, теперь формируем путь к логу
        m = re.match(r'.*/(?P<name>.*)_(?P<version>.*)\.dsc', dsc_file_path)
        pname, pversion = m.group('name'), m.group('version')
        log_file = os.path.join(self.__conf.logsdirpath, '{}_{}.log'.format(pname, pversion))
        # Формируем команду на запуск
        logging.info(_('Package building {}-{} ...'.format(pname, pversion)))
        chroot_helper_path = os.path.join('/srv', os.path.basename(self.__conf.chroot_helper))
        returncode = self._exec_nspawn(['--chdir=/srv', chroot_helper_path, dsc_file_path],
                                       self.deploypath, log_file, recreate_log=True)
        if returncode:
            raise RuntimeError(_('Package building {} failed').format(pname))

    def login(self, bind=[]):
        self.bind_directories
        for directory in bind:
            m = re.match(r'(?P<target>.*):(?P<dest>.*)', directory)
            if not m:
                logging.warning(_('Incorrect bind item: {}').format(directory))
                continue
            self.__bind_directories[m.group('target')] = (m.group('dest'), 'rw')
        if self._exec_nspawn(['/bin/bash'], self.deploypath, log_file=None):
            raise RuntimeError(_('Failed to login to deployed container {}'.format(self.name)))

    def create(self):
        def chroot_exclude_filter(tarinfo):
            if os.path.isfile(tarinfo.name) and re.match(r'.*/var/cache/apt/archives/.*.deb$', tarinfo.name):
                return None
            return tarinfo

        tmpdir = tmpdirmanager.create()
        dist_chroot_dir = os.path.join(tmpdir, self.__name)
        try:
            logging.info(_('Running bootstrap for chroot {} ...').format(self.__name))
            debootstrap_bin = shutil.which('debootstrap')
            if not debootstrap_bin:
                exit_with_error(_('Failed to find debootrap'))
            # Мы полагаем, что первого зеркала достаточно для bootstrap'а
            mirrors = self.__dist_info.get('mirrors')
            debootstrap_args = [debootstrap_bin,
                                '--no-check-gpg', '--verbose', '--variant=minbase',
                                '--components={}'.format(','.join(self.__dist_info.get('components'))),
                                self.__name, dist_chroot_dir,
                                mirrors[0]]
            chroot_script = self.__dist_info.get('chroot-script')
            if chroot_script:
                debootstrap_args.append(chroot_script)
            # Формируем путь к лог-файлу для лога debootstrap
            logpath = os.path.join(os.path.dirname(self.__conf.logsdirpath), 'chroot-{}.log'.format(self.__name))
            returncode = self._exec_command_log(debootstrap_args, logpath, recreate_log=True)
            if returncode:
                raise RuntimeError(_('Debootstrap failed: {}').format(returncode))
            # Создаем основные каталоги для сборки
            for srv_subdirs in ('build', 'repo'):
                subdir = os.path.join(dist_chroot_dir, 'srv', srv_subdirs)
                os.makedirs(subdir)
            # Формируем sources.list для контейнера
            chroot_apt_sources = os.path.join(dist_chroot_dir, 'etc', 'apt', 'sources.list')
            with open(chroot_apt_sources, mode='w') as apt_sources:
                # Сначала пропишем сборочный репозиторий
                apt_sources.write('deb file:///srv repo/\n')
                mirror_num = self._FIRST_MIRROR
                components = ' '.join(self.__dist_info.get('components'))
                for url in mirrors:
                    if url.startswith('file://'):
                        # Требуется создать каталоги под репозитории
                        url = os.path.join('srv', 'mirrors', 'mirror{}'.format(mirror_num))
                        mirror_num += 1
                        os.makedirs(os.path.join(dist_chroot_dir, url))
                        apt_sources.write('deb file:///{url} {dist} {components}\n'.format(url=url,
                                                                                           dist=self.__name,
                                                                                           components=components))
                    else:
                        apt_sources.write('deb {url} {dist} {components}\n'.format(url=url,
                                                                                   dist=self.__name,
                                                                                   components=components))
            # Подготавливаем APT
            chroot_apt_conf = os.path.join(dist_chroot_dir, 'etc', 'apt', 'apt.conf.d', '1000-buildrepo.conf')
            with open(chroot_apt_conf, mode='w') as apt_conf:
                apt_conf.write('APT::Get::Install-Recommends "false";\n')
                apt_conf.write('APT::Get::Install-Suggests "false";\n')
                apt_conf.write('APT::Get::Assume-Yes "true";\n')
                apt_conf.write('Acquire::AllowInsecureRepositories "true";\n')
                apt_conf.write('APT::Get::AllowUnauthenticated "true";\n')
            # Настраиваем /etc/hosts
            chroot_etc_hosts = os.path.join(dist_chroot_dir, 'etc', 'hosts')
            with open(chroot_etc_hosts, mode='w') as host_conf:
                host_conf.write('127.0.0.1\tlocalhost\n')
                host_conf.write('127.0.0.1\t{}\n'.format(self.__name))
            # Создаем каталоги в chroot'е для сборки
            # Копируем скрипт сборки пакетов в chroot'е
            dst = os.path.join(dist_chroot_dir, 'srv', 'chroot-helper.sh')
            logging.info(_('Copy chroot helper script ...'))
            shutil.copy(self.__conf.chroot_helper, dst)
            os.chmod(dst, 0o755)            # Создаем пользователя, от имени которого будем вести сборку
            build_user = self.__dist_info.get('build-user')
            logging.info(_('Create user {} in chroot {} ...').format(build_user, self.__name))
            returncode = self._exec_nspawn(['/sbin/adduser', build_user,
                                            '--disabled-password', '--gecos', 'chroot-builder'],
                                           dist_chroot_dir, logpath)
            if returncode:
                raise RuntimeError(_('User creation failed').format(build_user))
            # Создаем environment с основными переменными окружения
            chroot_main_env = os.path.join(dist_chroot_dir, 'srv', 'environment')
            with open(chroot_main_env, mode='w') as fp:
                fp.write('export BUILDUSER="{}"\n'.format(build_user))
            # Устанавливаем необходимые пакеты для сборки
            logging.info(_('Updating APT cache in chroot {} ...').format(self.__name))
            returncode = self._exec_nspawn(['apt-get', 'update'],
                                           dist_chroot_dir, logpath)
            if returncode:
                raise RuntimeError(_('APT cache update failed'))
            logging.info(_('Installing required packages in chroot {} ...').format(self.__name))
            returncode = self._exec_nspawn(['apt-get', 'install'] + self.CHROOT_REQUIRED_DEBS,
                                           dist_chroot_dir, logpath)
            if returncode:
                raise RuntimeError(_('Required packages installation in chroot failed'))
            user_selected_packages = self.__dist_info.get('debs', [])
            if len(user_selected_packages):
                logging.info(_('Installing user defined packages {} in chroot {} ...').format(
                    ', '.join(user_selected_packages), self.__name))
                returncode = self._exec_nspawn(['apt-get', 'install'] + user_selected_packages,
                                               dist_chroot_dir, logpath)
                if returncode:
                    raise RuntimeError(_('User selected packages installation in chroot failed'))
            # Создаем архив из chroot'а
            with change_directory(tmpdir):
                compressed_tar_path = os.path.join(tmpdir, '{}.tar.{}'.format(self.__name, self.CHROOT_COMPRESSION))
                logging.info(_('Creating archive with chroot {} ...').format(self.__name))
                with self.tarfile.open(compressed_tar_path,
                                       mode='w:{}'.format(self.CHROOT_COMPRESSION)) as tf:
                    tf.add(self.__name, filter=chroot_exclude_filter)
                # Move to chroot storage
                dst = os.path.join(self.__conf.chrootsdirpath, '{}.tar.{}'.format(self.__name, self.CHROOT_COMPRESSION))
                logging.info(_('Moving chroot to {} ...').format(dst))
                shutil.move(compressed_tar_path, dst)
        except Exception as e:
            exit_with_error(_('Chroot building failed: {}').format(e))


class BaseCommand:
    cmd = None
    cmdhelp = None
    alias = None
    root_required = False
    required_binaries = []

    def __init__(self, conf):
        conf = os.path.abspath(conf)
        if not os.path.exists(os.path.dirname(conf)):
            os.makedirs(os.path.dirname(conf))
        self._conf = Configuration(conf)
        if self.root_required and not os.getuid() == 0:
            exit_with_error(_('Must be run as superuser'))
        self.__check_required_binaries()
        if not self.cmd == 'init' and not self._conf.directories_created():
            exit_with_error(_('Required directories not created. '
                              'Please, run `{} init` first.').format(sys.argv[0]))

    def __check_required_binaries(self):
        missing_binaries = []
        for binary in self.required_binaries:
            if shutil.which(binary) is None:
                missing_binaries.append(binary)
        if len(missing_binaries):
            logging.warning(_('Missing binaries on host: {}').format(', '.join(missing_binaries)))
            logging.warning(_('Some steps of current command may failed'))

    def run(self):
        raise NotImplementedError()


class PackageType:
    (PACKAGE_BUILDED,
     PACKAGE_FROM_OS_REPO,
     PACKAGE_FROM_EXT_REPO,
     PACKAGE_FROM_OS_DEV_REPO,
     PACKAGE_FROM_EXT_DEV_REPO,
     PACKAGE_NOT_FOUND) = range(0, 6)

    @classmethod
    def available_types(cls):
        for obj in filter(lambda x: x.startswith('PACKAGE'), dir(cls)):
            yield getattr(cls, obj)


class DependencyFinder:
    FLAG_FINDER_MAIN = 1 << 0
    FLAG_FINDER_DEV = 1 << 1
    (DF_DEST,
     DF_PKGINFO,
     DF_RESOLVED,
     DF_REQUIRED) = range(4)

    def __init__(self, pkgname, rfcache, conf,
                 exclude_rules=None, black_list=[], flags=FLAG_FINDER_MAIN):
        self.__deps = []
        self.__rfcache = rfcache
        self.__exclude_rules = exclude_rules
        self.__black_list = black_list
        self.__conf = conf
        self.__flags = flags
        self.__seendeps = []
        pkg_deps_info = self.__find_dep(pkgname)
        self.__recurse_deps(self.__deps, pkg_deps_info)

    @property
    def deps(self):
        return self.__deps

    def __exec_exclude_filter(self, binaries, filter_list, func=None):
        if not (self.__flags & DependencyFinder.FLAG_FINDER_MAIN):
            return binaries
        processed = []
        for binary in binaries:
            pkgname, *unused = binary
            match = False
            for exclude in filter_list:
                if func(pkgname, exclude):
                    match = True
                    break
            if not match:
                processed.append(binary)
        return processed

    def __recurse_deps(self, s, p):
        required_by = form_dependency(p)
        dest, *unused, deps, binaries = self.__rfcache.find_dependencies(p, required_by)
        if dest == PackageType.PACKAGE_BUILDED:
            assert binaries is not None, 'binaries not found'
            if self.__flags & DependencyFinder.FLAG_FINDER_MAIN:
                # Прокатываем exclude_rules -- отсекаем по имени
                # пакеты, которые должны попасть на второй диск
                filtered_binaries = self.__exec_exclude_filter(binaries,
                                                               self.__exclude_rules,
                                                               func=lambda x, y: x.endswith(y))
                # Прокатываем black list
                if len(self.__black_list):
                    filtered_binaries = self.__exec_exclude_filter(filtered_binaries,
                                                                   self.__black_list,
                                                                   func=lambda x, y: x == y)
                # Считаем эти бинари зависимостями на основной диск
                for binary in filtered_binaries:
                    dependency = [binary]
                    if dependency not in deps:
                        deps.append(dependency)
        for dep in deps:
            if len(dep) == 1:
                dep = tuple(dep[0])
                seen_item = (dep, required_by,)
                if seen_item in self.__seendeps:
                    continue
                depdest, pdstinfo, resolved, *unused = self.__rfcache.find_dependencies(*seen_item)
                self.__seendeps.append(seen_item)
                if depdest == PackageType.PACKAGE_NOT_FOUND:
                    i = (depdest, pdstinfo, resolved, required_by)
                    assert (len(i) == 4), (i, len(i))
                    if i not in s:
                        s.append(i)
                else:
                    item = (depdest, pdstinfo, resolved, required_by)
                    if item not in s:
                        assert (len(item) == 4), (item, len(item))
                        s.append(item)
                        self.__recurse_deps(s, dep)
            else:
                # Альтернативная зависимость
                item = None
                resolved = None
                for dpitem in dep:
                    dpitem = tuple(dpitem)
                    seen_item = (dpitem, required_by,)
                    if seen_item in self.__seendeps:
                        continue
                    depdest, pdstinfo, resolved, *unused = self.__rfcache.find_dependencies(*seen_item)
                    if depdest == PackageType.PACKAGE_NOT_FOUND:
                        i = (depdest, pdstinfo, resolved, required_by)
                        assert (len(i) == 4), (item, len(i))
                        if i not in s:
                            s.append(i)
                    else:
                        item = (depdest, pdstinfo, resolved, required_by)
                        if item not in s:
                            assert (len(item) == 4), (item, len(item))
                            s.append(item)
                            self.__recurse_deps(s, dpitem)
                        break
                if self.__flags & DependencyFinder.FLAG_FINDER_MAIN and not item:
                    alt_dep_full = ' | '.join(form_dependency(d) for d in dep)
                    # Мы не смогли удовлетворить альтеранитивные зависимости. Добавляем последнюю
                    last_alt_dep = dep[-1]
                    item = (depdest, last_alt_dep, resolved, required_by)
                    logging.warning(_('Runtime dependency resolving {} failed for {}').format(
                        alt_dep_full, form_dependency(p)))
                    if item not in s:
                        assert (len(item) == 4), (item, len(item))
                        s.append(item)
                        self.__recurse_deps(s, last_alt_dep)

    def __find_dep(self, pkgname):
        def sortversions(versions):
            for i in range(1, len(versions)):
                item_to_insert = versions[i]
                j = i - 1
                while j >= 0 and apt_pkg.check_dep(versions[j], '<', item_to_insert):
                    versions[j + 1] = versions[j]
                    j -= 1
                versions[j + 1] = item_to_insert
            return versions

        pkg_glob_re = os.path.join(self.__conf.repodirpath, '{}_*.deb'.format(pkgname))
        packages = glob.glob(pkg_glob_re)
        versions = [re.match(r'.*_(?P<version>.*)_.*\.deb', p).group('version') for p in packages]
        if not len(packages):
            exit_with_error(_('Failed find package {} in repo').format(pkgname))
        elif len(packages) > 1:
            versions = sortversions(versions)
            logging.warning(_('Found {} versions of package {}: {}').format(
                len(packages), pkgname, ', '.join(versions)))
            version = versions[0]
            logging.warning(_('Will be processed {} = {}').format(pkgname, version))
            pkg_glob_re = os.path.join(self.__conf.repodirpath, '{}_{}_*.deb'.format(pkgname, version))
        else:
            version = versions[0]
        # Нашли зависимости?
        pkg = (pkgname, version, '=')
        depitem = (PackageType.PACKAGE_BUILDED,         # Где есть зависимость
                   pkg,                                 # Информация для анализа
                   (pkgname, version),                  # Как разрешается
                   form_dependency(pkg))                # Строка зависимости
        self.__deps.append(depitem)
        return pkg


class DebianIsoRepository:
    import platform

    def __init__(self, conf, tmpdir, is_dev):
        self.__conf = conf
        self.__is_dev = is_dev
        repotype = 'main' if not is_dev else 'dev'
        self.__tmpdir = os.path.join(tmpdir, '{}_{}_iso'.format(self.__conf.reponame, repotype))
        self.__name = self.__conf.reponame
        if self.__is_dev:
            self.__name = '{}-devel'.format(self.__name)
        self.__codename = self.__conf.distro
        self.__reprepro_bin = None
        self.__arch = None
        self.__base_init()

    def __base_init(self):
        conf_directory = os.path.join(self.__tmpdir, 'conf')
        os.makedirs(conf_directory, exist_ok=True)
        with open(os.path.join(conf_directory, 'distributions'), mode='w') as fp:
            codename = '{}{}'.format(self.__conf.reponame,
                                     '-devel' if self.__is_dev else '')
            fp.write('Codename: {}\n'.format(codename))
            fp.write('Version: {}\n'.format(self.__conf.repoversion))
            fp.write('Description: {} repository\n'.format(self.__conf.reponame))
            self.__arch = self.platform.machine()
            if self.__arch == 'x86_64':
                self.__arch = 'amd64'
            fp.write('Architectures: {}\n'.format(self.__arch))
            fp.writelines(['Components: main contrib non-free\n',
                           'DebIndices: Packages Release . .gz .bz2\n',
                           'Contents: . .gz .bz2\n'])
        self.__reprepro_bin = shutil.which('reprepro')
        if self.__reprepro_bin is None:
            exit_with_error(_('Failed to find reprepro binary'))
        with change_directory(self.__tmpdir):
            if not run_command_log([self.__reprepro_bin, 'export']):
                exit_with_error(_('Reprepro initialization failed'))
        disk_directory = os.path.join(self.__tmpdir, '.disk')
        os.makedirs(disk_directory, exist_ok=True)
        with open(os.path.join(disk_directory, 'info'), mode='w') as fp:
            fp.write('{reponame} {version} ({distro}) - {arch} DVD\n'.format(reponame=self.__name,
                                                                             version=self.__conf.repoversion,
                                                                             distro=self.__conf.distro,
                                                                             arch=self.__arch))

    def create(self, packagesdir):
        with change_directory(self.__tmpdir):
            logging.info(_('Creating repository for {} via reprepro ...').format(self.__name))
            for package in glob.glob('{}/*.deb'.format(packagesdir)):
                if not run_command_log([self.__reprepro_bin, 'includedeb',
                                        self.__name, package]):
                    exit_with_error(_('Including binaries to repo failure'))
            # Удаление ненужных директорий
            for directory in ['db', 'conf']:
                shutil.rmtree(directory)
        now = datetime.datetime.now().strftime('%Y-%m-%d')
        isoname = '{repo}_{version}_{distro}_{date}.iso'.format(repo=self.__name,
                                                                version=self.__conf.repoversion,
                                                                distro=self.__conf.distro,
                                                                date=now)
        isopath = os.path.join(self.__conf.isodirpath, isoname)
        label = '{repo} {version} ({distro}) {arch}'.format(repo=self.__name,
                                                            version=self.__conf.repoversion,
                                                            distro=self.__conf.distro,
                                                            arch=self.__arch)
        make_iso(isopath, self.__name, label, self.__tmpdir)


class RepositoryFullCache:
    def __init__(self, conf):
        self.__conf = conf
        self.__result_cache = {}
        self.__caches = []

    def load(self):
        cache_paths = (glob.glob(os.path.join(self.__conf.root, 'cache', self.__conf.distro, '*.cache')) +
                       glob.glob(os.path.join(self.__conf.cachedirpath, '*.cache')))
        for cache_path in cache_paths:
            self.__caches.append(RepositoryCache.load(self.__conf, cache_path))
        self.__caches = sorted(self.__caches)
        # Валидация
        if len(self.__caches) <= 1:
            exit_with_error(_('Caches for OS and OS-DEV repositories are required'))
        for repo, repo_type in (('OS', PackageType.PACKAGE_FROM_OS_REPO),
                                ('OS-DEV', PackageType.PACKAGE_FROM_OS_DEV_REPO)):
            caches = self._cache_by_type(repo_type)
            if len(caches) < 1:
                exit_with_error(_('Cache for {} repo is required').format(repo))
            elif len(caches) > 1:
                logging.warning(_('Found {} {} repos').format(len(caches), repo))

    def _cache_by_type(self, ctype):
        return [c for c in self.__caches if c.ctype == ctype]

    @property
    def builded_cache(self):
        return self._cache_by_type(PackageType.PACKAGE_BUILDED)[0]

    def binaries_from_same_sources(self, dep):
        build_cache = self.builded_cache
        return build_cache.binaries_from_same_sources(dep)

    def find_dependencies(self, dep, required_by):
        # Note: Тип, строка зависимости, котеж имя-версия пакета, зависимости
        # или PackageType.PACKAGE_NOT_FOUND, depstr, None, None
        depstr = form_dependency(dep)
        item = None
        logging.debug(_('Finding dependency {} required by {} ...'.format(depstr, required_by)))
        # Наивная проверка: пробуем кэш результатов
        if dep in self.__result_cache:
            return self.__result_cache.get(dep)
        for c in self.__caches:
            depinfo = c.find_dependency(dep)
            if depinfo is not None:
                resolved, deps, binaries = depinfo
                logging.debug(_('Dependency {} resolved by {} = {} ({} repo)').format(
                    depstr, *resolved, c.name))
                item = c.ctype, depstr, resolved, deps, binaries
                break
        if item is None:
            logging.debug(_('Dependency {} NOT FOUND').format(depstr))
            item = PackageType.PACKAGE_NOT_FOUND, depstr, None, required_by, None
        # Кэшируем результат
        self.__result_cache[dep] = item
        return item


class RepositoryCache:
    import gzip
    import json

    FLAG_DESTINATION = 1 << 0
    FLAG_DEPS = 1 << 1

    def __init__(self, conf, name, ctype, packages=[]):
        self.__conf = conf
        self.__name = name
        self.__ctype = ctype
        if self.__ctype not in (PackageType.PACKAGE_FROM_OS_REPO,
                                PackageType.PACKAGE_FROM_OS_DEV_REPO):
            self.__cache_path = os.path.join(self.__conf.cachedirpath,
                                             '{}.cache'.format(self.__name))
        else:
            cache_path = os.path.join(self.__conf.cachedirpath, '..', '{}.cache'.format(self.__name))
            self.__cache_path = os.path.abspath(cache_path)
        self.__packages = packages

    def __repr__(self):
        return '{classname}: {name} ({type})'.format(classname=self.__class__.__name__,
                                                     name=self.__name,
                                                     type=self.__ctype)

    def __len__(self):
        return len(self.__packages)

    def __lt__(self, other):
        return self.__ctype < other.__ctype

    @property
    def cache_path(self):
        return self.__cache_path

    @property
    def ctype(self):
        return self.__ctype

    @property
    def name(self):
        return self.__name

    @property
    def pkginfo(self):
        return self.__packages

    @property
    def packages(self):
        return (sorted(p.get('package') for p in self.__packages if not p['virtual']))

    def create(self, packages_path):
        version_fix_re = r'(?P<name>.*) (.*)'

        def process_line_buffer(line_buffer):
            pkginfo = {}
            keys = ['Package', 'Version', 'PreDepends', 'Depends', 'Provides']
            is_builded = self.__ctype == PackageType.PACKAGE_BUILDED
            if is_builded:
                keys.append('Source')
            for line in line_buffer:
                for key in keys:
                    key_re = r'{}: (?P<value>.*)'.format(key)
                    m = re.match(key_re, line)
                    if m:
                        value = m.group('value')
                        if key in ('PreDepends', 'Depends', 'Provides'):
                            value = apt_pkg.parse_depends(value)
                        pkginfo[key.lower()] = value
            pkginfo['virtual'] = False
            # Фикс для имени исходного кода
            # например, java-common (0.58)
            if is_builded:
                for key in ('package', 'source'):
                    val = pkginfo.get(key, None)
                    if val:
                        m = re.match(version_fix_re, val)
                        if m:
                            val = m.group('name')
                            pkginfo[key] = val
            full_pkginfo = []
            if 'provides' in pkginfo:
                provides = pkginfo.get('provides')
                for item in provides:
                    for subdep in item:
                        pr_name, pr_version, pr_op = subdep
                        pr_info = {
                            'package': pr_name,
                            'virtual': True,
                            'real_package': pkginfo.get('package'),
                            'real_version': pkginfo.get('version')
                        }
                        if len(pr_version):
                            pr_info['version'] = pr_version
                        if len(pr_op):
                            pr_info['op'] = pr_op
                        full_pkginfo.append(pr_info)
            full_pkginfo.insert(0, pkginfo)
            return full_pkginfo

        packages = []
        for path in packages_path:
            try:
                with self.gzip.open(path, mode='rb') as gfile:
                    content = gfile.read().decode('utf-8', 'ignore')
                    lines = content.split('\n')
            except OSError:
                with open(path, mode='r') as fp:
                    lines = [line.rstrip('\n') for line in fp.readlines()]
            line_buffer = []
            idx_line = 0
            while idx_line < len(lines):
                line = lines[idx_line]
                if len(line):
                    line_buffer.append(line)
                else:
                    if len(line_buffer):
                        packages += process_line_buffer(line_buffer)
                        line_buffer.clear()
                idx_line += 1
        self.__packages = packages
        # Запись на диск
        with open(self.__cache_path, mode='w') as out:
            cache_obj = {'name': self.__name,
                         'ctype': self.__ctype,
                         'packages': self.__packages}
            out.write(self.json.dumps(cache_obj, sort_keys=True, indent=4))
        return len(self) > 0

    @classmethod
    def load(cls, conf, cache_path):
        try:
            with open(cache_path, mode='r') as fp:
                cache_obj = cls.json.load(fp)
        except OSError as e:
            exit_with_error(_('Failed to load cache in {}: {}').format(cache_path, e))
        name = cache_obj.get('name', None)
        if not name:
            exit_with_error(_('Missing cache name in {}').format(cache_path))
        ctype = cache_obj.get('ctype', None)
        if ctype is None:
            exit_with_error(_('Missing cache type in {}').format(cache_path))
        else:
            try:
                ctype = int(ctype)
                if ctype not in PackageType.available_types():
                    raise RuntimeError(_('Unexpected cache type in {}').format(cache_path))
            except Exception as e:
                exit_with_error(_('Bad cache type: {} in {}').format(ctype, cache_path, e))
        packages = cache_obj.get('packages', None)
        if packages is None:
            exit_with_error(_('Missing packages in {}').format(cache_path))
        return RepositoryCache(conf, name, ctype, packages)

    def __check_dep(self, pkgver, depop, depver):
        return (apt_pkg.check_dep(pkgver, depop, depver) or
                pkgver == depver)

    def __process_virtual_dependency(self, vdep):
        vpkgname, vdepver, vdepop = vdep
        for pkginfo in self.__packages:
            pkgname, pkgver = pkginfo.get('package'), pkginfo.get('version', '')
            if pkgname == vpkgname:
                if self.__check_dep(pkgver, vdepver, vdepop):
                    deptuple = (pkginfo.get('real_package'),
                                pkginfo.get('real_version'),
                                '=')
                    logging.debug(_('Dependency {} is virtual, provided by {}').format(
                        form_dependency(vdep), form_dependency(deptuple)))
                    return self.find_dependency(deptuple)
                assert 'Check virtual dependency failed'
        return None

    def find_dependency(self, dep):
        def fix_debian_version(ver):
            m = re.match(r'.*:(?P<realver>.*)', ver)
            return m.group('realver') if m else ver

        depname, depver, depop = dep
        for pkginfo in self.__packages:
            pkgname, pkgver = pkginfo.get('package'), pkginfo.get('version')
            if pkgname == depname:
                # Проверяем имя пакета и его версию
                is_virtual = pkginfo.get('virtual')
                if is_virtual:
                    # Виртуальный?
                    # Берем зависимости от реального пакета
                    vdep = (pkgname,
                            pkginfo.get('op', ''),
                            pkginfo.get('version', ''))
                    return self.__process_virtual_dependency(vdep)
                # NB: получаем корректную версию
                pkgver = fix_debian_version(pkgver)
                depver = fix_debian_version(pkgver)
                if self.__check_dep(pkgver, depop, depver):
                    source = pkginfo.get('source') or pkgname
                    if source:
                        binaries = self.binaries_for_source(source, pkginfo.get('version'))
                    else:
                        binaries = None
                    return (pkgname, pkgver), pkginfo.get('depends', []), binaries
        return None

    def binaries_for_source(self, source, version):
        binaries = []
        for pkginfo in self.__packages:
            pkgsource, pkgver = pkginfo.get('source'), pkginfo.get('version')
            if pkgsource == source and pkgver == version:
                binaries.append((pkginfo.get('package'),
                                 pkgver,
                                 '='))
        return binaries

    def source_package(self, binary_package, version):
        for pkginfo in self.__packages:
            pkgname, pkgver = pkginfo.get('package'), pkginfo.get('version')
            # NB: Здесь используется проверка на вхождение по версии,
            # поскольку имеет место быть несовпадение версии исходника и бинарного пакета
            # например: 1:3.1+dfsg-2 и 3.1+dfsg-2
            if binary_package == pkgname and version in pkgver:
                # NB: Если source у пакета отстуствует, то
                # это поле считается равным имени бинарного пакета
                return pkginfo.get('source') or binary_package
        return None


class SourcesList:
    (SL_PKGNAME,
     SL_PKGVERSION) = range(2)

    def __init__(self, conf):
        self.__conf = conf
        self.__build_list = []
        sources_list_path = self.__conf.parser.get(BuildCmd.cmd, 'source-list', fallback=None)
        if not sources_list_path:
            exit_with_error(_('Source list does not specified in {}').format(self.__conf.conf_path))
        sources_list_path = os.path.abspath(sources_list_path)
        if not os.path.exists(sources_list_path):
            exit_with_error(_('File {} does not exists').format(sources_list_path))
        self.__sources_list_path = sources_list_path

    def load(self):
        logging.info(_('Loading sources list from {} ...').format(self.__sources_list_path))
        with open(self.__sources_list_path) as fp:
            for line in fp.readlines():
                line = line.strip()
                if line.startswith('#') or not len(line):
                    continue
                tokens = line.split(' ')
                if len(tokens) == 1:
                    self.__build_list.append((line, ''))
                elif len(tokens) == 2:
                    self.__build_list.append((tokens[self.SL_PKGNAME], tokens[self.SL_PKGVERSION]))
                else:
                    logging.warning(_('Mailformed line {} in {}').format(line, self.__sources_list_path))
                    continue
        if not len(self.__build_list):
            logging.warning(_('No one sources are found in {}').format(self.__sources_list_path))
            exit(0)

    @property
    def build_list(self):
        return self.__build_list

    @property
    def path(self):
        return self.__sources_list_path

    @property
    def build_list_str(self):
        return '\n'.join(p[self.SL_PKGNAME]
                         if not len(p[self.SL_PKGVERSION]) else
                         '{} = {}'.format(p[self.SL_PKGNAME], p[self.SL_PKGVERSION])
                         for p in self.__build_list)


class _RepoAnalyzerCmd(BaseCommand):
    _DEFAULT_DEV_PACKAGES_SUFFIXES = ['dbg', 'dbgsym', 'doc', 'dev']
    alias = 'binary-repo'

    def __init__(self, conf_path):
        super().__init__(conf_path)
        self._white_list_path = self._conf.parser.get(_RepoAnalyzerCmd.alias, 'white-list', fallback=None)
        self._dev_packages_suffixes = self._conf.parser.get(_RepoAnalyzerCmd.alias, 'dev-package-suffixes',
                                                            fallback=MakeRepoCmd._DEFAULT_DEV_PACKAGES_SUFFIXES)
        if not self._white_list_path:
            exit_with_error(_('White list does not specified in {}').format(self._conf.conf_path))
        if not os.path.exists(self._white_list_path):
            exit_with_error(_('File {} does not exist').format(self.__white_list_path))
        if isinstance(self._dev_packages_suffixes, str):
            self._dev_packages_suffixes = [item.strip() for item in self._dev_packages_suffixes.split(',')]
        logging.info(_('Using {} rule for packages for 2nd disk').format(
            ', '.join(self._dev_packages_suffixes)))
        self._packages = {}
        self._caches = []
        self._rfcache = RepositoryFullCache(self._conf)
        self.__build_cache_of_builded_packages()
        self._rfcache.load()
        self._builded_cache = self._rfcache.builded_cache
        self.__parse_white_list()

    def __parse_white_list(self):
        i = 1
        last_section = None
        for line in open(self._white_list_path, mode='r').readlines():
            i += 1
            if line.startswith('#') or line == '\n':
                continue
            line = line.rstrip('\n')
            if line.startswith('[') and line.endswith(']'):
                last_section = line[1:-1]
                self._packages[last_section] = []
            else:
                if last_section is None:
                    exit_with_error(_('Got package at line {}, '
                                      'but section expected').format(i))
                packages = self._packages.get(last_section)
                if line in packages:
                    logging.warning(_('Package {} already in {}, skipped').format(line, last_section))
                    continue
                packages.append(line)
                self._packages[last_section] = packages
        if 'target' not in self._packages:
            exit_with_error(_('White list for target repository is empty'))
        # Проверка на пересечение
        all_pkgs = set()
        for section, packages in self._packages.items():
            if not len(all_pkgs):
                all_pkgs = set(packages)
                continue
            if (all_pkgs & set(packages)):
                exit_with_error(_('Intersection is found in lists'))

    def __build_cache_of_builded_packages(self):
        logging.info(_('Building cache for builded packages ...'))
        maker = MakePackageCacheCmd(self._conf.conf_path)
        maker.run(mount_path=self._conf.repodirpath,
                  name='builded',
                  ctype=PackageType.PACKAGE_BUILDED,
                  info_message=False)

    def _get_depends_for_package(self, package, exclude_rules=None,
                                 black_list=None, flags=DependencyFinder.FLAG_FINDER_MAIN):
        depfinder = DependencyFinder(package,
                                     self._rfcache,
                                     self._conf,
                                     exclude_rules, black_list, flags)
        return depfinder.deps

    def _emit_unresolved(self, unresolve, exit=True):
        for p in unresolve:
            state, dependency, resolved, required_by = p
            logging.error(_('Could not resolve dependency {} for {}'.format(dependency, required_by)))
        if exit:
            exit_with_error(_('Could not resolve dependencies'))

    def _emit_resolved_in_dev(self, deps_in_dev, exit=True):
        for p in deps_in_dev:
            state, dependency, resolved, required_by = p
            logging.error(_('Dependency {} for {} found in one of dev of ext-dev repo').format(
                          dependency, required_by))
        if exit:
            exit_with_error(_('Could not resolve dependencies'))

    def _emit_deps_summary(self, all_unresolved, all_in_dev):
        def sort_deps(deps):
            res = {}
            for dep_info in deps:
                unused, dependency, unused2, required_by = dep_info
                req_values = res.get(required_by, [])
                req_values.append(dependency)
                req_values = sorted(list(set(req_values)))
                res[required_by] = req_values
            return res

        def print_items(deps):
            for dep, requirements in deps.items():
                sys.stdout.write(_('Package {}:\n').format(dep))
                for req in requirements:
                    sys.stdout.write('\t{}\n'.format(req))

        unresolved_hash = sort_deps(all_unresolved)
        sys.stdout.write(_('***** Unresolved ***** :\n'))
        print_items(unresolved_hash)
        in_dev_hash = sort_deps(all_in_dev)
        sys.stdout.write('\n')
        sys.stdout.write(_('***** Found in dev: *****\n'))
        print_items(in_dev_hash)
        sys.stdout.write(_('Summary: unresolved: {}, deps in dev: {}\n').format(len(all_unresolved), len(all_in_dev)))


class RepoInitializerCmd(BaseCommand):
    cmd = 'init'
    cmdhelp = _('Initializes directories used for building')
    """
    Класс выполняет подготовку при инициализации репозитория
    """

    def run(self):
        """
        Создает директории в корневой директории
        """
        for directory in [self._conf.srcdirpath,
                          self._conf.repodirpath,
                          self._conf.logsdirpath,
                          self._conf.cachedirpath]:
            if os.path.exists(directory):
                shutil.rmtree(directory)
            logging.info(_('Creating directory {} ...').format(directory))
            os.makedirs(directory, exist_ok=True)
        for directory in [self._conf.chrootsdirpath,
                          self._conf.chrootsinstdirpath,
                          self._conf.tmpdirpath,
                          self._conf.isodirpath]:
            logging.info(_('Creating directory {} ...').format(directory))
            os.makedirs(directory, exist_ok=True)
        # Создаем пустой файл Packages в каталоге репозитория
        with open(os.path.join(self._conf.repodirpath, 'Packages'), mode='w'):
            pass
        logging.info(_('Successfully inited'))


class BuildCmd(BaseCommand):
    cmd = 'build'
    cmdhelp = _('Builds packages from source list into distro chroot')
    root_required = True
    required_binaries = ['systemd-nspawn']
    (_BUILDED_PKGNAME,
     _BUILDED_PKGVERSION) = range(2)
    args = (
        ('--rebuild', {'required': False, 'nargs': '+', 'default': [],
                       'help': _('Specify package(s) for force rebuilding')}),
        ('--rebuild-all', {'required': False, 'action': 'store_true',
                           'default': False, 'help': _('Rebuild all packages in list')}),
        ('--clean', {'required': False, 'action': 'store_true',
                     'default': False, 'help': _('Remove installed packages on time of repo initializing')}),
        ('--jobs', {'required': False, 'type': int, 'default': 2, 'help': _('Jobs count for building')})
    )

    """
    Класс выполняет сборку пакетов
    """

    def __init__(self, conf_path):
        super().__init__(conf_path)
        self.__distribution_info = ChrootDistributionInfo(self._conf)
        self.__sources_list = SourcesList(self._conf)
        self.__sources_list.load()

    def __check_if_build_required(self, package, version, force_rebuild_list):
        # В зависимости от версии определяем набор исходников для сборки
        if len(version):
            glob_re = '{}_{}.dsc'.format(package, version)
        else:
            glob_re = '{}_*.dsc'.format(package)
        glob_re = os.path.join(self._conf.srcdirpath, glob_re)
        dsc_sources = glob.glob(glob_re)
        if len(dsc_sources) > 1:
            re_regexp = r'.*_(?P<version>.*)\.dsc'
            versions = [re.match(re_regexp, dsc).group('version') for dsc in dsc_sources]
            exit_with_error(_('There are {} versions of package {}: {}').format(
                len(versions), package, ', '.join(versions)))
        elif not len(dsc_sources):
            exit_with_error(_('Could not find sources of package {}').format(package))
        # Теперь считаем, что у нас есть dsc file требуемой версии
        dscfilepath = dsc_sources[0]
        pversion = re.match(r'.*_(?P<version>.*)\.dsc', dscfilepath).group('version')
        need_rebuild = False
        # Если уже требуется пересборка, выходим
        if package in force_rebuild_list:
            return (True, dscfilepath)
        # Открываем dsc file, читаем список файлов
        try:
            dscfile = apt.debfile.DscSrcPackage(filename=dscfilepath)
            missing_binaries = 0
            for binary in dscfile.binaries:
                # Для каждого deb-пакета ищем его в сборочном репозитории
                deb_re = os.path.join(self._conf.repodirpath, '{}_{}*.deb'.format(binary, pversion))
                if not len(glob.glob(deb_re)):
                    need_rebuild = True
                    missing_binaries += 1
            # Если бинарников меньше, чем в dsc, то выводим предупреждение
            if need_rebuild and not len(dscfile.binaries) == missing_binaries:
                logging.info(_('Source package {} will be rebuilded due to missing binaries').format(package))
            elif not need_rebuild:
                logging.info(_('Package {} already builded, skipped').format(
                    '{} = {}'.format(package, version) if len(version) else package))
            return (need_rebuild, dscfilepath)
        except Exception:
            exit_with_error(_('Failed to get binaries for {}').format(package))

    def __make_build(self, jobs, rebuild, clean):
        # Определяем факт наличия chroot'а
        logging.info(_('Following packages are found in build list: \n{}').format(self.__sources_list.build_list_str))
        dist_chroot = NSPContainer(self._conf)
        if not dist_chroot.exists():
            exit_with_error(_('Chroot for {} does not created').format(dist_chroot.name))
        for pkgname, version in self.__sources_list.build_list:
            need_building, dscfilepath = self.__check_if_build_required(pkgname, version, rebuild)
            if need_building:
                # Обработка опции --clean: мы должны удалить распакованный образ
                # и распаковать chroot снова
                dist_chroot.deploy(recreate=clean)
                # Теперь выполняем копирование в chroot
                try:
                    full_pkgname = '{} = {}'.format(pkgname, version) if len(version) else pkgname
                    logging.info(_('Copying sources for package {} to chroot {} ...').format(full_pkgname,
                                                                                             dist_chroot.name))
                    dscfile = apt.debfile.DscSrcPackage(filename=dscfilepath)
                    sources_list = dscfile.filelist + [os.path.basename(dscfilepath)]
                    # Абсолютное имя dsc файла пакета относительно корня chroot'а
                    chroot_dsc_source = os.path.join(dist_chroot.sources_dir, os.path.basename(dscfilepath))
                    dst_sources_list = [os.path.join(dist_chroot.abs_sources_dir, source)
                                        for source in sources_list]
                    for source, dst in zip(sources_list, dst_sources_list):
                        src = os.path.join(self._conf.srcdirpath, source)
                        logging.debug(_('Copying {} to {} ...').format(src, dst))
                        shutil.copy(src, dst)
                except Exception:
                    exit_with_error(_('Failed to determine sources of package {}').format(pkgname))
                # Теперь производим сборку пакета в chroot'е
                try:
                    dist_chroot.build_package(chroot_dsc_source, jobs)
                except Exception as e:
                    exit_with_error(e)
                finally:
                    # Удаляем исходники из chroot'а
                    for dst in dst_sources_list:
                        os.remove(dst)

    def run(self, jobs, rebuild, rebuild_all, clean):
        if rebuild_all:
            if len(rebuild):
                logging.warning(_('Package rebuilding {} ignored, '
                                  'because options --rebuild-all specified').format(', '.join(rebuild)))
            rebuild = self.__sources_list
            logging.warning(_('Will be rebuilded following packages: {}').format(self.__sources_list.build_list_str))
        self.__make_build(jobs, rebuild, clean)


class MakeRepoCmd(_RepoAnalyzerCmd):
    cmd = 'make-repo'
    cmdhelp = _('Creates repositories (main, devel and source) in reprepro format')
    required_binaries = ['reprepro', 'xorrisofs']
    _DEFAULT_DEV_PACKAGES_SUFFIXES = ['dbg', 'dbgsym', 'doc', 'dev']

    def __init__(self, conf_path):
        super().__init__(conf_path)
        sources_include = self._conf.parser.get(MakeRepoCmd.alias, 'source-include', fallback=[])
        if isinstance(sources_include, str):
            sources_include = sources_include.split(',')
        sources = []
        for source in sources_include:
            abspath = os.path.abspath(source)
            if os.path.exists(abspath):
                sources.append(abspath)
            else:
                exit_with_error(_('File {} does not exists').format(abspath))
        self.__sources_include = sources

    def __sources(self, pkg, version):
        # Определяем исходники по имени бинарного пакета и вресии
        source = self._builded_cache.source_package(pkg, version)
        if source is None:
            exit_with_error(_('Failed finding sources for {}_{}').format(pkg, version))
        glob_re = os.path.join(self._conf.srcdirpath, '{}_{}*.dsc'.format(source, version))
        dscfilepath = glob.glob(glob_re)
        if not len(dscfilepath):
            exit_with_error(_('Failed to find sources via regexp {}').format(glob_re))
        dscfilepath = dscfilepath[0]
        dscfile = apt.debfile.DscSrcPackage(filename=dscfilepath)
        return tuple(dscfile.filelist + [os.path.basename(dscfilepath)])

    def run(self):
        logging.info(_('Processing target repository ...'))
        # Анализ пакетов основного репозитория
        target_builded_deps = set()
        sources = dict()
        tmpdirpath = tmpdirmanager.create()
        frepodirpath = os.path.join(tmpdirpath, '{}_main'.format(self._conf.reponame))
        frepodevdirpath = os.path.join(tmpdirpath, '{}_dev'.format(self._conf.reponame))
        fsrcdirpath = os.path.join(tmpdirpath, '{}_src'.format(self._conf.reponame))
        for subdir in (frepodirpath, frepodevdirpath, fsrcdirpath):
            os.makedirs(subdir, exist_ok=True)
        for required in self._packages['target']:
            logging.info(_('Processing {} ...').format(required))
            deps = self._get_depends_for_package(required,
                                                 exclude_rules=self._dev_packages_suffixes,
                                                 black_list=self._packages.get('target-dev', []))
            unresolve = [d for d in deps if d[DependencyFinder.DF_DEST] == PackageType.PACKAGE_NOT_FOUND]
            deps_in_dev = [d for d in deps if d[DependencyFinder.DF_DEST] in (PackageType.PACKAGE_FROM_OS_DEV_REPO,
                                                                              PackageType.PACKAGE_FROM_EXT_DEV_REPO)]
            if len(unresolve):
                self._emit_unresolved(unresolve)
            if len(deps_in_dev):
                self._emit_resolved_in_dev(deps_in_dev)
            target_deps = [d for d in deps if d[DependencyFinder.DF_DEST] == PackageType.PACKAGE_BUILDED]
            files_to_copy = set()
            for p in target_deps:
                resolved = p[DependencyFinder.DF_RESOLVED]
                source_key = '{}_{}'.format(*resolved)
                if source_key in sources.keys():
                    continue
                sources[source_key] = self.__sources(*resolved)
                # Определяем бинарные пакеты для копирования
                glob_copy_re = os.path.join(self._conf.repodirpath, '{}_{}*.deb'.format(*resolved))
                binaries = glob.glob(glob_copy_re)
                if not len(binaries):
                    exit_with_error(_('Failed to find binaries by glob re: {}').format(glob_copy_re))
                assert (len(binaries) == 1)
                files_to_copy.add(binaries[0])
            target_builded_deps.update(files_to_copy)
            logging.debug(_('Copying dependencies for package {}: {}').format(required, files_to_copy))
            for f in files_to_copy:
                dst = os.path.join(frepodirpath, os.path.basename(f))
                try:
                    logging.debug(_('Copying {} to {}').format(f, dst))
                    shutil.copyfile(f, dst)
                except Exception as e:
                    exit_with_error(e)
        logging.info(_('Processing dev repository ...'))
        # Определяем репозиторий со средствами разработки -
        # все пакеты из сборочного репозитория за вычетом всех, указанных в target
        dev_packages = []
        for f in os.listdir(self._conf.repodirpath):
            m = re.match(r'(?P<name>.*)_.*_.*.deb', f)
            if m:
                package_name = m.group('name')
                dev_packages.append(package_name)
        dev_packages = sorted([p for p in set(dev_packages) - set(self._packages['target'])])
        for devpkg in dev_packages:
            logging.info(_('Processing {} ...').format(devpkg))
            deps = self._get_depends_for_package(devpkg, flags=DependencyFinder.FLAG_FINDER_DEV)
            unresolve = [d for d in deps if d[DependencyFinder.DF_DEST] == PackageType.PACKAGE_NOT_FOUND]
            if len(unresolve):
                self._emit_unresolved(unresolve)
            builded = [d[DependencyFinder.DF_RESOLVED] for d in deps
                       if d[DependencyFinder.DF_DEST] == PackageType.PACKAGE_BUILDED]
            # Определяем файлы для копирования на второй диск
            files_to_copy = set()
            for resolved in builded:
                glob_file_re = os.path.join(self._conf.repodirpath, '{}_{}*.deb'.format(*resolved))
                binaries = glob.glob(glob_file_re)
                if not len(binaries):
                    exit_with_error(_('Failed to find binaries by glob re: {}').format(glob_copy_re))
                assert (len(binaries) == 1)
                # Исключаем бинарники, которые уже есть в основном репозитории
                binary = binaries[0]
                if os.path.basename(binary) in os.listdir(frepodirpath):
                    continue
                # В противном случае добавляем их в список файлов для копирования
                files_to_copy.add(binary)
                # Определяем исходники по пакету
                source_key = '{}_{}'.format(*resolved)
                if source_key in sources:
                    continue
                sources[source_key] = self.__sources(*resolved)
            logging.debug(_('Copying dependencies for package {}: {}').format(devpkg, files_to_copy))
            for f in files_to_copy:
                dst = os.path.join(frepodevdirpath, os.path.basename(f))
                try:
                    logging.debug(_('Copying {} to {}').format(f, dst))
                    shutil.copyfile(f, dst)
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
            logging.info(_('Copying sources for package(s) {} ...').format(', '.join(packages)))
            for source in sourcelist:
                src = os.path.join(self._conf.srcdirpath, source)
                dst = os.path.join(fsrcdirpath, os.path.basename(source))
                try:
                    logging.debug(_('Copying {} to {}').format(src, dst))
                    shutil.copyfile(src, dst)
                except Exception as e:
                    exit_with_error(e)
        # Создаем образа дисков с репозиториями (main и dev)
        for items in ((frepodirpath, False),
                      (frepodevdirpath, True)):
            pkgpath, is_dev = items
            iso_maker = DebianIsoRepository(self._conf, tmpdirpath, is_dev)
            iso_maker.create(pkgpath)
        # Формируем образ диска с исходниками
        # Создаем каталог для образа диска с исходниками
        sources_iso_tmpdir = os.path.join(tmpdirpath, '{}_src_iso'.format(self._conf.reponame))
        os.makedirs(sources_iso_tmpdir, exist_ok=True)
        # Копируем исходники
        shutil.copytree(fsrcdirpath, os.path.join(sources_iso_tmpdir, 'src'))
        # Копируем файлы для сборки дистрибутива
        required_files = [os.path.abspath(os.path.abspath(sys.argv[0])),
                          os.path.abspath(self._conf.chroot_helper),
                          os.path.abspath(self._conf.parser.get(BuildCmd.cmd, 'source-list')),
                          os.path.abspath(self._conf.parser.get(MakeRepoCmd.alias, 'white-list'))]
        chroot_script = self._conf.parser.get('chroot', 'chroot-script', fallback=None)
        if chroot_script:
            required_files.append(os.path.abspath(chroot_script))
        if len(self.__sources_include):
            required_files += self.__sources_include
        for req in required_files:
            shutil.copyfile(req, os.path.join(sources_iso_tmpdir, os.path.basename(req)))
        now = datetime.datetime.now().strftime('%Y-%m-%d')
        isoname = 'sources-{}_{}_{}_{}.iso'.format(self._conf.reponame,
                                                   self._conf.repoversion,
                                                   self._conf.distro,
                                                   now)
        isopath = os.path.join(self._conf.isodirpath, isoname)
        label = '{} {} (sources)'.format(self._conf.reponame, self._conf.repoversion)
        make_iso(isopath, self._conf.reponame, label, sources_iso_tmpdir, sources_iso=True)


class MakePackageCacheCmd(BaseCommand):
    CacheMapped = {
        'os': PackageType.PACKAGE_FROM_OS_REPO,
        'os-dev': PackageType.PACKAGE_FROM_OS_DEV_REPO,
        'ext': PackageType.PACKAGE_FROM_EXT_REPO,
        'ext-dev': PackageType.PACKAGE_FROM_EXT_DEV_REPO
    }
    cmd = 'make-package-cache'
    cmdhelp = _('Creates repository cache for dependency resolving')
    args = (
        ('--mount-path', {'required': True, 'help': _('Set path to repo\'s mount point')}),
        ('--name', {'required': True, 'help': _('Set package name of repo')}),
        ('--type', {'dest': 'ctype', 'required': True, 'choices': CacheMapped})
    )

    def run(self, mount_path, name, ctype, info_message=True):
        if isinstance(ctype, str):
            ctype = self.CacheMapped.get(ctype)
        is_builded = (ctype == PackageType.PACKAGE_BUILDED)
        if not is_builded:
            packages_path = []
            dists_path = os.path.join(mount_path, 'dists')
            for root, dirs, files in os.walk(dists_path):
                if 'Packages.gz' in files:
                    packages_path.append(os.path.join(root, 'Packages.gz'))
        else:
            packages_path = [os.path.join(mount_path, 'Packages')]
        if not len(packages_path):
            exit_with_error(_('Can\'t find any Packages files in {}').format(mount_path))
        c = RepositoryCache(self._conf, name, ctype)
        if not c.create(packages_path):
            exit_with_error(_('Cache creation failed'))
        if info_message:
            logging.info(_('Cache saved to {}').format(c.cache_path))


class RemoveSourceCmd(BaseCommand):
    import collections

    cmd = 'remove-sources'
    cmdhelp = _('Removes sources and binaries from repository')
    args = (
        ('--package', {'required': True, 'help': _('Source package name to be removed')}),
    )

    def __process_line_buffer(self, line_buffer):
        last_processing = None
        pkginfo = self.collections.OrderedDict()
        for line in line_buffer:
            m = re.match(r'(?P<name>.*): (?P<value>.*)', line)
            if m:
                last_processing = m.group('name')
                pkginfo[last_processing] = m.group('value')
            else:
                value = pkginfo.get(last_processing)
                if isinstance(value, str):
                    value = [value] + [line]
                elif isinstance(value, list):
                    value += [line]
                pkginfo[last_processing] = value
        return pkginfo

    def run(self, package):
        expr = os.path.join(self._conf.srcdirpath, '{}_*.dsc'.format(package))
        sources = glob.glob(expr)
        if not len(sources):
            exit_with_error(_('No sources are found'))
        sys.stdout.write(_('The following sources are found:\n'))
        dscfiles = {num + 1: source for (num, source) in enumerate(sources)}
        for num, dsc in dscfiles.items():
            sys.stdout.write('{}\t{}\n'.format(num, os.path.basename(dsc)))
        while True:
            try:
                choice = int(input(_('\nChoose source to be removed:\n')))
            except ValueError:
                continue
            if choice not in dscfiles:
                continue
            dscfilepath = dscfiles.get(choice)
            break
        m = re.match(r'(?P<name>.*)_(?P<version>.*).dsc', dscfilepath)
        version = m.group('version')
        if not m:
            exit_with_error(_('Failed to determine package and version of {}').format(dscfilepath))
        sources_to_remove = []
        dscfile = apt.debfile.DscSrcPackage(filename=dscfilepath)
        sources_to_remove = [dscfilepath] + [os.path.join(self._conf.srcdirpath, source)
                                             for source in dscfile.filelist]
        packages_path = os.path.join(self._conf.repodirpath, 'Packages')
        packages_info = []
        try:
            with open(packages_path, mode='r') as fp:
                lines = [line.rstrip('\n') for line in fp.readlines()]
            idx_line = 0
            lines_buffer = []
            while idx_line < len(lines):
                line = lines[idx_line]
                if len(line):
                    lines_buffer.append(line)
                else:
                    if len(lines_buffer):
                        packages_info.append(self.__process_line_buffer(lines_buffer))
                        lines_buffer.clear()
                idx_line += 1
        except Exception as e:
            exit_with_error(_('Failed parsing Packages file: {}').format(e))
        # Определяем бинарные пакеты на удаление
        binary_to_remove = []
        binary_packages_to_remove = []
        binary_version = None
        for pkginfo in packages_info:
            pkgname, pkgversion, pkgsource = pkginfo.get('Package'), pkginfo.get('Version'), pkginfo.get('Source', None)
            if pkgname == package or pkgsource == package:
                if version in pkgversion:
                    binary_version = pkgversion
                    binary_to_remove.append(pkgname)
                    binary_glob_re = os.path.join(self._conf.repodirpath, '{}_{}_*.deb'.format(pkgname, version))
                    binary_packages_to_remove += glob.glob(binary_glob_re)
        sys.stdout.write(_('\nThe following sources will be removed:\n'))
        for src in sources_to_remove:
            sys.stdout.write('\t{}\n'.format(src))
        sys.stdout.write(_('\nThe following binaries will be removed:\n'))
        for binary in binary_packages_to_remove:
            sys.stdout.write('\t{}\n'.format(binary))
        while True:
            answer = input(_('Do you want to continue? (yes/NO): '))
            if not len(answer) or answer == _('NO'):
                logging.info(_('Operation was cancelled by user'))
                exit(0)
            elif answer == _('yes'):
                break
        # Переписываем Packages файл
        packages_backup_path = '{}.bak'.format(packages_path)
        with open(packages_backup_path, mode='w') as fp:
            for pkginfo in packages_info:
                pkgname, pkgver = pkginfo.get('Package'), pkginfo.get('Version')
                if (pkgname in binary_to_remove and pkgver == binary_version):
                    continue
                for key, value in pkginfo.items():
                    if isinstance(value, str):
                        fp.write('{}: {}\n'.format(key, value))
                    elif isinstance(value, list):
                        fp.write('{}: {}\n'.format(key, value[0]))
                        for row in value[1:]:
                            fp.write('{}\n'.format(row))
                fp.write('\n')
        # Удаляем бинари и исходники
        try:
            for filepath in sources_to_remove + binary_packages_to_remove:
                os.remove(filepath)
            # Заменяем Packages файл нашим сгенерированным
            shutil.move(packages_backup_path, packages_path)
        except Exception as e:
            exit_with_error(_('Sources removing failed: {}').format(e))


class RepoRuntimeDepsAnalyzerCmd(_RepoAnalyzerCmd):
    cmd = 'check-runtime-deps'
    cmdhelp = _('Analyzes all builded packages for resolving information')

    def run(self):
        all_unresolved = []
        all_in_dev = []
        for pkgname in self._builded_cache.packages:
            logging.info(_('Processing {} ...').format(pkgname))
            deps = self._get_depends_for_package(pkgname)
            unresolve = [d for d in deps if d[DependencyFinder.DF_DEST] == PackageType.PACKAGE_NOT_FOUND]
            deps_in_dev = [d for d in deps if d[DependencyFinder.DF_DEST] in (PackageType.PACKAGE_FROM_OS_DEV_REPO,
                                                                              PackageType.PACKAGE_FROM_EXT_DEV_REPO)]
            for dep in deps:
                deptype, depstr, resolved, required_by = dep
                if deptype == PackageType.PACKAGE_NOT_FOUND:
                    all_unresolved.append((deptype, depstr, None, required_by,))
                elif deptype in (PackageType.PACKAGE_FROM_OS_DEV_REPO,
                                 PackageType.PACKAGE_FROM_EXT_DEV_REPO):
                    all_in_dev.append((deptype, depstr, form_dependency((*resolved, '=')), required_by,))
            all_unresolved += list(unresolve)
            all_in_dev += list(deps_in_dev)
        self._emit_deps_summary(all_unresolved, all_in_dev)


class MakeDebianChrootCmd(BaseCommand):
    cmd = 'make-chroot'
    cmdhelp = _('Creates OS chroot for repository')
    root_required = True
    required_binaries = ['debootstrap', 'systemd-nspawn']

    def run(self):
        nsconainer = NSPContainer(self._conf)
        nsconainer.create()


class ChrootLoginCmd(BaseCommand):
    import collections

    cmd = 'login-chroot'
    cmdhelp = _('Logins to deployed chroot')
    args = (
        ('--deploy', {'required': False, 'action': 'store_true', 'default': False,
                      'help': _('Deploy container instance if not exists, default: False')}),
        ('--bind', {'required': False, 'nargs': '+', 'default': [],
                    'help': _('Additionally mounted directories for chroot (in systemd-nspawn format)')}),
    )
    root_required = True
    required_binaries = ['systemd-nspawn']

    def run(self, deploy=False, bind=[]):
        nsconainer = NSPContainer(self._conf)
        if not nsconainer.deployed() and not deploy:
            exit_with_error(_('Could not login to container {}: does not deployed').format(nsconainer.name))
        else:
            nsconainer.deploy()
        try:
            nsconainer.login(bind)
        except Exception as e:
            exit_with_error(e)


class SourcesSortCmd(BaseCommand):
    cmd = 'order-sources'
    cmdhelp = _('Orders sources in specified sources.list via build-depends for building')

    def __init__(self, conf_path):
        super().__init__(conf_path)
        self.__sources_list = SourcesList(self._conf)
        self.__sources_info = {}
        self.__build_cache = None
        self.__sources_list.load()
        self.__build_cache_of_builded_packages()

    def __build_cache_of_builded_packages(self):
        logging.info(_('Building cache for builded packages ...'))
        maker = MakePackageCacheCmd(self._conf.conf_path)
        maker.run(mount_path=self._conf.repodirpath,
                  name='builded',
                  ctype=PackageType.PACKAGE_BUILDED,
                  info_message=False)

    def __format_source(self, source):
        source_name, source_version = source
        return '{} {}'.format(source_name, source_version) if len(source_version) else source_name

    def __order_depends(self, ordered, unordered, source):
        ordered_info = [('', '')]
        for dep in self.__sources_info.get(source)['deps']:
            # Ищем source по бинарной зависимости
            source_name = None
            for pkginfo in self.__build_cache.pkginfo:
                pkgname = pkginfo.get('package')
                if dep == pkgname:
                    source_name = pkginfo.get('source', pkgname)
                    break
            if source_name is None:
                exit_with_error(_('Failed to get source name for {}').format(dep))
            in_ordered = source_name in [p[SourcesList.SL_PKGNAME] for p in ordered]
            if in_ordered:
                item = ('# {} (dep: {}) is needed for {}'.format(source_name, dep,
                                                                 self.__format_source(source)), '')
                if item not in ordered_info:
                    ordered_info.append(item)
            else:
                # Ищем исходник пакета для разрешения зависимости
                new_source = None
                for key, value in self.__sources_info.items():
                    pkgname, version = key
                    if dep in value.get('binaries'):
                        new_source = (pkgname, version)
                        break
                # Удаляем исходник из unordered и повторяем
                try:
                    item = ('# {} (dep: {}) is needed for {}'.format(new_source[0], dep,
                                                                     self.__format_source(source)), '')
                    if item not in ordered_info:
                        ordered_info.append(item)
                    unordered.remove(new_source)
                    ordered, unordered = self.__order_depends(ordered, unordered, new_source)
                except ValueError:
                    pass
        if source not in ordered_info:
            ordered_info.append(source)
        # Теперь формируем записи
        for order_item in ordered_info:
            ordered.append(order_item)
        return ordered, unordered

    def run(self, verbose=False):
        rfcache = RepositoryFullCache(self._conf)
        rfcache.load()
        self.__build_cache = rfcache.builded_cache
        for pkgname, pkgversion in self.__sources_list.build_list:
            if len(pkgversion):
                glob_re = '{}_{}.dsc'.format(pkgname, pkgversion)
            else:
                glob_re = '{}_*.dsc'.format(pkgname)
            glob_re = os.path.join(self._conf.srcdirpath, glob_re)
            source = glob.glob(glob_re)
            if not len(source):
                exit_with_error(_('Could not find source by regexp {}').format(glob_re))
            source = source[0]
            source_name = self.__format_source((pkgname, pkgversion))
            logging.info(_('Processing source {} ...').format(source_name))
            dscpackage = apt.debfile.DscSrcPackage(filename=source)
            m = re.match(r'.*_(?P<version>.*)\.dsc', source)
            if not m:
                exit_with_error(_('Failed determine source version of package {}').format(pkgname))
            version = m.group('version')
            deps_info = []
            pkginfo = (pkgname, pkgversion, '=')
            for dependency in dscpackage.depends:
                if len(dependency):
                    dependency = dependency[0]
                    repotype, *unused = rfcache.find_dependencies(dependency, pkginfo)
                    if repotype in (PackageType.PACKAGE_BUILDED,
                                    PackageType.PACKAGE_NOT_FOUND):
                        deps_info.append(dependency[0])
                else:
                    for alt in dependency:
                        repotype, *unused = rfcache.find_dependencies(dependency, pkginfo)
                        if repotype in (PackageType.PACKAGE_BUILDED,
                                        PackageType.PACKAGE_NOT_FOUND):
                            deps_info.append(alt[0])
                            break
            self.__sources_info[(pkgname, pkgversion)] = {
                'binaries': dscpackage.binaries,
                'version': version,
                'deps': deps_info
            }
        if verbose:
            for package, info in self.__sources_info.items():
                sys.stdout.write(_('Package {}:\n').format(package))
                for key, values in info.items():
                    sys.stdout.write('\t{}: {}\n'.format(key, values))
                sys.stdout.write('\n')
        # Заносим туда все пакеты, которые не имеют зависимостей
        ordered = []
        ordered.append(('# Those packages does not have build-depends from those repository:\n', ''))
        for key, info in self.__sources_info.items():
            if not len(info['deps']):
                ordered.append(key)
        unordered = sorted(list(set(self.__sources_info.keys() - ordered)), key=lambda item: item[0])
        while True:
            if not len(unordered):
                break
            src = unordered.pop()
            ordered, unordered = self.__order_depends(ordered, unordered, src)
        sources_list_new = '{}.ordered'.format(self.__sources_list.path)
        with open(sources_list_new, mode='w') as fp:
            for item in ordered:
                fp.write(self.__format_source(item))
                fp.write('\n')
        logging.info(_('Ordered sources list is saved to {}').format(sources_list_new))


def make_default_subparser(main_parser, cls):
    parser = main_parser.add_parser(cls.cmd, help=cls.cmdhelp)
    parser.add_argument('--config', required=False,
                        default=Configuration.DEFAULT_CONF,
                        help=_('Buildrepo config path (default: {})').format(Configuration.DEFAULT_CONF))
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


def register_atexit_callbacks():
    def _chown_recurse(path, user, group):
        for root, dirs, files in os.walk(path, topdown=False):
            for directory in [os.path.join(root, d) for d in dirs]:
                shutil.chown(directory, user, group)
            for file in [os.path.join(root, f) for f in files]:
                shutil.chown(file, user, group)

    def remove_temp_directory_atexit_callback():
        for directory in tmpdirmanager.dirs():
            if os.path.exists(directory):
                shutil.rmtree(directory)

    def chown_files_atexit_callback():
        sudo_user = os.environ.get('SUDO_USER', None)
        sudo_gid = os.environ.get('SUDO_GID', None)
        if not sudo_user or not sudo_gid:
            return
        try:
            import grp
            sudo_group = grp.getgrgid(int(sudo_gid)).gr_name
        except KeyError:
            logging.warning(_('Failed get group name for GID {}').format(sudo_gid))
            exit(0)
        conf = Configuration(args.config)
        if not conf.directories_created():
            return
        for item in (os.path.join(conf.root, 'logs'),
                     os.path.join(conf.root, 'repo'),
                     conf.cachedirpath,
                     conf.chrootsdirpath):
            _chown_recurse(item, sudo_user, sudo_group)
        # Файлы логов
        for file in (os.path.join(conf.root, 'logs', conf.distro, 'chroot-{}.log'.format(conf.distro)),
                     os.path.join(conf.root, 'build-{}.log'.format(conf.reponame))):
            if os.path.exists(file):
                shutil.chown(file, sudo_user, sudo_group)

    import atexit
    atexit.register(remove_temp_directory_atexit_callback)
    atexit.register(chown_files_atexit_callback)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='command')
    cmdmap = available_commands()

    for cmd in sorted(cmdmap.keys()):
        cls, cmdargs = cmdmap.get(cmd)
        subparser = make_default_subparser(subparsers, cls)
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
        cmdargs = {}
        register_atexit_callbacks()
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
