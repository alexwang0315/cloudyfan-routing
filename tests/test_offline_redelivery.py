import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cloudyfami_portal import Connectivity, DeliveryPolicy, Envelope, Mode, Relay
from cloudyfami_portal.http_link import HttpLink, PortalClient, PortalUnreachable

SRC = Path(__file__).resolve().parents[1] / 'src'


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def relay(tmp_path, clock):
    relay = Relay(tmp_path / 'portal.sqlite3', clock=clock,
                  policy=DeliveryPolicy(ack_timeout=30, max_attempts=3, ttl=3600))
    yield relay
    relay.close()


@pytest.fixture
def portal(relay):
    link = HttpLink(relay).start()
    yield link.url
    link.stop()


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def test_messages_sent_while_the_recipient_is_offline_arrive_in_order_once_it_returns(portal):
    alice = PortalClient(portal, '甲')
    sent = [alice.send('乙', f'第 {n} 則') for n in range(10)]
    bob = PortalClient(portal, '乙')
    received = []
    while batch := bob.receive(limit=4):
        for envelope in batch:
            bob.ack(envelope)
        received.extend(batch)
    assert [envelope.id for envelope in received] == [envelope.id for envelope in sent]


def test_a_message_resent_three_times_reaches_the_recipient_once(portal):
    alice, bob = PortalClient(portal, '甲'), PortalClient(portal, '乙')
    sent = alice.send('乙', '重送三次')
    for _ in range(3):
        alice.resend(sent)
    assert bob.receive() == [sent]
    assert bob.receive() == []


def test_sender_retries_while_the_portal_is_down_without_creating_a_second_message(tmp_path):
    port = free_port()
    relay = Relay(tmp_path / 'portal.sqlite3')
    started = []

    def portal_comes_back(_delay):
        if not started:
            started.append(HttpLink(relay, port=port).start())

    alice = PortalClient(f'http://127.0.0.1:{port}', '甲', retry_delays=(0, 0), sleep=portal_comes_back)
    sent = alice.send('乙', '平台剛恢復')
    try:
        bob = PortalClient(started[0].url, '乙')
        assert bob.receive() == [sent]
        assert [item['to'] for item in bob.transmissions()] == ['queued', 'delivered']
    finally:
        started[0].stop()
        relay.close()


def test_an_unacknowledged_message_is_handed_out_again_after_the_ack_timeout(relay, clock):
    sent = Envelope.new('甲', '乙', '沒回條')
    relay.submit(sent, 'test')
    assert relay.claim('乙', 'test') == [sent]
    clock.now += 29
    assert relay.claim('乙', 'test') == []
    clock.now += 2
    assert relay.claim('乙', 'test') == [sent]
    relay.ack('乙', sent.message_id, 'test')
    record = relay.status(sent.message_id)
    assert record['state'] == 'acked' and record['attempts'] == 2


def test_a_message_that_is_never_acknowledged_fails_after_the_last_attempt(relay, clock):
    sent = Envelope.new('甲', '乙', '一直沒回條')
    relay.submit(sent, 'test')
    for _ in range(3):
        assert relay.claim('乙', 'test') == [sent]
        clock.now += 31
    assert relay.claim('乙', 'test') == []
    record = relay.status(sent.message_id)
    assert record['state'] == 'failed' and record['attempts'] == 3
    assert record['history'][-1]['link'] == 'relay'


def test_an_expired_message_is_not_delivered_but_stays_in_the_log(relay, clock):
    sent = Envelope.new('甲', '乙', '太久沒人領')
    relay.submit(sent, 'test')
    clock.now += 3600
    assert relay.claim('乙', 'test') == []
    assert relay.status(sent.message_id)['state'] == 'expired'
    assert [(item['from'], item['to']) for item in relay.transmissions()] == [
        (None, 'queued'), ('queued', 'expired')]


def test_a_lost_acknowledgement_does_not_show_the_recipient_a_duplicate(portal, relay, clock):
    alice, bob = PortalClient(portal, '甲'), PortalClient(portal, '乙')
    sent = alice.send('乙', '回條在路上掉了')
    [received] = bob.receive()
    bob._acked[received.id] = None
    clock.now += 31
    assert bob.receive() == []
    assert relay.status(sent.message_id)['state'] == 'acked'


def test_connectivity_needs_three_failures_to_go_offline_and_two_successes_to_come_back():
    state = Connectivity(mode=Mode.ONLINE)
    for probe_ok, expected in [(False, Mode.ONLINE), (False, Mode.ONLINE), (True, Mode.ONLINE),
                               (False, Mode.ONLINE), (False, Mode.ONLINE), (False, Mode.OFFLINE),
                               (True, Mode.OFFLINE), (False, Mode.OFFLINE), (True, Mode.OFFLINE),
                               (True, Mode.ONLINE)]:
        state = state.observe(probe_ok)
        assert state.mode is expected


def test_client_reports_offline_only_after_three_failed_calls():
    client = PortalClient(f'http://127.0.0.1:{free_port()}', '甲', timeout=1, retry_delays=())
    for attempt in range(3):
        assert client.online
        with pytest.raises(PortalUnreachable):
            client.receive()
    assert not client.online


def start_server(db, port):
    env = {**os.environ, 'PYTHONPATH': str(SRC)}
    process = subprocess.Popen(
        [sys.executable, '-B', '-m', 'cloudyfami_portal', 'serve', '--db', str(db), '--port', str(port),
         '--ack-timeout', '0.5'], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    probe = PortalClient(f'http://127.0.0.1:{port}', 'probe', timeout=1, retry_delays=())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            probe._call('GET', '/health')
            return process
        except PortalUnreachable:
            time.sleep(0.1)
    process.kill()
    raise AssertionError('relay did not start')


def test_killing_the_relay_mid_delivery_loses_nothing_and_shows_nothing_twice(tmp_path):
    db, port = tmp_path / 'portal.sqlite3', free_port()
    url = f'http://127.0.0.1:{port}'
    server = start_server(db, port)
    try:
        alice = PortalClient(url, '甲')
        sent = [alice.send('乙', f'第 {n} 則') for n in range(10)]
        bob = PortalClient(url, '乙', retry_delays=())
        first = bob.receive(limit=5)
        for envelope in first[:3]:
            bob.ack(envelope)
        server.send_signal(signal.SIGKILL)
        server.wait()
        for envelope in first[3:]:
            with pytest.raises(PortalUnreachable):
                bob.ack(envelope)

        server = start_server(db, port)
        shown = list(first)
        deadline = time.monotonic() + 10
        while len(shown) < 10 and time.monotonic() < deadline:
            for envelope in bob.receive(wait=1):
                bob.ack(envelope)
                shown.append(envelope)
        assert [envelope.id for envelope in shown] == [envelope.id for envelope in sent]
        time.sleep(0.6)
        assert bob.receive(wait=1) == []
        for envelope in sent:
            assert alice.status(envelope.id, until=None)['state'] == 'acked'
    finally:
        server.kill()
        server.wait()
