"""DMK tracks: preserve recorded IDs, angular layout, marks and CRC bytes.

DMK stores decoded track bytes, not flux transitions or weak-bit probabilities.
Offsets in the IDAM table include the 128-byte table itself.
"""
from dataclasses import dataclass
from pathlib import Path
import binascii
import hashlib
from .disk import DiskImage, REVOLUTION


def crc16(data):
    return binascii.crc_hqx(data, 0xFFFF)


@dataclass(frozen=True)
class Record:
    offset: int
    position: int
    data_position: int
    ident: bytes  # CHRN and the two stored CRC bytes
    payload: bytes
    mark: int
    id_crc_ok: bool
    data_crc_ok: bool
    double: bool
    stride: int
    data_offset: int | None


class DMKImage(DiskImage):
    format = 'DMK'

    def __init__(self, path, writable=False):
        self.path = Path(path).expanduser().resolve()
        self.data = bytearray(self.path.read_bytes())
        self._digest = hashlib.sha256(self.data).digest()
        self.dirty = False
        d = self.data
        if len(d) < 16 or d[0] not in (0, 255) or any(d[5:16]) or d[4] & ~0xD0:
            raise ValueError('Invalid or unsupported DMK header')
        self.tracks = d[1]
        self.track_size = int.from_bytes(d[2:4], 'little')
        self.sides = 1 if d[4] & 16 else 2
        self.single_stride = 1 if d[4] & 0xC0 else 2
        self.media_protected = bool(d[0])
        self.writable = writable and not self.media_protected
        if (not 1 <= self.tracks <= 255 or not 128 < self.track_size <= 0x3FFF
                or len(d) != 16 + self.tracks * self.sides * self.track_size):
            raise ValueError('Invalid DMK track geometry or truncated image')
        self._records = {}
        for track in range(self.tracks):
            for side in range(self.sides):
                self.records(track, side)  # validate all pointers before mounting

    @staticmethod
    def identifies(data):
        if len(data) < 16 or data[0] not in (0, 255) or any(data[5:16]):
            return False
        size = int.from_bytes(data[2:4], 'little')
        return (128 < size <= 0x3FFF and data[1] > 0
                and len(data) == 16 + data[1] * (1 if data[4] & 16 else 2) * size)

    def track_offset(self, track, side):
        if not 0 <= track < self.tracks or not 0 <= side < self.sides:
            raise ValueError('Track not found')
        return 16 + (track * self.sides + side) * self.track_size

    def records(self, track, side):
        key = (track, side)
        if key in self._records:
            return self._records[key]
        base = self.track_offset(track, side)
        raw = self.data[base:base+self.track_size]
        length = len(raw) - 128
        pointers = []
        for i in range(0, 128, 2):
            value = int.from_bytes(raw[i:i+2], 'little')
            if value == 0:
                continue
            offset = value & 0x3FFF
            if not 128 <= offset < len(raw) or raw[offset] != 0xFE:
                raise ValueError(f'Invalid DMK ID pointer on track {track}, side {side}')
            if any(p[0] == offset for p in pointers):
                raise ValueError('Duplicate DMK ID pointer')
            pointers.append((offset, bool(value & 0x8000)))
        pointers.sort()
        def read(offset, count, stride):
            return bytes(raw[128 + (offset - 128 + i*stride) % length] for i in range(count))
        records = []
        for index, (offset, double) in enumerate(pointers):
            stride = 1 if double else self.single_stride
            ident = read(offset + stride, 6, stride)
            # Large/overlapping fields can exist on protected tracks, up to N=7.
            size = 128 << (ident[3] & 7)
            prefix = b'\xa1\xa1\xa1' if double else b''
            id_ok = crc16(prefix + b'\xfe' + ident) == 0
            next_id = pointers[(index+1) % len(pointers)][0]
            distance = (next_id - offset) % length or length
            mark_offset = None
            # WD179x searches the gap after the ID for a data address mark.
            for delta in range(7, min(distance // stride, 7+(43 if double else 30))):
                p = offset + delta*stride
                value = read(p, 1, stride)[0]
                if value in (0xF8, 0xF9, 0xFA, 0xFB) and (
                        not double or read(p-3*stride, 3, stride) == prefix):
                    mark_offset = 128 + (p - 128) % length
                    break
            payload, mark, data_ok = b'', 0, False
            data_position = offset
            if mark_offset is not None:
                mark = raw[mark_offset]
                field = read(mark_offset + stride, size + 2, stride)
                payload = field[:-2]
                data_ok = crc16(prefix + bytes([mark]) + field) == 0
                data_position = 128 + (mark_offset + stride - 128) % length
            records.append(Record(offset, (offset-128)*REVOLUTION//length,
                                  (data_position-128)*REVOLUTION//length, ident, payload,
                                  mark, id_ok, data_ok, double, stride, mark_offset))
        self._records[key] = tuple(records)
        return self._records[key]

    def track_stream(self, track, side, double=True):
        base = self.track_offset(track, side)
        raw = bytes(self.data[base+128:base+self.track_size])
        return raw if double or self.single_stride == 1 else raw[::2]

    def write_record(self, track, side, record, payload, deleted=False):
        if not self.writable:
            raise PermissionError('Disk image is write-protected')
        if record.data_offset is None or len(payload) != len(record.payload):
            raise ValueError('Missing data mark or incomplete sector write')
        base = self.track_offset(track, side)
        length = self.track_size - 128
        mark = 0xF8 if deleted else 0xFB
        field = bytes([mark]) + bytes(payload)
        prefix = b'\xa1'*3 if record.double else b''
        field += crc16(prefix + field).to_bytes(2, 'big')
        for i, value in enumerate(field):
            for duplicate in range(record.stride):
                p = 128 + (record.data_offset-128 + i*record.stride + duplicate) % length
                self.data[base+p] = value
        self._records.pop((track, side), None)
        self.dirty = True

    def replace_track(self, track, side, stream, pointers, double=True):
        if not self.writable:
            raise PermissionError('Disk image is write-protected')
        base = self.track_offset(track, side)
        stride = 1 if double else self.single_stride
        if len(pointers) > 64:
            raise ValueError('DMK supports at most 64 ID marks per track')
        # Keep the existing image's track geometry. Index terminates the write.
        raw = bytearray(b'\x4e' * self.track_size)
        raw[:128] = bytes(128)
        encoded = bytes(v for v in stream for _ in range(stride))
        raw[128:] = encoded[:self.track_size-128].ljust(self.track_size-128, b'\x4e')
        valid = [128 + p*stride for p in pointers if 128 + p*stride < self.track_size]
        for i, offset in enumerate(valid):
            raw[i*2:i*2+2] = (offset | (0x8000 if double else 0)).to_bytes(2, 'little')
        self.data[base:base+self.track_size] = raw
        self._records.pop((track, side), None)
        self.dirty = True


class TrackEncoder:
    """WD write-track tokens to decoded bytes; F7 consumes two on-disk cells."""
    def __init__(self, double=True):
        self.double = double
        self.data = bytearray()
        self.pointers = []
        self.crc_start = 0
        self.sync = 0

    def feed(self, value):
        if value == 0xF7:
            self.data.extend(crc16(self.data[self.crc_start:]).to_bytes(2, 'big'))
            self.sync = 0
            return 2
        if self.double and value == 0xF5:
            if self.sync == 0:
                self.crc_start = len(self.data)
            self.sync += 1
            self.data.append(0xA1)
            return 1
        if value == 0xFE and (not self.double or self.sync >= 3):
            self.pointers.append(len(self.data))
        if not self.double and value in (0xF8, 0xF9, 0xFA, 0xFB, 0xFE):
            self.crc_start = len(self.data)
        self.data.append(0xC2 if self.double and value == 0xF6 else value)
        self.sync = 0
        return 1
