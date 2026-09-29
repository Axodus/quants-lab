"""Durable canonical JSON evidence and process exclusion, outside Optuna."""
import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .models import OptimizationError, canonical, digest


def file_hash(path):
    hasher = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name('.' + path.name + '.' + uuid4().hex)
    try:
        with temp.open('x', encoding='utf-8') as stream:
            json.dump(canonical(data), stream, sort_keys=True, separators=(',', ':'), allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path):
    return json.loads(Path(path).read_text())


@contextmanager
def exclusive(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise OptimizationError('study already owned by another process') from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class EvidenceStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        if 'runs' in self.root.parts or '064e34bd-55c2-4be9-a8c4-212827eb5c9b' in str(self.root):
            raise OptimizationError('live experiment storage prohibited')
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, data):
        identity = digest(data)
        path = self.root / 'artifacts' / (identity + '.json')
        if path.exists():
            self.get(identity)
        else:
            atomic_json(path, data)
        return identity

    def get(self, identity):
        if len(identity) != 64 or any(c not in '0123456789abcdef' for c in identity):
            raise OptimizationError('invalid artifact identity')
        data = read_json(self.root / 'artifacts' / (identity + '.json'))
        if digest(data) != identity:
            raise OptimizationError('canonical evidence checksum mismatch')
        return data
