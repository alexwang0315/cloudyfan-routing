import pytest

from cloudyfami_portal import Conflict, Envelope, NotFound, Relay, State


@pytest.fixture
def relay(tmp_path):
    relay = Relay(tmp_path / 'portal.sqlite3')
    yield relay
    relay.close()


class FakeLink:
    """A link adapter that is not HTTP: it drives the relay directly, as a LoRa adapter would."""
    name = 'fake'

    def __init__(self, relay):
        self.relay = relay

    def deliver_packet(self, packet):
        return self.relay.submit(Envelope.from_compact(packet), self.name)

    def collect_packets(self, recipient):
        return [envelope.to_compact() for envelope in self.relay.claim(recipient, self.name)]

    def acknowledge(self, recipient, packet):
        return self.relay.ack(recipient, Envelope.from_compact(packet).message_id, self.name)


def states(relay, envelope):
    return [item['state'] for item in relay.status(envelope.message_id)['history']]


def test_sender_sees_a_receipt_after_the_recipient_acknowledges(relay):
    sent = Envelope.new('甲', '乙', '你好')
    relay.submit(sent, 'test')
    [received] = relay.claim('乙', 'test')
    assert received == sent and received.sha256 == sent.sha256
    assert relay.status(sent.message_id)['state'] == 'delivered'
    relay.ack('乙', sent.message_id, 'test')
    assert states(relay, sent) == ['queued', 'delivered', 'acked']


def test_message_for_an_absent_recipient_stays_queued_and_visible(relay):
    sent = Envelope.new('甲', '還沒上線', '留言')
    relay.submit(sent, 'test')
    assert relay.claim('乙', 'test') == []
    assert relay.status(sent.message_id)['state'] == 'queued'
    assert [item['to'] for item in relay.transmissions()] == ['queued']


def test_messages_arrive_in_the_order_they_were_sent(relay):
    sent = [Envelope.new('甲', '乙', f'第 {n} 則') for n in range(5)]
    for envelope in sent:
        relay.submit(envelope, 'test')
    assert relay.claim('乙', 'test') == sent


def test_a_delivered_message_is_not_handed_out_twice(relay):
    relay.submit(Envelope.new('甲', '乙', '一次就好'), 'test')
    assert len(relay.claim('乙', 'test')) == 1
    assert relay.claim('乙', 'test') == []


def test_resubmitting_the_same_message_does_not_create_a_second_one(relay):
    sent = Envelope.new('甲', '乙', '重送')
    relay.submit(sent, 'test')
    relay.claim('乙', 'test')
    assert relay.submit(sent, 'test') is State.DELIVERED
    assert relay.claim('乙', 'test') == []


def test_reusing_a_message_id_for_different_content_is_refused(relay):
    sent = Envelope.new('甲', '乙', '原文')
    relay.submit(sent, 'test')
    forged = Envelope(sent.message_id, sent.sender, sent.recipient, sent.created_at, sent.kind, b'changed')
    with pytest.raises(Conflict):
        relay.submit(forged, 'test')


def test_state_never_moves_backwards(relay):
    sent = Envelope.new('甲', '乙', '你好')
    relay.submit(sent, 'test')
    with pytest.raises(Conflict):
        relay.ack('乙', sent.message_id, 'test')
    relay.claim('乙', 'test')
    relay.ack('乙', sent.message_id, 'test')
    assert relay.ack('乙', sent.message_id, 'test') is State.ACKED
    assert states(relay, sent) == ['queued', 'delivered', 'acked']


def test_only_the_recipient_can_acknowledge(relay):
    sent = Envelope.new('甲', '乙', '你好')
    relay.submit(sent, 'test')
    relay.claim('乙', 'test')
    with pytest.raises(NotFound):
        relay.ack('丙', sent.message_id, 'test')


def test_queue_survives_a_restart(tmp_path):
    path = tmp_path / 'portal.sqlite3'
    first = Relay(path)
    sent = Envelope.new('甲', '乙', '重啟前送出')
    first.submit(sent, 'test')
    first.close()
    second = Relay(path)
    assert second.claim('乙', 'test') == [sent]
    second.close()


def test_another_link_adapter_works_without_changing_the_relay(relay):
    link = FakeLink(relay)
    sent = Envelope.new('基地-甲', '基地-乙', '需要協助')
    link.deliver_packet(sent.to_compact())
    [packet] = link.collect_packets('基地-乙')
    assert Envelope.from_compact(packet) == sent
    link.acknowledge('基地-乙', packet)
    assert {item['link'] for item in relay.transmissions()} == {'fake'}
    assert relay.status(sent.message_id)['state'] == 'acked'
