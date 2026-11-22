"""Read-only HFE v1/v3 bitstreams and indexed SCP flux captures.

250 kbit/s MFM only. Times use 360 MHz integer units: HFE v3 bitrate
divisors and SCP's 25 ns samples are both exact. Conversion to master ticks
happens at absolute deadlines, not by rounding each interval.
References: HxC's HFE specification, FlashFloppy hfe.c (opcode semantics),
and Jim Drew's SuperCard Pro image specification v2.5.
"""
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
import bisect
import binascii
import hashlib
import struct

from .clock import MASTER_HZ

UNITS = 360_000_000
REVERSE = bytes(int(f'{v:08b}'[::-1], 2) for v in range(256))


def ticks(time):
    return time * MASTER_HZ // UNITS


@dataclass(frozen=True)
class FluxRecord:
    position: int
    ident: bytes
    payload: bytes
    mark: int
    id_crc_ok: bool
    data_crc_ok: bool
    id_times: tuple
    data_times: tuple  # includes the two CRC byte ends


@dataclass(frozen=True)
class FluxTrack:
    records: tuple
    stream: bytes
    times: tuple


def decode_mfm(previous, current, following, duration, previous_duration):
    """Recognize missing-clock A1 sync, never ordinary A1 payload bytes.

    Read across index using the *next* capture, including its weak-bit state.
    Byte endpoints retain source timing. Clock violations in data are not
    repaired: the resulting bytes are checked against the recorded CRC.
    """
    bits0, ends0 = previous
    bits, ends = current
    bits1, ends1 = following
    prefix = min(64, len(bits0))
    before = previous_duration
    all_bits = bits0[-prefix:] + bits + bits1
    all_ends = [v-before for v in ends0[-prefix:]] + ends + [duration+v for v in ends1]
    limit = prefix + len(bits)
    words, word = {}, 0
    sync = []
    for i, bit in enumerate(all_bits):
        word = ((word << 1) | bit) & 0xFFFF
        if i >= 15 and word == 0x4489:
            words[i-15] = True
            if i-31 in words and i-47 in words:
                sync.append(i+1)  # first cell of address mark

    def byte(at):
        value = 0
        for p in range(at+1, at+16, 2):
            value = (value << 1) | all_bits[p]
        return value

    marks = [(p, byte(p)) for p in sync if p+16 <= len(all_bits)]
    records = []
    for index, (p, mark) in enumerate(marks):
        if mark != 0xFE or not prefix <= p < limit or p+112 > len(all_bits):
            continue
        ident = bytes(byte(p+16*i) for i in range(1, 7))
        id_times = tuple(all_ends[p+16*i+15] for i in range(1, 7))
        id_ok = binascii.crc_hqx(b'\xa1'*3+b'\xfe'+ident, 0xFFFF) == 0
        payload, dam, data_ok, data_times = b'', 0, False, ()
        for q, candidate in marks[index+1:]:
            # 43 byte cells after ID CRC (WD179x MFM search window).
            if candidate == 0xFE or q > p+16*(6+43):
                break
            if candidate not in (0xF8, 0xF9, 0xFA, 0xFB):
                continue
            length = 128 << (ident[3] & 7)
            if q+16*(length+3) > len(all_bits):
                break
            field = bytes(byte(q+16*i) for i in range(1, length+3))
            payload, dam = field[:-2], candidate
            data_ok = binascii.crc_hqx(b'\xa1'*3+bytes([dam])+field, 0xFFFF) == 0
            data_times = tuple(all_ends[q+16*i+15] for i in range(1, length+3))
            break
        records.append(FluxRecord(all_ends[p-1] if p else 0, ident, payload,
                                  dam, id_ok, data_ok, id_times, data_times))
    # READ TRACK realigns at every missing-clock sync sequence.
    sync_starts = {p-48 for p in sync}
    stream, times, p = bytearray(), [], prefix
    while p+16 <= limit:
        align = next((q for q in range(p, p+16) if q in sync_starts), p)
        p = align
        if p+16 > limit:
            break
        stream.append(byte(p))
        times.append(all_ends[p+15])
        p += 16
    return FluxTrack(tuple(records), bytes(stream), tuple(times))


class FluxImage:
    timed_track = True
    writable = False
    dirty = False
    write_protect_reason = 'HFE/SCP bitstream and flux images are read-only.'

    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.data = self.path.read_bytes()
        self._seed = hashlib.sha256(self.data).digest()
        self._durations = {}
        self._cache = OrderedDict()

    def flush(self):
        pass

    def revolution(self, track, side, now):
        """Return revolution number and absolute start/end in source units."""
        durations = self._durations.get((track, side), (UNITS//5,))
        total = sum(durations)
        # Invert floor(source_time * MASTER_HZ / UNITS) at exact boundaries.
        source = ((now+1)*UNITS-1)//MASTER_HZ
        cycles, phase = divmod(source, total)
        start, n = cycles*total, cycles*len(durations)
        for duration in durations:
            if phase < duration:
                return n, start, start+duration
            phase -= duration
            start += duration
            n += 1
        raise AssertionError('Invalid revolution duration')

    def snapshot(self, track, side, revolution):
        key = (track, side, revolution)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        if (track, side) not in self._durations:
            return FluxTrack((), b'', ())
        durations = self._durations[track, side]
        duration = durations[revolution % len(durations)]
        result = decode_mfm(self.cells(track, side, revolution-1),
                            self.cells(track, side, revolution),
                            self.cells(track, side, revolution+1), duration,
                            durations[(revolution-1) % len(durations)])
        self._cache[key] = result
        if len(self._cache) > 4:
            self._cache.popitem(last=False)
        return result


class HFEImage(FluxImage):
    format = 'HFE'

    def __init__(self, path, writable=False):
        super().__init__(path)
        d = self.data
        if len(d) < 512 or d[:8] not in (b'HXCPICFE', b'HXCHFEV3') or d[8] != 0:
            raise ValueError('Only HFE v1 and v3 revision 0 are supported (not HFE v2).')
        self.v3 = d[:8] == b'HXCHFEV3'
        self.tracks, self.sides = d[9:11]
        if not 1 <= self.tracks <= 84 or self.sides not in (1, 2):
            raise ValueError('Invalid HFE geometry')
        if d[11] not in (0, 255) or int.from_bytes(d[12:14], 'little') != 250:
            raise ValueError('HFE currently supports 250 kbit/s MFM tracks only.')
        for side in range(self.sides):
            if d[22+2*side] == 0 and d[23+2*side] not in (0, 255):
                raise ValueError('HFE alternate track encoding is not MFM.')
        lut = int.from_bytes(d[18:20], 'little')*512
        if lut < 512 or lut+4*self.tracks > len(d):
            raise ValueError('Truncated HFE track table')
        self._streams = {}
        self._loop_width = {}
        for track in range(self.tracks):
            block, length = struct.unpack_from('<HH', d, lut+4*track)
            offset, side_length = block*512, length//2
            storage = ((side_length+255)//256)*512
            if length % 2 or not side_length or offset < lut+4*self.tracks or offset+storage > len(d):
                raise ValueError('Invalid or truncated HFE track')
            for side in range(self.sides):
                raw = b''.join(d[p+side*256:p+side*256+256]
                               for p in range(offset, offset+storage, 512))[:side_length]
                self._streams[track, side] = raw
                self.cells(track, side, 0)  # validate and find bitrate at wrap
                _, ends = self.cells(track, side, 0)
                if not ends or not UNITS//10 <= ends[-1] <= UNITS//3:
                    raise ValueError('HFE track must represent a 100–333 ms revolution.')
                self._durations[track, side] = (ends[-1],)

    def cells(self, track, side, revolution):
        raw = self._streams[track, side]
        bits, widths, i, index = [], [], 0, None
        width = self._loop_width.get((track, side), 720)
        while i < len(raw):
            pos, value, skip = i, raw[i], 0
            i += 1
            if self.v3 and value & 15 == 15:
                if value == 0x0F:  # NOP, stored LSB first
                    continue
                if value == 0x8F:
                    if index is not None:
                        raise ValueError('Multiple HFE index pulses per revolution are unsupported.')
                    index = len(bits)
                    continue
                if value in (0x4F, 0xCF):
                    if i >= len(raw):
                        raise ValueError('Truncated HFE opcode')
                    arg = REVERSE[raw[i]]
                    i += 1
                    if value == 0x4F:
                        if not 36 <= arg <= 144:
                            raise ValueError('Unsupported HFE bitrate divisor (expected 36–144).')
                        width = arg*10  # divisor of 36 MHz, not kbit/s
                        continue
                    if not 1 <= arg <= 7 or i >= len(raw):
                        raise ValueError('Invalid HFE SKIP BITS opcode')
                    skip, value = arg, raw[i]
                    i += 1
                    if value & 15 == 15 and value != 0x2F:
                        raise ValueError('Unsupported HFE opcode after SKIP BITS')
                elif value != 0x2F:
                    raise ValueError(f'Unsupported HFE opcode ${REVERSE[value]:02X}')
                if value == 0x2F:
                    # Reproducible noise per image/track/side/revolution/location.
                    key = struct.pack('<BBqI', track, side, revolution, pos)
                    value = hashlib.blake2s(key, key=self._seed, digest_size=1).digest()[0]
            bits.extend((value >> b) & 1 for b in range(skip, 8))
            widths.extend([width]*(8-skip))
        # A loop boundary does not reset the v3 bitrate register. The header
        # rate is used only when no rate opcode has set it.
        self._loop_width[track, side] = width
        if index:
            bits = bits[index:]+bits[:index]
            widths = widths[index:]+widths[:index]
        ends, time = [], 0
        for width in widths:
            time += width
            ends.append(time)
        return bits, ends


class SCPImage(FluxImage):
    format = 'SCP'

    def __init__(self, path, writable=False):
        super().__init__(path)
        d = self.data
        if len(d) < 0x2B0 or d[:3] != b'SCP':
            raise ValueError('Invalid or truncated SCP header')
        count, first, last, flags, width, heads, resolution = d[5:12]
        if not flags & 1 or flags & 64 or width not in (0, 16):
            raise ValueError('SCP requires indexed floppy captures with 16-bit flux samples.')
        if not 1 <= count <= 32 or not 0 <= first <= last < 168 or heads not in (0, 1, 2):
            raise ValueError('Invalid SCP geometry or revolution count')
        checksum = int.from_bytes(d[12:16], 'little')
        if not (checksum == 0 and flags & 16) and checksum != sum(d[16:]) & 0xFFFFFFFF:
            raise ValueError('SCP checksum mismatch')
        self.tracks, self.sides = last//2+1, 1 if heads == 1 else 2
        self._samples = {}
        self._decoded_cells = OrderedDict()
        unit = 9*(resolution+1)
        for number in range(first, last+1):
            offset = struct.unpack_from('<I', d, 16+number*4)[0]
            if not offset:
                continue
            if heads and number % 2 != heads-1:
                raise ValueError('SCP track conflicts with selected head')
            header_end = offset+4+12*count
            if offset < 0x2B0 or header_end > len(d) or d[offset:offset+4] != b'TRK'+bytes([number]):
                raise ValueError('Invalid SCP track header')
            samples, durations, time, overflow = [], [], 0, 0
            for rev in range(count):
                duration, length, relative = struct.unpack_from('<III', d, offset+4+12*rev)
                start = offset+relative
                # Index durations are ALWAYS 25 ns, independently of flux resolution.
                duration *= 9
                if not UNITS//10 <= duration <= UNITS//3 or length > 500_000:
                    raise ValueError('Unsupported SCP revolution duration or sample count')
                if start < header_end or start+2*length > len(d):
                    raise ValueError('Truncated SCP flux data')
                for (sample,) in struct.iter_unpack('>H', d[start:start+2*length]):
                    if sample == 0:
                        overflow += 65536
                        continue
                    time += (overflow+sample)*unit
                    overflow = 0
                    samples.append(time)
                durations.append(duration)
            if overflow:
                raise ValueError('Unterminated SCP flux overflow')
            key = number//2, number%2
            self._samples[key] = samples
            self._durations[key] = tuple(durations)
        if not self._samples:
            raise ValueError('SCP contains no captured tracks')

    def cells(self, track, side, revolution):
        key = track, side
        durations = self._durations[key]
        if key in self._decoded_cells:
            self._decoded_cells.move_to_end(key)
            return self._decoded_cells[key][revolution % len(durations)]
        transitions = self._samples[key]
        duration = sum(durations)
        bits, ends, boundary, period = [], [], 0, 720
        # Bounded digital data separator, not a model of the analog WD VFO.
        for time in transitions:
            if time > duration:
                break
            interval = time-boundary
            cells = max(1, (interval+period//2)//period)
            if len(bits)+cells > 250_000*len(durations):
                raise ValueError('Excessive SCP cell count')
            for cell in range(1, cells+1):
                bits.append(int(cell == cells))
                ends.append(boundary+interval*cell//cells)
            if cells <= 4:
                period = max(612, min(828, period+(interval//cells-period)//8))
            boundary = time
        while boundary+period <= duration:
            boundary += period
            bits.append(0)
            ends.append(boundary)
        # Flux intervals run continuously across TDH revolution boundaries.
        # Neither elapsed time nor the separator phase restarts at an index.
        result, start, first = [], 0, 0
        for length in durations:
            last = bisect.bisect_right(ends, start+length)
            result.append((bits[first:last], [v-start for v in ends[first:last]]))
            first, start = last, start+length
        self._decoded_cells[key] = result
        if len(self._decoded_cells) > 2:
            self._decoded_cells.popitem(last=False)
        return result[revolution % len(durations)]
