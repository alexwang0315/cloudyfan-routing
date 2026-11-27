"""Large files: the sender uploads chunks to the portal's spool, the recipient gets a manifest message.

The manifest travels as an ordinary message of kind TRANSFER, so receipts, redelivery and expiry work
exactly as for any other message. The spool is freed once that message is settled.
"""
import hashlib
import json
import math
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .envelope import Envelope, EnvelopeError, Kind, check_name
from .relay import Conflict, NotFound, State

CHUNK_MIN = 64 * 1024
CHUNK_MAX = 8 * 1024 * 1024
DEFAULT_CHUNK = 1024 * 1024
DEFAULT_SPOOL_LIMIT = 2 * 1024**3
MAX_FILE_NAME_BYTES = 255


class TransferError(ValueError):
    pass


class StorageFull(TransferError):
    pass


class ChunkUnavailable(LookupError):
    pass


def _is_sha256(text):
    return isinstance(text, str) and len(text) == 64 and all(ch in '0123456789abcdef' for ch in text)


@dataclass(frozen=True)
class Manifest:
    transfer_id: bytes
    sender: str
    recipient: str
    created_at: int
    size: int
    sha256: str
    chunk_size: int
    chunks: tuple
    name: str = ''

    def __post_init__(self):
        if not isinstance(self.transfer_id, bytes) or len(self.transfer_id) != 16:
            raise TransferError('transfer_id must be 16 bytes')
        check_name('sender', self.sender)
        check_name('recipient', self.recipient)
        for field in ('created_at', 'size', 'chunk_size'):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TransferError(f'{field} must be a whole number')
        if self.size < 1:
            raise TransferError('size must be at least 1 byte')
        if not CHUNK_MIN <= self.chunk_size <= CHUNK_MAX:
            raise TransferError(f'chunk_size must be {CHUNK_MIN}..{CHUNK_MAX} bytes')
        if len(self.chunks) != math.ceil(self.size / self.chunk_size):
            raise TransferError('chunk count does not match size and chunk_size')
        if not _is_sha256(self.sha256) or not all(_is_sha256(chunk) for chunk in self.chunks):
            raise TransferError('hashes must be lowercase hex sha256')
        if not isinstance(self.name, str) or len(self.name.encode('utf-8')) > MAX_FILE_NAME_BYTES \
                or self.name in ('.', '..') or any(ch in self.name for ch in '/\\\0'):
            raise TransferError(f'file name must be a plain name of at most {MAX_FILE_NAME_BYTES} bytes')

    @property
    def id(self):
        return self.transfer_id.hex()

    @property
    def chunk_count(self):
        return len(self.chunks)

    def chunk_length(self, index):
        if not 0 <= index < self.chunk_count:
            raise TransferError(f'chunk {index} is out of range')
        return min(self.chunk_size, self.size - index * self.chunk_size)

    def check_chunk(self, index, data):
        return len(data) == self.chunk_length(index) and hashlib.sha256(data).hexdigest() == self.chunks[index]

    def to_json(self):
        return {
            'id': self.id, 'sender': self.sender, 'recipient': self.recipient, 'created_at': self.created_at,
            'size': self.size, 'sha256': self.sha256, 'chunk_size': self.chunk_size, 'chunks': list(self.chunks),
            'name': self.name,
        }

    def to_bytes(self):
        return json.dumps(self.to_json(), separators=(',', ':'), sort_keys=True).encode('utf-8')

    @classmethod
    def from_json(cls, data):
        try:
            return cls(bytes.fromhex(data['id']), data['sender'], data['recipient'], data['created_at'],
                       data['size'], data['sha256'], data['chunk_size'], tuple(data['chunks']), data.get('name', ''))
        except (TransferError, EnvelopeError):
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise TransferError(f'malformed manifest: {error}') from error

    @classmethod
    def from_file(cls, path, sender, recipient, *, chunk_size=DEFAULT_CHUNK, clock=time.time):
        whole, chunks = hashlib.sha256(), []
        size = 0
        with open(path, 'rb') as source:
            while piece := source.read(chunk_size):
                whole.update(piece)
                chunks.append(hashlib.sha256(piece).hexdigest())
                size += len(piece)
        return cls(os.urandom(16), sender, recipient, int(clock()), size, whole.hexdigest(), chunk_size,
                   tuple(chunks), Path(path).name)

    def envelope(self):
        return Envelope(self.transfer_id, self.sender, self.recipient, self.created_at, Kind.TRANSFER,
                        self.to_bytes())

    @classmethod
    def from_envelope(cls, envelope):
        if envelope.kind is not Kind.TRANSFER:
            raise TransferError('message is not a file transfer')
        try:
            data = json.loads(envelope.body)
        except ValueError as error:
            raise TransferError('transfer manifest is not JSON') from error
        manifest = cls.from_json(data)
        if manifest.transfer_id != envelope.message_id:
            raise TransferError('manifest does not belong to this message')
        return manifest


_LIVE = ('uploading', 'queued')
_SETTLED = {State.ACKED: 'done', State.EXPIRED: 'expired', State.FAILED: 'failed'}


class Transfers:
    """The portal's spool of chunks, bounded by `limit` bytes across unfinished transfers."""

    def __init__(self, relay, spool_dir, *, limit=DEFAULT_SPOOL_LIMIT):
        self._relay = relay
        self._spool = Path(spool_dir)
        self._spool.mkdir(parents=True, exist_ok=True)
        self.limit = limit
        relay.settle_listeners.append(self._settled)

    def create(self, manifest, link):
        """Register a transfer and reserve spool space. Registering it again reports what is missing."""
        full = None
        with self._relay.transaction() as db:
            self._expire_stale(db)
            row = db.execute('SELECT * FROM transfers WHERE transfer_id = ?', (manifest.transfer_id,)).fetchone()
            if row is not None and bytes(row['manifest']) != manifest.to_bytes():
                raise Conflict(f'transfer {manifest.id} already exists with a different manifest')
            if row is None or row['state'] == 'rejected':
                used = db.execute(
                    f'SELECT COALESCE(SUM(size), 0) FROM transfers WHERE state IN ({",".join("?" * len(_LIVE))})',
                    _LIVE).fetchone()[0]
                if used + manifest.size > self.limit:
                    full = (f'needs {manifest.size} bytes of spool, {max(0, self.limit - used)} of '
                            f'{self.limit} free')
                    state = 'rejected'
                else:
                    state = 'uploading'
                db.execute(
                    'INSERT INTO transfers (transfer_id, manifest, size, chunk_count, state, reason, started_at)'
                    ' VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (transfer_id) DO UPDATE SET'
                    ' state = excluded.state, reason = excluded.reason, started_at = excluded.started_at',
                    (manifest.transfer_id, manifest.to_bytes(), manifest.size, manifest.chunk_count, state, full,
                     self._relay.now))
                if state == 'uploading':
                    db.executemany(
                        'INSERT OR IGNORE INTO transfer_chunks (transfer_id, idx) VALUES (?, ?)',
                        [(manifest.transfer_id, index) for index in range(manifest.chunk_count)])
        if full:
            raise StorageFull(full)
        self._ensure_announced(manifest, link)
        return self.record(manifest.transfer_id)

    def put_chunk(self, transfer_id, index, data, link):
        manifest, state = self._manifest(transfer_id)
        if state != 'uploading':
            if state == 'queued':
                with self._relay.transaction() as db:
                    db.execute('UPDATE transfers SET uploads = uploads + 1 WHERE transfer_id = ?', (transfer_id,))
                return self.record(transfer_id)
            raise TransferError(f'transfer {transfer_id.hex()} is {state}, not accepting chunks')
        if not manifest.check_chunk(index, data):
            with self._relay.transaction() as db:
                db.execute('UPDATE transfers SET upload_rejects = upload_rejects + 1 WHERE transfer_id = ?',
                           (transfer_id,))
            raise TransferError(f'chunk {index} does not match its size or sha256')
        folder = self._spool / transfer_id.hex()
        folder.mkdir(exist_ok=True)
        staging = folder / f'{index}.part'
        staging.write_bytes(data)
        staging.replace(folder / str(index))
        complete = False
        with self._relay.transaction() as db:
            db.execute('UPDATE transfer_chunks SET present = 1 WHERE transfer_id = ? AND idx = ?',
                       (transfer_id, index))
            db.execute('UPDATE transfers SET uploads = uploads + 1 WHERE transfer_id = ?', (transfer_id,))
            missing = db.execute('SELECT COUNT(*) FROM transfer_chunks WHERE transfer_id = ? AND present = 0',
                                 (transfer_id,)).fetchone()[0]
            if missing == 0:
                if self._whole_matches(manifest):
                    complete = db.execute(
                        "UPDATE transfers SET state = 'queued', uploaded_at = ? WHERE transfer_id = ?"
                        " AND state = 'uploading'", (self._relay.now, transfer_id)).rowcount == 1
                else:
                    db.execute("UPDATE transfers SET state = 'failed', reason = ?, finished_at = ?"
                               " WHERE transfer_id = ?", ('whole file does not match its sha256', self._relay.now,
                                                          transfer_id))
                    shutil.rmtree(folder, ignore_errors=True)
        if complete:
            self._relay.submit(manifest.envelope(), link)
        return self.record(transfer_id)

    def get_chunk(self, transfer_id, index):
        manifest, state = self._manifest(transfer_id)
        if state != 'queued':
            raise ChunkUnavailable(f'transfer {transfer_id.hex()} is {state}')
        manifest.chunk_length(index)
        try:
            data = (self._spool / transfer_id.hex() / str(index)).read_bytes()
        except FileNotFoundError:
            data = None
        with self._relay.transaction() as db:
            if data is None or not manifest.check_chunk(index, data):
                db.execute('UPDATE transfer_chunks SET present = 0 WHERE transfer_id = ? AND idx = ?',
                           (transfer_id, index))
                db.execute("UPDATE transfers SET state = 'uploading' WHERE transfer_id = ?", (transfer_id,))
                damaged = True
            else:
                db.execute('UPDATE transfers SET downloads = downloads + 1 WHERE transfer_id = ?', (transfer_id,))
                damaged = False
        if damaged:
            raise ChunkUnavailable(f'chunk {index} is damaged on the portal and must be uploaded again')
        return data

    def record(self, transfer_id):
        with self._relay.transaction() as db:
            self._expire_stale(db)
            row = db.execute('SELECT * FROM transfers WHERE transfer_id = ?', (transfer_id,)).fetchone()
            if row is None:
                raise NotFound(f'no transfer {transfer_id.hex()}')
            missing = [item[0] for item in db.execute(
                'SELECT idx FROM transfer_chunks WHERE transfer_id = ? AND present = 0 ORDER BY idx',
                (transfer_id,))]
        return _record(row, missing)

    def list(self, limit=100):
        with self._relay.transaction() as db:
            rows = db.execute('SELECT * FROM transfers ORDER BY started_at DESC LIMIT ?', (limit,)).fetchall()
        return [_record(row, None) for row in reversed(rows)]

    def _manifest(self, transfer_id):
        with self._relay.transaction() as db:
            self._expire_stale(db)
            row = db.execute('SELECT manifest, state FROM transfers WHERE transfer_id = ?',
                             (transfer_id,)).fetchone()
        if row is None:
            raise NotFound(f'no transfer {transfer_id.hex()}')
        return Manifest.from_json(json.loads(row['manifest'])), row['state']

    def _ensure_announced(self, manifest, link):
        """Recover from a crash between finishing the upload and queuing the manifest message."""
        with self._relay.transaction() as db:
            state = db.execute('SELECT state FROM transfers WHERE transfer_id = ?',
                               (manifest.transfer_id,)).fetchone()['state']
        if state == 'queued' and not self._relay.exists(manifest.transfer_id):
            self._relay.submit(manifest.envelope(), link)

    def _whole_matches(self, manifest):
        whole = hashlib.sha256()
        folder = self._spool / manifest.id
        for index in range(manifest.chunk_count):
            whole.update((folder / str(index)).read_bytes())
        return whole.hexdigest() == manifest.sha256

    def _expire_stale(self, db):
        stale = db.execute("SELECT transfer_id FROM transfers WHERE state = 'uploading' AND started_at + ? <= ?",
                           (self._relay.policy.ttl, self._relay.now)).fetchall()
        for row in stale:
            db.execute("UPDATE transfers SET state = 'expired', reason = ?, finished_at = ? WHERE transfer_id = ?",
                       ('upload did not finish in time', self._relay.now, row['transfer_id']))
            shutil.rmtree(self._spool / bytes(row['transfer_id']).hex(), ignore_errors=True)

    def _settled(self, message_id, state):
        with self._relay.transaction() as db:
            changed = db.execute(
                f'UPDATE transfers SET state = ?, finished_at = ? WHERE transfer_id = ?'
                f' AND state IN ({",".join("?" * len(_LIVE))})',
                (_SETTLED[state], self._relay.now, message_id, *_LIVE)).rowcount
        if changed:
            shutil.rmtree(self._spool / message_id.hex(), ignore_errors=True)


def _record(row, missing):
    record = {
        'id': bytes(row['transfer_id']).hex(), 'state': row['state'], 'reason': row['reason'],
        'size': row['size'], 'chunks': row['chunk_count'], 'uploads': row['uploads'],
        'upload_rejects': row['upload_rejects'], 'downloads': row['downloads'],
        'retransmissions': max(0, row['uploads'] - row['chunk_count']) + row['upload_rejects']
        + max(0, row['downloads'] - row['chunk_count']),
        'started_at': row['started_at'], 'uploaded_at': row['uploaded_at'], 'finished_at': row['finished_at'],
        'upload_seconds': None if row['uploaded_at'] is None else row['uploaded_at'] - row['started_at'],
    }
    if missing is not None:
        record['missing'] = missing
    return record
