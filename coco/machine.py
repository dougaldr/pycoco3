"""Instruction-timed NTSC machine, independent of the host window rate."""
from collections import deque
from .cpu6809 import CPU6809
from .memory import Memory, character_keys
from .video import Video

from .clock import Clock, TimedBus, MASTER_HZ, LINE_TICKS, LINES, FRAME_TICKS, FRAME_HZ
from .timing import GimeTiming
from .audio import Audio, output_level
from .disk import FDC


class Machine:
    def __init__(self, rom, ram_kb=128, trace=False, video=True, disk_rom=None,
                 drives=None, audio=True, gime_revision=1987, timing_trace=False, bus_trace=False):
        self.memory = Memory(rom, ram_kb, cart_rom=disk_rom)
        self.video = Video(self.memory) if video else None
        self.trace = deque(maxlen=64) if trace else None
        self.drives = list(drives or [None] * 4)
        if len(self.drives) > 4:
            raise ValueError('At most four drives are supported')
        self.drives += [None] * (4 - len(self.drives))
        self.audio_enabled = audio
        self.gime_revision = gime_revision
        self.timing_trace = timing_trace
        self.bus_trace = bus_trace
        self._setup()

    def _setup(self):
        self.clock = Clock()
        self.bus = TimedBus(self.memory, self.clock, self.bus_trace)
        self.cpu = CPU6809(self.bus)
        self.audio = Audio() if self.audio_enabled else None
        self.audio_frame = b''
        self.timing = GimeTiming(self.clock, self.memory, self.video,
                                 self.gime_revision, self.timing_trace)
        self.fdc = FDC(self.clock, self.drives, self._nmi) if self.memory.cart_rom else None
        self.memory.fdc = self.fdc
        self.memory.on_write = self._written
        self.memory.before_write = self._before_write
        self.frames = 0
        self.host_keys = set()
        self._typing = deque()
        self._type_phase = 0
        self._type_keys = set()
        self.bus.enabled = True

    @property
    def master_ticks(self):
        return self.clock.now

    @property
    def line(self):
        return self.timing.line

    def _nmi(self, asserted):
        if asserted:
            self.cpu.nmi = True  # latch rising edge; CPU consumes it once

    def _before_write(self, address, value):
        if self.video is not None:
            if address >= 0xFF00:
                visible = (0xFF20 <= address < 0xFF40 or 0xFF90 <= address <= 0xFFBF
                           or 0xFFC0 <= address <= 0xFFD3)
            else:
                physical, is_rom = self.memory.physical(address)
                visible = not is_rom and self.video.reads_address(physical, self.timing.line - 18)
            if visible:
                self.timing.flush()

    def _written(self, port, value):
        self.timing.register_write(port)
        if self.audio is not None and port < 0x40:
            self.audio.set_level(self.clock.now, output_level(self.memory))

    def reset(self, cold=False):
        self.memory.reset(cold)
        self._setup()
        if self.trace is not None:
            self.trace.clear()

    def mount_disk(self, drive, image):
        """Replace media between commands, retaining it across machine resets."""
        if not 0 <= drive < 4:
            raise ValueError('Drive must be 0 through 3')
        if self.fdc is None:
            raise ValueError('Load a Disk BASIC ROM first, then reset to enable disks.')
        if self.fdc.busy:
            raise ValueError('Disk controller is busy. Resume and wait for the command to finish.')
        old = self.drives[drive]
        if old is not None:
            old.flush()
        # Never mount the same host file twice with write access.
        if image is not None:
            for n, other in enumerate(self.drives):
                if n != drive and other is not None and other.path == image.path and (other.writable or image.writable):
                    raise ValueError('This image is already mounted with write access in another drive.')
        self.fdc.mount_media(drive, image)
        self.drives[drive] = image
        self.fdc.last_error = None

    def set_disk_writable(self, drive, enabled):
        from .disk import open_disk
        old = self.drives[drive]
        if old is None:
            raise ValueError('No disk is mounted in this drive.')
        if self.fdc.busy:
            raise ValueError('Wait for the disk command to finish before changing write protection.')
        old.flush()
        image = open_disk(old.path, writable=enabled)
        if enabled and not image.writable:
            raise ValueError(getattr(image, 'write_protect_reason',
                                     'This image has write protection set in its header.'))
        self.mount_disk(drive, image)

    def load_disk_controller(self, data):
        if self.fdc is not None and self.fdc.busy:
            raise ValueError('Wait for the disk command to finish before changing its ROM.')
        self.memory.cart_rom = bytes(data)
        # Disk BASIC hooks are installed by the system ROM's cold-start path.
        self.reset(cold=True)

    def queue_text(self, text):
        # Validate the entire string before changing the queue.
        sequence = []
        for char in text.replace('\r\n', '\n'):
            sequence.append(character_keys(char))
            if char in ('\r', '\n'):
                # Give BASIC time to execute a short command before the next line.
                sequence.extend([set() for _ in range(4)])
        self._typing.extend(sequence)

    def _keyboard_frame(self):
        # Matrix events, not BASIC buffer injection: 3 frames down / 3 frames up.
        if self._type_phase == 0:
            self._type_keys = self._typing.popleft() if self._typing else set()
        if self._type_phase == 3:
            self._type_keys = set()
        self._type_phase = (self._type_phase + 1) % 6
        self.memory.set_keys(self.host_keys | self._type_keys)

    def run_frame(self):
        target = (self.frames + 1) * FRAME_TICKS
        self._keyboard_frame()
        cpu, memory, bus, clock = self.cpu, self.memory, self.bus, self.clock
        while clock.now < target:
            if self.fdc is not None and self.fdc.halted:
                clock.advance(min(target, clock.next_event or target))
                continue
            cpu.irq, cpu.firq = memory.irq, memory.firq
            if self.trace is not None:
                self.trace.append(cpu.state())
            bus.begin()
            cycles = cpu.step()
            if bus.used > cycles:
                raise RuntimeError(f'CPU bus cycle overflow at PC=${cpu.pc:04X}: {bus.used}>{cycles}')
            bus.idle_until(cycles)
        self.frames += 1
        if self.audio is not None:
            self.audio_frame = self.audio.drain(clock.now)

    def text_screen(self):
        """Read the compatibility text screen for diagnostics without I/O side effects."""
        regs = self.memory.regs
        offset = ((regs[13] << 8 | regs[14]) & 0xE0FF) * 8
        offset += ((self.memory.sam >> 3) & 127) * 512
        data = bytes(self.memory.ram[(offset + i) & self.memory.mask] for i in range(512))
        return '\n'.join(''.join(chr((v & 63) + (64 if (v & 63) < 32 else 0))
                                  if v < 128 else '#' for v in data[y:y+32])
                         for y in range(0, 512, 32))
