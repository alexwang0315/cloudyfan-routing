import threading

import pytest

from cloudyfami_portal import Kind, Relay, State
from cloudyfami_portal.http_link import HttpLink, PortalClient, PortalError


@pytest.fixture
def portal(tmp_path):
    relay = Relay(tmp_path / 'portal.sqlite3')
    link = HttpLink(relay).start()
    yield link.url
    link.stop()
    relay.close()


def test_two_clients_complete_a_round_trip_with_receipts(portal):
    alice, bob = PortalClient(portal, '甲'), PortalClient(portal, '乙')
    sent = alice.send('乙', '你好')
    [received] = bob.receive(wait=2)
    assert received == sent and received.sha256 == sent.sha256
    bob.ack(received)
    assert alice.status(sent.id, until=State.ACKED, wait=2)['state'] == 'acked'

    reply = bob.send('甲', '收到了')
    [answer] = alice.receive(wait=2)
    assert answer.body.decode('utf-8') == '收到了'
    alice.ack(answer)
    assert bob.status(reply.id, until=State.ACKED, wait=2)['state'] == 'acked'


def test_a_waiting_recipient_gets_the_message_as_soon_as_it_is_sent(portal):
    alice, bob = PortalClient(portal, '甲'), PortalClient(portal, '乙')
    inbox = []
    listener = threading.Thread(target=lambda: inbox.extend(bob.receive(wait=5)))
    listener.start()
    sent = alice.send('乙', '長輪詢')
    listener.join(timeout=5)
    assert inbox == [sent]


def test_binary_content_arrives_unchanged(portal):
    alice, bob = PortalClient(portal, '甲'), PortalClient(portal, '乙')
    payload = bytes(range(256)) * 64
    sent = alice.send('乙', payload, Kind.FILE)
    [received] = bob.receive(wait=2)
    assert received.body == payload and received.sha256 == sent.sha256


def test_transmission_log_records_who_what_and_each_state(portal):
    alice, bob = PortalClient(portal, '甲'), PortalClient(portal, '乙')
    sent = alice.send('乙', '紀錄')
    bob.ack(bob.receive(wait=2)[0])
    log = [item for item in alice.transmissions() if item['id'] == sent.id]
    assert [item['to'] for item in log] == ['queued', 'delivered', 'acked']
    assert {(item['sender'], item['recipient'], item['size'], item['link']) for item in log} == {
        ('甲', '乙', sent.size, 'http')}


def test_malformed_and_unknown_requests_are_reported_not_swallowed(portal):
    client = PortalClient(portal, '甲')
    with pytest.raises(PortalError, match='404'):
        client.status('00' * 16)
    with pytest.raises(PortalError, match='400'):
        client.status('not-hex')
    with pytest.raises(PortalError, match='400'):
        client._call('POST', '/v1/messages', {'id': 'broken'})


def test_unreachable_portal_is_an_explicit_error():
    with pytest.raises(PortalError, match='cannot reach'):
        PortalClient('http://127.0.0.1:1', '甲', timeout=1).receive()
