from collections import OrderedDict
from contextlib import contextmanager
from pathlib import PurePosixPath
from threading import RLock

from paramiko import (
    SSHClient, AutoAddPolicy, SSHException, AuthenticationException, SFTPClient)

from .deployer_error import DeployerError
from .linux import Linux


class PublicKey:
    def __init__(self, key_type, key_str, comment=None):
        self.key_type = key_type
        self.key_str = key_str
        self.comment = comment

    def get_as_line(self):
        if not self.comment:
            return f"{self.key_type} {self.key_str}"
        return f"{self.key_type} {self.key_str} {self.comment}"


class AuthorizedKeys:
    def __init__(self, authorized_keys):
        self.authorized_keys = authorized_keys
        self.keys = OrderedDict()
        self._parse()

    @property
    def count(self):
        return len(self.keys)

    def _parse(self):
        for l in self.authorized_keys.splitlines():
            self.add_or_update_full(l)

    def add_or_update_rsa(self, key_str, comment=None):
        if ("ssh-rsa", key_str) in self.keys:
            self.keys[("ssh-rsa", key_str)].comment = comment
        else:
            self.keys[("ssh-rsa", key_str)] = PublicKey(
                "ssh-rsa", key_str, comment)

    def add_or_update(self, key_type, key_str, comment=None):
        if (key_type, key_str) in self.keys:
            self.keys[(key_type, key_str)].comment = comment
        else:
            self.keys[(key_type, key_str)] = PublicKey(
                key_type, key_str, comment)

    def has_rsa_key(self, key_str: str):
        parts = key_str.split()
        if len(parts) > 1:
            if parts[0] != "ssh-rsa":
                raise DeployerError(
                    f"key is not ssh-rsa, but rather: {parts[0]}")
            key_str = parts[1]
        return ("ssh-rsa", key_str) in self.keys

    def add_or_update_full(self, line: str):
        parts = line.split()
        self.keys[tuple(parts[:2])] = PublicKey(*parts)

    def get_authorized_keys_str(self):
        return "".join(key.get_as_line() + "\n" for key in self.keys.values())


class SSH(Linux):
    ssh_file_lock = RLock()
    ssh_cx_lock = RLock()

    def __init__(self, paramiko_key, paramiko_pub_key, **kwargs):
        super(SSH, self).__init__(**kwargs)
        self.paramiko_key = paramiko_key
        self.paramiko_pub_key = paramiko_pub_key
        self.file_locks = {}

    def sftp_put(self, localpath, remotepath, user="root"):
        self.log(f"sftp put {localpath} {remotepath}...")
        with self.open_sftp(user) as sftp:   # type: SFTPClient
            sftp.put(str(localpath), str(remotepath))
        self.log(f"sftp put {localpath} {remotepath} done")

    def authorize_pub_key_for_root(self):
        auth_keys_file = PurePosixPath("/root/.ssh/authorized_keys")
        with self.get_lock_for_file(auth_keys_file):
            with self.ssh_root_with_password() as ssh:
                self._ssh_run("root", ssh, f'mkdir -p .ssh', check=True)
                self._ssh_run(
                    "root", ssh, f'touch {auth_keys_file}', check=True)
                self._ssh_run("root", ssh, f'chmod 700 .ssh', check=True)
                self._ssh_run(
                    "root", ssh, f'chmod 600 {auth_keys_file}', check=True)
                sftp = ssh.open_sftp()
                with sftp.file(str(auth_keys_file)) as f:
                    keys_str = f.read().decode()
                keys = AuthorizedKeys(keys_str)
                if keys.has_rsa_key(self.paramiko_pub_key):
                    return
                keys.add_or_update_full(self.paramiko_pub_key)
                sftp = ssh.open_sftp()
                with sftp.file(str(auth_keys_file), "w") as f:
                    f.write(keys.get_authorized_keys_str())

    def authorize_pub_key(self, user_obj):
        su = f'su - {user_obj.user} bash -c'
        commands = [
            f'{su} "mkdir -p .ssh"',
            f'{su} "touch .ssh/authorized_keys"',
            f'{su} "chmod 700 .ssh"',
            f'{su} "chmod 600 .ssh/authorized_keys"']
        if self.selinux_is_active():
            commands.append(f'{su} "restorecon -FR .ssh"')
        self.ssh_run_check(commands)
        self.add_authorized_key(user_obj, self.paramiko_pub_key, True)

    def get_lock_for_file(self, f):
        f = str(f)
        with SSH.ssh_file_lock:
            if f not in self.file_locks:
                self.file_locks[f] = RLock()
        return self.file_locks[f]

    def add_fingerprints(self, vms):
        for username in self.users:
            for vm in vms:
                self._connect_to_add_fingerprint(vm, username)

    def authorize_keys(self, vms):
        for username, user_obj in self.users.items():
            keys = [vm.get_pub_key(username) for vm in vms]
            self.log(f"will add pub keys of {username}")
            self.add_authorized_keys(user_obj, keys)
        self.ssh_run("systemctl reload sshd")

    def add_authorized_key(self, user_obj, pub_key, use_root=False):
        self.add_authorized_keys(user_obj, [pub_key], use_root)

    def add_authorized_keys(self, user_obj, pub_keys, use_root=False):
        user = user_obj.user
        auth_keys_file = (
            PurePosixPath(user_obj.home_dir) /
            PurePosixPath(".ssh/authorized_keys"))
        self.log(f"reading {auth_keys_file}")
        with self.open_sftp("root" if use_root else user) as sftp:
            with sftp.file(str(auth_keys_file)) as f:
                keys_str = f.read().decode()
        keys = AuthorizedKeys(keys_str)
        keys_added = False
        for key_to_add in pub_keys:
            key_comment = key_to_add.rpartition(" ")[2]
            if keys.has_rsa_key(key_to_add):
                self.log(
                    f"{key_to_add[:10]}...{key_comment} already authorized")
                continue
            keys_added = True
            self.log(f"authorizing {key_to_add[:10]}...{key_comment}")
            keys.add_or_update_full(key_to_add)
        if not keys_added:
            return
        with self.open_sftp("root" if use_root else user) as sftp:
            with sftp.file(str(auth_keys_file), "w") as f:
                f.write(keys.get_authorized_keys_str())

    def _connect_to_add_fingerprint(self, other, user):
        self.ssh_run_check(
            [f"ssh -o StrictHostKeyChecking=no {other.ip} true",
             f"ssh -o StrictHostKeyChecking=no {other.name} true"], user=user)

    def _create_rsa_key_pair(self, user):
        self.ssh_run_check(
            ["ssh-keygen -q -t rsa -N '' -f .ssh/id_rsa",
             "chmod 700 .ssh",
             "chmod 600 .ssh/id_rsa"], user=user)

    def get_pub_key(self, user):
        user_obj = self.users[user]
        if user_obj.public_ssh_key:
            return user_obj.public_ssh_key
        id_rsa_file = (
            PurePosixPath(user_obj.home_dir) / PurePosixPath(".ssh/id_rsa"))
        id_rsa_pub_file = (
            PurePosixPath(user_obj.home_dir) / PurePosixPath(".ssh/id_rsa.pub"))
        with self.get_lock_for_file(id_rsa_file):
            with self.open_sftp(user_obj.user) as sftp:
                try:
                    sftp.stat(str(id_rsa_file))
                except FileNotFoundError:
                    self._create_rsa_key_pair(user_obj.user)
            # grab the id_rsa.pub
            with self.open_sftp(user_obj.user) as sftp:
                key_bytes = sftp.file(str(id_rsa_pub_file)).read()
        user_obj.public_ssh_key = key_bytes.decode().strip()
        return user_obj.public_ssh_key

    @contextmanager
    def ssh_root_with_password(self):
        with SSHClient() as client:
            client.set_missing_host_key_policy(AutoAddPolicy())
            client.connect(
                self.ip, username="root", password=self.root_password)
            yield client

    @contextmanager
    def open_sftp(self, user="root"):
        with self.open_ssh(user) as ssh:
            yield ssh.open_sftp()

    @contextmanager
    def open_ssh(self, user="root"):
        with SSH.ssh_cx_lock:
            with SSHClient() as client:
                client.set_missing_host_key_policy(AutoAddPolicy())
                try:
                    client.connect(self.ip, username=user, pkey=self.paramiko_key)
                except AuthenticationException as e:
                    raise DeployerError(
                        f"AuthenticationException {user}@{self.ip} ({self.name}):\n"
                        f"{e}")
                except TimeoutError as e:
                    raise DeployerError(
                        f"TimeoutError {user}@{self.ip} ({self.name}):\n"
                        f"{e}")
                yield client

    def _ssh_run(self, user, ssh, command, *,
                 check=False, get_output=True):
        self.log(f"{user}: [{command}]")
        try:
            i, o, e = ssh.exec_command(command)
        except SSHException as e:
            raise DeployerError(
                f"{user}@{self.name}: SSHException for:\n"
                f"{command}\non {self.name}:\n{e}")
        if get_output:
            stdout = o.read().decode()
        if check:
            stderr = e.read().decode()
            exit_status = o.channel.recv_exit_status()
            if exit_status != 0:
                raise DeployerError(
                    f"{user}@{self.name}: exit status {exit_status} for:\n"
                    f"{command}\n"
                    f"stderr: {stderr}")
        if get_output:
            return stdout

    def ssh_run(self, command_or_commands, *,
                user="root", check=False, get_output=False):
        with self.open_ssh(user) as ssh:
            if isinstance(command_or_commands, str):
                return self._ssh_run(user, ssh, command_or_commands,
                                     check=check, get_output=get_output)
            else:
                ret = []
                for command in command_or_commands:
                    ret.append(self._ssh_run(
                        user, ssh, command,
                        check=check, get_output=get_output))
                return ret

    def ssh_run_check(self, command_or_commands, *,
                      user="root", get_output=False):
        return self.ssh_run(command_or_commands,
                            user=user, check=True, get_output=get_output)
