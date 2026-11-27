"""The LoRa link: messages as segments of at most one LoRa packet, with selective retransmission.

Frames (every frame fits one packet of FRAME_MAX_BYTES):

    DATA    0x01 | tag (6) | index (1) | total (1) | segment of (hops (1) + compact envelope)
    STATUS  0x02 | tag (6) | total (1) | bitmap of the segments received so far
    DONE    0x03 | tag (6)

The tag is the first six bytes of the message id. `hops` is how many more LoRa transmissions the
message may take; bases use it to stop forwarding (ADR 0040). The receiver answers DONE once it holds every
segment, or STATUS with what it is missing when segments stop arriving. The sender resends only the
missing segments, for a bounded number of rounds, then gives up and records why.

Time is counted in ticks so the link can be driven by a simulation as well as by a real radio loop.
Nothing here transmits; a radio driver or `lora_sim` carries the frames.
"""
import heapq
import itertools
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import IntEnum

from .envelope import COMPACT_MAX_BYTES, Envelope, EnvelopeError, Kind

FRAME_MAX_BYTES = COMPACT_MAX_BYTES
TAG_BYTES = 6
DATA, STATUS, DONE = 0x01, 0x02, 0x03
_DATA_HEADER = 1 + TAG_BYTES + 2
SEGMENT_BYTES = FRAME_MAX_BYTES - _DATA_HEADER
MAX_SEGMENTS = 255
DEFAULT_MAX_MESSAGE_BYTES = 1024


class Priority(IntEnum):
    """Lower goes first. Structured short signals always lead, as on the base radio (ADR 0029)."""
    SIGNAL = 0
    TEXT = 1
    FILE = 2


_PRIORITY = {Kind.SIGNAL: Priority.SIGNAL, Kind.TEXT: Priority.TEXT, Kind.RECEIPT: Priority.TEXT,
             Kind.FILE: Priority.FILE}


@dataclass(frozen=True)
class LinkPolicy:
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES
    max_rounds: int = 5
    status_timeout: int = 8
    gap_timeout: int = 3
    frames_per_tick: int = 1

    def __post_init__(self):
        if not 1 <= self.max_message_bytes <= SEGMENT_BYTES * MAX_SEGMENTS:
            raise ValueError(f'max_message_bytes must be 1..{SEGMENT_BYTES * MAX_SEGMENTS}')
        if min(self.max_rounds, self.status_timeout, self.gap_timeout, self.frames_per_tick) < 1:
            raise ValueError('rounds, timeouts and frames_per_tick must be at least 1')


DEFAULT_LINK_POLICY = LinkPolicy()
_REMEMBERED_FINISHED = 10_000


class RefusedOnLink(ValueError):
    """The message cannot travel over LoRa; it has to wait for the internet."""


@dataclass
class _Outgoing:
    envelope: Envelope
    priority: Priority
    order: int
    segments: list
    pending: list
    rounds: int = 1
    waiting_since: int | None = None


@dataclass
class _Incoming:
    total: int
    segments: dict = field(default_factory=dict)
    last_heard: int = 0
    status_sent_at: int | None = None


def _bitmap(total, held):
    bits = bytearray((total + 7) // 8)
    for index in held:
        bits[index // 8] |= 1 << (index % 8)
    return bytes(bits)


def _unbitmap(total, bits):
    return {index for index in range(total) if bits[index // 8] >> (index % 8) & 1}


class LoRaLink:
    """One end of a LoRa link. Feed it frames with `receive`, take frames to transmit with `poll`."""

    def __init__(self, name, *, policy=DEFAULT_LINK_POLICY, on_message=None, on_delivered=None, on_failed=None):
        self.name = name
        self.policy = policy
        self.on_message = on_message or (lambda envelope, hops: None)
        self.on_delivered = on_delivered or (lambda envelope: None)
        self.on_failed = on_failed or (lambda envelope, reason: None)
        self.log = []
        self._order = itertools.count()
        self._outgoing = {}
        self._queue = []
        self._control = []
        self._incoming = {}
        self._finished = OrderedDict()

    def accepts(self, envelope):
        return self._refusal(envelope) is None

    def send(self, envelope, now=0, *, hops=0):
        """Queue a message. Raises RefusedOnLink when it cannot go over LoRa at all."""
        if not 0 <= hops <= 255:
            raise ValueError('hops must be 0..255')
        reason = self._refusal(envelope)
        if reason:
            self._record(now, envelope.id, 'refused', reason)
            raise RefusedOnLink(reason)
        tag = envelope.message_id[:TAG_BYTES]
        if tag in self._outgoing:
            return
        packed = bytes([hops]) + envelope.to_compact(limit=None)
        segments = [packed[at:at + SEGMENT_BYTES] for at in range(0, len(packed), SEGMENT_BYTES)]
        item = _Outgoing(envelope, _PRIORITY[envelope.kind], next(self._order), segments, list(range(len(segments))))
        self._outgoing[tag] = item
        heapq.heappush(self._queue, (item.priority, item.order, tag))
        self._record(now, envelope.id, 'queued', f'{len(packed)} bytes in {len(segments)} segments')

    def pending(self):
        return len(self._outgoing)

    def poll(self, now):
        """Frames to transmit at tick `now`: acknowledgements first, then data by priority."""
        self._timers(now)
        frames, budget = [], self.policy.frames_per_tick
        while self._control and len(frames) < budget:
            frames.append(self._control.pop(0))
        while self._queue and len(frames) < budget:
            _, _, tag = self._queue[0]
            item = self._outgoing.get(tag)
            if item is None or not item.pending:
                heapq.heappop(self._queue)
                continue
            index = item.pending.pop(0)
            frames.append(bytes([DATA]) + tag + bytes([index, len(item.segments)]) + item.segments[index])
            if not item.pending:
                heapq.heappop(self._queue)
                item.waiting_since = now
        return frames

    def receive(self, frame, now):
        if not frame:
            return
        kind, tag = frame[0], frame[1:1 + TAG_BYTES]
        if len(tag) != TAG_BYTES:
            return
        if kind == DATA and len(frame) > _DATA_HEADER:
            self._on_data(tag, frame[7], frame[8], frame[_DATA_HEADER:], now)
        elif kind == STATUS and len(frame) >= 8:
            self._on_status(tag, frame[7], frame[8:], now)
        elif kind == DONE:
            self._on_done(tag, now)

    def _refusal(self, envelope):
        if envelope.kind is Kind.TRANSFER:
            return 'file transfers wait for the internet; their chunks are not reachable over LoRa'
        size = 1 + len(envelope.to_compact(limit=None))
        if size > self.policy.max_message_bytes:
            return (f'{size} bytes exceeds the LoRa limit of {self.policy.max_message_bytes} bytes; '
                    'waits for the internet')
        return None

    def _on_data(self, tag, index, total, segment, now):
        if tag in self._finished:
            self._queue_control(bytes([DONE]) + tag)
            return
        if not 1 <= total <= MAX_SEGMENTS or index >= total:
            return
        incoming = self._incoming.setdefault(tag, _Incoming(total))
        if incoming.total != total:
            return
        incoming.segments[index] = segment
        incoming.last_heard = now
        if len(incoming.segments) < total:
            if index == total - 1:
                self._send_status(tag, incoming, now)
            return
        del self._incoming[tag]
        packed = b''.join(incoming.segments[i] for i in range(total))
        try:
            hops, envelope = packed[0], Envelope.from_compact(packed[1:], limit=None)
        except EnvelopeError as error:
            self._record(now, tag.hex(), 'discarded', f'reassembled message is invalid: {error}')
            return
        if envelope.message_id[:TAG_BYTES] != tag:
            self._record(now, tag.hex(), 'discarded', 'reassembled message does not match its tag')
            return
        self._finished[tag] = None
        while len(self._finished) > _REMEMBERED_FINISHED:
            self._finished.popitem(last=False)
        self._queue_control(bytes([DONE]) + tag)
        self._record(now, envelope.id, 'received', f'{len(packed)} bytes in {total} segments')
        self.on_message(envelope, hops)

    def _on_status(self, tag, total, bits, now):
        item = self._outgoing.get(tag)
        if item is None or total != len(item.segments) or len(bits) < (total + 7) // 8:
            return
        missing = sorted(set(range(total)) - _unbitmap(total, bits))
        if missing:
            self._retransmit(tag, item, missing, now)

    def _on_done(self, tag, now):
        item = self._outgoing.pop(tag, None)
        if item is not None:
            self._record(now, item.envelope.id, 'delivered', f'after {item.rounds} round(s)')
            self.on_delivered(item.envelope)

    def _timers(self, now):
        for tag, item in list(self._outgoing.items()):
            if item.waiting_since is not None and now - item.waiting_since >= self.policy.status_timeout:
                self._retransmit(tag, item, list(range(len(item.segments))), now)
        give_up = self.policy.status_timeout * (self.policy.max_rounds + 1)
        for tag, incoming in list(self._incoming.items()):
            if now - incoming.last_heard > give_up:
                del self._incoming[tag]
                self._record(now, tag.hex(), 'abandoned', f'{len(incoming.segments)} of {incoming.total} '
                                                          'segments arrived before the sender went quiet')
                continue
            quiet = now - incoming.last_heard >= self.policy.gap_timeout
            due = incoming.status_sent_at is None or now - incoming.status_sent_at >= self.policy.status_timeout
            if quiet and due:
                self._send_status(tag, incoming, now)

    def _retransmit(self, tag, item, missing, now):
        if item.rounds >= self.policy.max_rounds:
            del self._outgoing[tag]
            reason = f'no receipt after {item.rounds} rounds; {len(missing)} of {len(item.segments)} segments missing'
            self._record(now, item.envelope.id, 'failed', reason)
            self.on_failed(item.envelope, reason)
            return
        item.rounds += 1
        item.pending = missing
        item.waiting_since = None
        heapq.heappush(self._queue, (item.priority, item.order, tag))
        self._record(now, item.envelope.id, 'retransmit', f'round {item.rounds}: segments {missing}')

    def _send_status(self, tag, incoming, now):
        incoming.status_sent_at = now
        self._queue_control(bytes([STATUS]) + tag + bytes([incoming.total]) + _bitmap(incoming.total,
                                                                                      incoming.segments))

    def _queue_control(self, frame):
        if frame not in self._control:
            self._control.append(frame)

    def _record(self, now, message_id, event, detail):
        self.log.append({'tick': now, 'link': self.name, 'id': message_id, 'event': event, 'detail': detail})
