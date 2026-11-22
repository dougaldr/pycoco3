"""Device-level deadlines, signal handshakes, image integrity and ROM integration."""
from array import array
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import Mock
import wave

from coco.audio import Audio, output_level, WaveRecorder, Playback
from coco.clock import Clock, TimedBus, MASTER_HZ, LINE_TICKS, FRAME_TICKS
from coco.cpu6809 import CPU6809
from coco.disk import DiskImage, FDC, REVOLUTION, LOST, RNF, WP, load_disk_rom
from coco.frontend import parse_args
from coco.machine import Machine
from coco.memory import Memory
from coco.timing import GimeTiming
from coco.video import Video, RGB, WIDTH
from test_hardware import test_rom, rom_for_tests


class TimingTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.memory = Memory(test_rom())

    def timing(self, revision=1987, video=None):
        t = GimeTiming(self.clock, self.memory, video, revision, trace=True)
        self.memory.on_write = lambda port, value: t.register_write(port)
        return t

    def test_order_cancel_and_reschedule(self):
        c, seen = self.clock, []
        c.at(4, lambda: seen.append('old'), 'cancel')
        c.at(4, lambda: seen.append('first'))
        c.cancel('cancel')
        c.at(4, lambda: seen.append('new'), 'cancel')
        c.at(6, lambda: c.after(2, lambda: seen.append(c.now)))
        c.advance(10)
        self.assertEqual(seen, ['first', 'new', 8])
        self.assertEqual(c.now, 10)
        self.assertIsNone(c.next_event)
        with self.assertRaises(ValueError): c.advance(9)

    def test_ntsc_raster_edges_and_frame_length(self):
        t = self.timing()
        self.clock.advance(FRAME_TICKS)
        falls = [at for at, name, _ in t.events if name == 'hsync-fall']
        rises = [at for at, name, _ in t.events if name == 'hsync-rise']
        self.assertEqual(falls, list(range(0, FRAME_TICKS + 1, 912)))
        self.assertEqual(rises, [at + 80 for at in falls[:-1]])
        self.assertEqual((t.frame, t.line), (1, 0))
        vsync = [(at, name) for at, name, _ in t.events if name.startswith('vsync')]
        self.assertEqual(vsync, [(225, 'vsync-fall'), (4*912+225, 'vsync-rise')])

    def test_fast_timer_revision_delay_and_period(self):
        for revision, first, period in ((1987, 40, 16), (1986, 44, 20)):
            with self.subTest(revision=revision):
                self.setUp()
                t = self.timing(revision)
                m = self.memory
                m.write8(0xFF90, 32)
                m.write8(0xFF92, 32)
                m.write8(0xFF91, 32)
                m.write8(0xFF95, 3)
                m.write8(0xFF94, 0)
                self.clock.advance(first - 1)
                self.assertFalse(m.irq)
                self.clock.advance(first)
                self.assertTrue(m.irq)
                self.assertEqual(m.read8(0xFF92), 32)
                self.clock.advance(first + period)
                self.assertTrue(m.irq)
                self.assertEqual([at for at, name, _ in t.events if name == 'timer-irq'], [first, first+period])

    def test_timer_cancel_and_low_byte_does_not_restart(self):
        t = self.timing()
        m = self.memory
        m.write8(0xFF91, 32)
        m.write8(0xFF95, 3)
        m.write8(0xFF94, 0)
        self.clock.advance(12)
        m.write8(0xFF95, 7)
        self.clock.advance(40)
        self.assertIn((40, 'timer-irq', 0), t.events)
        m.write8(0xFF95, 0)
        m.write8(0xFF94, 0)
        self.clock.advance(200)
        self.assertEqual(sum(name == 'timer-irq' for _, name, _ in t.events), 1)

    def test_line_timer_and_cpu_speed_independence(self):
        t = self.timing()
        m = self.memory
        m.write8(0xFF95, 1)
        m.write8(0xFF94, 0)
        m.write8(0xFFD9, 0)  # all-fast CPU clock must not accelerate devices
        self.clock.advance(4*LINE_TICKS)
        self.assertEqual([at for at, name, _ in t.events if name == 'timer-irq'], [912, 2736])

    def test_address_dependent_speed_and_timed_store(self):
        m, c = self.memory, self.clock
        m.write8(0xFFD7, 0)
        self.assertEqual([m.cycle_ticks(a) for a in (0x1000, 0x8000, None)], [16, 8, 16])
        m.write8(0xFFD6, 0)
        m.ram[0x11000:0x11003] = bytes.fromhex('b7 ff20')  # STA extended
        b = TimedBus(m, c)
        cpu = CPU6809(b)
        cpu.pc, cpu.a = 0x1000, 0xFC
        b.enabled = True
        seen = []
        m.before_write = lambda addr, value: seen.append((c.now, addr, value))
        c.at(79, lambda: seen.append(('before', m.pia1.ddr[0])))
        b.begin()
        cycles = cpu.step()
        b.idle_until(cycles)
        self.assertEqual(cycles, 5)
        self.assertEqual(seen, [('before', 0), (80, 0xFF20, 0xFC)])
        self.assertEqual(c.now, 80)

    def test_midline_palette_and_frame_latched_base(self):
        m, c = self.memory, self.clock
        v = Video(m)
        t = self.timing(video=v)
        m.regs[8], m.regs[9] = 128, 0x1E
        m.regs[33] = 36
        m.ram[:160] = b'\x11' * 160
        # The active area begins at presentation row 24, physical line 42.
        c.advance(42*LINE_TICKS)
        c.advance(t.start + t.pixel_start + 320)
        t.flush()
        m.regs[33] = 18
        m.regs[13] = 0x10
        c.advance(t.start + t.pixel_start + 640)
        start = 48*WIDTH*3
        self.assertEqual(v.pixels[start:start+3], RGB[36])
        self.assertEqual(v.pixels[start+639*3:start+640*3], RGB[18])
        self.assertEqual(v.latched_offset, 0)
        c.advance(FRAME_TICKS)
        self.assertEqual(v.latched_offset, 0x1000)

    def test_video_write_filter_matches_all_fetched_addresses(self):
        m, v = self.memory, Video(self.memory)
        fetched = set()
        v.byte = lambda address: fetched.add(address & m.mask) or 0
        for compat in (0, 128):
            for mode in range(8):
                for hres in range(8):
                    m.regs[0], m.regs[8], m.regs[9] = compat, 128 | mode, hres << 2 | 1
                    m.regs[15] = 0xFF
                    m.pia1.data[1] = 128 | mode << 4
                    m.pia1.ddr[1] = 255
                    for y in (24, 73, 215):
                        fetched.clear()
                        v.render_line(y)
                        self.assertTrue(all(v.reads_address(a, y) for a in fetched), (compat, mode, hres, y))


class AudioTests(unittest.TestCase):
    def test_playback_does_not_restart_during_sdl_queue_transition(self):
        pg = Mock()
        p = Playback(pg)
        channel = p.channel
        channel.get_sound.return_value = None
        channel.get_queue.return_value = None
        channel.get_busy.return_value = True
        p.feed(bytes(p.chunk_bytes * 2))
        self.assertTrue(p.started)
        channel.get_sound.return_value = object()
        channel.get_busy.return_value = False  # SDL completion callback waiting on GIL
        channel.get_queue.return_value = object()
        p.feed(bytes(p.chunk_bytes * 2))
        self.assertEqual(p.underruns, 0)
        self.assertEqual(channel.play.call_count, 1)
        channel.get_sound.return_value = None
        channel.get_queue.return_value = None
        p.feed(b'')
        self.assertEqual(p.underruns, 1)
        self.assertEqual(channel.play.call_count, 2)
        p.clear()
        self.assertFalse(p.started)
        self.assertFalse(p.chunks or p.pending)

    def test_exact_sample_count_and_chunk_invariance(self):
        a, b = Audio(), Audio()
        a.set_level(0, 12000)
        b.set_level(0, 12000)
        one = a.drain(MASTER_HZ)
        blocks = b''.join(b.drain(t) for t in range(1000, MASTER_HZ, 1000)) + b.drain(MASTER_HZ)
        self.assertEqual(one, blocks)
        self.assertEqual((len(one), a.total_samples), (96000, 48000))
        # AC-coupled DC settles toward zero.
        self.assertLess(abs(array('h', one)[-1]), 2)

    def test_subsample_pulse_preserves_area(self):
        a = Audio(sample_rate=1)
        a.hp = 1
        a.set_level(0, 1000)
        a.set_level(MASTER_HZ // 2, 0)
        self.assertEqual(array('h', a.drain(MASTER_HZ)).tolist(), [500])

    def test_dac_mux_enable_ddr_and_one_bit(self):
        m = Memory(test_rom())
        m.pia1.data[0] = 252
        m.pia1.ddr[0] = 252
        self.assertEqual(output_level(m), 0)
        m.pia1.control[1] = 8
        self.assertEqual(output_level(m), 63*256)
        m.pia0.control[0] = 8
        self.assertEqual(output_level(m), 0)
        m.pia1.data[1], m.pia1.ddr[1] = 2, 2
        self.assertEqual(output_level(m), 8192)
        m.pia1.ddr[1] = 0
        self.assertEqual(output_level(m), 0)

    def test_wave_capture_header_and_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.wav'
            w = WaveRecorder(path)
            w.write(b'\0\0\xff\x7f')
            w.close()
            with wave.open(str(path)) as source:
                self.assertEqual((source.getnchannels(), source.getsampwidth(), source.getframerate()), (1, 2, 48000))
                self.assertEqual(source.readframes(2), b'\0\0\xff\x7f')


class DiskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'test.dsk'
        self.image = DiskImage.create(self.path)
        self.clock = Clock()
        self.nmis = []
        self.fdc = FDC(self.clock, [self.image], self.nmis.append)
        self.fdc.write(0x40, 0x29)

    def next_event(self):
        self.assertIsNotNone(self.clock.next_event)
        self.clock.advance(self.clock.next_event)

    def transfer(self, command, payload=None):
        f = self.fdc
        f.write(0x48, command)
        result = bytearray()
        for _ in range(13000):
            if not f.busy: break
            self.next_event()
            if f.drq:
                if payload is None:
                    result.append(f.read(0x4B))
                else:
                    f.write(0x4B, payload[len(result)])
                    result.append(0)
        self.assertFalse(f.busy, 'FDC command did not terminate')
        return result

    def test_create_read_only_bounds_and_persist(self):
        with self.assertRaises(FileExistsError): DiskImage.create(self.path)
        readonly = DiskImage(self.path)
        with self.assertRaises(PermissionError): readonly.write_sector(0, 0, 1, bytes(256))
        with self.assertRaises(ValueError): self.image.read_sector(35, 0, 1)
        with self.assertRaises(ValueError): self.image.write_sector(0, 0, 1, b'bad')
        self.image.write_sector(2, 0, 4, bytes(range(256)))
        self.image.flush()
        self.assertEqual(DiskImage(self.path).read_sector(2, 0, 4), bytes(range(256)))
        self.assertFalse(self.image.dirty)

    def test_external_change_is_not_overwritten(self):
        self.image.write_sector(0, 0, 1, bytes(256))
        external = b'x' * len(self.image.data)
        self.path.write_bytes(external)
        with self.assertRaises(OSError): self.image.flush()
        self.assertEqual(self.path.read_bytes(), external)

    def test_jvc_vdk_and_invalid_geometry(self):
        p = Path(self.temp.name) / 'geometry.dsk'
        p.write_bytes(bytes((18, 2, 1, 1, 0)) + bytes(40*2*18*256))
        d = DiskImage(p)
        self.assertEqual((d.tracks, d.sides, d.header), (40, 2, 5))
        header = b'dk\x0c\0\x10\x10\0\0\x28\x02\0\0'
        p.write_bytes(header + bytes(40*2*18*256))
        self.assertEqual(DiskImage(p).format, 'VDK')
        p.write_bytes(b'bad')
        with self.assertRaises(ValueError): DiskImage(p)

    def test_sector_read_write_drq_halt_nmi_and_ack(self):
        f = self.fdc
        f.write(0x40, 0xA9)
        f.write(0x48, 0x80)
        self.assertTrue(f.halted)
        self.next_event()
        self.assertTrue(f.drq)
        self.assertFalse(f.halted)
        self.assertEqual(f.read(0x4B), 255)
        self.assertTrue(f.halted)
        for _ in range(255):
            self.next_event()
            self.assertEqual(f.read(0x4B), 255)
        self.assertTrue(f.busy)
        self.next_event()
        self.assertTrue(f.busy)  # CRC trailer is read without DRQ
        self.assertFalse(f.drq)
        self.next_event()
        self.assertFalse(f.halted)
        self.assertEqual(self.nmis, [True])
        self.assertEqual(f.read(0x48), 0)
        self.assertEqual(self.nmis, [True, False])
        self.transfer(0xA0, bytes(range(256)))
        self.assertEqual(DiskImage(self.path).read_sector(0, 0, 1), bytes(range(256)))
        self.assertEqual(self.transfer(0x80), bytes(range(256)))

    def test_byte_deadlines_and_lost_data(self):
        f = self.fdc
        f.write(0x48, 0x80)
        self.next_event()
        start = self.clock.now
        f.read(0x4B)
        self.clock.advance(start + MASTER_HZ*8//250000 - 1)
        self.assertFalse(f.drq)
        self.clock.advance(start + MASTER_HZ*8//250000)
        self.assertTrue(f.drq)
        self.next_event()
        self.assertTrue(f.status & LOST)
        while f.busy: self.next_event()
        self.assertEqual(self.clock.now, 258*MASTER_HZ*8//250000)

    def test_seek_delay_step_update_and_verify(self):
        f = self.fdc
        f.write(0x4B, 4)
        f.write(0x48, 0x10)
        self.clock.advance(4*6*MASTER_HZ//1000 - 1)
        self.assertTrue(f.busy)
        self.next_event()
        self.assertEqual((f.head[0], f.track), (4, 4))
        self.transfer(0x60)  # step out without updating track register
        self.assertEqual((f.head[0], f.track), (3, 4))
        self.transfer(0x80)
        self.assertTrue(f.status & RNF)

    def test_write_protect_and_force_interrupt_cancels_transfer(self):
        f = self.fdc
        f.drives[0] = DiskImage(self.path)
        self.transfer(0xA0, bytes(256))
        self.assertEqual(f.status, WP)
        f.write(0x48, 0x80)
        f.write(0x48, 0xD0)
        self.assertFalse(f.busy or f.intrq)
        self.assertIsNone(self.clock.next_event)
        f.write(0x48, 0xD4)
        self.next_event()
        self.assertEqual(self.clock.now % REVOLUTION, 0)
        self.assertTrue(f.intrq)

    def test_multi_sector_and_read_address(self):
        f = self.fdc
        f.sector = 17
        result = self.transfer(0x90)
        self.assertEqual(len(result), 512)
        self.assertEqual(f.sector, 19)
        self.assertEqual(f.status, RNF)
        f.sector = 1
        result = self.transfer(0xC0)
        self.assertEqual(result[:2], bytes((0, 0)))
        self.assertIn(result[2], range(1, 19))
        self.assertEqual(result[3], 1)
        self.assertEqual(len(result), 6)
        f.track, f.sector = 99, 0
        self.assertEqual(len(self.transfer(0xC0)), 6)
        self.assertEqual(f.status, 0)

    def test_normal_write_track_and_incomplete_track_no_partial_mutation(self):
        f = self.fdc
        stream = bytearray()
        for sector in range(1, 19):
            stream += b'\x4e'*16 + b'\xf5\xf5\xf5\xfe' + bytes((0, 0, sector, 1)) + b'\xf7'
            stream += b'\0'*12 + b'\xf5\xf5\xf5\xfb' + bytes([sector])*256 + b'\xf7'
        self.transfer(0xF0, stream.ljust(6250, b'\x4e'))
        self.assertEqual(f.status, 0)
        self.assertEqual(DiskImage(self.path).read_sector(0, 0, 18), bytes([18])*256)
        original = bytes(self.image.data)
        f._buffer = bytearray(b'\xfe\0\0\x01\x01\xf7\xfb' + bytes(256))
        with self.assertRaises(ValueError): f._format_track()
        self.assertEqual(self.image.data, original)

    def test_cli_override_does_not_inherit_write_permission(self):
        config = Path(self.temp.name) / 'config.toml'
        rom = Path(self.temp.name) / 'system.rom'
        diskrom = Path(self.temp.name) / 'disk.rom'
        rom.write_bytes(test_rom())
        diskrom.write_bytes(bytes(8192))
        config.write_text('rom="system.rom"\ndisk_rom="disk.rom"\ndisk0="test.dsk"\ndisk0_writable=true\n')
        args = parse_args(['--config', str(config)])
        self.assertTrue(args.drives[0].writable)
        args = parse_args(['--config', str(config), '--disk', str(self.path)])
        self.assertFalse(args.drives[0].writable)
        args = parse_args(['--config', str(config), '--disk', str(self.path), '--write-disk', '0'])
        self.assertTrue(args.drives[0].writable)


class DiskBootTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = rom_for_tests()
        path = os.environ.get('COCO_DISK_ROM')
        config = Path(__file__).resolve().parents[1] / 'config.local.toml'
        if not path and config.exists():
            path = tomllib.loads(config.read_text()).get('disk_rom')
            if path and not Path(path).expanduser().is_absolute(): path = config.parent / path
        if cls.rom is None or not path:
            raise unittest.SkipTest('Supply system and disk ROMs to run Disk BASIC integration')
        cls.diskrom = load_disk_rom(path)

    def boot(self, image, audio=False):
        m = Machine(self.rom, disk_rom=self.diskrom, drives=[image], video=False, audio=audio)
        for _ in range(60): m.run_frame()
        self.assertIn('DISK EXTENDED COLOR BASIC', m.text_screen())
        return m

    def command(self, m, text, frames=240):
        m.queue_text(text + '\n')
        for _ in range(frames): m.run_frame()
        self.assertNotIn(' ERROR', m.text_screen())
        self.assertIsNone(m.fdc.last_error)

    def test_save_reopen_load_run_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            image = DiskImage.create(Path(directory) / 'roundtrip.dsk')
            m = self.boot(image)
            self.command(m, '10 PRINT "PERSISTED"')
            self.command(m, 'SAVE "HELLO"', 360)
            m = self.boot(DiskImage(image.path))
            self.command(m, 'LOAD "HELLO"', 360)
            self.command(m, 'RUN', 90)
            self.assertIn('\nPERSISTED ', m.text_screen())
            self.command(m, 'DIR', 300)
            self.assertIn('HELLO    BAS', m.text_screen())

    def test_basic_sound_generates_audio_and_returns(self):
        m = self.boot(None, audio=True)
        m.queue_text('SOUND 128,5\n')
        peak = 0
        for _ in range(240):
            m.run_frame()
            peak = max(peak, max(map(abs, array('h', m.audio_frame)), default=0))
        self.assertGreater(peak, 1000)
        self.assertEqual(m.audio.total_samples, m.clock.now*48000//MASTER_HZ)
        self.assertIn('SOUND 128,5', m.text_screen())
        self.assertIn('OK', m.text_screen().split('SOUND 128,5')[1])

    def test_load_disk_rom_after_plain_basic_boot(self):
        m=Machine(self.rom,video=False,audio=False)
        for _ in range(60):m.run_frame()
        self.assertNotIn('DISK EXTENDED',m.text_screen())
        m.load_disk_controller(self.diskrom)
        for _ in range(60):m.run_frame()
        self.assertIn('DISK EXTENDED COLOR BASIC',m.text_screen())
