"""The relay service. It knows nothing about links: every link adapter drives it through this API."""
import sqlite3
import threading
import time
from enum import Enum

from .envelope import Envelope, Kind


class State(Enum):
    QUEUED = 'queued'
    DELIVERED = 'delivered'
    ACKED = 'acked'
    EXPIRED = 'expired'
    FAILED = 'failed'


TRANSITIONS = {
    State.QUEUED: {State.DELIVERED, State.EXPIRED, State.FAILED},
    State.DELIVERED: {State.ACKED, State.EXPIRED, State.FAILED},
    State.ACKED: set(),
    State.EXPIRED: set(),
    State.FAILED: set(),
}


class NotFound(LookupError):
    pass


class Conflict(ValueError):
    pass


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
    state TEXT NOT NULL
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
'''


class Relay:
    def __init__(self, path, *, clock=time.time):
        self._clock = clock
        self._changed = threading.Condition()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.executescript(_SCHEMA)

    def close(self):
        with self._changed:
            self._db.close()

    def submit(self, envelope, link):
        """Queue a message. Submitting the same message again returns its current state."""
        with self._changed:
            row = self._row(envelope.message_id)
            if row is not None:
                if _envelope(row) != envelope:
                    raise Conflict(f'message {envelope.id} already exists with different content')
                return State(row['state'])
            self._db.execute('BEGIN IMMEDIATE')
            self._db.execute(
                'INSERT INTO messages (message_id, sender, recipient, created_at, kind, body, size, sha256, state)'
                ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (envelope.message_id, envelope.sender, envelope.recipient, envelope.created_at,
                 envelope.kind.value, envelope.body, envelope.size, envelope.sha256, State.QUEUED.value))
            self._log(envelope.message_id, None, State.QUEUED, link)
            self._db.execute('COMMIT')
            self._changed.notify_all()
            return State.QUEUED

    def claim(self, recipient, link, *, limit=10, wait=0.0):
        """Hand the recipient its queued messages, oldest first, and mark them delivered."""
        deadline = time.monotonic() + wait
        with self._changed:
            while True:
                rows = self._db.execute(
                    'SELECT * FROM messages WHERE recipient = ? AND state = ? ORDER BY seq LIMIT ?',
                    (recipient, State.QUEUED.value, limit)).fetchall()
                remaining = deadline - time.monotonic()
                if rows or remaining <= 0:
                    break
                self._changed.wait(remaining)
            if rows:
                self._db.execute('BEGIN IMMEDIATE')
                for row in rows:
                    self._move(row, State.DELIVERED, link)
                self._db.execute('COMMIT')
                self._changed.notify_all()
            return [_envelope(row) for row in rows]

    def ack(self, recipient, message_id, link):
        """Record the recipient's receipt. Acknowledging twice is harmless."""
        with self._changed:
            row = self._row(message_id)
            if row is None or row['recipient'] != recipient:
                raise NotFound(f'no message {message_id.hex()} for {recipient}')
            if row['state'] == State.ACKED.value:
                return State.ACKED
            self._db.execute('BEGIN IMMEDIATE')
            try:
                self._move(row, State.ACKED, link)
            except Conflict:
                self._db.execute('ROLLBACK')
                raise
            self._db.execute('COMMIT')
            self._changed.notify_all()
            return State.ACKED

    def status(self, message_id, *, wait_for=None, wait=0.0):
        """Return a message's delivery record, optionally waiting until it reaches `wait_for`."""
        deadline = time.monotonic() + wait
        with self._changed:
            while True:
                row = self._row(message_id)
                if row is None:
                    raise NotFound(f'no message {message_id.hex()}')
                remaining = deadline - time.monotonic()
                state = State(row['state'])
                if wait_for is None or state is wait_for or not TRANSITIONS[state] or remaining <= 0:
                    return self._record(row)
                self._changed.wait(remaining)

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

    def _row(self, message_id):
        return self._db.execute('SELECT * FROM messages WHERE message_id = ?', (message_id,)).fetchone()

    def _move(self, row, target, link):
        current = State(row['state'])
        if target not in TRANSITIONS[current]:
            raise Conflict(f'message {row["message_id"].hex()} cannot go from {current.value} to {target.value}')
        self._db.execute('UPDATE messages SET state = ? WHERE message_id = ?', (target.value, row['message_id']))
        self._log(row['message_id'], current, target, link)

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
            'size': row['size'], 'sha256': row['sha256'], 'state': row['state'],
            'history': [{'at': item['at'], 'state': item['to_state'], 'link': item['link']} for item in history],
        }


def _envelope(row):
    return Envelope(bytes(row['message_id']), row['sender'], row['recipient'], row['created_at'],
                    Kind(row['kind']), bytes(row['body']))
