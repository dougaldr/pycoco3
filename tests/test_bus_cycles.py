"""Cycle traces checked against the 6809 bus sequences in MAME's reference core."""
import unittest
from coco.clock import Clock, TimedBus
from coco.cpu6809 import CPU6809, IllegalInstruction, E, I, F


class TraceMemory:
    def __init__(self):
        self.data = bytearray(65536)
        self.reads = []
    def cycle_ticks(self, address): return 16
    def read8(self, address):
        self.reads.append(address)
        return self.data[address]
    def write8(self, address, value): self.data[address] = value


class BusCycleTests(unittest.TestCase):
    def setup_cpu(self, program):
        m, clock = TraceMemory(), Clock()
        bus = TimedBus(m, clock, trace=True)
        cpu = CPU6809(bus)
        m.data[0x1000:0x1000+len(program)] = bytes(program)
        cpu.pc, cpu.s, cpu.x, cpu.y, cpu.u = 0x1000, 0x8000, 0x2000, 0x3000, 0x4000
        bus.enabled = True
        self.m, self.clock, self.bus, self.cpu = m, clock, bus, cpu
        return cpu

    def step(self):
        self.bus.begin()
        cycles = self.cpu.step()
        self.assertEqual(self.bus.used, cycles, f'Unspecified cycles at PC={self.cpu.pc:04x}')
        return [(kind, addr) for _, kind, addr, _ in self.bus.events]

    def test_load_store_rmw_and_16bit_arithmetic_order(self):
        cases = [
            ([0xB6, 0x20, 0], [('read', 0x1000), ('read', 0x1001), ('read', 0x1002), ('internal', 0xFFFF), ('read', 0x2000)]),
            ([0xB7, 0x20, 0], [('read', 0x1000), ('read', 0x1001), ('read', 0x1002), ('internal', 0xFFFF), ('write', 0x2000)]),
            ([0x0C, 0x20], [('read', 0x1000), ('read', 0x1001), ('internal', 0xFFFF), ('read', 0x20), ('dummy', 0x1002), ('write', 0x20)]),
            ([0x93, 0x20], [('read', 0x1000), ('read', 0x1001), ('internal', 0xFFFF), ('read', 0x20), ('read', 0x21), ('dummy', 0x1002)]),
        ]
        for code, expected in cases:
            with self.subTest(code=code):
                self.setup_cpu(code)
                self.assertEqual(self.step(), expected)
                self.assertEqual([t for t, *_ in self.bus.events], list(range(16, 16*(len(expected)+1), 16)))

    def test_indirect_indexed_pointer_reads_follow_address_calculation(self):
        self.setup_cpu([0xA6, 0x98, 4])  # LDA [4,X]
        self.m.data[0x2004:0x2006] = b'\x40\x00'
        self.m.data[0x4000] = 99
        self.assertEqual(self.step(), [('read',0x1000), ('read',0x1001), ('read',0x1002),
            ('dummy',0x1003), ('read',0x2004), ('read',0x2005), ('internal',0xFFFF), ('read',0x4000)])
        self.assertEqual(self.cpu.a, 99)

    def test_stack_dummy_reads_and_subroutine_order(self):
        self.setup_cpu([0x34, 2])
        self.assertEqual(self.step(), [('read',0x1000),('read',0x1001),('internal',0xFFFF),
            ('internal',0xFFFF),('dummy',0x8000),('write',0x7FFF)])
        self.setup_cpu([0x35, 2])
        self.assertEqual(self.step()[-2:], [('read',0x8000),('dummy',0x8001)])
        self.setup_cpu([0x39])
        self.assertEqual(self.step(), [('read',0x1000),('dummy',0x1001),('read',0x8000),('read',0x8001),('dummy',0x8002)])
        self.setup_cpu([0x17, 0, 2])
        self.assertEqual(self.step()[-2:], [('write',0x7FFF),('write',0x7FFE)])

    def test_irq_and_swi_stack_and_vector_cycles(self):
        self.setup_cpu([0x12])
        self.cpu.cc, self.cpu.irq = 0, True
        trace = self.step()
        self.assertEqual(trace[:3], [('dummy',0x1000),('dummy',0x1000),('internal',0xFFFF)])
        self.assertEqual(trace[3:15], [('write',p) for p in range(0x7FFF,0x7FF3,-1)])
        self.assertEqual(trace[-4:], [('internal',0xFFFF),('read',0xFFF8),('read',0xFFF9),('internal',0xFFFF)])
        for program in ([0x3F], [0x10,0x3F], [0x11,0x3F]):
            self.setup_cpu(program)
            trace = self.step()
            self.assertEqual(len(trace), 19 + (len(program)-1))

    def test_dummy_read_has_real_io_side_effects(self):
        from coco.memory import Memory
        from test_hardware import test_rom
        m, clock = Memory(test_rom()), Clock()
        bus = TimedBus(m, clock, trace=True)
        cpu = CPU6809(bus)
        # NOP at FEFF; its dummy read at FF00 acknowledges a PIA interrupt.
        m.regs[0] = 8
        m.ram[0x1FEFF] = 0x12
        m.pia0.control[0] = 0x85
        cpu.pc = 0xFEFF
        bus.enabled = True
        bus.begin()
        self.assertEqual(cpu.step(), 2)
        self.assertFalse(m.pia0.irq)
        self.assertEqual(bus.events[-1][1:3], ('dummy',0xFF00))

    def test_all_documented_opcodes_have_explicit_cycle_sequences(self):
        tested = 0
        for prefix in ([], [0x10], [0x11]):
            for opcode in range(256):
                self.setup_cpu(prefix + [opcode, 0x84, 0x20, 0, 0])
                try:
                    self.step()
                except IllegalInstruction:
                    continue
                except AssertionError as error:
                    self.fail(f'{prefix + [opcode]}: {error}')
                tested += 1
        self.assertGreater(tested, 260)

    def test_every_valid_indexed_postbyte_has_explicit_cycles(self):
        for opcode in (0xA6, 0xA3, 0xAD, 0x6C, 0x6E, 0x30):
            for postbyte in range(256):
                self.setup_cpu([opcode, postbyte, 0, 4])
                try: self.step()
                except IllegalInstruction: continue
                except AssertionError as error: self.fail(f'{opcode:02x} {postbyte:02x}: {error}')

    def test_event_between_rmw_read_and_write(self):
        self.setup_cpu([0x0C,0x20])
        self.m.data[0x20] = 8
        observed = []
        self.clock.at(4*16+1, lambda: observed.append(self.m.data[0x20]))
        self.clock.at(6*16+1, lambda: observed.append(self.m.data[0x20]))
        self.step()
        self.clock.advance(97)
        self.assertEqual(observed, [8,9])

    def test_cwai_releases_bus_then_vectors_without_restacking(self):
        self.setup_cpu([0x3C,0xEF])
        self.step()
        self.assertEqual(self.cpu.cycles,16)
        stack=self.cpu.s
        reads=len(self.m.reads)
        self.bus.events.clear()
        self.step()
        self.assertEqual(len(self.m.reads),reads)
        self.assertEqual(self.bus.events[-1][1:],('wait',None,None))
        self.bus.events.clear()
        self.cpu.irq=True
        self.assertEqual(self.step(),[('internal',0xFFFF),('read',0xFFF8),('read',0xFFF9),('internal',0xFFFF)])
        self.assertEqual(self.cpu.s,stack)
        self.assertEqual(self.cpu.cycles,21)  # 20 active cycles + one waiting cycle
