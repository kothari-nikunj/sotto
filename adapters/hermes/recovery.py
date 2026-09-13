"""Offline tenant export and verified cold restore; restored instances start on hold.

Archives contain private state and credentials and are created mode 0600. Keep them
in protected backup storage, never the source repository. No network or sends occur.
All runtime writers must be stopped; the runtime lock enforces this for shipped instances.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import tarfile
import tempfile
import time

LOCK = '.sotto-runtime.lock'
HOLD = '.sotto-recovery-hold.json'
MANIFEST = 'sotto-backup-manifest.json'
CHUNK = 1024 * 1024
MAX_FILES = 100000
MAX_BYTES = 32 * 1024 ** 3
MAX_MANIFEST_BYTES = 32 * 1024 ** 2


def digest_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b''):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def offline(data):
    path = Path(data) / LOCK
    if not path.is_file() or path.is_symlink():
        raise RuntimeError('Runtime lock is missing; initialize the shipped runtime before exporting')
    with path.open('r+b') as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('Stop the tenant runtime before recovery operations') from None
        yield


def _flush(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _tenant(data, tenant):
    if not tenant:
        raise ValueError('Expected tenant identity is required')
    marker = Path(data) / '.sotto-volume.json'
    if marker.exists() and json.loads(marker.read_text()).get('tenant_id') != tenant:
        raise ValueError('Volume belongs to another tenant')
    state = Path(data) / 'config/managed-capabilities.json'
    if state.exists() and json.loads(state.read_text()).get('tenant_id') not in (None, tenant):
        raise ValueError('Source state belongs to another tenant')


def export(data, archive, tenant):
    data, archive = Path(data).resolve(), Path(archive).absolute()
    if data == archive or data in archive.parents or archive.exists():
        raise ValueError('Use a new archive path outside the tenant volume')
    archive.parent.mkdir(parents=True, exist_ok=True)
    with offline(data):
        _tenant(data, tenant)
        records, signatures = [], {}
        total = 0
        for path in sorted(data.rglob('*')):
            relative = path.relative_to(data).as_posix()
            if relative in (LOCK, HOLD):
                continue
            if relative == MANIFEST:
                raise ValueError('Reserved backup manifest name exists in tenant state')
            if len(records) >= MAX_FILES:
                raise ValueError('Backup exceeds the bounded tenant export size')
            if path.is_symlink():
                resolved = path.resolve(strict=True)
                if data not in resolved.parents or not resolved.is_file():
                    raise ValueError('Export refuses external or directory symlinks')
                link = os.path.relpath(resolved, path.parent)
                signatures[relative] = ('symlink', os.readlink(path))
                records.append({'path': relative, 'kind': 'symlink', 'link': link, 'size': 0,
                                'sha256': hashlib.sha256(link.encode()).hexdigest()})
                continue
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError('Export supports regular state files only')
            stat = path.stat()
            total += stat.st_size
            if len(records) >= MAX_FILES or total > MAX_BYTES:
                raise ValueError('Backup exceeds the bounded tenant export size')
            signatures[relative] = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
            records.append({'path': relative, 'size': stat.st_size, 'sha256': digest_file(path),
                            'mode': 0o700 if stat.st_mode & 0o100 else 0o600})
        manifest = {'schema': 1, 'tenant_id': tenant, 'created_at': time.time(), 'files': records}
        encoded = json.dumps(manifest, sort_keys=True).encode()
        fd, temporary = tempfile.mkstemp(prefix='.sotto-backup-', dir=archive.parent)
        try:
            with os.fdopen(fd, 'w+b') as output:
                with tarfile.open(fileobj=output, mode='w:gz') as tar:
                    for record in records:
                        if record.get('kind') == 'symlink':
                            info = tarfile.TarInfo(record['path'])
                            info.type, info.linkname = tarfile.SYMTYPE, record['link']
                            tar.addfile(info)
                            continue
                        info = tar.gettarinfo(str(data / record['path']), arcname=record['path'])
                        info.uid = info.gid = 0
                        info.uname = info.gname = ''
                        info.mode = record['mode']
                        with (data / record['path']).open('rb') as stream:
                            tar.addfile(info, stream)
                    info = tarfile.TarInfo(MANIFEST)
                    info.size, info.mode = len(encoded), 0o600
                    tar.addfile(info, io.BytesIO(encoded))
                output.flush()
                os.fsync(output.fileno())
            # Detect accidental writers not participating in the runtime lock.
            for relative, signature in signatures.items():
                if signature[0] == 'symlink':
                    if not (data / relative).is_symlink() or os.readlink(data / relative) != signature[1]:
                        raise RuntimeError('Tenant state changed during export')
                    continue
                stat = (data / relative).stat()
                if (stat.st_size, stat.st_mtime_ns, stat.st_ino) != signature:
                    raise RuntimeError('Tenant state changed during export; stop every writer and retry')
            current = {p.relative_to(data).as_posix() for p in data.rglob('*')
                       if (p.is_file() or p.is_symlink()) and p.relative_to(data).as_posix() not in (LOCK, HOLD)}
            if current != set(signatures):
                raise RuntimeError('Tenant state changed during export; stop every writer and retry')
            os.link(temporary, archive)  # Publish atomically without replacing an existing backup.
            _flush(archive.parent)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return {'tenant_id': tenant, 'files': len(records), 'bytes': total,
            'manifest_sha256': hashlib.sha256(encoded).hexdigest()}


def _safe_name(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or '..' in path.parts or str(path) != name:
        raise ValueError('Unsafe archive path')
    return name


@contextmanager
def restore_destination(target):
    """An empty mounted directory is a valid target; publish only verified state under lock."""
    target = Path(target)
    created = not target.exists()
    if target.is_symlink():
        raise ValueError('Restore refuses a symlink target')
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(path.name != LOCK for path in target.iterdir()):
        raise ValueError('Restore requires an empty target volume or new directory')
    fd = os.open(target / LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('Stop the target runtime before restoring') from None
        with tempfile.TemporaryDirectory(prefix='.sotto-restore-', dir=target) as temporary:
            staged = Path(temporary)
            yield staged
            # The hold exists before the first restored file becomes visible. Interrupted
            # promotion leaves a nonempty held target and cannot be mistaken for a fresh runtime.
            os.link(staged / HOLD, target / HOLD)
            for path in staged.iterdir():
                if path.name not in (HOLD, LOCK):
                    os.rename(path, target / path.name)
            _flush(target)
    except Exception:
        if created and {path.name for path in target.iterdir()} == {LOCK}:
            (target / LOCK).unlink()
            target.rmdir()
        raise
    finally:
        os.close(fd)


def restore(archive, target, tenant, *, volume_id=None):
    target = Path(target).absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    with restore_destination(target) as staged:
        with tarfile.open(archive, 'r:gz') as tar:
            members = tar.getmembers()
            if len(members) > MAX_FILES + 1 or sum(m.size for m in members) > MAX_BYTES:
                raise ValueError('Archive exceeds the bounded tenant restore size')
            names = [_safe_name(member.name) for member in members]
            if len(set(names)) != len(names) or any(not (m.isfile() or m.issym()) for m in members):
                raise ValueError('Archive contains duplicate paths or nonregular files')
            if MANIFEST not in names:
                raise ValueError('Backup manifest missing')
            if not tar.getmember(MANIFEST).isfile() or tar.getmember(MANIFEST).size > MAX_MANIFEST_BYTES:
                raise ValueError('Backup manifest is too large')
            raw = tar.extractfile(MANIFEST).read()
            manifest = json.loads(raw)
            if manifest.get('schema') != 1 or manifest.get('tenant_id') != tenant:
                raise ValueError('Backup tenant or schema does not match')
            records = manifest.get('files', [])
            expected = {record['path']: record for record in records}
            if len(expected) != len(records) or set(expected) != set(names) - {MANIFEST}:
                raise ValueError('Backup manifest does not cover every file exactly once')
            links = []
            link_names = {member.name for member in members if member.issym()}
            if any(str(parent) in link_names for name in names for parent in PurePosixPath(name).parents):
                raise ValueError('Archive files may not be nested under symlinks')
            for member in members:
                if member.name == MANIFEST:
                    continue
                record = expected[member.name]
                if member.issym():
                    destination = os.path.normpath(str(PurePosixPath(member.name).parent / member.linkname))
                    _safe_name(destination)
                    if (record.get('kind') != 'symlink' or record.get('link') != member.linkname
                            or record.get('sha256') != hashlib.sha256(member.linkname.encode()).hexdigest()
                            or destination not in expected or expected[destination].get('kind') == 'symlink'):
                        raise ValueError('Backup symlink does not reference a canonical archived file')
                    links.append(member)
                    continue
                if member.size != record['size']:
                    raise ValueError('Backup file size mismatch')
                if record.get('kind') == 'symlink' or record.get('mode', 0o600) not in (0o600, 0o700):
                    raise ValueError('Backup file type or permissions are invalid')
                destination = staged / member.name
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                digest = hashlib.sha256()
                with tar.extractfile(member) as source, destination.open('xb') as output:
                    os.chmod(destination, record.get('mode', 0o600))
                    for chunk in iter(lambda: source.read(CHUNK), b''):
                        output.write(chunk)
                        digest.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if digest.hexdigest() != record['sha256']:
                    raise ValueError('Backup checksum mismatch')
            for member in links:
                destination = staged / member.name
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                destination.symlink_to(member.linkname)
            _tenant(staged, tenant)
            # SQLite state can include a journal/WAL. Check the restored set together.
            for record in records:
                path = staged / record['path']
                if path.is_symlink():
                    continue
                with path.open('rb') as stream:
                    sqlite = stream.read(16) == b'SQLite format 3\x00'
                if sqlite:
                    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
                        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                            raise ValueError('Restored SQLite integrity check failed')
        manifest_hash = hashlib.sha256(raw).hexdigest()
        if volume_id:
            (staged / '.sotto-volume.json').write_text(json.dumps({
                'schema': 1, 'tenant_id': tenant, 'volume_id': volume_id}))
            os.chmod(staged / '.sotto-volume.json', 0o600)
        hold = {'schema': 1, 'tenant_id': tenant, 'manifest_sha256': manifest_hash,
                'restored_at': time.time(), 'outbound_delivery': 'disabled_until_explicit_resume'}
        (staged / HOLD).write_text(json.dumps(hold))
        (staged / LOCK).touch(mode=0o600)
        os.chmod(staged / HOLD, 0o600)
        _flush(staged)
    return {'tenant_id': tenant, 'files': len(records), 'manifest_sha256': manifest_hash, 'on_hold': True}


def resume(data, tenant, manifest_sha256):
    data = Path(data)
    with offline(data):
        _tenant(data, tenant)
        hold = json.loads((data / HOLD).read_text())
        if hold.get('tenant_id') != tenant or hold.get('manifest_sha256') != manifest_sha256:
            raise ValueError('Recovery receipt does not match the reviewed restore')
        (data / HOLD).unlink()
        _flush(data)
    return {'tenant_id': tenant, 'on_hold': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('export', 'restore', 'resume'))
    parser.add_argument('--data', required=True)
    parser.add_argument('--tenant', required=True)
    parser.add_argument('--archive')
    parser.add_argument('--volume')
    parser.add_argument('--manifest-sha256')
    args = parser.parse_args()
    if args.operation in ('export', 'restore') and not args.archive:
        parser.error('--archive is required')
    if args.operation == 'resume' and not args.manifest_sha256:
        parser.error('--manifest-sha256 is required after reviewing pending deliveries')
    operation = {'export': lambda: export(args.data, args.archive, args.tenant),
                 'restore': lambda: restore(args.archive, args.data, args.tenant, volume_id=args.volume),
                 'resume': lambda: resume(args.data, args.tenant, args.manifest_sha256)}[args.operation]
    print(json.dumps(operation()))


if __name__ == '__main__':
    main()
