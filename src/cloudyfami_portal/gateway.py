"""The base deployment (ADR 0040): the same relay, plus a gateway that moves messages between bases.

Every client has a home: a base, or the cloud. A base is the local entry point for the clients homed
there. Messages for clients homed elsewhere go out over the internet while the cloud is reachable, and
over LoRa while it is not. The cloud is a plain relay: bases push into it and pull their clients'
messages out of it.

Receipts are end to end. When a client acknowledges a message from another home, its base sends a
RECEIPT message back to the sender's home, and every base holding the original along the way marks it
acknowledged. A forwarded message without a receipt is handed out again after the relay's ack timeout
and forwarded again, so a copy lost anywhere on the way is retried.
"""
from dataclasses import dataclass, field

from .connectivity import Connectivity, Mode
from .envelope import Envelope, Kind
from .http_link import PortalClient, PortalUnreachable
from .lora_link import RefusedOnLink
from .relay import Conflict, DeliveryPolicy, NotFound, State
from .transfer import Manifest

CLOUD = 'cloud'
FORWARD = 'forward'
DEFAULT_MAX_HOPS = 3
# A base keeps trying for as long as an outage may last; ttl still bounds how long a message lives.
BASE_POLICY = DeliveryPolicy(ack_timeout=120.0, max_attempts=50)


@dataclass(frozen=True)
class BaseConfig:
    name: str
    homes: dict = field(default_factory=dict)
    max_hops: int = DEFAULT_MAX_HOPS

    def __post_init__(self):
        if self.name == CLOUD:
            raise ValueError(f'a base cannot be called {CLOUD!r}')
        if not 1 <= self.max_hops <= 255:
            raise ValueError('max_hops must be 1..255')

    def home_of(self, client):
        return self.homes.get(client, CLOUD)


class HttpUplink:
    """The base's way into the cloud relay, acting on behalf of each client homed at the base."""

    def __init__(self, url, *, timeout=5.0):
        self.url = url
        self._timeout = timeout
        self._clients = {}

    def client(self, name):
        if name not in self._clients:
            self._clients[name] = PortalClient(self.url, name, timeout=self._timeout, retry_delays=())
            self._clients[name].keep_receipts = True
        return self._clients[name]

    def probe(self):
        try:
            self.client('probe')._call('GET', '/health')
            return True
        except PortalUnreachable:
            return False

    def push(self, envelope):
        self.client(envelope.sender).resend(envelope)

    def push_transfer(self, manifest, read_chunk):
        self.client(manifest.sender).upload_chunks(manifest, read_chunk)

    def pull(self, name):
        return self.client(name).receive()

    def ack(self, name, envelope):
        self.client(name).ack(envelope)

    def fetch_chunk(self, name, transfer_id, index):
        return self.client(name).download_chunk(transfer_id, index)


class Base:
    """Runs one base. Call `step(now)` regularly; it probes the cloud, pulls, and forwards."""

    def __init__(self, config, relay, transfers, *, uplink=None, lora=None):
        self.config = config
        self.relay = relay
        self.transfers = transfers
        self.uplink = uplink
        self.lora = lora
        self.connectivity = Connectivity(mode=Mode.ONLINE if uplink else Mode.OFFLINE)
        self.log = []
        self.now = 0
        self._hops = {}
        relay.settle_listeners.append(self._settled)
        if lora is not None:
            lora.on_message = self._from_lora

    @property
    def name(self):
        return self.config.name

    @property
    def online(self):
        return self.uplink is not None and self.connectivity.mode is Mode.ONLINE

    def is_local(self, client):
        return self.config.home_of(client) == self.name

    def step(self, now):
        self.now = now
        if self.uplink is not None:
            before = self.connectivity.mode
            self.connectivity = self.connectivity.observe(self.uplink.probe())
            if self.connectivity.mode is not before:
                self._record(None, 'online' if self.online else 'offline', 'cloud')
        if self.online:
            self._pull()
        self._forward()

    def _forward(self):
        for recipient in sorted(self.relay.due_recipients()):
            if self.is_local(recipient):
                continue
            for envelope in self.relay.claim(recipient, FORWARD, limit=50, accept=self._can_send_now):
                self._send_on(envelope)

    def _can_send_now(self, envelope):
        if self.online:
            return True
        return self.lora is not None and self.lora.accepts(envelope)

    def _send_on(self, envelope):
        recipient = envelope.recipient
        if self.online:
            try:
                if envelope.kind is Kind.TRANSFER:
                    manifest = Manifest.from_envelope(envelope)
                    self.uplink.push_transfer(manifest, lambda index: self.transfers.get_chunk(
                        manifest.transfer_id, index))
                else:
                    self.uplink.push(envelope)
            except PortalUnreachable as error:
                self.connectivity = self.connectivity.observe(False)
                self._record(envelope, 'held', f'cloud unreachable: {error}')
                return
            self._record(envelope, 'forwarded', 'cloud')
            if envelope.kind is Kind.RECEIPT or self.config.home_of(recipient) == CLOUD:
                self.relay.ack(recipient, envelope.message_id, CLOUD)
            return
        hops = self._hops.get(envelope.message_id, self.config.max_hops)
        try:
            self.lora.send(envelope, self.now, hops=hops)
        except RefusedOnLink as error:
            self._record(envelope, 'held', str(error))
            return
        self._record(envelope, 'forwarded', f'lora, {hops} hop(s) left')
        if envelope.kind is Kind.RECEIPT:
            self.relay.ack(recipient, envelope.message_id, 'lora')

    def _pull(self):
        local = [client for client, home in self.config.homes.items() if home == self.name]
        for client in local:
            try:
                incoming = self.uplink.pull(client)
            except PortalUnreachable:
                self.connectivity = self.connectivity.observe(False)
                return
            for envelope in incoming:
                self._arrived(envelope, CLOUD, self.config.max_hops)
                try:
                    self.uplink.ack(client, envelope)
                except PortalUnreachable:
                    self.connectivity = self.connectivity.observe(False)
                    return

    def _from_lora(self, envelope, hops):
        self._arrived(envelope, 'lora', hops)

    def _arrived(self, envelope, via, hops):
        if envelope.kind is Kind.RECEIPT:
            self._receipt(envelope, via, hops)
            return
        if self.relay.exists(envelope.message_id):
            record = self.relay.status(envelope.message_id)
            if record['state'] == State.ACKED.value and self.is_local(envelope.recipient):
                self._send_receipt(record)
            return
        if not self.is_local(envelope.recipient) and via == 'lora' and hops <= 1:
            self._record(envelope, 'expired', f'hop limit reached at {self.name}; not forwarded')
            return
        if envelope.kind is Kind.TRANSFER and via == CLOUD:
            self._copy_transfer(envelope)
        else:
            self.relay.submit(envelope, via)
        if not self.is_local(envelope.recipient):
            self._hops[envelope.message_id] = hops - 1 if via == 'lora' else self.config.max_hops
            self._record(envelope, 'relaying', via)
        else:
            self._record(envelope, 'received', via)

    def _copy_transfer(self, envelope):
        manifest = Manifest.from_envelope(envelope)
        record = self.transfers.create(manifest, CLOUD)
        for index in record['missing']:
            data = self.uplink.fetch_chunk(envelope.recipient, manifest.id, index)
            self.transfers.put_chunk(manifest.transfer_id, index, data, CLOUD)

    def _receipt(self, receipt, via, hops):
        original = receipt.body
        try:
            record = self.relay.status(original)
        except NotFound:
            record = None
        if record and record['state'] == State.DELIVERED.value:
            try:
                self.relay.ack(record['recipient'], original, via)
            except Conflict:
                pass
            self._record(receipt, 'receipt', f'for {original.hex()[:8]} via {via}')
        if self.is_local(receipt.recipient) or self.relay.exists(receipt.message_id):
            return
        if via == 'lora' and hops <= 1:
            return
        self.relay.submit(receipt, via)
        self._hops[receipt.message_id] = hops - 1 if via == 'lora' else self.config.max_hops

    def _settled(self, message_id, state):
        if state is not State.ACKED:
            return
        record = self.relay.status(message_id)
        if record['kind'] != Kind.RECEIPT.name.lower() and self.is_local(record['recipient']) \
                and not self.is_local(record['sender']):
            self._send_receipt(record)

    def _send_receipt(self, record):
        receipt = Envelope.new(record['recipient'], record['sender'], bytes.fromhex(record['id']), Kind.RECEIPT)
        self.relay.submit(receipt, FORWARD)
        self._record(receipt, 'receipt sent', f'for {record["id"][:8]}')

    def _record(self, envelope, event, detail):
        self.log.append({'tick': self.now, 'base': self.name, 'id': envelope.id if envelope else None,
                         'event': event, 'detail': detail})
