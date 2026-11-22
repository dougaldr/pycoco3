"""Deadline regressions against the WD179x command rules and NTSC clocks."""
from pathlib import Path
import tempfile
import unittest

from coco.clock import Clock, MASTER_HZ, FRAME_TICKS
from coco.disk import DiskImage, FDC, REVOLUTION
from coco.timing import GimeTiming
from coco.memory import Memory
from test_hardware import test_rom


class ControllerTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = Clock()
        self.image = DiskImage.create(Path(self.temp.name)/'timing.dsk')
        self.fdc = FDC(self.clock, [self.image])
        self.fdc.write(0x40, 0x29)

    def test_head_settle_precedes_rotational_search(self):
        self.fdc.write(0x48, 0x84)
        self.clock.advance(MASTER_HZ*15//1000)
        self.assertFalse(self.fdc.drq)
        # Sector 1 at index already passed during the 15 ms head settle.
        first = REVOLUTION+MASTER_HZ*8//250000
        self.clock.advance(first-1)
        self.assertFalse(self.fdc.drq)
        self.clock.advance(first)
        self.assertTrue(self.fdc.drq)

    def test_track_command_settle_can_skip_an_index(self):
        self.clock.advance(REVOLUTION-100)
        self.fdc.write(0x48, 0xE4)
        first = 2*REVOLUTION+MASTER_HZ*8//250000
        self.clock.advance(first-1)
        self.assertFalse(self.fdc.drq)
        self.clock.advance(first)
        self.assertTrue(self.fdc.drq)

    def test_failed_sector_search_counts_index_edges(self):
        self.clock.advance(REVOLUTION//2)
        self.fdc.sector = 99
        self.fdc.write(0x48, 0x80)
        self.clock.advance(5*REVOLUTION-1)
        self.assertTrue(self.fdc.busy)
        self.clock.advance(5*REVOLUTION)
        self.assertFalse(self.fdc.busy)

    def test_multisector_settles_once_and_crc_does_not_request_data(self):
        self.fdc.sector = 1
        self.fdc.write(0x48, 0x94)
        first_bytes = []
        count = 0
        while count < 257:
            self.clock.advance(self.clock.next_event)
            if self.fdc.drq:
                if count in (0, 256): first_bytes.append(self.clock.now)
                self.fdc.read(0x4B)
                count += 1
        assembled = MASTER_HZ*8//250000
        self.assertEqual(first_bytes, [REVOLUTION+assembled, REVOLUTION+REVOLUTION//18+assembled])

    def test_force_index_interrupt_rearms_until_cancelled(self):
        self.fdc.write(0x48, 0xD4)
        for index in range(1, 5):
            self.clock.advance(index*REVOLUTION)
            self.assertTrue(self.fdc.intrq)
            self.fdc.read(0x48)
            self.assertFalse(self.fdc.intrq)
        self.fdc.write(0x48, 0xD0)
        self.assertIsNone(self.clock.next_event)

    def test_rational_dd_and_sd_pacing_stays_exact_for_a_second(self):
        # Nominal data rate gives 31,250 DD bytes or 15,625 SD bytes/second.
        for control, count in ((0x29, 31250), (9, 15625)):
            self.fdc.write(0x40, control)
            self.fdc._phase = 0
            self.assertEqual(sum(self.fdc._byte_ticks() for _ in range(count)), MASTER_HZ)


class RasterTimingTests(unittest.TestCase):
    def test_long_run_raster_has_no_frame_accumulation_drift(self):
        clock, memory = Clock(), Memory(test_rom())
        timing = GimeTiming(clock, memory, trace=True)
        clock.advance(1000*FRAME_TICKS)
        self.assertEqual((timing.frame, timing.line, timing.start), (1000, 0, 239856000))
        falls = [at for at, name, _ in timing.events if name == 'hsync-fall']
        self.assertTrue(all(b-a == 912 for a, b in zip(falls, falls[1:])))

    def test_minimum_fast_timer_keeps_overlapping_irq_delays(self):
        for revision, period in ((1987, 8), (1986, 12)):
            clock, memory = Clock(), Memory(test_rom())
            timing = GimeTiming(clock, memory, revision=revision, trace=True)
            memory.regs[1], memory.regs[5] = 32, 1
            timing.register_write(0x94)
            clock.advance(200)
            expires = [at for at, name, _ in timing.events if name == 'timer-expire']
            irqs = [at for at, name, _ in timing.events if name == 'timer-irq']
            self.assertTrue(all(b-a == period for a, b in zip(expires, expires[1:])))
            self.assertEqual(irqs, [at+16 for at in expires if at+16 <= 200])
