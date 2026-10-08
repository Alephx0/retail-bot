"""Prevent two engine processes from purchasing from the same local workspace."""
import os
from pathlib import Path


class WorkspaceLock:
    def __init__(self, folder):
        Path(folder).mkdir(parents=True,exist_ok=True)
        self.file=open(Path(folder)/'engine.lock','a+b')
        try:
            self.file.seek(0)
            if self.file.read(1)==b'':
                self.file.write(b'0'); self.file.flush()
            self.file.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('Another Retail Desk engine owns this data directory. Use a separate --data-dir.') from None

    def close(self):
        if self.file.closed:
            return
        self.file.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            fcntl.flock(self.file,fcntl.LOCK_UN)
        self.file.close()
