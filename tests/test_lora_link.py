import random

import pytest

from cloudyfami_portal import DeliveryPolicy, Envelope, Kind, Relay
from cloudyfami_portal.lora_link import FRAME_MAX_BYTES, SEGMENT_BYTES, LinkPolicy, LoRaLink, RefusedOnLink
from cloudyfami_portal.lora_sim import Channel, LossyAir
from cloudyfami_portal.transfer import Manifest

DEFAULT = LinkPolicy()


class Endpoint:
    def __init__(self, name, policy=DEFAULT):
        self.received, self.delivered, self.failed = [], [], []
        self.link = LoRaLink(name, policy=policy, on_message=self.received.append,
                             on_delivered=self.delivered.append,
                             on_failed=lambda envelope, reason: self.failed.append((envelope, reason)))


def pair(policy=DEFAULT, **air):
    a, b = Endpoint('基地-甲', policy), Endpoint('基地-乙', policy)
    channel = Channel([a.link, b.link], LossyAir(random.Random(air.pop('seed', 1)), **air))
    return a, b, channel


def message_of(segments, kind=Kind.TEXT):
    overhead = len(Envelope.new('基地-甲', '乙', b'', kind).to_compact())
    return Envelope.new('基地-甲', '乙', b'x' * (segments * SEGMENT_BYTES - overhead - 10), kind)


def segments_on_air(channel, envelope):
    return [frame for _, _, frame in channel.on_air if frame[0] == 1 and frame[1:7] == envelope.message_id[:6]]


def test_a_single_packet_message_arrives_unchanged():
    a, b, channel = pair()
    sent = Envelope.new('基地-甲', '乙', '需要協助', Kind.SIGNAL)
    a.link.send(sent)
    channel.run_until_idle()
    assert b.received == [sent] and a.delivered == [sent]
    assert len(segments_on_air(channel, sent)) == 1


def test_a_three_segment_message_arrives_unchanged():
    a, b, channel = pair()
    sent = message_of(3)
    a.link.send(sent)
    channel.run_until_idle()
    assert b.received == [sent]
    assert len(segments_on_air(channel, sent)) == 3
    assert all(len(frame) <= FRAME_MAX_BYTES for _, _, frame in channel.on_air)


@pytest.mark.parametrize('seed', range(30))
def test_with_30_percent_loss_duplicates_and_reordering_a_message_arrives_once_or_fails_clearly(seed):
    a, b, channel = pair(LinkPolicy(max_rounds=8), loss=0.3, duplicate=0.2, reorder=0.5, seed=seed)
    sent = message_of(4)
    a.link.send(sent)
    channel.run_until_idle()
    if a.delivered:
        assert b.received == [sent] and a.failed == []
    else:
        assert len(b.received) <= 1 and b.received in ([], [sent])
        [(envelope, reason)] = a.failed
        assert envelope == sent and 'no receipt after 8 rounds' in reason
    assert all(envelope == sent for envelope in b.received)


def test_most_messages_get_through_30_percent_loss():
    delivered = 0
    for seed in range(30):
        a, _, channel = pair(LinkPolicy(max_rounds=8), loss=0.3, duplicate=0.2, reorder=0.5, seed=seed)
        a.link.send(message_of(4))
        channel.run_until_idle()
        delivered += bool(a.delivered)
    assert delivered >= 28


def test_only_missing_segments_are_sent_again():
    a, b, channel = pair()
    sent = message_of(5)
    dropped = []

    class DropSegmentTwoOnce(LossyAir):
        def carry(self, packets):
            kept = []
            for packet in packets:
                if packet[0] == 1 and packet[7] == 2 and not dropped:
                    dropped.append(packet)
                    continue
                kept.append(packet)
            return kept

    channel.air = DropSegmentTwoOnce(random.Random(0))
    a.link.send(sent)
    channel.run_until_idle()
    assert b.received == [sent]
    assert sorted(frame[7] for frame in segments_on_air(channel, sent)) == [0, 1, 2, 2, 3, 4]
    assert [entry['detail'] for entry in a.link.log if entry['event'] == 'retransmit'] == ['round 2: segments [2]']


def test_a_message_that_never_gets_through_fails_with_the_reason_logged():
    a, _, channel = pair(LinkPolicy(max_rounds=3), loss=1.0)
    sent = message_of(2)
    a.link.send(sent)
    channel.run_until_idle()
    [(envelope, reason)] = a.failed
    assert envelope == sent and reason == 'no receipt after 3 rounds; 2 of 2 segments missing'
    assert [entry['event'] for entry in a.link.log] == ['queued', 'retransmit', 'retransmit', 'failed']


def test_a_short_signal_goes_out_before_a_text_queued_earlier():
    a, b, channel = pair()
    text, signal = message_of(3), Envelope.new('基地-甲', '乙', '需要協助', Kind.SIGNAL)
    a.link.send(text)
    a.link.send(signal)
    channel.run_until_idle()
    first = next(frame for _, sender, frame in channel.on_air if sender == '基地-甲')
    assert first[1:7] == signal.message_id[:6]
    assert b.received == [signal, text]


def test_a_file_transfer_and_an_oversized_message_are_refused_with_a_reason():
    a, _, _ = pair(LinkPolicy(max_message_bytes=600))
    manifest = Manifest(b'\1' * 16, '基地-甲', '乙', 0, 10, 'a' * 64, 64 * 1024, ('b' * 64,), 'x.bin')
    with pytest.raises(RefusedOnLink, match='file transfers wait for the internet'):
        a.link.send(manifest.envelope())
    big = Envelope.new('基地-甲', '乙', b'x' * 700)
    with pytest.raises(RefusedOnLink, match='exceeds the LoRa limit of 600 bytes'):
        a.link.send(big)
    assert [entry['event'] for entry in a.link.log] == ['refused', 'refused']
    assert a.link.pending() == 0


def test_a_duplicate_of_a_finished_message_is_not_delivered_twice():
    a, b, channel = pair(duplicate=1.0, reorder=1.0)
    sent = message_of(3)
    a.link.send(sent)
    channel.run_until_idle()
    channel.run(20)
    assert b.received == [sent]


def test_the_relay_carries_messages_over_lora_without_any_change(tmp_path):
    base_a = Relay(tmp_path / 'a.sqlite3', policy=DeliveryPolicy(ack_timeout=30))
    base_b = Relay(tmp_path / 'b.sqlite3')
    try:
        link_a = LoRaLink('基地-甲', on_delivered=lambda envelope: base_a.ack('乙', envelope.message_id, 'lora'))
        link_b = LoRaLink('基地-乙', on_message=lambda envelope: base_b.submit(envelope, 'lora'))
        channel = Channel([link_a, link_b], LossyAir(random.Random(7), loss=0.3, duplicate=0.2, reorder=0.5))
        sent = [Envelope.new('甲', '乙', f'第 {n} 則，' + '內容' * 60) for n in range(3)]
        for envelope in sent:
            base_a.submit(envelope, 'http')
        for envelope in base_a.claim('乙', 'lora'):
            link_a.send(envelope)
        channel.run_until_idle()
        arrived = base_b.claim('乙', 'http')
        assert sorted(arrived, key=lambda envelope: envelope.id) == sorted(sent, key=lambda envelope: envelope.id)
        assert all(base_a.status(envelope.message_id)['state'] == 'acked' for envelope in sent)
        assert {item['link'] for item in base_b.transmissions() if item['to'] == 'queued'} == {'lora'}
    finally:
        base_a.close()
        base_b.close()


def test_link_policy_rejects_limits_that_cannot_work():
    with pytest.raises(ValueError):
        LinkPolicy(max_message_bytes=SEGMENT_BYTES * 255 + 1)
    with pytest.raises(ValueError):
        LinkPolicy(max_rounds=0)
