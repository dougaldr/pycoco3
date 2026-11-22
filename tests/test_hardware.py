import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from coco.memory import Memory, load_rom, character_keys
from coco.machine import Machine, FRAME_TICKS
from coco.video import Video, RGB, WIDTH, HEIGHT
from coco.fonts import GIME_FONT, LEGACY_FONT


def test_rom():
    data = bytearray(32768)
    for bank in range(4):
        data[bank*8192:(bank+1)*8192] = bytes([bank + 0x80]) * 8192
    data[-2:] = b'\x80\x00'
    return data


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.m = Memory(test_rom())

    def test_rom_modes_and_write_protection(self):
        m = self.m
        self.assertEqual(m.read8(0x8000), 0x80)
        self.assertEqual(m.read8(0xC000), 255)
        m.write8(0xFF90, 2)
        self.assertEqual(m.read8(0xC000), 0x82)
        m.write8(0xC000, 42)
        self.assertEqual(m.read8(0xC000), 0x82)
        m.write8(0xFFDF, 0)
        m.write8(0xC000, 42)
        self.assertEqual(m.read8(0xC000), 42)
        m.write8(0xFFDE, 0)
        self.assertEqual(m.read8(0xC000), 0x82)
        self.assertEqual((m.read8(0xFFFE), m.read8(0xFFFF)), (0x80, 0))

    def test_task_banks_and_disabled_mmu(self):
        m = self.m
        m.write8(0xFF90, 64)
        m.write8(0xFFA0, 1)
        m.write8(0xFFA8, 2)
        m.write8(0x0010, 11)
        m.write8(0xFF91, 1)
        m.write8(0x0010, 22)
        m.write8(0xFF91, 0)
        self.assertEqual(m.read8(0x0010), 11)
        self.assertEqual((m.ram[8192+16], m.ram[16384+16]), (11, 22))
        m.write8(0xFF90, 0)
        self.assertEqual(m.physical(16)[0], 0x10010)

    def test_fe_vectors_bypass_mmu_and_rom(self):
        m = self.m
        m.write8(0xFF90, 64 | 8 | 2)
        m.write8(0xFFA7, 0)
        m.write8(0xFE00, 42)
        self.assertEqual(m.ram[0x1FE00], 42)
        self.assertEqual(m.read8(0xFE00), 42)
        m.write8(0xFF90, 64 | 2)
        self.assertEqual(m.read8(0xFE00), 0)

    def test_128k_alias_and_512k_physical_address(self):
        self.m.write8(0xFF90, 64)
        self.m.write8(0xFFA0, 0x21)
        self.assertEqual(self.m.physical(0)[0], 0x2000)
        big = Memory(test_rom(), 512)
        big.write8(0xFF90, 64)
        big.write8(0xFFA0, 0x21)
        self.assertEqual(big.physical(0)[0], 0x42000)

    def test_keyboard_ddr_and_active_low_matrix(self):
        m = self.m
        m.write8(0xFF02, 255)  # DDR: column outputs
        m.write8(0xFF03, 4)    # select peripheral data
        m.write8(0xFF01, 4)    # rows are inputs
        m.set_keys({'a', 'shift'})
        m.write8(0xFF02, 0xFD)
        self.assertEqual(m.read8(0xFF00) & 127, 126)
        m.write8(0xFF02, 0x7F)
        self.assertEqual(m.read8(0xFF00) & 127, 63)
        m.set_keys(set())
        self.assertEqual(m.read8(0xFF00) & 127, 127)

    def test_pia_sync_flags_and_ack(self):
        m = self.m
        m.write8(0xFF03, 5)
        m.pia0.edge(1, True)
        self.assertFalse(m.irq)
        m.pia0.edge(1, False)
        self.assertTrue(m.irq)
        self.assertEqual(m.read8(0xFF03), 133)
        m.read8(0xFF02)
        self.assertFalse(m.irq)

    def test_gime_interrupt_enable_and_read_clear(self):
        m = self.m
        m.write8(0xFF92, 8)
        m.signal(24)
        self.assertFalse(m.irq)
        m.write8(0xFF90, 32)
        self.assertTrue(m.irq)
        self.assertEqual(m.read8(0xFF92), 8)
        self.assertFalse(m.irq)
        self.assertEqual(m.read8(0xFF92), 0)

    def test_timer_uses_master_clock_and_reloads(self):
        from coco.clock import Clock
        from coco.timing import GimeTiming
        m = self.m
        clock = Clock()
        timing = GimeTiming(clock, m)
        m.on_write = lambda port, value: timing.register_write(port)
        m.write8(0xFF90, 32)
        m.write8(0xFF92, 32)
        m.write8(0xFF91, 32)
        m.write8(0xFF95, 3)
        m.write8(0xFF94, 0)
        clock.advance(39)
        self.assertFalse(m.irq)
        clock.advance(40)
        self.assertTrue(m.irq)
        m.read8(0xFF92)
        clock.advance(56)
        self.assertTrue(m.irq)

    def test_rom_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'test.rom'
            p.write_bytes(b'x')
            with self.assertRaises(ValueError):
                load_rom(p)
            p.write_bytes(test_rom())
            self.assertEqual(len(load_rom(p)), 32768)
            p.write_bytes(bytes(32768))
            with self.assertRaises(ValueError):
                load_rom(p)


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.m = Memory(test_rom())
        self.v = Video(self.m)
        for index in range(16):
            self.m.regs[32+index] = index

    def pixel(self, x, y):
        off = (y * WIDTH + x) * 3
        return bytes(self.v.pixels[off:off+3])

    def test_font_conversion_and_rgb(self):
        self.assertEqual((len(LEGACY_FONT), len(GIME_FONT)), (112*12, 256*12))
        self.assertEqual(RGB[0], b'\0\0\0')
        self.assertEqual(RGB[63], b'\xff\xff\xff')
        self.assertEqual(RGB[36], b'\xff\0\0')

    def test_bitmap_reads_physical_memory_and_packed_nibbles(self):
        m = self.m
        m.regs[0] = 64
        m.regs[8] = 128
        m.regs[9] = 0x1E  # 320 pixels, 16 colors (160 bytes)
        m.regs[13:15] = b'\x20\x00'  # physical $10000
        m.regs[16] = 3  # unrelated CPU mapping
        m.ram[0x10000] = 0x12
        m.regs[33], m.regs[34] = 36, 18
        self.v.render()
        self.assertEqual(self.pixel(0, 48), RGB[36])
        self.assertEqual(self.pixel(1, 48), RGB[36])
        self.assertEqual(self.pixel(2, 48), RGB[18])
        self.assertEqual(self.pixel(0, 49), RGB[36])

    def test_text_attributes_underline_and_blink(self):
        m = self.m
        m.regs[8], m.regs[9] = 3, 0x15  # 80 columns, 8 lines, attributes
        m.ram[0], m.ram[1] = 32, 0x48  # blank, underline, FG=9 BG=0
        self.v.render(0)
        self.assertEqual(self.pixel(0, 48), RGB[0])
        self.assertEqual(self.pixel(0, 62), RGB[9])
        m.ram[1] |= 128
        self.v.render(16)
        self.assertEqual(self.pixel(0, 62), RGB[0])

    def test_each_mode_preserves_buffer_and_size(self):
        original = self.v.pixels
        for hres in range(8):
            for color in range(4):
                for graphics in (0, 128):
                    self.m.regs[8] = graphics | 3
                    self.m.regs[9] = hres << 2 | color
                    self.v.render_line(24)
                    self.assertIs(original, self.v.pixels)
                    self.assertEqual(len(original), WIDTH*HEIGHT*3)

    def test_text_cache_invalidates_on_ram_and_palette_changes(self):
        self.m.regs[0] = 128
        self.m.ram[:32] = b'\x41'*32
        self.v.render_line(28)
        before=bytes(self.v.pixels)
        self.m.ram[:32] = b'\xff'*32
        self.v.render_line(28)
        changed=bytes(self.v.pixels)
        self.assertNotEqual(before,changed)
        self.m.regs[39] = 36  # semigraphics foreground palette entry 7
        self.v.render_line(28)
        self.assertNotEqual(changed,bytes(self.v.pixels))


def rom_for_tests():
    path = os.environ.get('COCO_ROM')
    if not path:
        config = Path(__file__).resolve().parents[1] / 'config.local.toml'
        if config.exists():
            path = tomllib.loads(config.read_text()).get('rom')
            if path and not Path(path).expanduser().is_absolute():
                path = str(config.parent / path)
    return load_rom(path) if path else None


class BootTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = rom_for_tests()
        if cls.rom is None:
            raise unittest.SkipTest('Set COCO_ROM or config.local.toml for ROM integration tests')

    def boot(self, size=128):
        machine = Machine(self.rom, size, video=False)
        for _ in range(60):
            machine.run_frame()
        self.assertIn('EXTENDED COLOR BASIC 2.0', machine.text_screen())
        self.assertIn('OK', machine.text_screen())
        return machine

    def test_real_rom_arithmetic_through_keyboard(self):
        m = self.boot()
        m.queue_text('PRINT 2+2\n')
        for _ in range(120):
            m.run_frame()
        self.assertIn('\n 4 ', m.text_screen())
        self.assertGreaterEqual(m.master_ticks, m.frames * FRAME_TICKS)
        self.assertLess(m.master_ticks - m.frames * FRAME_TICKS, 400)

    def test_basic_loop_and_floating_point(self):
        m = self.boot()
        m.queue_text('FOR I=1 TO 5:PRINT I*I;:NEXT\nPRINT SQR(81)\n')
        for _ in range(360):
            m.run_frame()
        text = m.text_screen()
        self.assertIn(' 1  4  9  16  25', text)
        self.assertIn('\n 9 ', text)

    def test_512k_boot_and_reset(self):
        m = self.boot(512)
        m.reset()
        for _ in range(60):
            m.run_frame()
        self.assertIn('OK', m.text_screen())

    def test_width80_native_text(self):
        m = self.boot()
        m.queue_text('WIDTH 80\nPRINT 12345\n')
        for _ in range(240):
            m.run_frame()
        self.assertFalse(m.memory.regs[0] & 128)
        self.assertEqual(m.memory.regs[9] & 0x1D, 0x15)
        base = ((m.memory.regs[13] << 8 | m.memory.regs[14]) * 8) & m.memory.mask
        data = m.memory.ram[base:base + 80*24*2:2]
        self.assertIn(b' 12345', data)

    def test_hscreen_program(self):
        m = self.boot()
        m.queue_text('10 HSCREEN 2:HCLS 3\n20 GOTO 20\nRUN\n')
        for _ in range(400):
            m.run_frame()
        self.assertFalse(m.memory.regs[0] & 128)
        self.assertTrue(m.memory.regs[8] & 128)
        base = ((m.memory.regs[13] << 8 | m.memory.regs[14]) * 8) & m.memory.mask
        self.assertEqual(m.memory.ram[base:base+32], bytes([0x33])*32)
