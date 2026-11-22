"""Deterministic, cancellable events on the CoCo master-clock timeline."""
import heapq
from collections import deque

MASTER_HZ = 14_318_180
LINE_TICKS = 912
LINES = 263  # NTSC GIME (the MC6847 has 262).
FRAME_TICKS = LINE_TICKS * LINES
FRAME_HZ = MASTER_HZ / FRAME_TICKS


class Clock:
    def __init__(self):
        self.now = 0
        self._events = []
        self._serial = 0
        self._generation = {}

    def at(self, when, callback, tag=None):
        if when < self.now:
            raise ValueError('Cannot schedule an event in the past')
        self._serial += 1
        generation = self._generation.get(tag, 0)
        heapq.heappush(self._events, (when, self._serial, tag, generation, callback))

    def after(self, ticks, callback, tag=None):
        self.at(self.now + ticks, callback, tag)

    def cancel(self, tag):
        self._generation[tag] = self._generation.get(tag, 0) + 1

    @property
    def next_event(self):
        while self._events:
            event = self._events[0]
            if event[2] is not None and self._generation.get(event[2], 0) != event[3]:
                heapq.heappop(self._events)
            else:
                return event[0]
        return None

    def advance(self, target):
        if target < self.now:
            raise ValueError('Clock cannot run backwards')
        while self._events and self._events[0][0] <= target:
            when, _, tag, generation, callback = heapq.heappop(self._events)
            if tag is not None and self._generation.get(tag, 0) != generation:
                continue
            self.now = when
            callback()
        self.now = target


class TimedBus:
    """One scheduled E cycle per access, including dummy reads and bus release."""
    def __init__(self, memory, clock, trace=False):
        self.memory, self.clock = memory, clock
        self.enabled = False
        self.used = 0
        self.events = deque(maxlen=100_000) if trace else None

    def begin(self):
        self.used = 0

    def idle_until(self, cycles):
        while self.enabled and cycles > self.used:
            self.internal()

    def _access(self, address):
        if self.enabled:
            self.used += 1
            self.clock.advance(self.clock.now + self.memory.cycle_ticks(address))

    def read8(self, address):
        self._access(address)
        value = self.memory.read8(address)
        if self.events is not None:
            self._record('read', address, value)
        return value

    def write8(self, address, value):
        self._access(address)
        self.memory.write8(address, value)
        if self.events is not None:
            self._record('write', address, value)

    def _record(self, kind, address, value):
        if self.enabled and self.events is not None:
            self.events.append((self.clock.now, kind, address, value))

    def dummy_read(self, address):
        self._access(address)
        value = self.memory.read8(address)
        if self.events is not None:
            self._record('dummy', address, value)
        return value

    def internal(self, count=1):
        for _ in range(count):
            self._access(0xFFFF)
            value = self.memory.read8(0xFFFF)
            if self.events is not None:
                self._record('internal', 0xFFFF, value)

    def wait_cycle(self, count=1):
        """SYNC/CWAI release the bus while waiting; do not invent memory reads."""
        for _ in range(count):
            if self.enabled:
                self.used += 1
                self.clock.advance(self.clock.now + self.memory.cycle_ticks(None))
                self._record('wait', None, None)
