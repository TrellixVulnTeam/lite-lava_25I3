# Copyright (C) 2011 Linaro Limited
#
# Author: Michael Hudson-Doyle <michael.hudson@linaro.org>
# Author: Paul Larson <paul.larson@linaro.org>
#
# This file is part of LAVA Dispatcher.
#
# LAVA Dispatcher is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# LAVA Dispatcher is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along
# with this program; if not, see <http://www.gnu.org/licenses>.

import contextlib
import logging
import os
import time
import re

import pexpect

from lava_dispatcher.device import boot_options
from lava_dispatcher import tarballcache

from lava_dispatcher.client.base import (
    NetworkCommandRunner,
)
from lava_dispatcher.device.target import (
    Target
)
from lava_dispatcher.downloader import (
    download_image,
    download_with_retry,
)
from lava_dispatcher.errors import (
    NetworkError,
    CriticalError,
    OperationFailed,
)
from lava_dispatcher.utils import (
    connect_to_serial,
    mk_targz,
    string_to_list,
    rmtree,
    mkdtemp,
    extract_targz,
)
from lava_dispatcher.client.lmc_utils import (
    generate_image,
    image_partition_mounted,
)
from lava_dispatcher import deployment_data


class MasterImageTarget(Target):

    MASTER_PS1 = ' [rc=$(echo \$?)]# '
    MASTER_PS1_PATTERN = ' \[rc=(\d+)\]# '

    def __init__(self, context, config):
        super(MasterImageTarget, self).__init__(context, config)

        # Update variable according to config file
        self.MASTER_PS1 = self.config.master_str + self.MASTER_PS1
        self.MASTER_PS1_PATTERN = self.config.master_str + self.MASTER_PS1_PATTERN

        self.master_ip = None
        self.device_version = None

        if config.pre_connect_command:
            self.context.run_command(config.pre_connect_command)

        self.proc = connect_to_serial(self.context)

        self.__boot_cmds_dynamic__ = None

    def get_device_version(self):
        return self.device_version

    def power_on(self):
        self._boot_linaro_image()
        return self.proc

    def power_off(self, proc):
        if self.config.power_off_cmd:
            self.context.run_command(self.config.power_off_cmd)

    def deploy_linaro(self, hwpack, rfs, bootloadertype):
        self.boot_master_image()

        image_file = generate_image(self, hwpack, rfs, self.scratch_dir, bootloadertype)
        (boot_tgz, root_tgz, distro) = self._generate_tarballs(image_file)

        self._read_boot_cmds(boot_tgz=boot_tgz)
        self._deploy_tarballs(boot_tgz, root_tgz)

    def deploy_android(self, boot, system, userdata):
        self.deployment_data = deployment_data.android
        self.boot_master_image()

        sdir = self.scratch_dir
        boot = download_image(boot, self.context, sdir, decompress=False)
        system = download_image(system, self.context, sdir, decompress=False)
        data = download_image(userdata, self.context, sdir, decompress=False)

        with self._as_master() as master:
            self._format_testpartition(master, 'ext4')
            self._deploy_android_tarballs(master, boot, system, data)

            if master.has_partition_with_label('userdata') and \
                    master.has_partition_with_label('sdcard'):
                _purge_linaro_android_sdcard(master)

    def _deploy_android_tarballs(self, master, boot, system, data):
        tmpdir = self.context.config.lava_image_tmpdir
        url = self.context.config.lava_image_url

        boot = boot.replace(tmpdir, '')
        system = system.replace(tmpdir, '')
        data = data.replace(tmpdir, '')

        boot_url = '/'.join(u.strip('/') for u in [url, boot])
        system_url = '/'.join(u.strip('/') for u in [url, system])
        data_url = '/'.join(u.strip('/') for u in [url, data])

        _deploy_linaro_android_boot(master, boot_url, self)
        _deploy_linaro_android_system(master, system_url)
        _deploy_linaro_android_data(master, data_url)

    def deploy_linaro_prebuilt(self, image, bootloadertype):
        self.boot_master_image()

        if self.context.job_data.get('health_check', False):
            (boot_tgz, root_tgz, distro) = tarballcache.get_tarballs(
                self.context, image, self.scratch_dir, self._generate_tarballs)
            self.deployment_data = deployment_data.get(distro)
        else:
            image_file = download_image(image, self.context, self.scratch_dir)
            (boot_tgz, root_tgz, distro) = self._generate_tarballs(image_file)

        self._read_boot_cmds(boot_tgz=boot_tgz)
        self._deploy_tarballs(boot_tgz, root_tgz)

    def _deploy_tarballs(self, boot_tgz, root_tgz):
        tmpdir = self.context.config.lava_image_tmpdir
        url = self.context.config.lava_image_url

        boot_tarball = boot_tgz.replace(tmpdir, '')
        root_tarball = root_tgz.replace(tmpdir, '')
        boot_url = '/'.join(u.strip('/') for u in [url, boot_tarball])
        root_url = '/'.join(u.strip('/') for u in [url, root_tarball])
        with self._as_master() as master:
            self._format_testpartition(master, 'ext4')
            try:
                _deploy_linaro_rootfs(master, root_url)
                _deploy_linaro_bootfs(master, boot_url)
            except:
                logging.exception("Deployment failed")
                raise CriticalError("Deployment failed")

    def _rewrite_partition_number(self, matchobj):
        """ Returns the partition number after rewriting it to
        n + testboot_offset.
        """
        boot_device = str(self.config.boot_device)
        testboot_offset = self.config.testboot_offset
        partition = int(matchobj.group('partition')) + testboot_offset
        return ' ' + boot_device + ':' + str(partition) + ' '

    def _rewrite_boot_cmds(self, boot_cmds):
        """
        Returns boot_cmds list after rewriting things such as:

        * partition number from n to n + testboot_offset
        * root=LABEL=testrootfs instead of root=UUID=ab34-...
        """
        boot_cmds = re.sub(
            r"root=UUID=\S+", "root=LABEL=testrootfs", boot_cmds, re.MULTILINE)

        pattern = "\s+\d+:(?P<partition>\d+)\s+"
        boot_cmds = re.sub(
            pattern, self._rewrite_partition_number, boot_cmds, re.MULTILINE)

        return boot_cmds.split('\n')

    def _read_boot_cmds(self, image=None, boot_tgz=None):
        boot_file_path = None

        if not self.config.read_boot_cmds_from_image:
            return

        # If we have already obtained boot commands dynamically, then return.
        if self.__boot_cmds_dynamic__ is not None:
            logging.debug("We already have boot commands in place.")
            return

        if image:
            boot_part = self.config.boot_part
            # Read boot related file from the boot partition of image.
            with image_partition_mounted(image, boot_part) as mnt:
                for boot_file in self.config.boot_files:
                    boot_path = os.path.join(mnt, boot_file)
                    if os.path.exists(boot_path):
                        boot_file_path = boot_path
                        break

        elif boot_tgz:
            tmp_dir = mkdtemp()
            extracted_files = extract_targz(boot_tgz, tmp_dir)
            for boot_file in self.config.boot_files:
                for file_path in extracted_files:
                    if boot_file == os.path.basename(file_path):
                        boot_file_path = file_path
                        break

        if boot_file_path and os.path.exists(boot_file_path):
            with open(boot_file_path, 'r') as f:
                boot_cmds = self._rewrite_boot_cmds(f.read())
                self.__boot_cmds_dynamic__ = boot_cmds
        else:
            logging.debug("Unable to read boot commands dynamically.")

    def _format_testpartition(self, runner, fstype):
        logging.info("Format testboot and testrootfs partitions")
        runner.run('umount /dev/disk/by-label/testrootfs', failok=True)
        runner.run('nice mkfs -t %s -q /dev/disk/by-label/testrootfs -L testrootfs'
                   % fstype, timeout=1800)
        runner.run('umount /dev/disk/by-label/testboot', failok=True)
        runner.run('nice mkfs.vfat /dev/disk/by-label/testboot -n testboot')

    def _generate_tarballs(self, image_file):
        self._customize_linux(image_file)
        self._read_boot_cmds(image=image_file)
        boot_tgz = os.path.join(self.scratch_dir, "boot.tgz")
        root_tgz = os.path.join(self.scratch_dir, "root.tgz")
        try:
            _extract_partition(image_file, self.config.boot_part, boot_tgz)
            _extract_partition(image_file, self.config.root_part, root_tgz)
        except:
            logging.exception("Failed to generate tarballs")
            raise

        return boot_tgz, root_tgz, self.deployment_data['distro']

    def target_extract(self, runner, tar_url, dest, timeout=-1, num_retry=5):
        decompression_char = ''
        if tar_url.endswith('.gz') or tar_url.endswith('.tgz'):
            decompression_char = 'z'
        elif tar_url.endswith('.bz2'):
            decompression_char = 'j'
        else:
            raise RuntimeError('bad file extension: %s' % tar_url)

        while num_retry > 0:
            try:
                runner.run(
                    'wget --no-check-certificate --no-proxy '
                    '--connect-timeout=30 -S --progress=dot -e dotbytes=2M '
                    '-O- %s | '
                    'tar --warning=no-timestamp --numeric-owner -C %s -x%sf -'
                    % (tar_url, dest, decompression_char),
                    timeout=timeout)
                return
            except (OperationFailed, pexpect.TIMEOUT):
                logging.warning(("transfering %s failed. %d retry left."
                                 % (tar_url, num_retry - 1)))

            if num_retry > 1:
                # send CTRL C in case wget still hasn't exited.
                self.proc.sendcontrol("c")
                self.proc.sendline(
                    "echo 'retry left %s time(s)'" % (num_retry - 1))
                # And wait a little while.
                sleep_time = 60
                logging.info("Wait %d second before retry" % sleep_time)
                time.sleep(sleep_time)
            num_retry -= 1

        raise RuntimeError('extracting %s on target failed' % tar_url)

    def get_partition(self, runner, partition):
        if partition == self.config.boot_part:
            partition = '/dev/disk/by-label/testboot'
        elif partition == self.config.root_part:
            partition = '/dev/disk/by-label/testrootfs'
        elif partition == self.config.sdcard_part_android_org:
            partition = '/dev/disk/by-label/sdcard'
        elif partition == self.config.data_part_android_org:
            lbl = _android_data_label(runner)
            partition = '/dev/disk/by-label/%s' % lbl
        else:
            raise RuntimeError(
                'unknown master image partition(%d)' % partition)
        return partition

    @contextlib.contextmanager
    def file_system(self, partition, directory):
        logging.info('attempting to access master filesystem %r:%s' %
                     (partition, directory))

        assert directory != '/', "cannot mount entire partition"

        with self._as_master() as runner:
            partition = self.get_partition(runner, partition)
            runner.run('mount %s /mnt' % partition)
            try:
                targetdir = os.path.join('/mnt/%s' % directory)
                if not runner.is_file_exist(targetdir):
                    runner.run('mkdir %s' % targetdir)

                parent_dir, target_name = os.path.split(targetdir)

                runner.run('nice tar -czf /tmp/fs.tgz -C %s %s' %
                           (parent_dir, target_name))
                runner.run('cd /tmp')  # need to be in same dir as fs.tgz
                self.proc.sendline('python -m SimpleHTTPServer 0 2>/dev/null')
                match_id = self.proc.expect([
                    'Serving HTTP on 0.0.0.0 port (\d+) \.\.',
                    pexpect.EOF, pexpect.TIMEOUT])
                if match_id != 0:
                    msg = "Unable to start HTTP server on master"
                    logging.error(msg)
                    raise CriticalError(msg)
                port = self.proc.match.groups()[match_id]

                url = "http://%s:%s/fs.tgz" % (self.master_ip, port)
                tf = download_with_retry(
                    self.context, self.scratch_dir, url, False)

                tfdir = os.path.join(self.scratch_dir, str(time.time()))
                try:
                    os.mkdir(tfdir)
                    self.context.run_command('nice tar -C %s -xzf %s' % (tfdir, tf))
                    yield os.path.join(tfdir, target_name)

                finally:
                    tf = os.path.join(self.scratch_dir, 'fs.tgz')
                    mk_targz(tf, tfdir)
                    rmtree(tfdir)

                    self.proc.sendcontrol('c')  # kill SimpleHTTPServer

                    # get the last 2 parts of tf, ie "scratchdir/tf.tgz"
                    tf = '/'.join(tf.split('/')[-2:])
                    url = '%s/%s' % (self.context.config.lava_image_url, tf)
                    runner.run('rm -rf %s' % targetdir)
                    self.target_extract(runner, url, parent_dir)

            finally:
                    self.proc.sendcontrol('c')  # kill SimpleHTTPServer
                    runner.run('umount /mnt')

    def extract_tarball(self, tarball_url, partition, directory='/'):
        logging.info('extracting %s to target' % tarball_url)

        with self._as_master() as runner:
            partition = self.get_partition(runner, partition)
            runner.run('mount %s /mnt' % partition)
            try:
                self.target_extract(runner, tarball_url, '/mnt/%s' % directory)
            finally:
                runner.run('umount /mnt')

    def _wait_for_master_boot(self):
        self.proc.expect(self.config.image_boot_msg, timeout=300)
        self._wait_for_prompt(self.proc, self.config.master_str, timeout=300)

    def boot_master_image(self):
        """
        reboot the system, and check that we are in a master shell
        """
        boot_attempts = self.config.boot_retries
        attempts = 0
        in_master_image = False
        while (attempts < boot_attempts) and (not in_master_image):
            logging.info("Booting the system master image. Attempt: %d" %
                         (attempts + 1))
            try:
                self._soft_reboot()
                self._wait_for_master_boot()
            except (OperationFailed, pexpect.TIMEOUT) as e:
                logging.info("Soft reboot failed: %s" % e)
                try:
                    self._hard_reboot()
                    self._wait_for_master_boot()
                except (OperationFailed, pexpect.TIMEOUT) as e:
                    msg = "Hard reboot into master image failed: %s" % e
                    logging.warning(msg)
                    attempts += 1
                    continue

            try:
                self.proc.sendline('export PS1="%s"' % self.MASTER_PS1)
                self.proc.expect(
                    self.MASTER_PS1_PATTERN, timeout=120, lava_no_logging=1)
            except pexpect.TIMEOUT as e:
                msg = "Failed to get command line prompt: " % e
                logging.warning(msg)
                attempts += 1
                continue

            runner = MasterCommandRunner(self)
            try:
                self.master_ip = runner.get_target_ip()
                self.device_version = runner.get_device_version()
            except NetworkError as e:
                msg = "Failed to get network up: " % e
                logging.warning(msg)
                attempts += 1
                continue

            lava_proxy = self.context.config.lava_proxy
            if lava_proxy:
                logging.info("Setting up http proxy")
                runner.run("export http_proxy=%s" % lava_proxy, timeout=30)
            logging.info("System is in master image now")
            in_master_image = True

        if not in_master_image:
            msg = "Could not get master image booted properly"
            logging.critical(msg)
            raise CriticalError(msg)

    @contextlib.contextmanager
    def _as_master(self):
        """A session that can be used to run commands in the master image."""
        self.proc.sendline("")
        match_id = self.proc.expect(
            [self.MASTER_PS1_PATTERN, pexpect.TIMEOUT],
            timeout=10, lava_no_logging=1)
        if match_id == 1:
            self.boot_master_image()
        yield MasterCommandRunner(self)

    def _soft_reboot(self):
        logging.info("Perform soft reboot the system")
        self.master_ip = None
        # Try to C-c the running process, if any.
        self.proc.sendcontrol('c')
        self.proc.sendline(self.config.soft_boot_cmd)
        # Looking for reboot messages or if they are missing, the U-Boot
        # message will also indicate the reboot is done.
        match_id = self.proc.expect(
            [pexpect.TIMEOUT, 'Restarting system.',
             'The system is going down for reboot NOW',
             'Will now restart', 'U-Boot'], timeout=120)
        if match_id == 0:
            raise OperationFailed("Soft reboot failed")

    def _hard_reboot(self):
        logging.info("Perform hard reset on the system")
        self.master_ip = None
        if self.config.hard_reset_command != "":
            self.context.run_command(self.config.hard_reset_command)
        else:
            self.proc.send("~$")
            self.proc.sendline("hardreset")
            self.proc.empty_buffer()

    def _boot_linaro_image(self):
        boot_cmds_job_file = False
        boot_cmds_boot_options = False
        boot_cmds = self.deployment_data['boot_cmds']
        options = boot_options.as_dict(self, defaults={'boot_cmds': boot_cmds})

        boot_cmds_job_file = self._is_job_defined_boot_cmds(self.config.boot_cmds)

        if 'boot_cmds' in options:
            if options['boot_cmds'].value != 'boot_cmds':
                boot_cmds_boot_options = True

        # Interactive boot_cmds from the job file are a list.
        # We check for them first, if they are present, we use
        # them and ignore the other cases.
        if boot_cmds_job_file:
            logging.info('Overriding boot_cmds from job file')
            boot_cmds = self.config.boot_cmds
        # If there were no interactive boot_cmds, next we check
        # for boot_option overrides. If one exists, we use them
        # and ignore all other cases.
        elif boot_cmds_boot_options:
            logging.info('Overriding boot_cmds from boot_options')
            boot_cmds = options['boot_cmds'].value
            logging.info('boot_option=%s' % boot_cmds)
            boot_cmds = self.config.cp.get('__main__', boot_cmds)
            boot_cmds = string_to_list(boot_cmds.encode('ascii'))
        # No interactive or boot_option overrides are present,
        # we prefer to get the boot_cmds for the image if they are
        # present.
        elif self.__boot_cmds_dynamic__ is not None:
            logging.info('Loading boot_cmds from image')
            boot_cmds = self.__boot_cmds_dynamic__
        # This is the catch all case. Where we get the default boot_cmds
        # from the deployment data.
        else:
            logging.info('Loading boot_cmds from device configuration')
            boot_cmds = self.config.cp.get('__main__', boot_cmds)
            boot_cmds = string_to_list(boot_cmds.encode('ascii'))

        logging.info('boot_cmds: %s', boot_cmds)

        self._boot(boot_cmds)

    def _boot(self, boot_cmds):
        try:
            self._soft_reboot()
            self._enter_bootloader(self.proc)
        except:
            logging.exception("_enter_bootloader failed")
            self._hard_reboot()
            self._enter_bootloader(self.proc)
        self._customize_bootloader(self.proc, boot_cmds)

target_class = MasterImageTarget


class MasterCommandRunner(NetworkCommandRunner):
    """A CommandRunner to use when the board is booted into the master image.
    """

    def __init__(self, target):
        super(MasterCommandRunner, self).__init__(
            target, target.MASTER_PS1_PATTERN, prompt_str_includes_rc=True)

    def get_device_version(self):
        pattern = 'device_version=(\d+-\d+/\d+-\d+)'
        self.run("echo \"device_version="
                 "$(lava-master-image-info --master-image-hwpack "
                 "| sed 's/[^0-9-]//g; s/^-\+//')"
                 "/"
                 "$(lava-master-image-info --master-image-rootfs "
                 "| sed 's/[^0-9-]//g; s/^-\+//')"
                 "\"",
                 [pattern, pexpect.EOF, pexpect.TIMEOUT],
                 timeout=5)

        device_version = None
        if self.match_id == 0:
            device_version = self.match.group(1)
            logging.debug('Master image version (hwpack/rootfs) is %s' % device_version)
        else:
            logging.warning('Could not determine image version!')

        return device_version

    def has_partition_with_label(self, label):
        if not label:
            return False

        path = '/dev/disk/by-label/%s' % label
        return self.is_file_exist(path)

    def is_file_exist(self, path):
        cmd = 'ls %s > /dev/null' % path
        rc = self.run(cmd, failok=True)
        if rc == 0:
            return True
        return False


def _extract_partition(image, partno, tarfile):
    """Mount a partition and produce a tarball of it

    :param image: The image to mount
    :param partno: The index of the partition in the image
    :param tarfile: path and filename of the tgz to output
    """
    with image_partition_mounted(image, partno) as mntdir:
        mk_targz(tarfile, mntdir, asroot=True)


def _deploy_linaro_rootfs(session, rootfs):
    logging.info("Deploying linaro image")
    session.run('udevadm trigger')
    session.run('mkdir -p /mnt/root')
    session.run('mount /dev/disk/by-label/testrootfs /mnt/root')
    # The timeout has to be this long for vexpress. For a full desktop it
    # takes 214 minutes, plus about 25 minutes for the mkfs ext3, add
    # another hour to err on the side of caution.
    session._client.target_extract(session, rootfs, '/mnt/root', timeout=18000)

    #DO NOT REMOVE - diverting flash-kernel and linking it to /bin/true
    #prevents a serious problem where packages getting installed that
    #call flash-kernel can update the kernel on the master image
    if session.run('chroot /mnt/root which dpkg-divert', failok=True) == 0:
        session.run(
            'chroot /mnt/root dpkg-divert --local /usr/sbin/flash-kernel')
        session.run(
            'chroot /mnt/root ln -sf /bin/true /usr/sbin/flash-kernel')

    session.run('umount /mnt/root')


def _deploy_linaro_bootfs(session, bootfs):
    logging.info("Deploying linaro bootfs")
    session.run('udevadm trigger')
    session.run('mkdir -p /mnt/boot')
    session.run('mount /dev/disk/by-label/testboot /mnt/boot')
    session._client.target_extract(session, bootfs, '/mnt/boot')
    session.run('umount /mnt/boot')


def _deploy_linaro_android_boot(session, boottbz2, target):
    logging.info("Deploying test boot filesystem")
    session.run('mkdir -p /mnt/lava/boot')
    session.run('mount /dev/disk/by-label/testboot /mnt/lava/boot')
    session._client.target_extract(session, boottbz2, '/mnt/lava')
    _recreate_uInitrd(session, target)
    session.run('umount /mnt/lava/boot')


def _update_uInitrd_partitions(session, rc_filename):
    # Original android sdcard partition layout by l-a-m-c
    sys_part_org = session._client.config.sys_part_android_org
    cache_part_org = session._client.config.cache_part_android_org
    data_part_org = session._client.config.data_part_android_org
    partition_padding_string_org = session._client.config.partition_padding_string_org

    # Sdcard layout in Lava image
    sys_part_lava = session._client.config.sys_part_android
    data_part_lava = session._client.config.data_part_android
    partition_padding_string_lava = session._client.config.partition_padding_string_android

    blkorg = session._client.config.android_orig_block_device
    blklava = session._client.config.android_lava_block_device

    # delete use of cache partition
    session.run('sed -i "/\/dev\/block\/%s%s%s/d" %s'
                % (blkorg, partition_padding_string_org, cache_part_org, rc_filename))
    session.run('sed -i "s/%s%s%s/%s%s%s/g" %s' % (blkorg, partition_padding_string_org, data_part_org, blklava,
                                                   partition_padding_string_lava, data_part_lava, rc_filename))
    session.run('sed -i "s/%s%s%s/%s%s%s/g" %s' % (blkorg, partition_padding_string_org, sys_part_org, blklava,
                                                   partition_padding_string_lava, sys_part_lava, rc_filename))


def _recreate_uInitrd(session, target):
    logging.debug("Recreate uInitrd")

    session.run('mkdir -p ~/tmp/')
    session.run('mv /mnt/lava/boot/uInitrd ~/tmp')
    session.run('cd ~/tmp/')

    session.run('nice dd if=uInitrd of=uInitrd.data ibs=64 skip=1')
    session.run('mv uInitrd.data ramdisk.cpio.gz')
    session.run('nice gzip -d -f ramdisk.cpio.gz; cpio -i -F ramdisk.cpio')

    session.run(
        'sed -i "/export PATH/a \ \ \ \ export PS1 \'%s\'" init.rc' %
        target.deployment_data['TESTER_PS1'])

    # The mount partitions have moved from init.rc to init.partitions.rc
    # For backward compatible with early android build, we update both rc files
    # For omapzoom and aosp and JB4.2 the operation for mounting partitions are
    # in init.omap4pandaboard.rc and fstab.* files
    possible_partitions_files = session._client.config.possible_partitions_files

    for f in possible_partitions_files:
        if session.is_file_exist(f):
            _update_uInitrd_partitions(session, f)
            session.run("cat %s" % f, failok=True)

    session.run('nice cpio -i -t -F ramdisk.cpio | cpio -o -H newc | \
            gzip > ramdisk_new.cpio.gz')

    session.run(
        'nice mkimage -A arm -O linux -T ramdisk -n "Android Ramdisk Image" \
            -d ramdisk_new.cpio.gz uInitrd')

    session.run('cd -')
    session.run('mv ~/tmp/uInitrd /mnt/lava/boot/uInitrd')
    session.run('rm -rf ~/tmp')


def _deploy_linaro_android_system(session, systemtbz2):
    logging.info("Deploying the system filesystem")
    target = session._client

    session.run('mkdir -p /mnt/lava/system')
    session.run('mount /dev/disk/by-label/testrootfs /mnt/lava/system')
    # Timeout has to be this long because of older vexpress motherboards
    # being somewhat slower
    session._client.target_extract(
        session, systemtbz2, '/mnt/lava', timeout=3600)

    if session.has_partition_with_label('userdata') and \
       session.has_partition_with_label('sdcard') and \
       session.is_file_exist('/mnt/lava/system/etc/vold.fstab'):
        # If there is no userdata partition on the sdcard(like iMX and Origen),
        # then the sdcard partition will be used as the userdata partition as
        # before, and so cannot be used here as the sdcard on android
        original = 'dev_mount sdcard %s %s ' % (
            target.config.sdcard_mountpoint_path,
            target.config.sdcard_part_android_org)
        replacement = 'dev_mount sdcard %s %s ' % (
            target.config.sdcard_mountpoint_path,
            target.config.sdcard_part_android)
        sed_cmd = "s@{original}@{replacement}@".format(original=original,
                                                       replacement=replacement)
        session.run(
            'sed -i "%s" /mnt/lava/system/etc/vold.fstab' % sed_cmd,
            failok=True)
        session.run("cat /mnt/lava/system/etc/vold.fstab", failok=True)

    script_path = '%s/%s' % ('/mnt/lava', '/system/bin/disablesuspend.sh')
    if not session.is_file_exist(script_path):
        session.run("sh -c 'export http_proxy=%s'" %
                    target.context.config.lava_proxy)
        session.run('wget --no-check-certificate %s -O %s' %
                    (target.config.git_url_disablesuspend_sh, script_path))
        session.run('chmod +x %s' % script_path)
        session.run('chown :2000 %s' % script_path)

    session.run(
        ('sed -i "s/^PS1=.*$/PS1=\'%s\'/" '
         '/mnt/lava/system/etc/mkshrc') % target.deployment_data['TESTER_PS1'],
        failok=True)

    session.run('umount /mnt/lava/system')


def _purge_linaro_android_sdcard(session):
    logging.info("Reformatting Linaro Android sdcard filesystem")
    session.run('nice mkfs.vfat /dev/disk/by-label/sdcard -n sdcard')
    session.run('udevadm trigger')


def _android_data_label(session):
    data_label = 'userdata'
    if not session.has_partition_with_label(data_label):
        #consider the compatiblity, here use the existed sdcard partition
        data_label = 'sdcard'
    return data_label


def _deploy_linaro_android_data(session, datatbz2):
    data_label = _android_data_label(session)
    session.run('umount /dev/disk/by-label/%s' % data_label, failok=True)
    session.run('nice mkfs.ext4 -q /dev/disk/by-label/%s -L %s' %
                (data_label, data_label))
    session.run('udevadm trigger')
    session.run('mkdir -p /mnt/lava/data')
    session.run('mount /dev/disk/by-label/%s /mnt/lava/data' % data_label)
    session._client.target_extract(session, datatbz2, '/mnt/lava', timeout=600)
    session.run('umount /mnt/lava/data')
