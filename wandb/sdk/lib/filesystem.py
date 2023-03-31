import errno
import logging
import os
import platform
import re
import shutil
import stat
import threading
from pathlib import Path
from typing import BinaryIO, Optional, Union

import psutil

StrPath = Union[str, "os.PathLike[str]"]

logger = logging.getLogger(__name__)

WRITE_PERMISSIONS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH | stat.S_IWRITE


def mkdir_exists_ok(dir_name: StrPath) -> None:
    """Create `dir_name` and any parent directories if they don't exist.

    Raises:
        FileExistsError: if `dir_name` exists and is not a directory.
        PermissionError: if `dir_name` is not writable.
    """
    try:
        os.makedirs(dir_name, exist_ok=True)
    except FileExistsError as e:
        raise FileExistsError(f"{dir_name!s} exists and is not a directory") from e
    except PermissionError as e:
        raise PermissionError(f"{dir_name!s} is not writable") from e


def check_available_space(
    path: StrPath,
    size: Optional[int] = None,
    reserve: int = 0,
) -> int:
    """Check that `path` has at least `size` + `reserve` available.

    Args:
        path: The path whose file system will be checked. It can be a file intended to
            be written to, in which case `size` is optional.
        reserve: The number of bytes to reserve on `path` in addition to the bytes
            requested. [default: 0]
        size: The number of bytes that will be written to `path`. If None, then
            `path` must be a real file and its size will be used. [default: None]

    Returns:
        The number of bytes available, minus `reserve` and `size`.

    Raises:
        OSError [28]: if `path` has less than `size` available.
        ValueError: if `size` is not None and `path` is not a real file.
    """
    path = Path(path).resolve()
    if size is None:
        if not path.is_file():
            raise ValueError(
                "If `size` is not specified then `path` must be a real file."
            )
        size = os.stat(path).st_size

    # The path might be theoretical, so find the nearest parent that actually exists.
    while not path.exists():
        path = path.parent
    usage = psutil.disk_usage(str(path))
    remaining_bytes = usage.free - size

    if remaining_bytes < reserve:
        raise OSError(
            errno.ENOSPC,  # No space left on device
            f"{path!s} has only {usage.free} bytes available, but {size} bytes "
            f" are required, which would leave only {remaining_bytes} bytes available, "
            f"which is less than the requested {reserve} bytes. You can set "
            "WANDB_MINIMUM_FREE_SPACE to a lower value, use a disk with more space, or "
            "delete some files to allow this copy to proceed.",
        )
    if remaining_bytes - reserve < size:
        # One more write of this size would exceed the available space.
        logger.warning(
            f"Running low on disk space. Only {remaining_bytes - reserve} bytes "
            f"bytes available for use. (reserving {reserve} to avoid system errors)"
        )
    return remaining_bytes


class WriteSerializingFile:
    """Wrapper for a file object that serializes writes."""

    def __init__(self, f: BinaryIO) -> None:
        self.lock = threading.Lock()
        self.f = f

    def write(self, *args, **kargs) -> None:  # type: ignore
        self.lock.acquire()
        try:
            self.f.write(*args, **kargs)
            self.f.flush()
        finally:
            self.lock.release()

    def close(self) -> None:
        self.lock.acquire()  # wait for pending writes
        try:
            self.f.close()
        finally:
            self.lock.release()


class CRDedupedFile(WriteSerializingFile):
    def __init__(self, f: BinaryIO) -> None:
        super().__init__(f=f)
        self._buff = b""

    def write(self, data) -> None:  # type: ignore
        lines = re.split(b"\r\n|\n", data)
        ret = []  # type: ignore
        for line in lines:
            if line[:1] == b"\r":
                if ret:
                    ret.pop()
                elif self._buff:
                    self._buff = b""
            line = line.split(b"\r")[-1]
            if line:
                ret.append(line)
        if self._buff:
            ret.insert(0, self._buff)
        if ret:
            self._buff = ret.pop()
        super().write(b"\n".join(ret) + b"\n")

    def close(self) -> None:
        if self._buff:
            super().write(self._buff)
        super().close()


def copy_or_overwrite_changed(source_path: StrPath, target_path: StrPath) -> StrPath:
    """Copy source_path to target_path, unless it already exists with the same mtime.

    We liberally add write permissions to deal with the case of multiple users needing
    to share the same cache or run directory.

    Args:
        source_path: The path to the file to copy.
        target_path: The path to copy the file to.

    Returns:
        The path to the copied file (which may be different from target_path).
    """
    return_type = type(target_path)

    if platform.system() == "Windows":
        head, tail = os.path.splitdrive(str(target_path))
        if ":" in tail:
            logger.warning("Replacing ':' in %s with '-'", tail)
            target_path = os.path.join(head, tail.replace(":", "-"))

    need_copy = (
        not os.path.isfile(target_path)
        or os.stat(source_path).st_mtime != os.stat(target_path).st_mtime
    )

    permissions_plus_write = os.stat(source_path).st_mode | WRITE_PERMISSIONS
    if need_copy:
        mkdir_exists_ok(os.path.dirname(target_path))
        try:
            # Use copy2 to preserve file metadata (including modified time).
            shutil.copy2(source_path, target_path)
        except PermissionError:
            # If the file is read-only try to make it writable.
            try:
                os.chmod(target_path, permissions_plus_write)
                shutil.copy2(source_path, target_path)
            except PermissionError as e:
                raise PermissionError("Unable to overwrite '{target_path!s}'") from e
        # Prevent future permissions issues by universal write permissions now.
        os.chmod(target_path, permissions_plus_write)

    return return_type(target_path)  # type: ignore  # 'os.PathLike' is abstract.
