#    Copyright (C) 2012-2016 Germar Reitze, Taylor Raack
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License along
#    with this program; if not, write to the Free Software Foundation, Inc.,
#    51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import os
import grp
import subprocess
import gettext
import string
import random
import tempfile
import socket
import re
from time import sleep

import config
import logger
import tools
import password_ipc
from mount import MountControl
from exceptions import MountException
import bcolors

_=gettext.gettext

class SSH(MountControl):
    """
    This is a backend for the mount API :py:class:`mount.MountControl`.
    This will mount the remote path with ``sshfs``, prepair the remote path and
    check that everything is set up correctly for `Back In Time` to run
    snapshots through SSH.

    This class will only mount the remote path. The real takeSnapshot process
    will use rsync over ssh. Other commands run remote over ssh.

    Args:
        cfg (config.Config):    current config
                                (handled by inherited :py:class:`mount.MountControl`)
        user (str):             User name on remote host
        host (str):             Name or IP Address of remote host
        port (int):             Port used by SSHd on remote host
        path (str):             remote path where snapshots are stored. Can be
                                either relative from remote users homedir or
                                an absolute path
        cipher (str):           Cipher used to encrypt the network transfer
        private_key_file (str): Private key which is able to log on with
                                Public/Private Key-Method on remote host
        nice (bool):            use ``nice -n 19`` to run commands with
                                low CPU priority on remote host
        ionice (bool):          use ``ionice -c2 -n7`` to run commands with
                                low IO priority on remote host
        nocache (bool):         use ``nocache`` to deactivate RAM caching of
                                files on remote host
        password (str):         password to unlock the private key
        profile_id (str):       profile ID that should be used
                                (handled by inherited :py:class:`mount.MountControl`)
        hash_id (str):          crc32 hash used to identify identical mountpoints
                                (handled by inherited :py:class:`mount.MountControl`)
        tmp_mount (bool):       if ``True`` mount to a temporary destination
                                (handled by inherited :py:class:`mount.MountControl`)
        parent (QWidget):       parent widget for QDialogs or ``None`` if there
                                is no parent
                                (handled by inherited :py:class:`mount.MountControl`)
        symlink (bool):         if ``True`` set symlink to mountpoint
                                (handled by inherited :py:class:`mount.MountControl`)
        mode (str):             one of ``local``, ``local_encfs``, ``ssh`` or
                                ``ssh_encfs``
                                (handled by inherited :py:class:`mount.MountControl`)
        hash_collision (int):   global value used to prevent hash collisions on
                                mountpoints
                                (handled by inherited :py:class:`mount.MountControl`)

    Note:
        All Arguments are optional. Default values will be fetched from
        :py:class:`config.Config`. But after changing Settings we need to test
        the new values **before** storing them into :py:class:`config.Config`.
        This is why all values will be added as arguments.
    """
    def __init__(self, *args, **kwargs):
        #init MountControl
        super(SSH, self).__init__(*args, **kwargs)

        self.setattrKwargs('user', self.config.sshUser(self.profile_id), **kwargs)
        self.setattrKwargs('host', self.config.sshHost(self.profile_id), **kwargs)
        self.setattrKwargs('port', self.config.sshPort(self.profile_id), **kwargs)
        self.setattrKwargs('path', self.config.sshSnapshotsPath(self.profile_id), **kwargs)
        self.setattrKwargs('cipher', self.config.sshCipher(self.profile_id), **kwargs)
        self.setattrKwargs('private_key_file', self.config.sshPrivateKeyFile(self.profile_id), **kwargs)
        self.setattrKwargs('nice', self.config.niceOnRemote(self.profile_id), store = False, **kwargs)
        self.setattrKwargs('ionice', self.config.ioniceOnRemote(self.profile_id), store = False, **kwargs)
        self.setattrKwargs('nocache', self.config.nocacheOnRemote(self.profile_id), store = False, **kwargs)
        self.setattrKwargs('password', None, store = False, **kwargs)

        if not self.path:
            self.path = './'
        self.setDefaultArgs()

        # config strings used in ssh-calls
        self.user_host_path = '%s@%s:%s' % (self.user, tools.escapeIPv6Address(self.host), self.path)
        self.user_host = '%s@%s' % (self.user, self.host)

        self.mountproc = 'sshfs'
        self.symlink_subfolder = None
        self.log_command = '%s: %s' % (self.mode, self.user_host_path)

        self.private_key_fingerprint = tools.sshKeyFingerprint(self.private_key_file)
        if not self.private_key_fingerprint:
            logger.warning('Couldn\'t get fingerprint for private key %(path)s. '
                           'Most likely because the public key %(path)s.pub wasn\'t found. '
                           'Using fallback to private keys path instead. '
                           'But this can make troubles with passphrase-less keys.'
                           %{'path': self.private_key_file},
                           self)
            self.private_key_fingerprint = self.private_key_file
        self.unlockSshAgent()

    def _mount(self):
        """
        Backend mount method. This will call ``sshfs`` to mount the remote path.

        Raises:
            exceptions.MountException:  if mount wasn't successful
        """
        sshfs  = [self.mountproc]
        sshfs += self.config.sshDefaultArgs(self.profile_id)
        sshfs += ['-p', str(self.port)]
        if not self.cipher == 'default':
            sshfs.extend(['-o', 'Ciphers=%s' % self.cipher])
        sshfs.extend(['-o', 'idmap=user',
                      '-o', 'cache_dir_timeout=2'])

        sshfs.extend([self.user_host_path, self.currentMountpoint])
        #bugfix: sshfs doesn't mount if locale in LC_ALL is not available on remote host
        #LANG or other envirnoment variable are no problem.
        env = os.environ.copy()
        if 'LC_ALL' in list(env.keys()):
            env['LC_ALL'] = 'C'
        logger.debug('Call mount command: %s'
                     %' '.join(sshfs),
                     self)
        proc = subprocess.Popen(sshfs,
                                env = env,
                                stdout = subprocess.DEVNULL,
                                stderr = subprocess.PIPE,
                                universal_newlines = True)
        err = proc.communicate()[1]
        if proc.returncode:
            raise MountException(_('Can\'t mount %s') % ' '.join(sshfs)
                                  + '\n\n' + err)

    def preMountCheck(self, first_run = False):
        """
        Check that everything is prepaired and ready for successfully mount the
        remote path. Default is to run a light version of checks which will
        only make sure the remote host is online, ``sshfs`` is installed and
        the remote folder is available.

        After changing settings this should be run with ``first_run = True``
        to run a full check with all tests.

        Args:
            first_run (bool):           run a full test with all checks

        Raises:
            exceptions.MountException:  if one test failed an we can not mount
                                        the remote path
        """
        self.checkPingHost()
        self.checkFuse()
        if first_run:
            self.unlockSshAgent(force = True)
            self.checkKnownHosts()
        self.checkLogin()
        if first_run:
            self.checkCipher()
        self.checkRemoteFolder()
        if first_run:
            self.checkRemoteCommands()
        return True

    def unlockSshAgent(self, force = False):
        """
        Unlock the private key in ``ssh-agent`` which will provide it for
        all other commands. The password to unlock the key will be provided
        by ``backintime-askpass``.

        Args:
            force (bool):               force to unlock the key by removing it
                                        first and add it again to make sure,
                                        the given values are correct

        Raises:
            exceptions.MountException:  if unlock failed
        """
        env = os.environ.copy()
        env['SSH_ASKPASS'] = 'backintime-askpass'
        env['ASKPASS_PROFILE_ID'] = self.profile_id
        env['ASKPASS_MODE'] = self.mode

        if force:
            #remove private key first so we can check if the given password is valid
            logger.debug('Remove private key %s from ssh agent' % self.private_key_file, self)
            proc = subprocess.Popen(['ssh-add', '-d', self.private_key_file],
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    universal_newlines = True)
            proc.communicate()

        proc = subprocess.Popen(['ssh-add', '-l'],
                                stdout = subprocess.PIPE,
                                universal_newlines = True)
        output = proc.communicate()[0]
        if force or not output.find(self.private_key_fingerprint) >= 0:
            logger.debug('Add private key %s to ssh agent' % self.private_key_file, self)
            password_available = any([self.config.passwordSave(self.profile_id),
                                      self.config.passwordUseCache(self.profile_id),
                                      not self.password is None
                                      ])
            logger.debug('Password available: %s' %password_available, self)
            if not password_available and not tools.checkXServer():
                #we need to unlink stdin from ssh-add in order to make it
                #use our own backintime-askpass.
                #But because of this we can NOT use getpass inside backintime-askpass
                #if password is not saved and there is no x-server.
                #So, let's just keep ssh-add asking for the password in that case.
                alarm = tools.Alarm()
                alarm.start(10)
                try:
                    proc = subprocess.call(['ssh-add', self.private_key_file])
                    alarm.stop()
                except tools.Timeout:
                    pass
            else:
                if self.password:
                    logger.debug('Provide password through temp FIFO', self)
                    thread = password_ipc.TempPasswordThread(self.password)
                    env['ASKPASS_TEMP'] = thread.temp_file
                    thread.start()

                proc = subprocess.Popen(['ssh-add', self.private_key_file],
                                        stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE,
                                        env = env,
                                        preexec_fn = os.setsid,
                                        universal_newlines = True)
                output, error = proc.communicate()
                if proc.returncode:
                    logger.error('Failed to unlock SSH private key %s: %s'
                                 %(self.private_key_file, error),
                                 self)

                if self.password:
                    thread.stop()

            proc = subprocess.Popen(['ssh-add', '-l'],
                                    stdout = subprocess.PIPE,
                                    universal_newlines = True)
            output = proc.communicate()[0]
            if not output.find(self.private_key_fingerprint) >= 0:
                logger.debug('Was not able to unlock private key %s' %self.private_key_file, self)
                raise MountException(_('Could not unlock ssh private key. Wrong password '
                                        'or password not available for cron.'))
        else:
            logger.debug('Private key %s is already unlocked in ssh agent'
                         %self.private_key_file, self)

    def checkLogin(self):
        """
        Try to login to remote host with public/private-key-method (passwordless).

        Raises:
            exceptions.MountException:  if login failed
        """
        logger.debug('Check login', self)
        ssh = self.config.sshCommand(cmd = ['echo', '"Hello"'],
                                      custom_args = ['-o', 'PreferredAuthentications=publickey',
                                                     '-p', str(self.port),
                                                     self.user_host],
                                      port = False,
                                      cipher = False,
                                      user_host = False,
                                      nice = False,
                                      ionice = False,
                                      profile_id = self.profile_id)
        proc = subprocess.Popen(ssh,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE,
                                universal_newlines = True)
        err = proc.communicate()[1]
        if proc.returncode:
            raise MountException(_('Password-less authentication for %(user)s@%(host)s '
                                    'failed. Look at \'man backintime\' for further '
                                    'instructions.')  % {'user' : self.user, 'host' : self.host}
                                    + '\n\n' + err)

    def checkCipher(self):
        """
        Try to login to remote host with the choosen cipher. This should make
        sure both `localhost` and the remote host support the choosen cipher.

        Raises:
            exceptions.MountException:  if login with the cipher failed
        """
        if not self.cipher == 'default':
            logger.debug('Check cipher', self)
            ssh = self.config.sshCommand(cmd = ['echo', '"Hello"'],
                                          custom_args = ['-o', 'Ciphers=%s' % self.cipher,
                                                         '-p', str(self.port),
                                                         self.user_host],
                                          port = False,
                                          cipher = False,
                                          user_host = False,
                                          nice = False,
                                          ionice = False,
                                          profile_id = self.profile_id)
            proc = subprocess.Popen(ssh,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE,
                                    universal_newlines = True)
            err = proc.communicate()[1]
            if proc.returncode:
                logger.debug('Ciper %s is not supported' %self.config.SSH_CIPHERS[self.cipher], self)
                raise MountException(_('Cipher %(cipher)s failed for %(host)s:\n%(err)s')
                                      % {'cipher' : self.config.SSH_CIPHERS[self.cipher], 'host' : self.host, 'err' : err})

    def benchmarkCipher(self, size = 40):
        """
        Rudimental benchmark to compare transfer speed of all available ciphers.

        Args:
            size (int):     size of the testfile in MiB
        """
        temp = tempfile.mkstemp()[1]
        print('create random data file')
        subprocess.call(['dd', 'if=/dev/urandom', 'of=%s' % temp, 'bs=1M', 'count=%s' % size])
        keys = list(self.config.SSH_CIPHERS.keys())
        keys.sort()
        for cipher in keys:
            if cipher == 'default':
                continue
            print('%s%s:%s' %(bcolors.BOLD, cipher, bcolors.ENDC))
            for i in range(2):
                # scp uses -P instead of -p for port
                subprocess.call(['scp', '-P', str(self.port), '-c', cipher, temp, self.user_host_path])
        ssh = self.config.sshCommand(cmd = ['rm', os.path.join(self.path, os.path.basename(temp))],
                                      custom_args = ['-p', str(self.port), self.user_host],
                                      port = False,
                                      cipher = False,
                                      user_host = False,
                                      nice = False,
                                      ionice = False,
                                      profile_id = self.profile_id)
        subprocess.call(ssh)
        os.remove(temp)

    def checkKnownHosts(self):
        """
        Check if the remote host is in current users ``known_hosts`` file.

        Raises:
            exceptions.MountException:  if the remote host wasn't found
                                        in ``known_hosts`` file
        """
        logger.debug('Check known hosts file', self)
        for host in (self.host, '[%s]:%s' % (self.host, self.port)):
            proc = subprocess.Popen(['ssh-keygen', '-F', host],
                                    stdout=subprocess.PIPE,
                                    universal_newlines = True)
            output = proc.communicate()[0] #subprocess.check_output doesn't exist in Python 2.6 (Debian squeeze default)
            if output.find('Host %s found' % host) >= 0:
                logger.debug('Host %s was found in known hosts file' % host, self)
                return True
        logger.debug('Host %s is not in known hosts file' %self.host, self)
        raise MountException(_('%s not found in ssh_known_hosts.') % self.host)

    def checkRemoteFolder(self):
        """
        Check the remote path. If the remote path doesn't exist this will create
        it. If it already exist this will check, that it is a folder and has
        correct permissions.

        Raises:
            exceptions.MountException:  if remote path couldn't be created or
                                        doesn't have correct permissions.
        """
        logger.debug('Check remote folder', self)
        cmd  = 'd=0;'
        cmd += 'test -e "%s" || d=1;' % self.path                 #path doesn't exist. set d=1 to indicate
        cmd += 'test $d -eq 1 && mkdir "%s"; err=$?;' % self.path #create path, get errorcode from mkdir
        cmd += 'test $d -eq 1 && exit $err;'                      #return errorcode from mkdir
        cmd += 'test -d "%s" || exit 11;' % self.path #path is no directory
        cmd += 'test -w "%s" || exit 12;' % self.path #path is not writeable
        cmd += 'test -x "%s" || exit 13;' % self.path #path is not executable
        cmd += 'exit 20'                              #everything is fine
        ssh = self.config.sshCommand(cmd = [cmd],
                                      custom_args = ['-p', str(self.port), self.user_host],
                                      port = False,
                                      cipher = False,
                                      user_host = False,
                                      nice = False,
                                      ionice = False,
                                      profile_id = self.profile_id)
        logger.debug('Call command: %s' %' '.join(ssh), self)
        proc = subprocess.Popen(ssh,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        proc.communicate()
        if proc.returncode:
            logger.debug('Command returncode: %s' %proc.returncode, self)
            if proc.returncode == 20:
                #clean exit
                pass
            elif proc.returncode == 11:
                raise MountException(_('Remote path exists but is not a directory:\n %s') % self.path)
            elif proc.returncode == 12:
                raise MountException(_('Remote path is not writeable:\n %s') % self.path)
            elif proc.returncode == 13:
                raise MountException(_('Remote path is not executable:\n %s') % self.path)
            else:
                raise MountException(_('Couldn\'t create remote path:\n %s') % self.path)
        else:
            #returncode is 0
            logger.info('Create remote folder %s' %self.path, self)

    def checkPingHost(self):
        """
        Check if the remote host is online. Other than methods name may let suppose
        this does not use Ping (``ICMP``) but try to open a connection to
        the configured port on the remote host. In this way it will even work
        on remote hosts which have ``ICMP`` disabled.

        If connection failed it will retry five times before failing.

        Raises:
            exceptions.MountException:  if connection failed most probably
                                        because remote host is offline
        """
        logger.debug('Check ping host', self)
        count = 0
        while count < 5:
            try:
                with socket.create_connection((self.host, self.port), 2.0) as s:
                    result = s.connect_ex(s.getpeername())
            except:
                result = -1
            if result == 0:
                logger.debug('Host %s is available' %self.host, self)
                return
            logger.debug('Could not ping host %s. Try again' %self.host, self)
            count += 1
            sleep(0.2)
        if result != 0:
            logger.debug('Failed pinging host %s' %self.host, self)
            raise MountException(_('Ping %s failed. Host is down or wrong address.') % self.host)

    def checkRemoteCommands(self, retry = False):
        """
        Try out all relevant commands used by `Back In Time` on the remote host
        to make sure snapshots will be successful with the remote host.
        This will also check that hard-links are supported on the remote host.

        This check can be disabled with :py:func:`config.Config.sshCheckCommands`

        Args:
            retry (bool):               retry to run the commands if it failed
                                        because the command string was to long

        Raises:
            exceptions.MountException:  if a command is not supported on
                                        remote host or if hard-links are not
                                        supported
        """
        if not self.config.sshCheckCommands():
            return
        logger.debug('Check remote commands', self)
        def maxArg():
            if retry:
                raise MountException("Checking commands on remote host didn't return any output. "
                                     "We already checked the maximum argument lenght but it seem like "
                                     "there is an other problem")
            logger.warning('Looks like the command was to long for remote SSHd. We will test max arg length now and retry.',
                           self)
            import sshMaxArg
            mid = sshMaxArg.maxArgLength(self.config)
            sshMaxArg.reportResult(self.host, mid)
            self.config.setSshMaxArgLength(mid, self.profile_id)
            return self.checkRemoteCommands(retry = True)

        remote_tmp_dir_1 = os.path.join(self.path, 'tmp_%s' % self.randomId())
        remote_tmp_dir_2 = os.path.join(self.path, 'tmp_%s' % self.randomId())
        with tempfile.TemporaryDirectory() as tmp:
            tmp_file = os.path.join(tmp, 'a')
            with open(tmp_file, 'wt') as f:
                f.write('foo')

            #check rsync
            rsync1 =  tools.rsyncPrefix(self.config, no_perms = False, progress = False)
            rsync1.append(tmp_file)
            rsync1.append('%s@%s:"%s"/' %(self.user,
                                        tools.escapeIPv6Address(self.host),
                                        remote_tmp_dir_1))

            #check remote rsync hard-link support
            rsync2 =  tools.rsyncPrefix(self.config, no_perms = False, progress = False)
            rsync2.append('--link-dest=../%s' %os.path.basename(remote_tmp_dir_1))
            rsync2.append(tmp_file)
            rsync2.append('%s@%s:"%s"/' %(self.user,
                                        tools.escapeIPv6Address(self.host),
                                        remote_tmp_dir_2))

            for cmd in (rsync1, rsync2):
                logger.debug('Check rsync command: %s' %cmd, self)

                proc = subprocess.Popen(cmd,
                                        stdout = subprocess.PIPE,
                                        stderr = subprocess.PIPE,
                                        universal_newlines = True)
                out, err = proc.communicate()
                if err or proc.returncode:
                    logger.debug('rsync command returned error: %s' %err, self)
                    raise MountException(_('Remote host %(host)s doesn\'t support \'%(command)s\':\n'
                                            '%(err)s\nLook at \'man backintime\' for further instructions')
                                            % {'host' : self.host, 'command' : cmd, 'err' : err})

        #check cp chmod find and rm
        head  = 'tmp1="%s"; tmp2="%s"; ' %(remote_tmp_dir_1, remote_tmp_dir_2)
        #first define a function to clean up and exit
        head += 'cleanup(){ '
        head += 'test -e "$tmp1/a" && rm "$tmp1/a" >/dev/null 2>&1; '
        head += 'test -e "$tmp2/a" && rm "$tmp2/a" >/dev/null 2>&1; '
        head += 'test -e smr.lock && rm smr.lock >/dev/null 2>&1; '
        head += 'test -e "$tmp1" && rmdir "$tmp1" >/dev/null 2>&1; '
        head += 'test -e "$tmp2" && rmdir "$tmp2" >/dev/null 2>&1; '
        head += 'test -n "$tmp3" && test -e "$tmp3" && rmdir "$tmp3" >/dev/null 2>&1; '
        head += 'exit $1; }; '
        tail = []

        #list inodes
        cmd  = 'ls -i "$tmp1/a"; ls -i "$tmp2/a"; '
        tail.append(cmd)
        #try nice -n 19
        if self.nice:
            cmd  = 'echo \"nice -n 19\"; nice -n 19 true >/dev/null; err_nice=$?; '
            cmd += 'test $err_nice -ne 0 && cleanup $err_nice; '
            tail.append(cmd)
        #try ionice -c2 -n7
        if self.ionice:
            cmd  = 'echo \"ionice -c2 -n7\"; ionice -c2 -n7 true >/dev/null; err_nice=$?; '
            cmd += 'test $err_nice -ne 0 && cleanup $err_nice; '
            tail.append(cmd)
        #try nocache
        if self.nocache:
            cmd  = 'echo \"nocache\"; nocache true >/dev/null; err_nocache=$?; '
            cmd += 'test $err_nocache -ne 0 && cleanup $err_nocache; '
            tail.append(cmd)
        #try screen, bash and flock used by smart-remove running in background
        if self.config.smartRemoveRunRemoteInBackground(self.profile_id):
            cmd  = 'echo \"screen -d -m bash -c ...\"; screen -d -m bash -c \"true\" >/dev/null; err_screen=$?; '
            cmd += 'test $err_screen -ne 0 && cleanup $err_screen; '
            tail.append(cmd)
            cmd  = 'echo \"(flock -x 9) 9>smr.lock\"; bash -c \"(flock -x 9) 9>smr.lock\" >/dev/null; err_flock=$?; '
            cmd += 'test $err_flock -ne 0 && cleanup $err_flock; '
            tail.append(cmd)
            cmd  = 'echo \"rmdir \$(mktemp -d)\"; tmp3=$(mktemp -d); test -z "$tmp3" && cleanup 1; rmdir $tmp3 >/dev/null; err_rmdir=$?; '
            cmd += 'test $err_rmdir -ne 0 && cleanup $err_rmdir; '
            tail.append(cmd)
        #if we end up here, everything should be fine
        cmd = 'echo \"done\"; cleanup 0'
        tail.append(cmd)

        maxLength = self.config.sshMaxArgLength(self.profile_id)
        additionalChars = len('echo ""') + len(self.config.sshPrefixCmd(self.profile_id, cmd_type = str))

        output = ''
        err = ''
        returncode = 0
        for cmd in tools.splitCommands(tail,
                                       head = head,
                                       maxLength = maxLength - additionalChars):
            if cmd.endswith('; '):
                cmd += 'echo ""'
            c = self.config.sshCommand(cmd = [cmd],
                                        custom_args = ['-p', str(self.port), self.user_host],
                                        port = False,
                                        cipher = False,
                                        user_host = False,
                                        nice = False,
                                        ionice = False,
                                        profile_id = self.profile_id)
            try:
                logger.debug('Call command: %s' %' '.join(c), self)
                proc = subprocess.Popen(c,
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE,
                                        universal_newlines = True)
                ret = proc.communicate()
            except OSError as e:
                #Argument list too long
                if e.errno == 7:
                    logger.debug('Argument list too log (Python exception)', self)
                    return maxArg()
                else:
                    raise
            logger.debug('Command stdout: %s' %ret[0], self)
            logger.debug('Command stderr: %s' %ret[1], self)
            logger.debug('Command returncode: %s' %proc.returncode, self)
            output += ret[0].strip('\n') + '\n'
            err    += ret[1].strip('\n') + '\n'
            returncode += proc.returncode
            if proc.returncode:
                break

        output_split = output.strip('\n').split('\n')

        while True:
            if output_split and not output_split[-1]:
                output_split = output_split[:-1]
            else:
                break

        if not output_split:
            return maxArg()

        if returncode or not output_split[-1].startswith('done'):
            for command in ('rm', 'nice', 'ionice', 'nocache', 'screen', '(flock'):
                if output_split[-1].startswith(command):
                    raise MountException(_('Remote host %(host)s doesn\'t support \'%(command)s\':\n'
                                            '%(err)s\nLook at \'man backintime\' for further instructions')
                                            % {'host' : self.host, 'command' : output_split[-1], 'err' : err})
            raise MountException(_('Check commands on host %(host)s returned unknown error:\n'
                                    '%(err)s\nLook at \'man backintime\' for further instructions')
                                    % {'host' : self.host, 'err' : err})

        inodes = []
        for tmp in (remote_tmp_dir_1, remote_tmp_dir_2):
            for line in output_split:
                m = re.match(r'^(\d+).*?%s' %tmp, line)
                if m:
                    inodes.append(m.group(1))

        logger.debug('remote inodes: ' + ' | '.join(inodes), self)
        if len(inodes) == 2 and inodes[0] != inodes[1]:
            raise MountException(_('Remote host %s doesn\'t support hardlinks') % self.host)

    def randomId(self, size=6, chars=string.ascii_uppercase + string.digits):
        """
        Create a random string.

        Args:
            size (int):     length of the string
            chars (str):    characters used as basis for the random string

        Returns:
            str:            random string with lenght ``size``
        """
        return ''.join(random.choice(chars) for x in range(size))
