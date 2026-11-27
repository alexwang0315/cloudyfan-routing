import hashlib
import os
import socket

import pytest

from cloudyfami_portal import DeliveryPolicy, Relay
from cloudyfami_portal.http_link import (
    ChunkUnavailableError,
    HttpLink,
    PortalClient,
    PortalError,
    PortalUnreachable,
    StorageFullError,
)
from cloudyfami_portal.transfer import CHUNK_MIN, Manifest, TransferError, Transfers

MiB = 1024 * 1024


def write_random(path, size):
    with open(path, 'wb') as target:
        remaining = size
        while remaining:
            piece = os.urandom(min(MiB, remaining))
            target.write(piece)
            remaining -= len(piece)
    return path


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        while piece := source.read(MiB):
            digest.update(piece)
    return digest.hexdigest()


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class Portal:
    def __init__(self, tmp_path, *, limit=1024 * MiB, port=0):
        self.relay = Relay(tmp_path / 'portal.sqlite3', policy=DeliveryPolicy(ack_timeout=30))
        self.spool = tmp_path / 'spool'
        self.transfers = Transfers(self.relay, self.spool, limit=limit)
        self.port = port
        self.link = None
        self.start()

    def start(self):
        self.link = HttpLink(self.relay, port=self.port, transfers=self.transfers).start()
        self.port = self.link._server.server_address[1]

    def stop(self):
        self.link.stop()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.port}'

    def close(self):
        self.stop()
        self.relay.close()


@pytest.fixture
def portal(tmp_path):
    portal = Portal(tmp_path)
    yield portal
    portal.close()


def deliver(portal, source, tmp_path, name='received.bin'):
    bob = PortalClient(portal.url, '乙')
    [envelope] = bob.receive(wait=2)
    target = tmp_path / name
    bob.fetch(envelope, target)
    bob.ack(envelope)
    return bob, envelope, target


def test_a_200_mb_file_arrives_with_the_same_hash(portal, tmp_path):
    source = write_random(tmp_path / 'recording.bin', 200 * MiB)
    manifest = PortalClient(portal.url, '甲').send_file('乙', source)
    _, _, target = deliver(portal, source, tmp_path)
    assert sha256_of(target) == sha256_of(source) == manifest.sha256
    record = portal.transfers.record(manifest.transfer_id)
    assert record['state'] == 'done'
    assert record['chunks'] == 200 and record['uploads'] == 200 and record['downloads'] == 200
    assert record['retransmissions'] == 0 and record['upload_seconds'] >= 0
    assert not (portal.spool / manifest.id).exists()


def test_an_interrupted_upload_resumes_with_only_the_missing_chunks(tmp_path):
    portal = Portal(tmp_path, port=free_port())
    try:
        source = write_random(tmp_path / 'recording.bin', 20 * MiB)
        alice = PortalClient(portal.url, '甲', retry_delays=())
        manifest = Manifest.from_file(source, '甲', '乙')

        def cut_after_eight(index):
            if index == 7:
                portal.stop()

        with pytest.raises(PortalUnreachable):
            alice.upload(manifest, source, on_chunk=cut_after_eight)
        portal.start()
        assert len(alice.transfer(manifest.id)['missing']) == 12

        uploaded = []
        alice.upload(manifest, source, on_chunk=uploaded.append)
        assert uploaded == list(range(8, 20))
        _, _, target = deliver(portal, source, tmp_path)
        assert sha256_of(target) == manifest.sha256
        assert portal.transfers.record(manifest.transfer_id)['uploads'] == 20
    finally:
        portal.close()


def test_a_chunk_damaged_on_the_way_down_is_fetched_again(portal, tmp_path, monkeypatch):
    source = write_random(tmp_path / 'recording.bin', 5 * MiB)
    manifest = PortalClient(portal.url, '甲').send_file('乙', source)
    bob = PortalClient(portal.url, '乙')
    original = bob._download_chunk
    damaged = []

    def flip_a_bit_once(transfer_id, index):
        data = original(transfer_id, index)
        if index == 3 and not damaged:
            damaged.append(index)
            return bytes([data[0] ^ 1]) + data[1:]
        return data

    monkeypatch.setattr(bob, '_download_chunk', flip_a_bit_once)
    [envelope] = bob.receive(wait=2)
    bob.fetch(envelope, tmp_path / 'received.bin')
    assert damaged == [3]
    assert sha256_of(tmp_path / 'received.bin') == manifest.sha256
    record = portal.transfers.record(manifest.transfer_id)
    assert record['downloads'] == 6 and record['retransmissions'] == 1


def test_a_chunk_damaged_on_the_portal_is_reported_and_uploaded_again(portal, tmp_path):
    source = write_random(tmp_path / 'recording.bin', 5 * MiB)
    alice = PortalClient(portal.url, '甲')
    manifest = alice.send_file('乙', source)
    stored = portal.spool / manifest.id / '2'
    stored.write_bytes(b'\0' + stored.read_bytes()[1:])

    bob = PortalClient(portal.url, '乙')
    [envelope] = bob.receive(wait=2)
    with pytest.raises(ChunkUnavailableError, match='chunk 2 is damaged'):
        bob.fetch(envelope, tmp_path / 'received.bin')
    assert not (tmp_path / 'received.bin').exists()
    assert alice.transfer(manifest.id)['missing'] == [2]

    uploaded = []
    alice.upload(manifest, source, on_chunk=uploaded.append)
    assert uploaded == [2]
    bob.fetch(envelope, tmp_path / 'received.bin')
    assert sha256_of(tmp_path / 'received.bin') == manifest.sha256
    assert portal.transfers.record(manifest.transfer_id)['uploads'] == 6


def test_a_file_larger_than_the_free_spool_is_refused_with_the_reason_logged(tmp_path):
    portal = Portal(tmp_path, limit=5 * MiB)
    try:
        source = write_random(tmp_path / 'recording.bin', 8 * MiB)
        alice = PortalClient(portal.url, '甲')
        with pytest.raises(StorageFullError, match='needs 8388608 bytes'):
            alice.send_file('乙', source)
        [logged] = alice.transfers()
        assert logged['state'] == 'rejected' and 'needs 8388608 bytes' in logged['reason']
        assert PortalClient(portal.url, '乙').receive() == []
    finally:
        portal.close()


def test_space_is_freed_once_the_recipient_acknowledges(tmp_path):
    portal = Portal(tmp_path, limit=6 * MiB)
    try:
        alice = PortalClient(portal.url, '甲')
        first = alice.send_file('乙', write_random(tmp_path / 'first.bin', 4 * MiB))
        with pytest.raises(StorageFullError):
            alice.send_file('乙', write_random(tmp_path / 'second.bin', 4 * MiB))
        deliver(portal, tmp_path / 'first.bin', tmp_path)
        assert portal.transfers.record(first.transfer_id)['state'] == 'done'
        alice.send_file('乙', tmp_path / 'second.bin')
    finally:
        portal.close()


def test_a_chunk_that_does_not_match_the_manifest_is_rejected_and_counted(portal, tmp_path):
    source = write_random(tmp_path / 'recording.bin', 2 * MiB)
    alice = PortalClient(portal.url, '甲')
    manifest = Manifest.from_file(source, '甲', '乙')
    alice._call('POST', '/v1/transfers', manifest.to_json())
    with pytest.raises(PortalError, match='400'):
        alice._call('PUT', f'/v1/transfers/{manifest.id}/chunks/0', body=b'x' * MiB)
    assert alice.transfer(manifest.id)['upload_rejects'] == 1
    alice.upload(manifest, source)
    assert alice.transfer(manifest.id)['state'] == 'queued'


def test_a_file_transfer_is_announced_once_even_when_uploaded_twice(portal, tmp_path):
    source = write_random(tmp_path / 'recording.bin', 2 * MiB)
    alice = PortalClient(portal.url, '甲')
    manifest = alice.send_file('乙', source)
    alice.upload(manifest, source)
    bob = PortalClient(portal.url, '乙')
    assert [envelope.id for envelope in bob.receive(wait=1)] == [manifest.id]
    assert bob.receive() == []


@pytest.mark.parametrize('change', [
    {'size': 0}, {'chunk_size': CHUNK_MIN - 1}, {'chunks': ('0' * 64,)}, {'sha256': 'not-a-hash'},
])
def test_inconsistent_manifests_are_rejected(change):
    fields = {'transfer_id': b'\1' * 16, 'sender': '甲', 'recipient': '乙', 'created_at': 0, 'size': 3 * CHUNK_MIN,
              'sha256': 'a' * 64, 'chunk_size': CHUNK_MIN, 'chunks': ('b' * 64,) * 3}
    with pytest.raises(TransferError):
        Manifest(**{**fields, **change})
