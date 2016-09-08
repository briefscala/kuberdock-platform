#!/usr/bin/env python
# -*- coding: utf-8 -*-

import abc
import time
import argparse
import datetime
import grp
import logging
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import zipfile
import urllib

logger = logging.getLogger("kd_master_backup")
logger.setLevel(logging.INFO)

stdout_handler = logging.StreamHandler()
logger.addHandler(stdout_handler)

formatter = logging.Formatter(
    "[%(asctime)-15s - %(name)-6s - %(levelname)-8s]"
    " %(message)s")
stdout_handler.setFormatter(formatter)


DATABASES = ('kuberdock', )
NICENESS = 19
ETCD_DATA = '/var/lib/etcd/default.etcd/member/'
KNOWN_TOKENS = '/etc/kubernetes/known_tokens.csv'
NODE_CONFIGFILE = '/etc/kubernetes/configfile_for_nodes'
SSH_KEY = '/var/lib/nginx/.ssh/id_rsa'
ETCD_PKI = '/etc/pki/etcd/'
LICENSE = '/var/opt/kuberdock/.license'
LOCKFILE = '/var/lock/kd-master-backup.lock'


def lock(lockfile):
    def decorator(clbl):
        def wrapper(*args, **kwargs):
            try:
                # Create or fail
                os.open(lockfile, os.O_CREAT | os.O_EXCL)
            except OSError:
                raise BackupError(
                    "Another backup/restore process already running."
                    " If it is not, try to remove `{0}` and "
                    "try again.".format(lockfile))
            try:
                result = clbl(*args, **kwargs)
            finally:
                os.unlink(lockfile)
            return result

        return wrapper

    return decorator


class BackupError(Exception):
    pass


def nice(cmd, n):
    return ["nice", "-n", str(n), ] + cmd


def sudo(cmd, as_user):
    return ["sudo", "-u", as_user] + cmd


def zipdir(path, ziph):
    for root, dirs, files in os.walk(path):
        for fn in files:
            full_fn = os.path.join(root, fn)
            ziph.write(full_fn, os.path.relpath(full_fn, path))


def pg_dump(src, dst, username="postgres"):
    cmd = sudo(nice(["pg_dump", "-C", "-Fc", "-U",
                     username, src], NICENESS), "postgres")
    with open(dst, 'wb') as out:
        subprocess.check_call(cmd, stdout=out)


def etcd_backup(src, dst):
    cmd = nice(["etcdctl", "backup", "--data-dir", src, "--backup-dir", dst],
               NICENESS)
    subprocess.check_call(cmd, stdout=subprocess.PIPE)


def pg_restore(src, username="postgres"):
    cmd = sudo(["pg_restore", "-U", username, "-n", "public",
                "-c", "-1", "-d", "kuberdock", src], "postgres")
    subprocess.check_call(cmd)


def etcd_restore(src, dst):
    cmd = ["etcdctl", "backup", "--data-dir", src, "--backup-dir", dst]
    subprocess.check_call(cmd, stdout=subprocess.PIPE)


class BackupResource(object):
    __metaclass__ = abc.ABCMeta

    @abc.abstractmethod
    def backup(cls, dst):
        pass

    @abc.abstractmethod
    def restore(cls, zip_archive):
        pass


class PostgresResource(BackupResource):

    @classmethod
    def backup(cls, dst):
        _, postgres_tmp = tempfile.mkstemp(prefix="postgres-", dir=dst,
                                           suffix='.backup.in_progress')
        pg_dump(DATABASES[0], postgres_tmp)

        result = os.path.join(dst, "postgresql.backup")
        os.rename(postgres_tmp, result)
        return result

    @classmethod
    def restore(cls, zip_archive):
        fd, src = tempfile.mkstemp()
        try:
            uid = pwd.getpwnam("postgres").pw_uid
            os.fchown(fd, uid, -1)
            with os.fdopen(fd, 'w') as tmp:
                shutil.copyfileobj(zip_archive.open('postgresql.backup'), tmp)
                tmp.seek(0)
                pg_restore(src)
        finally:
            os.remove(src)
        return src


class EtcdResource(BackupResource):

    @classmethod
    def backup(cls, dst):
        etcd_tmp = tempfile.mkdtemp(prefix="etcd-", dir=dst,
                                    suffix="-inprogress")

        for fn in os.listdir(ETCD_DATA):
            copy_from = os.path.join(ETCD_DATA, fn)
            copy_to = os.path.join(etcd_tmp, fn)
            shutil.copytree(copy_from, copy_to)

        open(os.path.join(etcd_tmp, 'snap', 'dummy'), 'a').close()
        result = os.path.join(dst, "etcd")
        os.rename(etcd_tmp, result)
        return result

    @classmethod
    def restore(cls, zip_archive):
        src = tempfile.mkdtemp()
        try:
            zip_archive.extractall(src, filter(lambda x: x.startswith('etcd'),
                                   zip_archive.namelist()))

            pki_src = os.path.join(src, 'etcd_pki')
            for fn in os.listdir(pki_src):
                shutil.copy(os.path.join(pki_src, fn), ETCD_PKI)

            data_src = os.path.join(src, 'etcd')
            try:
                shutil.rmtree(os.path.join(ETCD_DATA, 'wal'))
                shutil.rmtree(os.path.join(ETCD_DATA, 'snap'))
            except OSError:
                pass

            for fn in os.listdir(data_src):
                copy_from = os.path.join(data_src, fn)
                copy_to = os.path.join(ETCD_DATA, fn)
                shutil.copytree(copy_from, copy_to)

            uid = pwd.getpwnam("etcd").pw_uid
            gid = grp.getgrnam("etcd").gr_gid
            for root, dirs, files in os.walk(ETCD_DATA):
                for fn in dirs:
                    os.chown(os.path.join(root, fn), uid, gid)
                for fn in files:
                    os.chown(os.path.join(root, fn), uid, gid)
        finally:
            shutil.rmtree(src)
        return src


class KubeTokenResource(BackupResource):

    @classmethod
    def backup(cls, dst):
        shutil.copy(KNOWN_TOKENS, dst)
        shutil.copy(NODE_CONFIGFILE, dst)

        return os.path.join(dst, 'known_tokens.csv')

    @classmethod
    def restore(cls, zip_archive):
        with open(KNOWN_TOKENS, 'w') as tmp:
            shutil.copyfileobj(zip_archive.open('known_tokens.csv'), tmp)

        with open(NODE_CONFIGFILE, 'w') as tmp:
            shutil.copyfileobj(zip_archive.open('configfile_for_nodes'), tmp)


class SSHKeysResource(BackupResource):

    @classmethod
    def backup(cls, dst):
        fd, key_tmp = tempfile.mkstemp(prefix="ssh-key-", dir=dst,
                                       suffix='.backup.in_progress')
        with open(SSH_KEY, 'r') as src:
            with os.fdopen(fd, 'w') as tmp_dst:
                shutil.copyfileobj(src, tmp_dst)

        shutil.copy(SSH_KEY + '.pub', dst)
        result = os.path.join(dst, "id_rsa")
        os.rename(key_tmp, result)
        return result

    @classmethod
    def restore(cls, zip_archive):
        with open(SSH_KEY, 'w') as tmp:
            shutil.copyfileobj(zip_archive.open('id_rsa'), tmp)
        with open(SSH_KEY + '.pub', 'w') as tmp:
            shutil.copyfileobj(zip_archive.open('id_rsa.pub'), tmp)
        return tmp


class EtcdCertResource(BackupResource):

    @classmethod
    def backup(cls, dst):
        pki_src = '/etc/pki/etcd/'
        etcd_tmp = tempfile.mkdtemp(prefix="etcd-", dir=dst,
                                    suffix="-inprogress")
        for fn in os.listdir(pki_src):
            shutil.copy(os.path.join(pki_src, fn), etcd_tmp)

        result = os.path.join(dst, "etcd_pki")
        os.rename(etcd_tmp, result)
        return result

    @classmethod
    def restore(cls, zip_archive):
        pass


class LicenseResource(BackupResource):

    @classmethod
    def backup(cls, dst):
        if os.path.isfile(LICENSE):
            shutil.copy(LICENSE, dst)
            return os.path.join(dst, '.license')

    @classmethod
    def restore(cls, zip_archive):
        if '.license' in zip_archive.namelist():
            with open(LICENSE, 'w') as tmp:
                shutil.copyfileobj(zip_archive.open('.license'), tmp)


class SharedNginxConfigResource(BackupResource):

    config_src = '/etc/nginx/conf.d/'

    @classmethod
    def backup(cls, dst):
        config_tmp = tempfile.mkdtemp(prefix="nginx-config-", dir=dst,
                                      suffix="-inprogress")
        for fn in os.listdir(cls.config_src):
            shutil.copy(os.path.join(cls.config_src, fn), config_tmp)

        result = os.path.join(dst, "nginx_config")
        os.rename(config_tmp, result)
        return result

    @classmethod
    def restore(cls, zip_archive):
        src = tempfile.mkdtemp()
        try:
            zip_archive.extractall(src, filter(lambda x: x.startswith('nginx'),
                                   zip_archive.namelist()))
            pki_src = os.path.join(src, 'nginx_config')
            for fn in os.listdir(pki_src):
                shutil.copy(os.path.join(pki_src, fn), cls.config_src)
        finally:
            shutil.rmtree(src)
        return src


backup_chain = (PostgresResource, EtcdResource, SSHKeysResource,
                EtcdCertResource, KubeTokenResource, LicenseResource,
                SharedNginxConfigResource)

restore_chain = (PostgresResource, EtcdResource, SSHKeysResource,
                 EtcdCertResource, KubeTokenResource, LicenseResource,
                 SharedNginxConfigResource)


@lock(LOCKFILE)
def do_backup(backup_dir, callback, skip_errors, **kwargs):

    if not os.path.exists(backup_dir):
        os.makedirs(backup_dir)

    timestamp = datetime.datetime.today().isoformat()
    logger.info('Backup started {0}'.format(backup_dir))
    backup_dst = tempfile.mkdtemp(dir=backup_dir, prefix=timestamp)

    logger.addHandler(logging.FileHandler(os.path.join(backup_dst,
                      'main.log')))

    for res in backup_chain:
        try:
            subresult = res.backup(backup_dst)
            logger.info("File collected: {0}".format(subresult))
        except subprocess.CalledProcessError as err:
            logger.error("%s backuping error: %s" % (res, err))
            if not skip_errors:
                raise

    result = os.path.join(backup_dir, timestamp + ".zip")
    with zipfile.ZipFile(result, 'w', zipfile.ZIP_DEFLATED) as zipf:
        zipdir(backup_dst, zipf)

    logger.info('Backup finished {0}'.format(result))
    if callback:
        subprocess.Popen("{0} {1}".format(callback, result),
                         shell=True)
    return result


def purge_nodes():
    """ Delete all remains of nodes. Usefull when restored master
    have no need in nodes from backup. [AC-4339]
    """
    from kubedock.users import User
    from kubedock.api import create_app
    from kubedock.kapi.node_utils import get_all_nodes
    from kubedock.kapi.nodes import delete_node
    from kubedock.nodes.models import Node
    from kubedock.pods.models import PersistentDisk
    from kubedock.kapi.podcollection import PodCollection

    create_app(fake_sessions=True).app_context().push()

    internal_pods = [pod['name'] for pod in PodCollection(
        User.get_internal()).get(as_json=False)]

    pod_collection = PodCollection()
    for pod in pod_collection.get(as_json=False):
        if pod['name'] not in internal_pods:
            pod_collection.delete(pod['id'])
            logger.debug("Pod `{0}` deleted".format(pod['name']))

    for node in get_all_nodes():
        node_name = node['metadata']['name']
        db_node = Node.get_by_name(node_name)
        PersistentDisk.get_by_node_id(db_node.id).delete(
            synchronize_session=False)
        delete_node(node=db_node, force=True)
        logger.debug("Node `{0}` deleted".format(node_name))


@lock(LOCKFILE)
def do_restore(backup_file, without_nodes, skip_errors, **kwargs):
    """ Restore from backup file.
    If skip_error is True it will not interrupt restore due to errors
    raised on some step from restore_chain.
    If without_nodes is True it will remove any traces of old nodes
    right after restore is succeded.
    """
    logger.info('Restore started {0}'.format(backup_file))

    subprocess.check_call(["systemctl", "restart", "postgresql"])
    subprocess.check_call(["systemctl", "stop", "etcd", "kube-apiserver"])
    with zipfile.ZipFile(backup_file, 'r') as zip_archive:
        for res in restore_chain:
            try:
                subresult = res.restore(zip_archive)
                logger.info("File restored: {0} ({1})".format(subresult, res))
            except subprocess.CalledProcessError as err:
                logger.error("%s restore error: %s" % (res, err))
                if not skip_errors:
                    raise


    subprocess.check_call(["systemctl", "start", "etcd"])
    time.sleep(5)
    subprocess.check_call(["systemctl", "start", "kube-apiserver"])

    subprocess.check_call(["systemctl", "restart", "nginx"])
    subprocess.check_call(["systemctl", "restart", "emperor.uwsgi"])

    if without_nodes:
        purge_nodes()

    logger.info('Restore finished')


def parse_args(args):
    parser = argparse.ArgumentParser()

    group = parser.add_mutually_exclusive_group()
    group.add_argument('-v', '--verbose', help='Verbose (debug) logging',
                       action='store_const', const=logging.DEBUG,
                       dest='loglevel')
    group.add_argument('-q', '--quiet', help='Silent mode, only log warnings',
                       action='store_const', const=logging.WARN,
                       dest='loglevel')
    parser.add_argument("-n", '--without-nodes', action='store_true',
                        dest='without_nodes',
                        help="Do not stop if one steps is failed")
    parser.add_argument("-s", '--skip', action='store_false',
                        dest='skip_errors',
                        help="Do not stop if one steps is failed")

    subparsers = parser.add_subparsers()

    parser_backup = subparsers.add_parser('backup', help='backup')
    parser_backup.add_argument('backup_dir',
                               help="Destination for all created files")
    parser_backup.add_argument(
        "-e", '--callback', help='Callback for each backup file'
        ' (backup path passed as a 1st arg)')
    parser_backup.set_defaults(func=do_backup)

    parser_restore = subparsers.add_parser('restore', help='restore')
    parser_restore.add_argument('backup_file')
    parser_restore.set_defaults(func=do_restore)

    return parser.parse_args(args)


def main():
    if os.getuid() != 0:
        raise Exception('Root permissions required to run this script')

    args = parse_args(sys.argv[1:])
    logger.setLevel(args.loglevel or logging.INFO)
    args.func(**vars(args))


if __name__ == '__main__':
    main()
