import unittest
from coco.cpu6809 import CPU6809, IllegalInstruction, E, F, H, I, N, Z, V, C


class RAM:
    def __init__(self):
        self.data = bytearray(65536)
    def read8(self, addr):
        return self.data[addr & 65535]
    def write8(self, addr, value):
        self.data[addr & 65535] = value & 255


class CPUTests(unittest.TestCase):
    def setUp(self):
        self.bus = RAM()
        self.cpu = CPU6809(self.bus)
        self.cpu.pc = 0x1000
        self.cpu.s = 0x8000

    def program(self, data):
        self.bus.data[0x1000:0x1000+len(data)] = bytes(data)
        self.cpu.pc = 0x1000

    def test_reset_vector_and_masks(self):
        self.bus.data[0xFFFE:] = bytes.fromhex('1234')
        self.cpu.reset()
        self.assertEqual((self.cpu.pc, self.cpu.cc), (0x1234, I | F))

    def test_all_8bit_add_sub_operands_and_carries(self):
        c = self.cpu
        # Execute real immediate opcodes; expected overflow uses signed arithmetic.
        for op, subtract, with_carry in ((0x8B, False, False), (0x89, False, True),
                                        (0x80, True, False), (0x82, True, True)):
            self.program([op, 0])
            for left in range(256):
                for right in range(256):
                    for carry in (range(2) if with_carry else (0,)):
                        c.pc, c.a, c.cc = 0x1000, left, I | F | H | carry
                        self.bus.data[0x1001] = right
                        cycles = c.step()
                        unsigned = left - right - carry if subtract else left + right + carry
                        sl, sr = (left if left < 128 else left-256), (right if right < 128 else right-256)
                        sv = sl - sr - carry if subtract else sl + sr + carry
                        result = unsigned & 255
                        expected = (N if result >= 128 else 0) | (Z if result == 0 else 0)
                        expected |= V if not -128 <= sv <= 127 else 0
                        expected |= C if unsigned < 0 or unsigned > 255 else 0
                        if c.a != result or c.cc & 15 != expected or cycles != 2:
                            self.fail((hex(op), left, right, carry, c.state(), expected))
                        self.assertEqual(c.cc & (I | F), I | F)
                        self.assertEqual(bool(c.cc & H), True if subtract else (left & 15)+(right & 15)+carry > 15)

    def test_daa_all_decimal_pairs(self):
        c = self.cpu
        self.program([0x8B, 0, 0x19])
        for a in range(100):
            for b in range(100):
                c.pc, c.cc = 0x1000, 0
                c.a = (a // 10) * 16 + a % 10
                self.bus.data[0x1001] = (b // 10) * 16 + b % 10
                c.step(); c.step()
                total = (a + b) % 100
                self.assertEqual(c.a, (total // 10)*16 + total % 10)
                self.assertEqual(bool(c.cc & C), a+b >= 100)

    def test_sixteen_bit_arithmetic_flags(self):
        for op, initial, operand, result, flags in (
            (0xC3, 0x7FFF, 1, 0x8000, N | V),
            (0xC3, 0xFFFF, 1, 0, Z | C),
            (0x83, 0, 1, 0xFFFF, N | C),
            (0x83, 0x8000, 1, 0x7FFF, V),
        ):
            self.program([op, operand >> 8, operand & 255])
            self.cpu.d, self.cpu.cc = initial, H
            self.assertEqual(self.cpu.step(), 4)
            self.assertEqual((self.cpu.d, self.cpu.cc), (result, flags | H))

    def test_indexed_addressing_and_cycles(self):
        # LDA indexed, all base registers, displacement signs, update and indirection.
        cases = [([0x00], 0x2000, 0x2000, 5), ([0x1F], 0x1FFF, 0x2000, 5),
                 ([0x80], 0x2000, 0x2001, 6), ([0x81], 0x2000, 0x2002, 7),
                 ([0x82], 0x1FFF, 0x1FFF, 6), ([0x83], 0x1FFE, 0x1FFE, 7),
                 ([0x84], 0x2000, 0x2000, 4), ([0x85], 0x1FFF, 0x2000, 5),
                 ([0x86], 0x1FFE, 0x2000, 5), ([0x88, 0x80], 0x1F80, 0x2000, 5),
                 ([0x89, 0xFE, 0], 0x1E00, 0x2000, 8),
                 ([0x8B], 0x1EFF, 0x2000, 8),
                 ([0x8C, 0xFD], 0x1000, 0x2000, 5),
                 ([0x8D, 0x0F, 0xFC], 0x2000, 0x2000, 9)]
        for regno, reg in enumerate(('x', 'y', 'u', 's')):
            for encoded, addr, final, cycles in cases:
                with self.subTest(reg=reg, encoded=encoded):
                    post = [encoded[0] | (regno << 5), *encoded[1:]]
                    self.program([0xA6, *post])
                    setattr(self.cpu, reg, 0x2000)
                    self.cpu.a, self.cpu.b = 0xFE, 0xFF
                    if addr != 0x1000:
                        self.bus.data[addr] = 0x5A
                    self.assertEqual(self.cpu.step(), cycles)
                    self.assertEqual(self.cpu.a, 0xA6 if addr == 0x1000 else 0x5A)
                    self.assertEqual(getattr(self.cpu, reg), final)
        self.program([0xA6, 0x9F, 0x20, 0])
        self.bus.data[0x2000:0x2002] = bytes.fromhex('3000')
        self.bus.data[0x3000] = 77
        self.assertEqual(self.cpu.step(), 9)
        self.assertEqual(self.cpu.a, 77)

    def test_direct_page_and_word_wrap(self):
        self.program([0xDC, 0xFF])  # LDD <$FF with DP=$FF wraps address $FFFF.
        self.cpu.dp = 255
        self.bus.data[65535], self.bus.data[0] = 0xAB, 0xCD
        self.cpu.step()
        self.assertEqual(self.cpu.d, 0xABCD)

    def test_subroutine_and_stack_layout(self):
        self.program([0x8D, 3, 0x12, 0x20, 0xFE, 0x86, 42, 0x39])
        self.assertEqual(self.cpu.step(), 7)
        self.assertEqual(bytes(self.bus.data[0x7FFE:0x8000]), b'\x10\x02')
        self.cpu.step(); self.cpu.step()
        self.assertEqual((self.cpu.a, self.cpu.pc, self.cpu.s), (42, 0x1002, 0x8000))

    def test_push_pull_all_registers(self):
        self.program([0x34, 0x7F, 0x35, 0x7F])
        c = self.cpu
        c.a, c.b, c.dp, c.cc, c.x, c.y, c.u = 1, 2, 3, 4, 0x4567, 0x6789, 0xABCD
        before = c.state()
        self.assertEqual(c.step(), 15)
        c.a = c.b = c.dp = c.cc = c.x = c.y = c.u = 0
        self.assertEqual(c.step(), 15)
        after = c.state()
        before.pop('pc'); after.pop('pc')
        self.assertEqual(before, after)

    def test_irq_rti_restores_entire_state(self):
        c = self.cpu
        c.cc, c.a, c.b, c.x = H | C, 12, 34, 0x5678
        before = c.state()
        self.bus.data[0xFFF8:0xFFFA] = bytes.fromhex('4000')
        self.bus.data[0x4000] = 0x3B
        c.irq = True
        self.assertEqual(c.step(), 19)
        self.assertEqual(c.s, 0x8000 - 12)
        self.assertEqual(self.bus.data[c.s], before['cc'] | E)
        c.irq = False
        self.assertEqual(c.step(), 15)
        before['cc'] |= E
        self.assertEqual(c.state(), before)

    def test_firq_short_stack_and_priority(self):
        c = self.cpu
        c.cc = 0
        self.bus.data[0xFFF6:0xFFF8] = bytes.fromhex('4000')
        self.bus.data[0x4000] = 0x3B
        c.irq = c.firq = True
        self.assertEqual(c.step(), 10)
        self.assertEqual((c.pc, c.s, c.cc), (0x4000, 0x7FFD, I | F))
        c.irq = c.firq = False
        self.assertEqual(c.step(), 6)
        self.assertEqual((c.pc, c.s, c.cc), (0x1000, 0x8000, 0))

    def test_cwai_only_stacks_once(self):
        self.program([0x3C, 0xEF])
        c = self.cpu
        self.assertEqual(c.step(), 16)
        stack = c.s
        self.assertEqual(c.step(), 1)
        c.irq = True
        self.assertEqual(c.step(), 4)
        self.assertEqual(c.s, stack)
        self.assertIsNone(c.waiting)

    def test_sync_wakes_even_for_masked_irq(self):
        self.program([0x13, 0x12])
        c = self.cpu
        c.step()
        self.assertEqual(c.step(), 1)
        c.irq = True
        self.assertEqual(c.step(), 2)
        self.assertEqual(c.pc, 0x1002)

    def test_nmi_waits_until_lds(self):
        self.program([0x10, 0xCE, 0x80, 0])
        self.bus.data[0xFFFC:0xFFFE] = bytes.fromhex('3456')
        self.cpu.nmi = True
        self.assertEqual(self.cpu.step(), 4)
        self.assertEqual(self.cpu.step(), 19)
        self.assertEqual(self.cpu.pc, 0x3456)

    def test_tfr_to_s_arms_nmi_but_exg_does_not(self):
        for op, armed in ((0x1E,False),(0x1F,True)):
            self.program([op,0x14])
            self.cpu.nmi_armed=False
            self.cpu.x=0x7000
            self.cpu.step()
            self.assertEqual(self.cpu.s,0x7000)
            self.assertEqual(self.cpu.nmi_armed,armed)

    def test_long_branch_cycles_and_flags(self):
        self.program([0x10, 0x26, 0xFF, 0xFC])
        self.cpu.cc = 0
        self.assertEqual(self.cpu.step(), 6)
        self.assertEqual(self.cpu.pc, 0x1000)
        self.cpu.cc = Z
        self.assertEqual(self.cpu.step(), 5)
        self.assertEqual(self.cpu.pc, 0x1004)

    def test_prefix_load_compare_store(self):
        self.program([0x10, 0x8E, 0x12, 0x34, 0x10, 0xBF, 0x20, 0,
                      0x10, 0x8C, 0x12, 0x34, 0x11, 0x83, 0, 1])
        c = self.cpu
        self.assertEqual(c.step(), 4)
        self.assertEqual(c.step(), 7)
        self.assertEqual(bytes(self.bus.data[0x2000:0x2002]), b'\x12\x34')
        self.assertEqual(c.step(), 5)
        self.assertTrue(c.cc & Z)
        self.assertEqual(c.step(), 5)
        self.assertEqual(c.cc & (N | Z | V | C), N | C)

    def test_unary_corner_flags(self):
        for op, initial, flags, value, expected in (
            (0x40, 128, H, 128, H | N | V | C),
            (0x43, 255, 0, 0, Z | C),
            (0x44, 1, V, 0, V | Z | C),
            (0x46, 0, C, 128, N),
            (0x47, 129, V, 192, V | N | C),
            (0x48, 128, 0, 0, Z | V | C),
            (0x49, 64, C, 129, N | V),
            (0x4A, 128, C, 127, V | C),
            (0x4C, 127, C, 128, N | V | C),
            (0x4D, 0, C | V | N, 0, C | Z),
            (0x4F, 255, 255, 0, E | F | H | I | Z),
        ):
            self.program([op])
            self.cpu.a, self.cpu.cc = initial, flags
            self.assertEqual(self.cpu.step(), 2)
            self.assertEqual((self.cpu.a, self.cpu.cc), (value, expected))

    def test_sex_mul_and_register_transfers(self):
        self.program([0x1D, 0x1F, 0x01, 0x1E, 0x12, 0x3D])
        c = self.cpu
        c.b, c.y, c.cc = 128, 0x1234, V | C
        c.step()
        self.assertEqual((c.d, c.cc), (0xFF80, N | V | C))
        c.step(); c.step()
        self.assertEqual((c.x, c.y), (0x1234, 0xFF80))
        c.a, c.b = 16, 16
        c.step()
        self.assertEqual(c.d, 256)
        self.assertFalse(c.cc & (C | Z))

    def test_unsupported_opcode_fails_loudly(self):
        self.program([0x02])
        with self.assertRaises(IllegalInstruction):
            self.cpu.step()
