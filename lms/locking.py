from contextlib import contextmanager
import os
from pathlib import Path

from .sync import private_dir


@contextmanager
def output_lock(root: Path):
    private_dir(root)
    descriptor = os.open(root / ".sync.lock", os.O_RDWR | os.O_CREAT, 0o600)
    stream = os.fdopen(descriptor, "r+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            if stream.seek(0, 2) == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ValueError("В этом каталоге уже выполняется обновление") from exc
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("В этом каталоге уже выполняется обновление") from exc
        locked = True
        yield
    finally:
        if locked and os.name == "nt":
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        stream.close()
