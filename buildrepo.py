#!/usr/bin/python3

import os
import sys
import logging
import re
import gettext
import apt.debfile
import glob
import apt_pkg
import shutil
import argparse
import subprocess
import platform
import atexit
import time
import datetime
import configparser
import traceback

CURDIR = os.path.abspath(os.path.curdir)
DEVNULL = open(os.devnull, 'wb')

DEB_RE = '^(?P<name>[\w\-\.\+]+)_(?P<version>[\w\.\-\~\+]+)_(?P<arch>[\w]+)\.deb$'
DSC_FULL_RE = '^(?P<name>[\w\-\.\+]+)_(?P<version>[\w\.\-\~\+]+)\.dsc$'


# Ключи кэша
DIRECTIVE_CACHE_NAME = 'cache_name'
DIRECTIVE_CACHE_TYPE = 'cache_type'
DIRECTIVE_CACHE_VERSION = 'version'
DIRECTIVE_CACHE_PACKAGES = 'packages'
DIRECTIVE_CACHE_PACKAGES_PACKAGE_NAME = 'name'
DIRECTIVE_CACHE_PACKAGES_PACKAGE_VERSION = 'version'

# gettext
_ = gettext.gettext

# Disable warnings
import warnings
warnings.filterwarnings('ignore')


def exit_with_error(error):
    logging.critical(error)
    exit(1)


def fix_package_version(pver):
    # TODO: Version hack
    if ':' in pver:
        pver = pver.split(':')[-1]
    return pver


def form_dependency(dep):
    depname, depver, depop = dep
    if len(depver):
        return '{} ({} {})'.format(depname, depop, depver)
    return depname


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

    DEFAULT_BUILD_DIR = os.path.abspath(os.path.join(CURDIR, 'build'))
    DEFAULT_CONF = os.path.join(CURDIR, 'buildrepo.conf')
    DEFAULT_CHROOT_SCRIPT = os.path.join(CURDIR, 'chroot-helper.sh')

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
        self.__base_init()
        if not self.reponame:
            exit_with_error(_('Repository name is missing in {}').format(conf_path))
        elif not self.repoversion:
            exit_with_error(_('Repository version is missing in {}').format(conf_path))
        elif not self.distro:
            exit_with_error(_('Distro name is missing in {}').format(conf_path))

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

    def __init_logger(self):
        if os.path.exists(self.logsdirpath):
            logname = os.path.join(self.root, self.distro, 'build-{}.log'.format(self.reponame))
            logging.basicConfig(level=logging.DEBUG,
                                format='%(asctime)-8s %(levelname)-8s %(message)s',
                                datefmt='%Y-%m-%d %H:%M:%S',
                                filename=os.path.join(self.root, 'build-{}.log'.format(self.reponame)),
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
            exit_with_error(_('No one mirror is present in section {}').format(section))


class NSPContainer:
    import tarfile
    _FIRST_MIRROR = 0
    DEFAULT_DIST_COMPONENTS = ['main', 'contrib', 'non-free']
    DEFAULT_USER_PACKAGES = []
    CHROOT_REQUIRED_DEBS = ['dpkg-dev', 'fakeroot', 'quilt', 'sudo']
    CHROOT_COMPRESSION = 'xz'

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
        mode = 'a' if os.path.exists(log_file) and not recreate_log else 'w'
        try:
            logstream = open(log_file, mode=mode)
        except OSError as e:
            exit_with_error(_('Error opening logfile: {}').format(e))
        if mode == 'a':
            logstream.write('\n')
            logstream.flush()
        logstream.write('Executing {} ...\n'.format(' '.join(cmdargs)))
        logstream.flush()
        start = datetime.datetime.now()
        # TODO: Отключить цвета в логе
        proc = subprocess.Popen(cmdargs, stdout=logstream, stderr=logstream,
                                universal_newlines=True)
        proc.communicate()
        end = datetime.datetime.now() - start
        logstream.write('\nReturncode: {}'.format(proc.returncode))
        logstream.write('\nTime: {}\n'.format(time.strftime('%H:%M:%S', time.gmtime(end.seconds))))
        logstream.close()
        return proc.returncode

    def _exec_nspawn(self, cmdargs, container_path, log_file, recreate_log=False):
        nspawn_bin = shutil.which('systemd-nspawn')
        if not nspawn_bin:
            exit_with_error(_('systemd-nspawn does not found'))
        nspawn_args = [nspawn_bin, '-D', container_path,
                       '--hostname', self.__name,
                       '-E', 'TERM=vt220',
                       '-E', 'LC_ALL=C']
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
                exit_with_error(_('Failed to remove deploy path {}').format(self.deploypath))
        elif self.deployed():
            return
        logging.info(_('Deploying {} to {} ...').format(self.__name, self.deploypath))
        try:
            os.chdir(self.__conf.chrootsinstdirpath)
            with self.tarfile.open(self.chroot_path,
                                   mode='r:{}'.format(self.CHROOT_COMPRESSION)) as tf:
                tf.extractall()
            shutil.move(self.__name,
                        self.hostname)
        except Exception as e:
            traceback.print_exc()
            exit_with_error(_('Chroot deployment {} failed: {}').format(self.__name, e))
        finally:
            os.chdir(CURDIR)

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
        logdir = os.path.join(self.__conf.logsdirpath, self.__conf.reponame)
        log_file = os.path.join(self.__conf.logsdirpath, '{}_{}.log'.format(
            pname, pversion))
        # Формируем команду на запуск
        logging.info(_('Package building {}-{} ...'.format(pname, pversion)))
        chroot_helper_path = os.path.join('/srv', os.path.basename(self.__conf.chroot_helper))
        returncode = self._exec_nspawn(['--chdir=/srv', chroot_helper_path, dsc_file_path],
                                       self.deploypath, log_file, recreate_log=True)
        if returncode:
            raise RuntimeError(_('Package building {} failed').format(pname))

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
                for url in mirrors:
                    if url.startswith('file://'):
                        old_url = url[7:]
                        # Требуется создать каталоги под репозитории
                        url = os.path.join('srv', 'mirrors', 'mirror{}'.format(mirror_num))
                        mirror_num += 1
                        os.makedirs(os.path.join(dist_chroot_dir, url))
                        apt_sources.write('deb file:///{url} {dist} {components}\n'.format(
                                          url=url, dist=self.__name, components=' '.join(self.__dist_info.get('components'))))
                    else:
                        apt_sources.write('deb {url} {dist} {components}\n'.format(
                                          url=url, dist=self.__name, components=' '.join(self.__dist_info.get('components'))))
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
                fp.write('export TERM="xterm-mono"\n')
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
            os.chdir(tmpdir)
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
            traceback.print_exc()
            exit_with_error(_('Chroot building failed: {}').format(e))
        finally:
            os.chdir(CURDIR)


class BaseCommand:
    cmd = None
    alias = None
    root_needed = False
    required_binaries = []

    def __init__(self, conf):
        if not os.path.exists(os.path.dirname(conf)):
            os.makedirs(os.path.dirname(conf))
        self._conf = Configuration(conf)
        if self.root_needed and not os.getuid() == 0:
            exit_with_error(_('Must be run as superuser'))
        self.__check_required_binaries()

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


class RepoInitializer(BaseCommand):
    cmd = 'init'
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
            logging.debug(_('Creating directory {} ...').format(directory))
            os.makedirs(directory, exist_ok=True)
        for directory in [self._conf.chrootsdirpath,
                          self._conf.chrootsinstdirpath,
                          self._conf.tmpdirpath,
                          self._conf.isodirpath]:
            logging.debug(_('Creating directory {} ...').format(directory))
            os.makedirs(directory, exist_ok=True)
        # Создаем пустой файл Packages в каталоге репозитория
        with open(os.path.join(self._conf.repodirpath, 'Packages'), mode='w') as fp:
            pass
        logging.info(_('Succefully inited'))


class BuildCmd(BaseCommand):
    cmd = 'build'
    root_needed = True
    required_binaries = ['systemd-nspawn']
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
        self.__build_list = []
        self.__distribution_info = ChrootDistributionInfo(self._conf)
        self.__parse_source_list()

    def __parse_source_list(self):
        scenario_path = self._conf.parser.get(BuildCmd.cmd, 'source-list', fallback=None)
        if not scenario_path:
            exit_with_error(_('Source list does not specified in {}').format(self._conf.conf_path))
        scenario_path = os.path.abspath(scenario_path)
        if not os.path.exists(scenario_path):
            exit_with_error(_('File {} does not exists').format(scenario_path))
        scenario_path = os.path.abspath(scenario_path)
        logging.info(_('Loading source list from {} ...').format(scenario_path))
        with open(scenario_path) as fp:
            for line in fp.readlines():
                line = line.strip()
                if line.startswith('#') or not len(line):
                    continue
                tokens = line.split(' ')
                if len(tokens) == 1:
                    self.__build_list.append((line, ''))
                elif len(tokens) == 2:
                    self.__build_list.append((tokens[0], tokens[1]))
                else:
                    logging.warning(_('Mailformed line {} in {}').format(line, scenario_path))
                    continue
        if not len(self.__build_list):
            logging.warning(_('No one sources are found in {}').format(scenario_path))
            exit(0)
        logging.info(_('Following packages are found in build list: \n{}').format(
                        '\n'.join([p[0] if not len(p[1]) else '{} = {}'.format(p[0], p[1])
                                   for p in self.__build_list])))

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
        if package in [p[0] for p in force_rebuild_list]:
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
            # Если бинарников меньше, чем в dsc, то выводим предупореждение
            if need_rebuild and not len(dscfile.binaries) == missing_binaries:
                logging.info(_('Source package {} will be rebuilded due to missing binaries').format(package))
            elif not need_rebuild:
                logging.info(_('Package {} already builded, skipped').format(
                    '{} = {}'.format(package, version) if len(version) else package))
            return (need_rebuild, dscfilepath)
        except Exception:
            traceback.print_exc()
            exit_with_error(_('Failed to get binaries for {}').format(package))

    def __make_build(self, jobs, rebuild, clean):
        # Определяем факт наличия chroot'а
        dist_chroot = NSPContainer(self._conf)
        if not dist_chroot.exists():
            exit_with_error(_('Chroot for {} does not created').format(dist_chroot.name))
        for pkgname, version in self.__build_list:
            need_building, dscfilepath = self.__check_if_build_required(pkgname, version, rebuild)
            if need_building:
                # Обработка опции --clean: мы должны удалить распакованный образ
                # и распаковать chroot снова
                dist_chroot.deploy(recreate=clean)
                # Теперь выполняем копирование в chroot
                try:
                    logging.info(_('Copying sources for package to chroot {} ...').format(dist_chroot.name))
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
                    traceback.print_exc()
                    exit_with_error(_('Failed to determine sources of package {}').format(pkgname))
                # Теперь производим сборку пакета в chroot'е
                try:
                    dist_chroot.build_package(chroot_dsc_source, jobs)
                except Exception as e:
                    traceback.print_exc()
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
            rebuild = self.__build_list
            logging.warning(_('Will be rebuilded following packages: {}').format(
                ', '.join([p[0] if not len(p[1]) else '{} = {}'.format(p[0], p[1])
                                   for p in self.__build_list])))
        self.__make_build(jobs, rebuild, clean)


class PackageType:
    (PACKAGE_BUILDED,
     PACKAGE_FROM_OS_REPO,
     PACKAGE_FROM_EXT_REPO,
     PACKAGE_FROM_OS_DEV_REPO,
     PACKAGE_FROM_EXT_DEV_REPO,
     PACKAGE_NOT_FOUND) = range(0, 6)

    @classmethod
    def available_types(cls):
        for obj in filter(lambda x: x.startswith('PACKAGE') , dir(cls)):
            yield getattr(cls, obj)

class DependencyFinder:
    FLAG_FINDER_MAIN = 1 << 0
    FLAG_FINDER_DEV = 1 << 1

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

    def __recurse_deps(self, s, p):
        required_by = form_dependency(p)
        *unused, deps = self.__rfcache.find_dependencies(p, required_by)
        for dep in deps:
            if len(dep) == 1:
                dep = tuple(dep[0])
                seen_item = (dep, required_by,)
                if seen_item in self.__seendeps:
                    continue
                depdest, pdstinfo, resolved, unused = self.__rfcache.find_dependencies(*seen_item)
                self.__seendeps.append(seen_item)
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
                    depdest, pdstinfo, resolved, unused = self.__rfcache.find_dependencies(*seen_item)
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
        pkgpath = packages[0]
        deb_package = apt.debfile.DebPackage(filename=pkgpath)
        # Нашли зависимости?
        pkg = (pkgname, version, '=')
        depitem = (PackageType.PACKAGE_BUILDED,         # Где есть зависимость
                   pkg,                                 # Информация для анализа
                   (pkgname, version),                  # Как разрешается
                   form_dependency(pkg))                # Строка зависимости
        self.__deps.append(depitem)
        return pkg

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


class _RepoAnalyzerCmd(BaseCommand):
    _DEFAULT_DEV_PACKAGES_SUFFIXES = ['dbg', 'dbgsym', 'doc', 'dev']
    alias = 'binary-repo'

    def __init__(self, conf_path):
        super().__init__(conf_path)
        self._white_list_path = self._conf.parser.get(_RepoAnalyzerCmd.alias, 'white-list', fallback=None)
        self.__no_create_iso = self._conf.parser.getboolean(MakeRepoCmd.cmd, 'no-create-iso', fallback=False)
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
        logging.info(_('Build cache for builded packages ...'))
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

    def _cache_type_str(self, cache_type):
        for c in self.__caches:
            if c[DIRECTIVE_CACHE_TYPE] == cache_type:
                return c[DIRECTIVE_CACHE_NAME]
        return '<UNKNOWN>'

    def _emit_resolved_in_dev(self, current_package, deps_in_dev, exit=True):
        for p in deps_in_dev:
            unused, pkg, cache_type = p
            package_name, package_ver = pkg.name, pkg.versions[0].version
            logging.error(_('%s %s (%s) for %s is founded in %s repo') %
                           ('Dependency' if p[0] == current_package else 'Subdependency',
                            package_name, package_ver, p[0], self._cache_type_str(cache_type)))
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
                print(_('Package {}:').format(dep))
                for req in requirements:
                    print('\t{}'.format(req))

        unresolved_hash = sort_deps(all_unresolved)
        print(_('***** Unresolved ***** :'))
        print_items(unresolved_hash)
        in_dev_hash = sort_deps(all_in_dev)
        print()
        print(_('***** Found in dev: *****'))
        print_items(in_dev_hash)
        logging.info(_('Summary: unresolved: {}, deps in dev: {}').format(len(all_unresolved), len(all_in_dev)))


class MakeRepoCmd(_RepoAnalyzerCmd):
    cmd = 'make-repo'
    required_binaries = ['reprepro', 'genisoimage']
    args = (
                ('--no-create-iso', {'required': False, 'help': _('Skip ISO creation')}),
           )

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
                Debhelper.run_command('genisoimage --joliet-long -r -J -o %s -V "%s" %s' % (isopath,
                                                                                            label,
                                                                                            self.__directory))
            except Exception as e:
                exit_with_error(_('Failed to make iso: %s') % e)
            finally:
                os.chdir(CURDIR)

    _DEFAULT_DEV_PACKAGES_SUFFIXES = ['dbg', 'dbgsym', 'doc', 'dev']

    def __init__(self, conf_path):
        super().__init__(conf_path)
        self.__no_create_iso = self._conf.parser.get(_RepoAnalyzerCmd.alias, 'no-create-iso', fallback=False)

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
        return dscfile.filelist + [os.path.basename(dscfilepath)]

    def run(self, no_create_iso):
        def get_deb_dev_to_copy(pkgs):
            filenames = set()
            for pkg in pkgs:
                p = pkg[1]
                if self._conf.repodirpath in p.versions[0].uris[0]:
                    filenames.add(p.versions[0].filename)
            return filenames

        logging.info(_('Processing target repository ...'))
        # Анализ пакетов основного репозитория
        target_builded_deps = set()
        sources = dict()
        tmpdirpath = tmpdirmanager.create()
        frepodirpath = os.path.join(tmpdirpath, '{}_main'.format(self._conf.reponame))
        frepodevdirpath = os.path.join(tmpdirpath, '{}_dev'.format(self._conf.reponame))
        for subdir in (frepodirpath, frepodevdirpath):
            os.makedirs(subdir, exist_ok=True)
        for required in self._packages['target']:
            logging.info(_('Processing {} ...').format(required))
            deps = self._get_depends_for_package(required,
                                                 exclude_rules=self._dev_packages_suffixes,
                                                 black_list=self._packages.get('target-dev', []))
            unresolve = [d for d in deps if d[0] == PackageType.PACKAGE_NOT_FOUND]
            deps_in_dev = [d for d in deps if d[0] in (PackageType.PACKAGE_FROM_OS_DEV_REPO,
                                                       PackageType.PACKAGE_FROM_EXT_DEV_REPO)]
            if len(unresolve):
                self._emit_unresolved(unresolve)
            if len(deps_in_dev):
                self._emit_resolved_in_dev(deps_in_dev)
            target_deps = [d for d in deps if d[0] == PackageType.PACKAGE_BUILDED]
            files_to_copy = set()
            for p in target_deps:
                resolved = p[2]
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
            m = re.match(DEB_RE, f)
            if m:
                package_name = m.group('name')
                dev_packages.append(package_name)
        dev_packages = sorted([p for p in set(dev_packages) - set(self._packages['target'])])
        for devpkg in dev_packages:
            logging.info(_('Processing {} ...').format(devpkg))
            deps = self._get_depends_for_package(devpkg, flags=DependencyFinder.FLAG_FINDER_DEV)
            unresolve = [d for d in deps if d[0] == PackageType.PACKAGE_NOT_FOUND]
            if len(unresolve):
                self._emit_unresolved(unresolve)
            builded = [d[2] for d in deps if d[0] == PackageType.PACKAGE_BUILDED]
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
                    traceback.print_exc()
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
                dst = os.path.join(self._conf.fsrcdirpath, os.path.basename(source))
                try:
                    logging.debug(_('Copying {} to {}').format(source, dst))
                    shutil.copyfile(source, dst)
                except Exception as e:
                    exit_with_error(e)
        if no_create_iso:
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
            isoname = 'sources-{}_{}_{}_{}.iso'.format(self._conf.reponame,
                                                       self._conf.repoversion,
                                                       self.IsoRepositoryMaker.get_codename(),
                                                       now)
            isopath = os.path.join(self._conf.isodirpath, isoname)
            label = '{} {} (sources)'.format(self._conf.reponame, self._conf.repoversion)
            logging.info(_('Building sources iso {} for {} ...').format(isopath, self._conf.reponame))
            Debhelper.run_command('genisoimage --joliet-long -r -J -o {} -V "{}" {}' % (isopath,
                                                                                        label,
                                                                                        tmpdir))
        except Exception as e:
            exit_with_error(_('Failed to create source iso: {}').format(e))
        finally:
            os.chdir(CURDIR)


class RepositoryFullCache:
    def __init__(self, conf):
        self.__conf = conf
        self.__caches = []

    def load(self):
        cache_paths = glob.glob(os.path.join(self.__conf.root, 'cache', self.__conf.distro, '*.cache')) + \
                      glob.glob(os.path.join(self.__conf.cachedirpath, '*.cache'))
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

    def find_dependencies(self, dep, required_by):
        ## Note: Тип, строка зависимости, котеж имя-версия пакета, зависимости
        ## или PackageType.PACKAGE_NOT_FOUND, depstr, None, None
        depstr = form_dependency(dep)
        logging.debug(_('Finding dependency {} required by {} ...'.format(depstr, required_by)))
        for c in self.__caches:
            depinfo = c.find_dependency(dep)
            if depinfo is not None:
                resolved, deps = depinfo
                logging.debug(_('Dependency {} resolved by {} = {} ({} repo)').format(
                    depstr, *resolved, c.name))
                return c.ctype, depstr, resolved, deps
        logging.debug(_('Dependency {} NOT FOUND').format(depstr))
        return PackageType.PACKAGE_NOT_FOUND, depstr, None, required_by


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
            self.__cache_path = os.path.abspath(os.path.join(self.__conf.cachedirpath, '..',
                                                '{}.cache'.format(self.__name)))
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
    def packages(self):
        return (sorted(p.get('package') for p in self.__packages if p['virtual'] == False))

    def create(self, packages_path):
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
                if not ctype in PackageType.available_types():
                    raise RuntimeError()
            except Exception as e:
                exit_with_error(_('Bad cache type: {} in {}').format(ctype, cache_path))
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
            pkgname, pkgver = pkginfo.get('package'), pkginfo.get('version')
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
                ## Hack по версиям
                pkgver = fix_debian_version(pkgver)
                depver = fix_debian_version(pkgver)
                is_virtual = pkginfo.get('virtual')
                if is_virtual:
                    # Виртуальный?
                    # Берем зависимости от реального пакета
                    vdep = (pkgname,
                            pkginfo.get('op', ''),
                            pkginfo.get('version', ''))
                    return self.__process_virtual_dependency(vdep)
                if self.__check_dep(pkgver, depop, depver):
                    return (pkgname, pkgver), pkginfo.get('depends', [])
        return None

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


class MakePackageCacheCmd(BaseCommand):
    CacheMapped = {
        'os': PackageType.PACKAGE_FROM_OS_REPO,
        'os-dev': PackageType.PACKAGE_FROM_OS_DEV_REPO,
        'ext': PackageType.PACKAGE_FROM_EXT_REPO,
        'ext-dev': PackageType.PACKAGE_FROM_EXT_DEV_REPO
    }
    cmd = 'make-package-cache'
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
    cmd = 'remove-sources'
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
            dbg_expr = '%s/%s-dbgsym_%s*deb' % (self._conf.repodirpath, binary, pver)
            binaries = binaries + glob.glob(expr) + glob.glob(dbg_expr)
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


class RepoRuntimeDepsAnalyzerCmd(_RepoAnalyzerCmd):
    cmd = 'check-runtime-deps'

    def run(self):
        all_unresolved = []
        all_in_dev = []
        for pkgname in self._builded_cache.packages:
            logging.info(_('Processing {} ...').format(pkgname))
            deps = self._get_depends_for_package(pkgname)
            unresolve = [d for d in deps if d[0] == PackageType.PACKAGE_NOT_FOUND]
            deps_in_dev = [d for d in deps if d[0] in (PackageType.PACKAGE_FROM_OS_DEV_REPO,
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
    root_needed = True
    required_binaries = ['debootstrap', 'systemd-nspawn']

    def run(self):
        nsconainer = NSPContainer(self._conf)
        nsconainer.create()


def make_default_subparser(main_parser, command):
    parser = main_parser.add_parser(command)
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
        for item in (os.path.join(conf.root, 'logs'),
                     os.path.join(conf.root, 'repo'),
                     conf.cachedirpath,
                     conf.chrootsdirpath):
            _chown_recurse(item, sudo_user, sudo_group)
        # Файлы логов
        for file in (os.path.join(conf.root, 'logs', conf.distro, 'chroot-{}.log'.format(conf.distro)),
                     os.path.join(conf.root, 'build-{}.log'.format(conf.reponame))):
            shutil.chown(file, sudo_user, sudo_group)

    import atexit
    atexit.register(remove_temp_directory_atexit_callback)
    atexit.register(chown_files_atexit_callback)

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
