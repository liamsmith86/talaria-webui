"""Select one local Hermes account before inspecting or invoking its runtime."""

import os
import pwd
import stat
from pathlib import Path

from .maintenance import DeploymentError


def require_owner_path(path, uid, *, missing=False, links=0):
    """Accept paths controlled by the target account or root, including symlinks.

    A sticky ancestor such as /tmp cannot replace an existing root-owned child.
    A missing child or a writable final path has no such protection.
    """
    if links > 40:
        raise DeploymentError("Too many symlinks in the Hermes maintenance path.")
    path = Path(path).expanduser().absolute()
    current = Path(path.anchor)
    previous_mode = current.stat().st_mode
    for index, part in enumerate(path.parts[1:], 1):
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if missing and not previous_mode & 0o022:
                return
            raise DeploymentError("Hermes maintenance paths must have protected parents.") from None
        if info.st_uid not in {0, uid}:
            raise DeploymentError(
                "Hermes maintenance paths must be controlled by its owner or root."
            )
        if stat.S_ISLNK(info.st_mode):
            target = current.readlink()
            if not target.is_absolute():
                target = current.parent / target
            require_owner_path(
                target.joinpath(*path.parts[index + 1 :]), uid, missing=missing, links=links + 1
            )
            return
        ancestor = index < len(path.parts) - 1
        sticky = ancestor and stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX
        if info.st_mode & 0o022 and not sticky:
            raise DeploymentError("Hermes maintenance paths must not be writable by other users.")
        previous_mode = info.st_mode


def select_owner(home):
    try:
        uid = home.stat().st_uid
    except FileNotFoundError:
        uid = os.geteuid()
    owner = pwd.getpwuid(uid)
    if os.geteuid() not in {0, owner.pw_uid}:
        raise DeploymentError("Run Hermes maintenance as the Hermes owner or root.")
    if os.geteuid() == 0:
        require_owner_path(home, owner.pw_uid, missing=True)
    return owner


def execution_identity(owner, *, groups=False):
    if os.geteuid() == 0:
        return {
            "user": owner.pw_uid,
            "group": owner.pw_gid,
            "extra_groups": os.getgrouplist(owner.pw_name, owner.pw_gid) if groups else [],
        }
    if os.geteuid() != owner.pw_uid:
        raise DeploymentError("Run Hermes maintenance as the Hermes owner or root.")
    return {}


def validate_execution(home, executable, owner):
    # Execute exactly the path inspected here, without a later PATH lookup or a
    # change in how a relative argument is resolved after entering Hermes home.
    executable = Path(executable).expanduser().absolute()
    if os.geteuid() == 0:
        require_owner_path(home, owner.pw_uid)
        require_owner_path(executable, owner.pw_uid)
    return executable
