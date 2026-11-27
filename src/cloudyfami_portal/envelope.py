"""The message envelope, with one full form (JSON) and one compact form (a single LoRa packet)."""
import base64
import hashlib
import os
import struct
import time
from dataclasses import dataclass
from enum import Enum

COMPACT_MAX_BYTES = 200
COMPACT_VERSION = 1
MAX_NAME_BYTES = 32
_HASH_PREFIX = 4
_FIXED = struct.Struct('>B16sIB')


class EnvelopeError(ValueError):
    pass


class TooLargeForCompact(EnvelopeError):
    pass


class Kind(Enum):
    TEXT = 1
    SIGNAL = 2
    FILE = 3
    TRANSFER = 4


def check_name(field, value):
    if not isinstance(value, str):
        raise EnvelopeError(f'{field} must be text')
    raw = value.encode('utf-8')
    if not 1 <= len(raw) <= MAX_NAME_BYTES:
        raise EnvelopeError(f'{field} must be 1..{MAX_NAME_BYTES} bytes')
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value) or '/' in value:
        raise EnvelopeError(f'{field} must not contain control characters or "/"')
    return raw


@dataclass(frozen=True)
class Envelope:
    message_id: bytes
    sender: str
    recipient: str
    created_at: int
    kind: Kind
    body: bytes

    def __post_init__(self):
        if not isinstance(self.message_id, bytes) or len(self.message_id) != 16:
            raise EnvelopeError('message_id must be 16 bytes')
        check_name('sender', self.sender)
        check_name('recipient', self.recipient)
        if isinstance(self.created_at, bool) or not isinstance(self.created_at, int) \
                or not 0 <= self.created_at < 2**32:
            raise EnvelopeError('created_at must be whole seconds since the epoch')
        if not isinstance(self.kind, Kind):
            raise EnvelopeError('kind must be a Kind')
        if not isinstance(self.body, bytes):
            raise EnvelopeError('body must be bytes')

    @classmethod
    def new(cls, sender, recipient, body, kind=Kind.TEXT, *, clock=time.time):
        if isinstance(body, str):
            body = body.encode('utf-8')
        return cls(os.urandom(16), sender, recipient, int(clock()), kind, body)

    @property
    def id(self):
        return self.message_id.hex()

    @property
    def size(self):
        return len(self.body)

    @property
    def sha256(self):
        return hashlib.sha256(self.body).hexdigest()

    def to_json(self):
        return {
            'id': self.id, 'sender': self.sender, 'recipient': self.recipient,
            'created_at': self.created_at, 'kind': self.kind.name.lower(),
            'body': base64.b64encode(self.body).decode('ascii'),
            'size': self.size, 'sha256': self.sha256,
        }

    @classmethod
    def from_json(cls, data):
        if not isinstance(data, dict):
            raise EnvelopeError('envelope must be a JSON object')
        try:
            message_id = bytes.fromhex(data['id'])
            body = base64.b64decode(data['body'], validate=True)
            kind = Kind[data['kind'].upper()]
            envelope = cls(message_id, data['sender'], data['recipient'], data['created_at'], kind, body)
        except EnvelopeError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise EnvelopeError(f'malformed envelope: {error}') from error
        if data.get('size') != envelope.size or data.get('sha256') != envelope.sha256:
            raise EnvelopeError('size or sha256 does not match the body')
        return envelope

    def to_compact(self):
        sender = check_name('sender', self.sender)
        recipient = check_name('recipient', self.recipient)
        packet = b''.join((
            _FIXED.pack(COMPACT_VERSION, self.message_id, self.created_at, self.kind.value),
            bytes([len(sender)]), sender, bytes([len(recipient)]), recipient,
            hashlib.sha256(self.body).digest()[:_HASH_PREFIX], self.body,
        ))
        if len(packet) > COMPACT_MAX_BYTES:
            raise TooLargeForCompact(f'compact form is {len(packet)} bytes, limit {COMPACT_MAX_BYTES}')
        return packet

    @classmethod
    def from_compact(cls, packet):
        if not isinstance(packet, bytes) or len(packet) > COMPACT_MAX_BYTES:
            raise EnvelopeError('compact packet must be bytes within the packet limit')
        try:
            version, message_id, created_at, kind = _FIXED.unpack_from(packet)
            if version != COMPACT_VERSION:
                raise EnvelopeError(f'unknown compact version {version}')
            at = _FIXED.size
            names = []
            for _ in range(2):
                length = packet[at]
                raw = packet[at + 1:at + 1 + length]
                if len(raw) != length:
                    raise EnvelopeError('compact packet is truncated')
                names.append(raw.decode('utf-8'))
                at += 1 + length
            digest = packet[at:at + _HASH_PREFIX]
            body = packet[at + _HASH_PREFIX:]
            if len(digest) != _HASH_PREFIX:
                raise EnvelopeError('compact packet is truncated')
            envelope = cls(message_id, names[0], names[1], created_at, Kind(kind), body)
        except EnvelopeError:
            raise
        except (struct.error, IndexError, ValueError) as error:
            raise EnvelopeError(f'malformed compact packet: {error}') from error
        if hashlib.sha256(body).digest()[:_HASH_PREFIX] != digest:
            raise EnvelopeError('compact packet body does not match its hash')
        return envelope
