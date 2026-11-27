"""A simulated stretch of air between LoRa links, for tests and offline runs. Nothing is transmitted.

`LossyAir` follows scout-lora-qa's E0 harness: seeded loss, duplication and reordering. It is not a
radio model; it only exercises the protocol.
"""
from .lora_link import FRAME_MAX_BYTES


class LossyAir:
    def __init__(self, rng, loss=0.0, duplicate=0.0, reorder=0.0):
        self.rng, self.loss, self.duplicate, self.reorder = rng, loss, duplicate, reorder

    def carry(self, packets):
        delivered = []
        for packet in packets:
            if self.rng.random() < self.loss:
                continue
            delivered.append(packet)
            if self.rng.random() < self.duplicate:
                delivered.append(packet)
        if len(delivered) > 1 and self.rng.random() < self.reorder:
            self.rng.shuffle(delivered)
        return delivered


class Channel:
    """Links sharing a stretch of air. With `neighbors` (name -> names), each hears only its neighbors."""

    def __init__(self, links, air, neighbors=None):
        self.links = list(links)
        self.air = air
        self.neighbors = neighbors
        self.now = 0
        self.on_air = []

    def step(self):
        self.now += 1
        for sender in self.links:
            frames = sender.poll(self.now)
            for frame in frames:
                if len(frame) > FRAME_MAX_BYTES:
                    raise AssertionError(f'{sender.name} built a {len(frame)}-byte frame')
                self.on_air.append((self.now, sender.name, frame))
            for receiver in self.links:
                if receiver is not sender and (self.neighbors is None
                                               or receiver.name in self.neighbors.get(sender.name, ())):
                    for frame in self.air.carry(frames):
                        receiver.receive(frame, self.now)

    def run(self, ticks):
        for _ in range(ticks):
            self.step()

    def run_until_idle(self, limit=10_000):
        for _ in range(limit):
            if not any(link.pending() for link in self.links):
                return self.now
            self.step()
        raise AssertionError(f'links still busy after {limit} ticks')
