"""Sector-image storage and clocked CoCo WD1793/FD-502 interface.

GPL-3.0-or-later. References: VCC FD502/wd1793.cpp, WD179x command descriptions.
DMK support preserves track-level protection; sector images cannot preserve it.
"""
from pathlib import Path
import binascii
import hashlib
import os
import tempfile
import zipfile
from .clock import MASTER_HZ

REVOLUTION = MASTER_HZ // 5  # 300 RPM
BUSY, DRQ, LOST, CRC, RNF, WRITE_FAULT, WP, NOT_READY = 1, 2, 4, 8, 16, 32, 64, 128


def load_disk_rom(path):
    path = Path(path).expanduser()
    if path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path) as archive:
            data = archive.read('disk11.rom')
    else:
        data = path.read_bytes()
    if len(data) not in (8192, 16384):
        raise ValueError('Disk ROM must be 8 or 16 KB (or a ZIP containing disk11.rom)')
    return bytes(data)


def open_disk(path, writable=False):
    from .dmk import DMKImage
    from .flux import HFEImage, SCPImage
    path = Path(path).expanduser()
    data = path.read_bytes()
    if path.suffix.lower() == '.hfe' or data[:8] in (b'HXCPICFE', b'HXCHFEV3'):
        return HFEImage(path, writable)
    if path.suffix.lower() == '.scp' or data[:3] == b'SCP':
        return SCPImage(path, writable)
    if path.suffix.lower() == '.dmk' or DMKImage.identifies(data):
        return DMKImage(path, writable)
    return DiskImage(path, writable)


class DiskImage:
    def __init__(self, path, writable=False):
        self.path = Path(path).expanduser().resolve()
        self.writable = writable
        self.data = bytearray(self.path.read_bytes())
        self._digest = hashlib.sha256(self.data).digest()
        self.dirty = False
        self.sides, self.sectors, self.size, self.first = 1, 18, 256, 1
        if self.data[:2] == b'dk':
            self.header = int.from_bytes(self.data[2:4], 'little')
            if not 12 <= self.header <= 256 or len(self.data) < self.header:
                raise ValueError('Invalid VDK header')
            self.tracks, self.sides = self.data[8], self.data[9]
            self.format = 'VDK'
        else:
            self.format = 'JVC'
            self.header = len(self.data) % 256
            if self.path.suffix.lower() == '.dmk' or self.header > 5:
                raise ValueError('Invalid sector image; use open_disk() for DMK track images')
            if self.header:
                self.sectors = self.data[0]
            if self.header >= 2:
                self.sides = self.data[1]
            if self.header >= 3:
                code = self.data[2]
                if code > 3:
                    raise ValueError('Unsupported JVC sector size')
                self.size = 128 << code
            if self.header >= 4:
                self.first = self.data[3]
            if self.header == 5 and self.data[4]:
                raise ValueError('JVC per-sector attributes are not supported')
            if not self.header and len(self.data) in (322560, 368640, 737280):
                self.sides = 2
            unit = self.sides * self.sectors * self.size
            self.tracks = (len(self.data) - self.header) // unit if unit else 0
        if (self.sides not in (1, 2) or not 1 <= self.sectors <= 64
                or not 1 <= self.tracks <= 86
                or self.first + self.sectors > 256
                or len(self.data) - self.header != self.tracks * self.sides * self.sectors * self.size):
            raise ValueError('Invalid or ambiguous disk geometry')

    @classmethod
    def create(cls, path):
        """Create a new formatted 35-track RSDOS disk; never overwrite a file."""
        with Path(path).open('xb') as stream:
            stream.write(bytes([255]) * (35 * 18 * 256))
        return cls(path, writable=True)

    def offset(self, track, side, sector):
        if not (0 <= track < self.tracks and 0 <= side < self.sides
                and self.first <= sector < self.first + self.sectors):
            raise ValueError('Sector not found')
        return self.header + ((track * self.sides + side) * self.sectors + sector - self.first) * self.size

    def read_sector(self, track, side, sector):
        offset = self.offset(track, side, sector)
        return bytes(self.data[offset:offset + self.size])

    def write_sector(self, track, side, sector, data):
        if not self.writable:
            raise PermissionError('Disk image is write-protected')
        if len(data) != self.size:
            raise ValueError('Incomplete sector write')
        offset = self.offset(track, side, sector)
        self.data[offset:offset + self.size] = data
        self.dirty = True

    def flush(self):
        if not self.dirty:
            return
        if not self.writable:
            raise PermissionError('Disk image is write-protected')
        if hashlib.sha256(self.path.read_bytes()).digest() != self._digest:
            raise OSError(f'Disk changed outside the emulator: {self.path}')
        descriptor, name = tempfile.mkstemp(prefix='.coco-disk-', dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(self.data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(name, self.path.stat().st_mode & 0o777)
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        self._digest = hashlib.sha256(self.data).digest()
        self.dirty = False


class FDC:
    def __init__(self, clock, drives=None, nmi_callback=None):
        self.clock = clock
        self.drives = list(drives or [None] * 4)
        self.drives += [None] * (4 - len(self.drives))
        self.nmi_callback = nmi_callback or (lambda level: None)
        self.head = [0] * 4
        self.last_error = None
        self.reset()

    def reset(self):
        self.clock.cancel('fdc')
        self.control = self.track = self.data = 0
        self.sector = 1
        self.command = self.status = 0
        self.busy = self.drq = self.intrq = self._nmi = False
        self.direction = 1
        self.type_one = True
        self._buffer = bytearray()
        self._index = 0
        self._write = False
        self._operation = None
        self.transfer_bytes = self.completed_commands = 0
        self._phase = 0
        self._force = 0
        self._record = None
        self._read_error = 0
        self._crc_cell = False
        self._timed_bytes = ()
        self._timed_end = 0
        self._lines()

    @property
    def drive_number(self):
        if self.control & 4: return 2
        if self.control & 2: return 1
        if self.control & 1: return 0
        return 3 if self.control & 64 else 0

    @property
    def image(self):
        return self.drives[self.drive_number]

    @property
    def side(self):
        return int(bool(self.control & 64) and self.drive_number != 3)

    @property
    def ready(self):
        return self.image is not None and bool(self.control & 8)

    @property
    def halted(self):
        return bool(self.control & 128) and not self.drq and not self.intrq

    def _lines(self):
        if self.intrq:
            self.control &= 127
        nmi = bool(self.intrq and self.control & 32)
        if nmi != self._nmi:
            self._nmi = nmi
            self.nmi_callback(nmi)

    def _ready_changed(self, old_ready):
        if (self._force & 1 and not old_ready and self.ready) or (self._force & 2 and old_ready and not self.ready):
            self.intrq = True
            self._lines()

    def mount_media(self, drive, image):
        if self.busy:
            raise ValueError('Disk controller is busy')
        old_ready = self.ready
        self.drives[drive] = image
        self._ready_changed(old_ready)

    def _schedule(self, ticks, callback):
        self.clock.after(max(1, ticks), callback, 'fdc')

    def _byte_ticks(self):
        # 8 bits / 250 kbit/s (DD), or 125 kbit/s (SD), without rounding drift.
        rate = 250_000 if self.control & 32 else 125_000
        self._phase += MASTER_HZ * 8
        ticks, self._phase = divmod(self._phase, rate)
        return ticks

    def _rotation(self):
        if self.image is not None and getattr(self.image, 'timed_track', False):
            from .flux import ticks
            _, start, end = self.image.revolution(self.head[self.drive_number], self.side, self.clock.now)
            return ticks(start), ticks(end)
        start = self.clock.now//REVOLUTION*REVOLUTION
        return start, start+REVOLUTION

    def _search_delay(self):
        # WD179x counts index edges while looking for a matching ID.
        return 5*REVOLUTION-self.clock.now % REVOLUTION

    def read(self, port):
        index = port & 3
        if not 0x48 <= port <= 0x4F:
            return 0
        if index == 0:
            value = self.status | (BUSY if self.busy else 0)
            if self.type_one:
                if self.clock.now-self._rotation()[0] < MASTER_HZ // 250: value |= 2
                if self.head[self.drive_number] == 0: value |= 4
                if self.image and not self.image.writable: value |= WP
                if self.command & 8: value |= 32
            elif self.drq:
                value |= DRQ
            if not self.ready: value |= NOT_READY
            self.intrq = False
            self._lines()
            return value
        if index == 1: return self.track
        if index == 2: return self.sector
        if self.drq and not self._write:
            self.drq = False
            self._lines()
        return self.data

    def write(self, port, value):
        value &= 255
        if 0x40 <= port <= 0x47:
            old_ready = self.ready
            self.control = value
            self._lines()
            if self.busy and not self.ready:
                self._finish(NOT_READY)
            self._ready_changed(old_ready)
        elif port & 3 == 0:
            self._command(value)
        elif port & 3 == 1:
            if not self.busy: self.track = value
        elif port & 3 == 2:
            if not self.busy: self.sector = value
        else:
            self.data = value
            if self.drq and self._write:
                self._buffer.append(value)
                if self._operation == 15:
                    cells = self._encoder.feed(value)
                    self._index += cells
                    self._crc_cell = cells == 2
                else:
                    self._index += 1
                self.transfer_bytes += 1
                self.drq = False
                self._lines()

    def _finish(self, error=0):
        self.clock.cancel('fdc')
        self.busy = self.drq = False
        self.status |= error
        self.intrq = True
        self.completed_commands += 1
        self._lines()

    def _command(self, command):
        if self.busy and command >> 4 != 13:
            return
        self.clock.cancel('fdc')
        self.command = command
        self.intrq = self.drq = False
        self.status = 0
        self._force = 0
        self._lines()
        kind = command >> 4
        if kind == 13:
            self.busy = False
            self._operation = None
            self._force = command & 15
            if command & 8:
                self.intrq = True
            elif command & 4:
                self._schedule(self._rotation()[1]-self.clock.now, self._index_interrupt)
            self._lines()
            return
        self.busy = True
        self.type_one = kind < 8
        self._buffer = bytearray()
        self._index = self._phase = 0
        self._record = None
        self._read_error = 0
        self._crc_cell = False
        self._timed_bytes = ()
        self._timed_end = 0
        self._operation = kind
        self._write = kind in (10, 11, 15)
        if not self.ready:
            self._schedule(REVOLUTION * 5, lambda: self._finish(NOT_READY | RNF))
            return
        if kind < 8:
            current = self.head[self.drive_number]
            if kind == 0: target = 0
            elif kind == 1: target = self.data
            else:
                if kind in (4, 5): self.direction = 1
                if kind in (6, 7): self.direction = -1
                target = max(0, min(255, current + self.direction))
            delay = max(1, abs(target-current)) * (6, 12, 20, 30)[command & 3] * MASTER_HZ // 1000
            def seek_done():
                self.head[self.drive_number] = target
                if kind < 2 or kind & 1:
                    self.track = target
                if command & 4 and getattr(self.image, 'timed_track', False):
                    self._verify_flux()
                    return
                if command & 4 and self.image.format == 'DMK':
                    try:
                        records = self.image.records(target, self.side)
                    except ValueError:
                        records = ()
                    valid = [r for r in records if r.ident[0] == self.track
                             and r.id_crc_ok and r.double == bool(self.control & 32)]
                    if valid:
                        wait = min((r.position-self.clock.now) % REVOLUTION for r in valid)
                        self._schedule(max(1,wait), self._finish)
                    else:
                        self._schedule(self._search_delay(), lambda: self._finish(RNF))
                    return
                error = RNF if command & 4 and (target >= self.image.tracks or self.track != target) else 0
                self._finish(error)
            self._schedule(delay, seek_done)
            return
        if self._write and not self.image.writable:
            self._schedule(32, lambda: self._finish(WP))
            return
        self._active_drive = self.drive_number
        self._active_side = self.side
        self._active_track = self.head[self.drive_number]
        self._active_sector = self.sector
        self._active_image = self.image
        if command & 4:
            self._schedule(MASTER_HZ*15//1000, self._begin_transfer)
        else:
            self._begin_transfer()

    def _begin_transfer(self):
        kind = self._operation
        if kind == 12 and self.image.format != 'DMK' and not getattr(self.image, 'timed_track', False):
            # READ ADDRESS finds the next physical ID, ignoring track/sector registers.
            position = self.clock.now % REVOLUTION
            next_id = (position * self.image.sectors + REVOLUTION - 1) // REVOLUTION
            self._active_sector = self.image.first + next_id % self.image.sectors
        if kind in (14, 15):
            self._schedule(self._rotation()[1]-self.clock.now, self._start_track)
        else:
            self._find_sector()

    def _index_interrupt(self):
        self.intrq = True
        self._lines()
        if self._force & 4:
            self._schedule(self._rotation()[1]-self.clock.now, self._index_interrupt)

    def _find_sector(self):
        image = self._active_image
        self._phase = 0
        if getattr(image, 'timed_track', False):
            self._find_flux_record()
            return
        if image.format == 'DMK':
            self._find_record()
            return
        sector = self._active_sector
        try:
            image.offset(self._active_track, self._active_side, sector)
            if self._operation != 12 and self.track != self._active_track:
                raise ValueError('Track ID mismatch')
            if self._operation != 12 and self.command & 2 and self._active_side != ((self.command >> 3) & 1):
                raise ValueError('Side ID mismatch')
        except ValueError:
            self._schedule(self._search_delay(), lambda: self._finish(RNF))
            return
        # Uniformly spaced sector IDs; raw images have no recorded angular layout.
        angular = (sector - image.first) * REVOLUTION // image.sectors
        wait = (angular - self.clock.now % REVOLUTION) % REVOLUTION
        if not self._write:
            wait += sum(self._byte_ticks() for _ in range(2 if self._operation == 12 else 1))
        self._schedule(max(wait, 32), self._start_sector)

    def _flux_candidates(self, image, track, side):
        from .flux import ticks
        n, start, end = image.revolution(track, side, self.clock.now)
        # Five index edges bound the search; don't repeatedly reuse a frozen
        # revolution when weak IDs or captured sectors change on the next turn.
        for _ in range(5):
            if self.control & 32:
                for record in image.snapshot(track, side, n).records:
                    if ticks(start+record.position) >= self.clock.now:
                        yield start, record
            n, start, end = image.revolution(track, side, ticks(end))
        yield start, None  # fifth index, even on an empty track

    def _verify_flux(self):
        from .flux import ticks
        for start, record in self._flux_candidates(self.image, self.head[self.drive_number], self.side):
            if record is None:
                self._schedule(ticks(start)-self.clock.now, lambda: self._finish(RNF))
            elif record.ident[0] == self.track and record.id_crc_ok:
                self._schedule(ticks(start+record.id_times[-1])-self.clock.now, self._finish)
                return

    def _find_flux_record(self):
        from .flux import ticks
        crc_seen = False
        for start, record in self._flux_candidates(self._active_image, self._active_track, self._active_side):
            if record is None:
                self._schedule(ticks(start)-self.clock.now, lambda: self._finish(RNF | (CRC if crc_seen else 0)))
                return
            if self._operation != 12:
                if record.ident[0] != self.track or record.ident[2] != self.sector:
                    continue
                if self.command & 2 and record.ident[1] & 1 != (self.command >> 3) & 1:
                    continue
                if not record.id_crc_ok:
                    crc_seen = True
                    continue
                if not record.mark:
                    continue
            if self._operation == 12:
                self._buffer = record.ident
                times = record.id_times
                self._read_error = 0 if record.id_crc_ok else CRC
                self.sector = record.ident[0]
            else:
                self._buffer = record.payload
                times = record.data_times
                self._read_error = (0 if record.data_crc_ok else CRC) | (32 if record.mark != 0xFB else 0)
            self._length, self._index = len(self._buffer), 0
            self._timed_bytes = tuple(ticks(start+t) for t in times)
            self._timed_end = self._timed_bytes[-1]
            self._schedule(self._timed_bytes[0]-self.clock.now, self._byte_event)
            return

    def _find_record(self):
        image = self._active_image
        self._read_error = 0
        try:
            records = image.records(self._active_track, self._active_side)
        except ValueError:
            records = ()
        position = self.clock.now % REVOLUTION
        candidates = []
        crc_seen = False
        for record in records:
            if record.double != bool(self.control & 32):
                continue
            if self._operation != 12:
                if record.ident[0] != self.track or record.ident[2] != self.sector:
                    continue
                if self.command & 2 and record.ident[1] & 1 != ((self.command >> 3) & 1):
                    continue
                if not record.id_crc_ok:
                    crc_seen = True
                    continue
                if not record.mark:
                    continue
            wait = (record.position - position) % REVOLUTION
            candidates.append((wait, record.offset, record))
        if not candidates:
            self._schedule(self._search_delay(), lambda: self._finish(RNF | (CRC if crc_seen else 0)))
            return
        wait, _, self._record = min(candidates)
        record = self._record
        if self._operation == 12:
            # Assemble the ID mark and first CHRN byte before asserting DRQ.
            wait += self._byte_ticks()+self._byte_ticks()
            self._read_error = 0 if record.id_crc_ok else CRC
        else:
            wait += (record.data_position - record.position) % REVOLUTION
            if not self._write:
                wait += self._byte_ticks()
                self._read_error = (0 if record.data_crc_ok else CRC) | (32 if record.mark != 0xFB else 0)
        self._schedule(max(1, wait), self._start_sector)

    def _start_sector(self):
        image = self._active_image
        if self._record is not None:
            record = self._record
            self._buffer = bytearray() if self._write else bytearray(record.ident if self._operation == 12 else record.payload)
            self._length = 6 if self._operation == 12 else len(record.payload)
            if self._operation == 12:
                self.sector = record.ident[0]
            self._index = 0
            self._byte_event()
            return
        if self._operation == 12:
            record = bytes((self._active_track, self._active_side, self._active_sector,
                            image.size.bit_length() - 8))
            crc = binascii.crc_hqx(b'\xa1\xa1\xa1\xfe' + record, 65535)
            self._buffer = bytearray(record + crc.to_bytes(2, 'big'))
            self.sector = self._active_track
        elif self._write:
            self._buffer = bytearray()
        else:
            self._buffer = bytearray(image.read_sector(self._active_track, self._active_side, self._active_sector))
        self._length = 6 if self._operation == 12 else image.size
        self._index = 0
        self._byte_event()

    def _start_track(self):
        self._length = 6250 if self.control & 32 else 3125
        self._index = 0
        if getattr(self._active_image, 'timed_track', False):
            from .flux import ticks
            image = self._active_image
            n, start, end = image.revolution(self._active_track, self._active_side, self.clock.now)
            track = image.snapshot(self._active_track, self._active_side, n)
            self._buffer = track.stream if self.control & 32 else b''
            self._length = len(self._buffer)
            self._timed_bytes = tuple(ticks(start+t) for t in track.times) if self.control & 32 else ()
            self._timed_end = ticks(end)
            if not self._length:
                self._schedule(self._timed_end-self.clock.now, self._finish)
            else:
                self._schedule(self._timed_bytes[0]-self.clock.now, self._byte_event)
            return
        if self._write:
            from .dmk import TrackEncoder
            self._encoder = TrackEncoder(bool(self.control & 32))
            self._buffer = bytearray()
        else:
            try:
                if self._active_image.format == 'DMK':
                    self._buffer = self._active_image.track_stream(self._active_track, self._active_side, bool(self.control & 32))
                    self._length = len(self._buffer)
                else:
                    self._buffer = self._track_stream().ljust(self._length, b'\x4e')[:self._length]
            except ValueError:
                self._finish(RNF)
                return
            start = self.clock.now
            self._timed_bytes = tuple(start+(i+1)*REVOLUTION//self._length for i in range(self._length))
            self._timed_end = start+REVOLUTION
            self._schedule(self._timed_bytes[0]-start, self._byte_event)
            return
        self._byte_event()

    def _track_stream(self):
        result = bytearray(b'\x4e' * 80)
        image = self._active_image
        for sector in range(image.first, image.first + image.sectors):
            ident = b'\xa1\xa1\xa1\xfe' + bytes((self._active_track, self._active_side, sector, image.size.bit_length()-8))
            data = b'\xa1\xa1\xa1\xfb' + image.read_sector(self._active_track, self._active_side, sector)
            result += b'\0'*12 + ident + binascii.crc_hqx(ident, 65535).to_bytes(2,'big') + b'\x4e'*22
            result += b'\0'*12 + data + binascii.crc_hqx(data, 65535).to_bytes(2,'big') + b'\x4e'*24
        return result

    def _byte_event(self):
        if not self.ready or self.drive_number != self._active_drive or self.side != self._active_side:
            self._finish(NOT_READY)
            return
        if self._crc_cell:
            self._crc_cell = False
            self._schedule(self._byte_ticks(), self._byte_event)
            return
        if self.drq:
            self.status |= LOST
            if self._write:
                self._buffer.append(0)
                self._index += 1
                if self._operation == 15:
                    self._encoder.feed(0)
        if self._index >= self._length:
            self.drq = False
            self._lines()
            if self._timed_bytes:
                delay = self._timed_end-self.clock.now
            elif self._operation in (8, 9, 10, 11):
                # CRC bytes do not request CPU data. Writes append a gap byte.
                # For reads this event is already the first CRC byte's end.
                delay = sum(self._byte_ticks() for _ in range(3 if self._write else 1))
            else:
                delay = 0
            if delay > 0:
                self._schedule(delay, self._complete_transfer)
            else:
                self._complete_transfer()
            return
        if not self._write:
            self.data = self._buffer[self._index]
            self._index += 1
            self.transfer_bytes += 1
        self.drq = True
        self._lines()
        if self._operation == 12 and self._index == self._length:
            self._complete_transfer()
            self.drq = True
            self._lines()
            return
        if self._timed_bytes:
            if self._index < len(self._timed_bytes):
                deadline = self._timed_bytes[self._index]
            else:
                deadline = self._timed_end
            if deadline == self.clock.now:
                # READ ADDRESS/TRACK may deliver their last byte at completion.
                self._complete_transfer()
                self.drq = True
                self._lines()
                return
            ticks = deadline-self.clock.now
        else:
            ticks = self._byte_ticks()
        self._schedule(ticks, self._byte_event)

    def _complete_transfer(self):
        if self._write:
            try:
                if self._operation == 15:
                    if self._active_image.format == 'DMK':
                        self._active_image.replace_track(self._active_track, self._active_side,
                            self._encoder.data, self._encoder.pointers, bool(self.control & 32))
                    else:
                        self._format_track()
                elif self._record is not None:
                    self._active_image.write_record(self._active_track, self._active_side,
                                                   self._record, self._buffer, bool(self.command & 1))
                else:
                    self._active_image.write_sector(self._active_track, self._active_side,
                                                    self._active_sector, self._buffer)
                self._active_image.flush()
            except (OSError, ValueError) as error:
                self.last_error = str(error)
                self._finish(WRITE_FAULT)
                return
        self.status |= self._read_error
        if self._operation in (9, 11) and not self.status & (CRC | LOST):
            self.sector = (self.sector + 1) & 255
            self._active_sector = self.sector
            self.drq = False
            self._find_sector()
        else:
            self._finish()
        return

    def _format_track(self):
        # Decode normal WD write-track ID/data marks into logical sectors.
        # F5/F6/F7 represent special marks/CRC on disk, not literal payload bytes.
        data = bytes(self._buffer)
        image = self._active_image
        offset = 0
        sectors = {}
        while offset < len(data):
            ident = data.find(b'\xfe', offset)
            if ident < 0 or ident + 5 >= len(data): break
            track, side, sector, size = data[ident+1:ident+5]
            mark = data.find(b'\xfb', ident+5)
            if mark < 0: break
            length = 128 << (size & 3)
            if track == self._active_track and side == self._active_side and length == image.size:
                payload = data[mark+1:mark+1+length]
                image.offset(track, side, sector)
                if len(payload) != image.size or sector in sectors:
                    raise ValueError('Incomplete or duplicate sector in write-track')
                sectors[sector] = payload
            offset = mark + length + 1
        if len(sectors) != image.sectors:
            raise ValueError(f'Write-track contains {len(sectors)} sectors; expected {image.sectors}')
        for sector, payload in sectors.items():
            image.write_sector(self._active_track, self._active_side, sector, payload)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Create a new, empty RSDOS disk (35 tracks, 18 sectors)')
    parser.add_argument('path', type=Path)
    options = parser.parse_args()
    try:
        result = DiskImage.create(options.path)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f'Created {result.path} ({result.tracks} tracks, {result.size}-byte sectors)')
