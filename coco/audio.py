"""Clocked DAC/single-bit audio, integer-phase resampling and host playback."""
from array import array
from collections import deque
import math
import sys
import wave
from .clock import MASTER_HZ

SAMPLE_RATE = 48_000


def output_level(memory):
    p0, p1 = memory.pia0, memory.pia1
    mux = ((p0.control[0] >> 3) & 1) | ((p0.control[1] >> 2) & 2)
    dac = ((p1.data[0] & p1.ddr[0]) >> 2) if mux == 0 and p1.control[1] & 8 else 0
    bit = bool(p1.data[1] & p1.ddr[1] & 2)
    return dac * 256 + (8192 if bit else 0)


class Audio:
    def __init__(self, sample_rate=SAMPLE_RATE):
        self.rate = sample_rate
        self.now = 0
        self.level = 0
        self.phase = 0
        self.area = 0
        self.samples = array('h')
        self.total_samples = 0
        self.previous_input = self.previous_output = 0.0
        # AC coupling, ~5 Hz corner; avoid imposing a game-specific audio filter.
        self.hp = math.exp(-2 * math.pi * 5 / sample_rate)

    def advance(self, when):
        units = (when - self.now) * self.rate
        if units < 0:
            raise ValueError('Audio time cannot move backwards')
        self.now = when
        while units:
            amount = min(units, MASTER_HZ - self.phase)
            self.area += self.level * amount
            self.phase += amount
            units -= amount
            if self.phase == MASTER_HZ:
                value = self.area / MASTER_HZ
                filtered = self.hp * (self.previous_output + value - self.previous_input)
                self.previous_input, self.previous_output = value, filtered
                self.samples.append(max(-32768, min(32767, round(filtered))))
                self.total_samples += 1
                self.phase = self.area = 0

    def set_level(self, when, level):
        if level != self.level:
            self.advance(when)
            self.level = level

    def drain(self, when):
        self.advance(when)
        data, self.samples = self.samples, array('h')
        if sys.byteorder != 'little':
            data.byteswap()
        return data.tobytes()


class WaveRecorder:
    def __init__(self, path):
        self.file = wave.open(str(path), 'wb')
        self.file.setparams((1, 2, SAMPLE_RATE, 0, 'NONE', 'not compressed'))

    def write(self, data):
        self.file.writeframesraw(data)

    def close(self):
        self.file.close()


class Playback:
    """Bounded, frame-independent SDL mixer queue with a short prebuffer."""
    def __init__(self, pygame, volume=0.35):
        self.pg = pygame
        pygame.mixer.init(frequency=SAMPLE_RATE, size=-16, channels=1, buffer=512,
                          allowedchanges=0)
        self.channel = pygame.mixer.Channel(0)
        self.channel.set_volume(volume)
        self.pending = bytearray()
        self.chunks = deque()
        self.started = False
        self.underruns = self.overruns = 0
        # Two 2048-sample blocks absorb frame scheduling jitter on CoreAudio.
        self.chunk_bytes = 2048 * 2

    def clear(self):
        self.channel.stop()
        self.pending.clear()
        self.chunks.clear()
        self.started = False

    def feed(self, data):
        self.pending.extend(data)
        while len(self.pending) >= self.chunk_bytes:
            block = bytes(self.pending[:self.chunk_bytes])
            del self.pending[:self.chunk_bytes]
            self.chunks.append(self.pg.mixer.Sound(buffer=block))
        if len(self.chunks) > 6:
            self.overruns += 1
            while len(self.chunks) > 3:
                self.chunks.popleft()
        # SDL marks a channel idle before pygame's completion callback acquires
        # the GIL and starts its queued sound. get_sound() stays populated during
        # that transition; treating get_busy()==False as an underrun drops audio.
        if self.channel.get_sound() is None:
            if self.started:
                self.underruns += 1
                self.started = False
            if len(self.chunks) >= 2:
                self.channel.play(self.chunks.popleft())
                self.started = True
        if (self.started and self.channel.get_busy()
                and self.channel.get_queue() is None and self.chunks):
            self.channel.queue(self.chunks.popleft())
