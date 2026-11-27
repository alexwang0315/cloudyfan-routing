"""The relay service. It knows nothing about links: every link adapter drives it through this API."""
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum

from .envelope import Envelope, Kind

SCHEMA_VERSION = 3
SYSTEM_LINK = 'relay'


class State(Enum):
    QUEUED = 'queued'
    DELIVERED = 'delivered'
    ACKED = 'acked'
    EXPIRED = 'expired'
    FAILED = 'failed'


# DELIVERED -> DELIVERED is a redelivery after the recipient failed to acknowledge in time.
TRANSITIONS = {
    State.QUEUED: {State.DELIVERED, State.EXPIRED, State.FAILED},
    State.DELIVERED: {State.DELIVERED, State.ACKED, State.EXPIRED, State.FAILED},
    State.ACKED: set(),
    State.EXPIRED: set(),
    State.FAILED: set(),
}


@dataclass(frozen=True)
class DeliveryPolicy:
    ack_timeout: float = 60.0
    max_attempts: int = 5
    ttl: float = 7 * 24 * 3600.0

    def __post_init__(self):
        if self.ack_timeout <= 0 or self.ttl <= 0 or self.max_attempts < 1:
            raise ValueError('ack_timeout and ttl must be positive and max_attempts at least 1')


DEFAULT_POLICY = DeliveryPolicy()


class NotFound(LookupError):
    pass


class Conflict(ValueError):
    pass


TERMINAL = frozenset({State.ACKED, State.EXPIRED, State.FAILED})

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id BLOB NOT NULL UNIQUE,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    kind INTEGER NOT NULL,
    body BLOB NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    state TEXT NOT NULL,
    queued_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL
);
CREATE INDEX IF NOT EXISTS messages_inbox ON messages (recipient, state, seq);
CREATE TABLE IF NOT EXISTS transmissions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id BLOB NOT NULL,
    at REAL NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    link TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transfers (
    transfer_id BLOB PRIMARY KEY,
    manifest BLOB NOT NULL,
    size INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    state TEXT NOT NULL,
    reason TEXT,
    started_at REAL NOT NULL,
    uploaded_at REAL,
    finished_at REAL,
    uploads INTEGER NOT NULL DEFAULT 0,
    upload_rejects INTEGER NOT NULL DEFAULT 0,
    downloads INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS transfer_chunks (
    transfer_id BLOB NOT NULL,
    idx INTEGER NOT NULL,
    present INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (transfer_id, idx)
);
'''

_LIVE = (State.QUEUED.value, State.DELIVERED.value)


class Relay:
    def __init__(self, path, *, clock=time.time, policy=DEFAULT_POLICY):
        self._clock = clock
        self.policy = policy
        self._changed = threading.Condition()
        self._settled = []
        self.settle_listeners = []
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute('PRAGMA journal_mode=WAL')
        version = self._db.execute('PRAGMA user_version').fetchone()[0]
        has_tables = self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'messages'").fetchone()
        if has_tables and version != SCHEMA_VERSION:
            self._db.close()
            raise RuntimeError(f'{path} has schema version {version}, expected {SCHEMA_VERSION}')
        self._db.executescript(_SCHEMA)
        self._db.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')

    @property
    def now(self):
        return self._clock()

    def close(self):
        with self._changed:
            self._db.close()

    @contextmanager
    def transaction(self):
        """Run one write transaction under the relay's lock.

        Listeners in `settle_listeners` hear about every message that reached a final state, after
        the transaction commits.
        """
        with self._changed:
            self._db.execute('BEGIN IMMEDIATE')
            try:
                yield self._db
            except BaseException:
                self._db.execute('ROLLBACK')
                self._settled.clear()
                raise
            self._db.execute('COMMIT')
            settled, self._settled = self._settled, []
            self._changed.notify_all()
            for message_id, state in settled:
                for listener in self.settle_listeners:
                    listener(message_id, state)

    def submit(self, envelope, link):
        """Queue a message. Submitting the same message again returns its current state."""
        with self.transaction():
            self._sweep()
            row = self._row(envelope.message_id)
            if row is not None:
                if _envelope(row) != envelope:
                    raise Conflict(f'message {envelope.id} already exists with different content')
                return State(row['state'])
            self._db.execute(
                'INSERT INTO messages (message_id, sender, recipient, created_at, kind, body, size, sha256,'
                ' state, queued_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (envelope.message_id, envelope.sender, envelope.recipient, envelope.created_at,
                 envelope.kind.value, envelope.body, envelope.size, envelope.sha256, State.QUEUED.value,
                 self._clock()))
            self._log(envelope.message_id, None, State.QUEUED, link)
            return State.QUEUED

    def claim(self, recipient, link, *, limit=10, wait=0.0):
        """Hand the recipient its due messages, oldest first, and mark them delivered.

        A message is due when it is queued, or when it was delivered but not acknowledged within the
        policy's ack timeout. Each hand-out counts as one attempt.
        """
        deadline = time.monotonic() + wait
        while True:
            with self.transaction():
                self._sweep()
                now = self._clock()
                rows = self._db.execute(
                    'SELECT * FROM messages WHERE recipient = ? AND (state = ? OR (state = ?'
                    ' AND next_attempt_at <= ? AND attempts < ?)) ORDER BY seq LIMIT ?',
                    (recipient, State.QUEUED.value, State.DELIVERED.value, now, self.policy.max_attempts,
                     limit)).fetchall()
                for row in rows:
                    self._move(row, State.DELIVERED, link)
                    self._db.execute(
                        'UPDATE messages SET attempts = attempts + 1, next_attempt_at = ? WHERE seq = ?',
                        (now + self.policy.ack_timeout, row['seq']))
            remaining = deadline - time.monotonic()
            if rows or remaining <= 0:
                return [_envelope(row) for row in rows]
            # Redeliveries become due by time rather than by a notify, so wake up periodically.
            with self._changed:
                self._changed.wait(min(remaining, 0.5))

    def ack(self, recipient, message_id, link):
        """Record the recipient's receipt. Acknowledging twice is harmless."""
        with self.transaction():
            row = self._row(message_id)
            if row is None or row['recipient'] != recipient:
                raise NotFound(f'no message {message_id.hex()} for {recipient}')
            if row['state'] != State.ACKED.value:
                self._move(row, State.ACKED, link)
            return State.ACKED

    def sweep(self):
        """Expire messages past their time to live and fail those out of delivery attempts."""
        with self.transaction():
            return self._sweep()

    def exists(self, message_id):
        with self._changed:
            return self._row(message_id) is not None

    def status(self, message_id, *, wait_for=None, wait=0.0):
        """Return a message's delivery record, optionally waiting until it reaches `wait_for`."""
        deadline = time.monotonic() + wait
        while True:
            with self.transaction():
                self._sweep()
                row = self._row(message_id)
                if row is None:
                    raise NotFound(f'no message {message_id.hex()}')
                record = self._record(row)
            state = State(record['state'])
            remaining = deadline - time.monotonic()
            if wait_for is None or state is wait_for or state in TERMINAL or remaining <= 0:
                return record
            with self._changed:
                self._changed.wait(min(remaining, 0.5))

    def transmissions(self, limit=100):
        """The debugging log: who sent what to whom, when, how big, and each state change."""
        with self._changed:
            rows = self._db.execute(
                'SELECT t.seq, t.at, t.from_state, t.to_state, t.link, m.message_id, m.sender, m.recipient,'
                ' m.size, m.kind FROM transmissions t JOIN messages m USING (message_id)'
                ' ORDER BY t.seq DESC LIMIT ?', (limit,)).fetchall()
        return [{
            'seq': row['seq'], 'at': row['at'], 'id': row['message_id'].hex(), 'sender': row['sender'],
            'recipient': row['recipient'], 'size': row['size'], 'kind': Kind(row['kind']).name.lower(),
            'from': row['from_state'], 'to': row['to_state'], 'link': row['link'],
        } for row in reversed(rows)]

    def _sweep(self):
        now = self._clock()
        expired = self._db.execute(
            f'SELECT * FROM messages WHERE state IN ({",".join("?" * len(_LIVE))}) AND queued_at + ? <= ?',
            (*_LIVE, self.policy.ttl, now)).fetchall()
        for row in expired:
            self._move(row, State.EXPIRED, SYSTEM_LINK)
        exhausted = self._db.execute(
            'SELECT * FROM messages WHERE state = ? AND attempts >= ? AND next_attempt_at <= ?',
            (State.DELIVERED.value, self.policy.max_attempts, now)).fetchall()
        for row in exhausted:
            self._move(row, State.FAILED, SYSTEM_LINK)
        return len(expired) + len(exhausted)

    def _row(self, message_id):
        return self._db.execute('SELECT * FROM messages WHERE message_id = ?', (message_id,)).fetchone()

    def _move(self, row, target, link):
        current = State(row['state'])
        if target not in TRANSITIONS[current]:
            raise Conflict(f'message {row["message_id"].hex()} cannot go from {current.value} to {target.value}')
        self._db.execute('UPDATE messages SET state = ? WHERE message_id = ?', (target.value, row['message_id']))
        self._log(row['message_id'], current, target, link)
        if target in TERMINAL:
            self._settled.append((bytes(row['message_id']), target))

    def _log(self, message_id, source, target, link):
        self._db.execute(
            'INSERT INTO transmissions (message_id, at, from_state, to_state, link) VALUES (?, ?, ?, ?, ?)',
            (message_id, self._clock(), source.value if source else None, target.value, link))

    def _record(self, row):
        history = self._db.execute(
            'SELECT at, to_state, link FROM transmissions WHERE message_id = ? ORDER BY seq',
            (row['message_id'],)).fetchall()
        return {
            'id': row['message_id'].hex(), 'sender': row['sender'], 'recipient': row['recipient'],
            'size': row['size'], 'sha256': row['sha256'], 'state': row['state'], 'attempts': row['attempts'],
            'history': [{'at': item['at'], 'state': item['to_state'], 'link': item['link']} for item in history],
        }


def _envelope(row):
    return Envelope(bytes(row['message_id']), row['sender'], row['recipient'], row['created_at'],
                    Kind(row['kind']), bytes(row['body']))
