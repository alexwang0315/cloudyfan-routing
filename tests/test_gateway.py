import hashlib
import itertools
import os
import random
import subprocess
import sys
import time

import pytest
from support import clean_env, free_port, start_server

from cloudyfami_portal import DeliveryPolicy, Envelope, Relay
from cloudyfami_portal.gateway import Base, BaseConfig, HttpUplink
from cloudyfami_portal.http_link import HttpLink, PortalClient
from cloudyfami_portal.lora_link import LoRaLink
from cloudyfami_portal.lora_sim import Channel, LossyAir
from cloudyfami_portal.transfer import Manifest, Transfers


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class Net:
    """Bases, an optional cloud and a LoRa mesh, all advanced one tick (one second) at a time."""

    def __init__(self, tmp_path, *, bases, edges, homes, uplinks=(), max_hops=3, policy=None, air=None):
        self.clock = Clock()
        self.policy = policy or DeliveryPolicy(ack_timeout=30, max_attempts=50)
        self.cloud = None
        if uplinks:
            self.cloud = Relay(tmp_path / 'cloud.sqlite3', clock=self.clock)
            self.cloud_transfers = Transfers(self.cloud, tmp_path / 'cloud.spool')
            self.port = free_port()
            self.cloud_link = HttpLink(self.cloud, port=self.port, transfers=self.cloud_transfers).start()
        neighbors = {name: set() for name in bases}
        for a, b in edges:
            neighbors[a].add(b)
            neighbors[b].add(a)
        self.bases = {}
        for name in bases:
            relay = Relay(tmp_path / f'{name}.sqlite3', clock=self.clock, policy=self.policy)
            transfers = Transfers(relay, tmp_path / f'{name}.spool')
            uplink = HttpUplink(f'http://127.0.0.1:{self.port}', timeout=2) if name in uplinks else None
            self.bases[name] = Base(BaseConfig(name, homes, max_hops), relay, transfers, uplink=uplink,
                                    lora=LoRaLink(name))
        self.channel = Channel([base.lora for base in self.bases.values()],
                               air or LossyAir(random.Random(0)), neighbors)

    def tick(self):
        self.clock.now += 1
        self.channel.step()
        for base in self.bases.values():
            base.step(self.channel.now)

    def run(self, ticks):
        for _ in range(ticks):
            self.tick()

    def run_until(self, condition, limit=600):
        for _ in range(limit):
            if condition():
                return
            self.tick()
        raise AssertionError('condition not reached')

    def send(self, base, sender, recipient, text):
        envelope = Envelope.new(sender, recipient, text)
        self.bases[base].relay.submit(envelope, 'http')
        return envelope

    def take(self, base, client):
        relay = self.bases[base].relay
        taken = relay.claim(client, 'http', limit=100)
        for envelope in taken:
            relay.ack(client, envelope.message_id, 'http')
        return taken

    def state(self, base, envelope):
        return self.bases[base].relay.status(envelope.message_id)['state']

    def lora_frames(self, envelope):
        return [frame for _, _, frame in self.channel.on_air if frame[0] == 1 and frame[1:7] == envelope.message_id[:6]]

    def events(self, envelope):
        return [(entry['base'], entry['event'], entry['detail']) for base in self.bases.values()
                for entry in base.log if entry['id'] == envelope.id]

    def cut(self):
        self.cloud_link.stop()

    def restore(self):
        self.cloud_link = HttpLink(self.cloud, port=self.port, transfers=self.cloud_transfers).start()

    def close(self):
        if self.cloud:
            self.cloud_link.stop()
            self.cloud.close()
        for base in self.bases.values():
            base.relay.close()


@pytest.fixture
def nets():
    made = []

    def make(*args, **kwargs):
        made.append(Net(*args, **kwargs))
        return made[-1]

    yield make
    for net in made:
        net.close()


def test_three_bases_in_a_line_carry_a_message_and_its_receipt_over_lora(tmp_path, nets):
    net = nets(tmp_path, bases=['甲基地', '乙基地', '丙基地'], edges=[('甲基地', '乙基地'), ('乙基地', '丙基地')],
               homes={'alice': '甲基地', 'bob': '丙基地'})
    sent = net.send('甲基地', 'alice', 'bob', '經乙基地轉送')
    received = []
    net.run_until(lambda: received.extend(net.take('丙基地', 'bob')) or received)
    assert received == [sent]
    net.run_until(lambda: net.state('甲基地', sent) == 'acked')
    events = net.events(sent)
    assert ('甲基地', 'forwarded', 'lora, 3 hop(s) left') in events
    assert ('乙基地', 'relaying', 'lora') in events and ('乙基地', 'forwarded', 'lora, 2 hop(s) left') in events
    assert ('丙基地', 'received', 'lora') in events
    history = net.bases['甲基地'].relay.status(sent.message_id)['history']
    assert [(step['state'], step['link']) for step in history] == [
        ('queued', 'http'), ('delivered', 'forward'), ('acked', 'lora')]


def test_internet_first_lora_during_the_outage_and_no_repeats_after_recovery(tmp_path, nets):
    net = nets(tmp_path, bases=['甲基地', '乙基地', '丙基地'], edges=[('甲基地', '乙基地'), ('乙基地', '丙基地')],
               homes={'alice': '甲基地', 'bob': '丙基地'}, uplinks=['甲基地', '丙基地'])
    received = []

    def bob_has(count):
        received.extend(net.take('丙基地', 'bob'))
        return len(received) >= count

    before = net.send('甲基地', 'alice', 'bob', '網路正常時送出')
    net.run_until(lambda: bob_has(1))
    net.run_until(lambda: net.state('甲基地', before) == 'acked')
    assert net.lora_frames(before) == [] and net.cloud.exists(before.message_id)

    net.cut()
    net.run_until(lambda: not net.bases['甲基地'].online and not net.bases['丙基地'].online)
    during = net.send('甲基地', 'alice', 'bob', '斷網時送出')
    net.run_until(lambda: bob_has(2))
    net.run_until(lambda: net.state('甲基地', during) == 'acked')
    assert net.lora_frames(during) and not net.cloud.exists(during.message_id)

    net.restore()
    net.run_until(lambda: net.bases['甲基地'].online and net.bases['丙基地'].online)
    net.run(90)
    bob_has(3)
    assert received == [before, during]
    assert not net.cloud.exists(during.message_id)


def test_a_message_no_lora_path_could_carry_goes_by_internet_once_it_returns(tmp_path, nets):
    net = nets(tmp_path, bases=['甲基地', '丁基地'], edges=[], homes={'alice': '甲基地', 'dave': '丁基地'},
               uplinks=['甲基地', '丁基地'])
    net.cut()
    net.run_until(lambda: not net.bases['甲基地'].online)
    stranded = net.send('甲基地', 'alice', 'dave', '斷網時沒有路')
    net.run(40)
    assert net.take('丁基地', 'dave') == []
    net.restore()
    received = []
    net.run_until(lambda: received.extend(net.take('丁基地', 'dave')) or received)
    net.run_until(lambda: net.state('甲基地', stranded) == 'acked')
    net.run(60)
    received.extend(net.take('丁基地', 'dave'))
    assert received == [stranded]


def test_a_file_queued_during_the_outage_crosses_by_internet_with_the_same_hash(tmp_path, nets):
    net = nets(tmp_path, bases=['甲基地', '乙基地', '丙基地'], edges=[('甲基地', '乙基地'), ('乙基地', '丙基地')],
               homes={'alice': '甲基地', 'bob': '丙基地'}, uplinks=['甲基地', '丙基地'])
    source = tmp_path / '錄製.bin'
    source.write_bytes(os.urandom(3 * 1024 * 1024 + 123))
    net.cut()
    net.run_until(lambda: not net.bases['甲基地'].online)

    origin = net.bases['甲基地'].transfers
    manifest = Manifest.from_file(source, 'alice', 'bob')
    origin.create(manifest, 'http')
    with open(source, 'rb') as reader:
        for index in range(manifest.chunk_count):
            reader.seek(index * manifest.chunk_size)
            origin.put_chunk(manifest.transfer_id, index, reader.read(manifest.chunk_length(index)), 'http')
    net.run(30)
    assert net.lora_frames(manifest.envelope()) == [] and net.take('丙基地', 'bob') == []

    net.restore()
    arrived = []
    net.run_until(lambda: arrived.extend(net.bases['丙基地'].relay.claim('bob', 'http')) or arrived)
    [envelope] = arrived
    target = net.bases['丙基地'].transfers
    data = b''.join(target.get_chunk(manifest.transfer_id, i) for i in range(manifest.chunk_count))
    assert hashlib.sha256(data).hexdigest() == manifest.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    net.bases['丙基地'].relay.ack('bob', envelope.message_id, 'http')
    net.run_until(lambda: net.state('甲基地', manifest.envelope()) == 'acked')
    assert origin.record(manifest.transfer_id)['state'] == 'done'


def test_a_message_past_its_hop_limit_is_dropped_and_forwarding_stops(tmp_path, nets):
    names = ['甲基地', '乙基地', '丙基地', '丁基地']
    net = nets(tmp_path, bases=names, edges=list(itertools.pairwise(names)),
               homes={'alice': '甲基地', 'dan': '丁基地'}, max_hops=2,
               policy=DeliveryPolicy(ack_timeout=30, max_attempts=3))
    sent = net.send('甲基地', 'alice', 'dan', '太遠了')
    net.run(400)
    assert net.take('丁基地', 'dan') == []
    assert ('丙基地', 'expired', 'hop limit reached at 丙基地; not forwarded') in net.events(sent)
    assert all(tick < net.channel.now - 100 for tick, _, frame in net.channel.on_air if frame[0] == 1)
    assert net.state('甲基地', sent) == 'failed'


def test_a_message_arriving_by_two_paths_is_delivered_once(tmp_path, nets):
    net = nets(tmp_path, bases=['甲基地', '乙基地', '丙基地', '丁基地'],
               edges=[('甲基地', '乙基地'), ('甲基地', '丁基地'), ('乙基地', '丙基地'), ('丁基地', '丙基地')],
               homes={'alice': '甲基地', 'bob': '丙基地'})
    sent = net.send('甲基地', 'alice', 'bob', '兩條路')
    received = []
    net.run_until(lambda: received.extend(net.take('丙基地', 'bob')) or received)
    net.run(120)
    received.extend(net.take('丙基地', 'bob'))
    assert received == [sent]
    relayed_by = {base for base, event, _ in net.events(sent) if event == 'relaying'}
    assert relayed_by == {'乙基地', '丁基地'}


def test_a_lossy_mesh_still_gets_the_message_and_the_receipt_through(tmp_path, nets):
    net = nets(tmp_path, bases=['甲基地', '乙基地', '丙基地'], edges=[('甲基地', '乙基地'), ('乙基地', '丙基地')],
               homes={'alice': '甲基地', 'bob': '丙基地'},
               air=LossyAir(random.Random(3), loss=0.3, duplicate=0.2, reorder=0.5))
    sent = [net.send('甲基地', 'alice', 'bob', f'第 {n} 則') for n in range(3)]
    received = []
    net.run_until(lambda: received.extend(net.take('丙基地', 'bob')) or len(received) >= 3, limit=2000)
    net.run_until(lambda: all(net.state('甲基地', envelope) == 'acked' for envelope in sent), limit=2000)
    assert sorted(envelope.id for envelope in received) == sorted(envelope.id for envelope in sent)


class ScriptedUplink:
    def __init__(self, probes):
        self.probes = list(probes)

    def probe(self):
        return self.probes.pop(0)

    def pull(self, name):
        return []


def test_flapping_internet_does_not_flip_the_base_back_and_forth(tmp_path):
    relay = Relay(tmp_path / 'base.sqlite3')
    probes = [True, False, False, True, False, False, True,
              False, False, False,
              True, False, True,
              True, True]
    base = Base(BaseConfig('甲基地', {'alice': '甲基地'}), relay, Transfers(relay, tmp_path / 'spool'),
                uplink=ScriptedUplink(probes))
    modes = []
    for tick in range(len(probes)):
        base.step(tick)
        modes.append(base.online)
    assert modes == [True] * 9 + [False] * 4 + [True] * 2
    assert [entry['event'] for entry in base.log] == ['offline', 'online']
    relay.close()


def test_the_same_program_runs_as_cloud_or_as_base_by_configuration(tmp_path):
    cloud_port, base_port = free_port(), free_port()
    cloud = start_server(tmp_path / 'cloud.sqlite3', cloud_port)
    config = tmp_path / 'base.toml'
    config.write_text(f'name = "甲基地"\ncloud = "http://127.0.0.1:{cloud_port}"\ntick_seconds = 0.2\n'
                      '[homes]\nalice = "甲基地"\ndave = "cloud"\n', encoding='utf-8')
    base = subprocess.Popen(
        [sys.executable, '-B', '-m', 'cloudyfami_portal', 'serve', '--role', 'base', '--base-config', str(config),
         '--db', str(tmp_path / 'base.sqlite3'), '--port', str(base_port)],
        env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert '甲基地' in base.stdout.readline()
        alice = PortalClient(f'http://127.0.0.1:{base_port}', 'alice')
        dave = PortalClient(f'http://127.0.0.1:{cloud_port}', 'dave')
        sent = alice.send('dave', '從基地經雲端')
        [got] = dave.receive(wait=10)
        assert got == sent
        dave.ack(got)
        assert alice.status(sent.id, until=None)['history'][-1]['link'] == 'cloud'

        reply = dave.send('alice', '從雲端回基地')
        deadline = time.monotonic() + 10
        inbox = []
        while not inbox and time.monotonic() < deadline:
            inbox = alice.receive(wait=1)
        assert inbox == [reply]
        alice.ack(inbox[0])
    finally:
        base.kill()
        cloud.kill()
        base.wait()
        cloud.wait()
