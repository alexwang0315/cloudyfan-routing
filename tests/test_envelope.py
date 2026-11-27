import pytest

from cloudyfami_portal import COMPACT_MAX_BYTES, Envelope, EnvelopeError, Kind, TooLargeForCompact


def test_full_form_round_trips_with_matching_hash():
    envelope = Envelope.new('scout', 'mac', '收到了', clock=lambda: 1_800_000_000)
    assert Envelope.from_json(envelope.to_json()) == envelope


def test_full_form_rejects_a_body_that_does_not_match_its_hash():
    data = Envelope.new('scout', 'mac', 'hello').to_json()
    data['sha256'] = '0' * 64
    with pytest.raises(EnvelopeError):
        Envelope.from_json(data)


def test_short_message_fits_one_lora_packet_and_round_trips():
    envelope = Envelope.new('基地-甲', '基地-乙', '需要協助', Kind.SIGNAL)
    packet = envelope.to_compact()
    assert len(packet) <= COMPACT_MAX_BYTES
    assert Envelope.from_compact(packet) == envelope


def test_compact_and_full_forms_convert_into_each_other_without_change():
    envelope = Envelope.new('a', 'b', b'\x00\x01binary\xff', Kind.FILE)
    via_compact = Envelope.from_compact(envelope.to_compact())
    assert Envelope.from_json(via_compact.to_json()) == envelope


def test_message_too_large_for_one_packet_is_refused_not_truncated():
    with pytest.raises(TooLargeForCompact):
        Envelope.new('a', 'b', b'x' * COMPACT_MAX_BYTES).to_compact()


def test_corrupted_compact_packet_is_rejected():
    packet = bytearray(Envelope.new('a', 'b', 'hello').to_compact())
    packet[-1] ^= 0x01
    with pytest.raises(EnvelopeError):
        Envelope.from_compact(bytes(packet))


@pytest.mark.parametrize('name', ['', 'x' * 33, 'a/b', 'line\nbreak'])
def test_client_names_that_cannot_be_addressed_are_rejected(name):
    with pytest.raises(EnvelopeError):
        Envelope.new(name, 'b', 'hi')
