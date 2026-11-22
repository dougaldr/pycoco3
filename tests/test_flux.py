"""Independent encoded fixtures: no commercial dumps or encoder under test."""
import binascii
from pathlib import Path
import struct
import tempfile
import unittest

from coco.clock import Clock, MASTER_HZ
from coco.disk import FDC, open_disk, CRC, RNF, WP, LOST
from coco.flux import UNITS, ticks


def mfm_track(fill=0x5A, bad_crc=False, deleted=False, sector=1, position=100):
    bits, previous = [], 0
    def byte(value, sync=False):
        nonlocal previous
        if sync:
            bits.extend(int(v) for v in '0100010010001001')
        else:
            for b in range(7, -1, -1):
                data = (value >> b) & 1
                bits.extend((int(not (previous or data)), data))
                previous = data
        previous = value & 1
    def field(value, corrupt=False):
        for _ in range(3): byte(0xA1, True)
        crc = binascii.crc_hqx(b'\xa1'*3+value, 0xFFFF) ^ corrupt
        for v in value+crc.to_bytes(2, 'big'): byte(v)
    for _ in range(position): byte(0x4E)
    field(bytes((0xFE, 0, 0, sector, 0)))
    for _ in range(22): byte(0x4E)
    for _ in range(12): byte(0)
    payload_byte = len(bits)//16+4
    field(bytes((0xF8 if deleted else 0xFB,))+bytes([fill])*128, bad_crc)
    while len(bits) < 100000: byte(0x4E)
    return bits[:100000], payload_byte


def hfe(path, bits, v3=False, weak_byte=None, rate_at=None, skip=False):
    raw = bytearray(sum(bits[i+b] << b for b in range(8)) for i in range(0, len(bits), 8))
    if weak_byte is not None:
        raw[weak_byte*2:weak_byte*2+2] = b'\x2f\x2f'
    if skip:
        # A seven-bit prefix discarded before the final low bit of a data byte.
        first = raw[0]
        raw[:1] = bytes((0xCF, 0xE0, (first & 1) << 7, 0xCF, 0x80, first << 1 & 255))
    if rate_at is not None:
        # divisor 80 at payload: 2.222... us per cell, retained in deadlines.
        raw[rate_at*2:rate_at*2] = b'\x4f\x0a'
        raw[:0] = b'\x4f\x12'  # restore divisor 72 at the beginning of each turn
    if v3: raw[:0] = b'\x8f'
    header = bytearray(b'\xff'*512)
    header[:8] = b'HXCHFEV3' if v3 else b'HXCPICFE'
    header[8:12] = bytes((0, 1, 1, 0))
    struct.pack_into('<HH', header, 12, 250, 300)
    struct.pack_into('<H', header, 18, 1)
    table = bytearray(512)
    struct.pack_into('<HH', table, 0, 2, len(raw)*2)
    body = b''.join(bytes(raw[i:i+256]).ljust(256, b'\x0f')+bytes(256)
                    for i in range(0, len(raw), 256))
    path.write_bytes(header+table+body)
    return path


def scp(path, revolutions, resolution=0, duration=8_000_000, jitter=False):
    header = bytearray(0x2B0)
    header[:12] = b'SCP'+bytes((0x25, 0x80, len(revolutions), 0, 0, 0x81, 0, 1, resolution))
    struct.pack_into('<I', header, 16, len(header))
    track = bytearray(b'TRK\0'+bytes(12*len(revolutions)))
    previous = 0
    for rev, bits in enumerate(revolutions):
        samples = bytearray()
        for i, bit in enumerate(bits):
            if not bit: continue
            # Header index duration is in 25 ns even at non-default resolution.
            time = rev*duration+(i+1)*80
            if jitter: time += (i % 3-1)*4
            sample = (time-previous)//(resolution+1)
            previous = time
            while sample > 65535:
                samples += b'\0\0'
                sample -= 65536
            samples += sample.to_bytes(2, 'big')
        struct.pack_into('<III', track, 4+12*rev, duration, len(samples)//2, len(track))
        track += samples
    data = header+track
    struct.pack_into('<I', data, 12, sum(data[16:]) & 0xFFFFFFFF)
    path.write_bytes(data)
    return path


class FluxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def mount(self, path):
        self.image = open_disk(path, writable=True)
        self.clock = Clock()
        self.fdc = FDC(self.clock, [self.image])
        self.fdc.write(0x40, 0x29)
        return self.image

    def transfer(self, command):
        self.fdc.write(0x48, command)
        data, times = bytearray(), []
        for _ in range(15000):
            if not self.fdc.busy: break
            self.assertIsNotNone(self.clock.next_event)
            self.clock.advance(self.clock.next_event)
            if self.fdc.drq:
                data.append(self.fdc.read(0x4B))
                times.append(self.clock.now)
        self.assertFalse(self.fdc.busy)
        return bytes(data), times

    def test_hfe_v1_missing_clock_marks_crc_and_exact_drq_times(self):
        bits, payload = mfm_track()
        self.mount(hfe(self.root/'disk.hfe', bits))
        data, times = self.transfer(0x80)
        self.assertEqual(data, b'\x5a'*128)
        self.assertEqual(self.fdc.status, 0)
        expected = [((payload+i+1)*32*MASTER_HZ)//1_000_000 for i in range(128)]
        self.assertEqual(times, expected)
        self.assertEqual(self.clock.now, ((payload+130)*32*MASTER_HZ)//1_000_000)
        address, _ = self.transfer(0xC0)
        self.assertEqual(address[:4], b'\0\0\x01\0')
        self.assertEqual(binascii.crc_hqx(b'\xa1'*3+b'\xfe'+address, 0xFFFF), 0)

    def test_hfe_v3_weak_cells_change_each_revolution_but_replay_is_deterministic(self):
        bits, payload = mfm_track()
        path = hfe(self.root/'weak.hfe', bits, True, weak_byte=payload+7)
        self.mount(path)
        first, _ = self.transfer(0x80)
        self.assertEqual(self.fdc.status, CRC)
        second, _ = self.transfer(0x80)
        self.assertEqual(self.fdc.status, CRC)
        self.assertNotEqual(first[7], second[7])
        self.assertEqual(first[:7]+first[8:], b'\x5a'*127)
        self.assertEqual(second[:7]+second[8:], b'\x5a'*127)
        self.mount(path)
        self.assertEqual(self.transfer(0x80)[0], first)
        self.assertEqual(self.transfer(0x80)[0], second)

    def test_hfe_rate_opcode_preserves_nonuniform_byte_deadlines(self):
        bits, payload = mfm_track()
        self.mount(hfe(self.root/'rate.hfe', bits, True, rate_at=payload+10))
        data, times = self.transfer(0x80)
        self.assertEqual(data, b'\x5a'*128)
        self.assertEqual(times[9], ticks((payload+10)*16*720))
        self.assertEqual(times[10], ticks((payload+10)*16*720+16*800))
        self.assertGreater(times[11]-times[10], times[1]-times[0])

    def test_hfe_skip_bits_is_not_data_or_time(self):
        bits, _ = mfm_track()
        self.mount(hfe(self.root/'skip.hfe', bits, True, skip=True))
        self.assertEqual(self.image._durations[0, 0], (UNITS//5,))
        self.assertEqual(self.transfer(0x80)[0], b'\x5a'*128)

    def test_hfe_bitrate_register_persists_across_index(self):
        bits, payload = mfm_track()
        path = hfe(self.root/'rate-wrap.hfe', bits, True, rate_at=payload+10)
        raw = bytearray(path.read_bytes())
        raw[1025:1027] = b'\x0f\x0f'  # remove the initial rate reset
        path.write_bytes(raw)
        self.mount(path)
        self.assertEqual(self.image._durations[0, 0], (100000*800,))
        data, times = self.transfer(0x80)
        self.assertEqual(data, b'\x5a'*128)
        self.assertEqual(times[0], ticks((payload+1)*16*800))

    def test_scp_multiple_captures_crc_deleted_and_capture_resolution(self):
        a, _ = mfm_track(0x33)
        b, _ = mfm_track(0x77, bad_crc=True, deleted=True)
        for resolution in (0, 1):
            with self.subTest(resolution=resolution):
                self.mount(scp(self.root/'capture.scp', [a, b], resolution))
                self.assertEqual(self.transfer(0x80)[0], b'\x33'*128)
                self.assertEqual(self.fdc.status, 0)
                self.assertEqual(self.transfer(0x80)[0], b'\x77'*128)
                self.assertEqual(self.fdc.status, CRC | 32)
                self.assertEqual(self.transfer(0x80)[0], b'\x33'*128)

    def test_scp_jitter_and_zero_overflow_flux_intervals(self):
        bits, _ = mfm_track()
        bits[:2000] = [0]*2000  # exceeds 65535 flux ticks; overflow is not a pulse
        self.mount(scp(self.root/'overflow.scp', [bits], jitter=True))
        # Sector was in erased region; a false sync must not be manufactured.
        self.assertEqual(self.transfer(0x80)[0], b'')
        self.assertEqual(self.fdc.status, RNF)
        bits, _ = mfm_track(position=300)
        bits[:2000] = [0]*2000
        self.mount(scp(self.root/'jitter.scp', [bits], jitter=True))
        self.assertEqual(self.transfer(0x80)[0], b'\x5a'*128)
        self.assertEqual(self.fdc.status, 0)

    def test_index_period_and_read_track_follow_capture_duration_without_drift(self):
        bits, _ = mfm_track()
        duration = 8_000_003
        self.mount(scp(self.root/'timed.scp', [bits], duration=duration))
        self.fdc.write(0x48, 0xD4)
        for turn in range(1, 101):
            self.clock.advance(self.clock.next_event)
            self.assertEqual(self.clock.now, duration*turn*MASTER_HZ//40_000_000)
            self.assertTrue(self.fdc.intrq)
            self.fdc.read(0x48)
        self.fdc.write(0x48, 0xD0)
        data, times = self.transfer(0xE0)
        self.assertEqual(len(data), 6250)
        self.assertEqual(self.clock.now, duration*102*MASTER_HZ//40_000_000)
        self.assertLessEqual(times[-1], self.clock.now)

    def test_search_sees_later_capture_and_finishes_at_fifth_index(self):
        no_match, _ = mfm_track(sector=2)
        match, _ = mfm_track()
        self.mount(scp(self.root/'changing.scp', [no_match, match]))
        self.assertEqual(self.transfer(0x80)[0], b'\x5a'*128)
        self.assertGreater(self.clock.now, MASTER_HZ//5)
        self.fdc.sector = 99
        self.transfer(0x80)
        self.assertEqual(self.fdc.status, RNF)
        self.assertEqual(self.clock.now, 6*MASTER_HZ//5)

    def test_sector_crosses_index_without_restarting_flux_phase(self):
        bits, payload = mfm_track()
        # Rotate a valid track so that index cuts through a payload byte.
        cut = payload*16+39
        bits = bits[cut:]+bits[:cut]
        for path in (hfe(self.root/'wrap.hfe', bits), scp(self.root/'wrap.scp', [bits, bits])):
            with self.subTest(format=path.suffix):
                self.mount(path)
                self.assertEqual(self.transfer(0x80)[0], b'\x5a'*128)
                self.assertEqual(self.fdc.status, 0)
                self.assertGreater(self.clock.now, MASTER_HZ//5)

    def test_weak_id_crc_is_checked_during_all_five_revolutions(self):
        bits, _ = mfm_track()
        path = hfe(self.root/'weak-id.hfe', bits, True, weak_byte=109)
        self.mount(path)
        # Weak CRC bytes must be checked, never replaced by a generated CRC.
        self.transfer(0x80)
        self.assertEqual(self.fdc.status, RNF | CRC)
        self.assertEqual(self.clock.now, MASTER_HZ)

    def test_seek_verify_and_wrong_density_do_not_accept_invalid_ids(self):
        bits, _ = mfm_track()
        self.mount(scp(self.root/'verify.scp', [bits]))
        self.transfer(0x04)
        self.assertEqual(self.fdc.status, 0)
        self.fdc.write(0x40, 9)
        self.assertEqual(self.transfer(0x80)[0], b'')
        self.assertEqual(self.fdc.status, RNF)

    def test_malformed_opcodes_and_scp_offsets_are_rejected(self):
        bits, _ = mfm_track()
        path = hfe(self.root/'invalid.hfe', bits, True)
        original = bytearray(path.read_bytes())
        for opcode in (b'\xff', b'\x4f\0', b'\xcf\0'):
            damaged = original.copy()
            damaged[1024:1024+len(opcode)] = opcode
            path.write_bytes(damaged)
            with self.subTest(opcode=opcode), self.assertRaises(ValueError): open_disk(path)
        path = scp(self.root/'invalid.scp', [bits])
        original = bytearray(path.read_bytes())
        for at, value in ((8, 0x80), (9, 8), (0x2B0+12, 0)):
            damaged = original.copy()
            damaged[at] = value
            struct.pack_into('<I', damaged, 12, sum(damaged[16:]) & 0xFFFFFFFF)
            path.write_bytes(damaged)
            with self.subTest(at=at), self.assertRaises(ValueError): open_disk(path)

    def test_menu_loads_both_formats_and_explains_write_limit(self):
        from test_menu import pygame
        from coco.machine import Machine
        from coco.menu import DiskMenu
        from test_hardware import test_rom
        pygame.display.init()
        pygame.display.set_mode((640, 480))
        self.addCleanup(pygame.quit)
        machine = Machine(test_rom(), disk_rom=bytes(8192), video=False, audio=False)
        menu = DiskMenu(pygame, machine)
        bits, _ = mfm_track()
        for path in (hfe(self.root/'menu.hfe', bits), scp(self.root/'menu.scp', [bits])):
            menu.begin_load(0)
            menu.change_directory(self.root)
            self.assertIn(path, menu.entries)
            menu.load_path(path)
            self.assertFalse(menu.error)
            self.assertEqual(machine.drives[0].path, path)
            with self.assertRaisesRegex(ValueError, 'read-only'):
                machine.set_disk_writable(0, True)
            self.assertEqual(machine.drives[0].path, path)

    def test_readonly_and_bad_files_do_not_fall_back_to_sector_images(self):
        bits, _ = mfm_track()
        for path in (hfe(self.root/'readonly.hfe', bits), scp(self.root/'readonly.scp', [bits])):
            self.mount(path)
            before = path.read_bytes()
            self.assertFalse(self.image.writable)
            self.transfer(0xA0)
            self.assertEqual(self.fdc.status, WP)
            self.assertEqual(path.read_bytes(), before)
            path.write_bytes(before[:-512])
            with self.assertRaises(ValueError): open_disk(path)
        path = self.root/'revision.hfe'
        hfe(path, bits)
        raw = bytearray(path.read_bytes()); raw[8] = 1; path.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, 'v2'): open_disk(path)

    def test_lost_data_deadline_and_force_interrupt_cancel_flux_transfer(self):
        bits, _ = mfm_track()
        self.mount(hfe(self.root/'deadline.hfe', bits))
        self.fdc.write(0x48, 0x80)
        self.clock.advance(self.clock.next_event)
        self.clock.advance(self.clock.next_event-1)
        self.assertFalse(self.fdc.status & LOST)
        self.clock.advance(self.clock.next_event)
        self.assertTrue(self.fdc.status & LOST)
        self.fdc.write(0x48, 0xD8)
        self.assertFalse(self.fdc.busy or self.fdc.drq)
        self.assertTrue(self.fdc.intrq)
        self.assertIsNone(self.clock.next_event)

    def test_6809_polling_loader_observes_payload_and_crc_status(self):
        from coco.machine import Machine
        bits, _ = mfm_track(bad_crc=True, deleted=True)
        for path in (hfe(self.root/'cpu.hfe', bits), scp(self.root/'cpu.scp', [bits])):
            # LDA/STA FDC registers, poll DRQ, transfer 128 bytes to $0200,
            # then poll BUSY until the on-disk CRC trailer has passed.
            code = bytes.fromhex(
                '8629 b7ff40 8601 b7ff4a 8680 b7ff48 8e0200 c680 '
                'b6ff48 8502 27f9 b6ff4b a780 5a 26f1 '
                'b6ff48 8501 26f9 b70300 8655 b70301 20fe')
            rom = bytearray(32768)
            rom[:len(code)], rom[-2:] = code, b'\x80\x00'
            m = Machine(rom, disk_rom=bytes(8192), drives=[open_disk(path)], video=False, audio=False)
            for _ in range(3): m.run_frame()
            self.assertEqual(m.memory.ram[0x10200:0x10280], b'\x5a'*128)
            self.assertEqual(m.memory.read8(0x300), CRC | 32)
            self.assertEqual(m.memory.read8(0x301), 0x55)
