"""Motorola 6809 interpreter with explicit E-cycle bus sequences.

Documented opcodes only. Memory is supplied by a bus exposing read8/write8.
References: VCC mc6809.cpp and MAME 6809 bus sequences. See NOTICE, LICENSE
(GPL-3.0-or-later) and LICENSE-MAME (BSD-3-Clause reference attribution).
"""

E, F, H, I, N, Z, V, C = 128, 64, 32, 16, 8, 4, 2, 1


def signed(value, bits=8):
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


class IllegalInstruction(RuntimeError):
    pass


class CPU6809:
    def __init__(self, bus):
        self.bus = bus
        self.read = bus.read8
        self.write = bus.write8
        self.idle_until = getattr(bus, 'idle_until', lambda cycles: None)
        self.dummy = getattr(bus, 'dummy_read', bus.read8)
        self.internal = getattr(bus, 'internal', self._internal)
        self.wait_cycle = getattr(bus, 'wait_cycle', lambda count=1: None)
        self.reset()

    def _internal(self, count=1):
        for _ in range(count):
            self.read(0xFFFF)

    @property
    def d(self):
        return self.a << 8 | self.b

    @d.setter
    def d(self, value):
        self.a, self.b = (value >> 8) & 255, value & 255

    def reset(self):
        self.a = self.b = self.dp = self.x = self.y = self.u = self.s = 0
        self.cc = I | F
        self.pc = self.read16(0xFFFE)
        self.cycles = self.instructions = 0
        self.irq = self.firq = self.nmi = False
        self.nmi_armed = False
        self.waiting = None
        self.extra = 0

    def state(self):
        return {r: getattr(self, r) for r in ('pc', 'a', 'b', 'x', 'y', 'u', 's', 'dp', 'cc')}

    def read16(self, addr):
        return self.read(addr & 65535) << 8 | self.read((addr + 1) & 65535)

    def write16(self, addr, value):
        self.write(addr & 65535, (value >> 8) & 255)
        self.write((addr + 1) & 65535, value & 255)

    def fetch(self):
        value = self.read(self.pc)
        self.pc = (self.pc + 1) & 65535
        return value

    def fetch16(self):
        return self.fetch() << 8 | self.fetch()

    def push8(self, stack, value):
        addr = (getattr(self, stack) - 1) & 65535
        setattr(self, stack, addr)
        self.write(addr, value & 255)

    def push16(self, stack, value):
        self.push8(stack, value)
        self.push8(stack, value >> 8)

    def pull8(self, stack):
        addr = getattr(self, stack)
        value = self.read(addr)
        setattr(self, stack, (addr + 1) & 65535)
        return value

    def pull16(self, stack):
        return self.pull8(stack) << 8 | self.pull8(stack)

    def nz(self, value, bits=8, clear_v=True):
        mask = (1 << bits) - 1
        value &= mask
        self.cc &= ~(N | Z | (V if clear_v else 0))
        if not value:
            self.cc |= Z
        if value & (1 << (bits - 1)):
            self.cc |= N
        return value

    def add(self, left, right, carry=0, bits=8):
        result = left + right + carry
        sign = 1 << (bits - 1)
        self.cc &= ~(N | Z | V | C | (H if bits == 8 else 0))
        if result >> bits:
            self.cc |= C
        if (~(left ^ right) & (left ^ result)) & sign:
            self.cc |= V
        if bits == 8 and (left ^ right ^ result) & 16:
            self.cc |= H
        return self.nz(result, bits, False)

    def sub(self, left, right, carry=0, bits=8):
        result = left - right - carry
        self.cc &= ~(N | Z | V | C)
        if result < 0:
            self.cc |= C
        if ((left ^ right) & (left ^ result)) & (1 << (bits - 1)):
            self.cc |= V
        return self.nz(result, bits, False)

    def indexed(self):
        post = self.fetch()
        reg = ('x', 'y', 'u', 's')[(post >> 5) & 3]
        base = getattr(self, reg)
        if not post & 128:
            self.extra += 1
            self.dummy(self.pc)
            self.internal()
            return (base + signed(post & 31, 5)) & 65535
        kind = post & 15
        indirect = bool(post & 16)
        if kind in (0, 2) and indirect:
            self.illegal(post, 'indexed postbyte')
        if kind == 0:
            addr = base
            setattr(self, reg, (base + 1) & 65535)
            self.extra += 2
        elif kind == 1:
            addr = base
            setattr(self, reg, (base + 2) & 65535)
            self.extra += 3
        elif kind == 2:
            addr = (base - 1) & 65535
            setattr(self, reg, addr)
            self.extra += 2
        elif kind == 3:
            addr = (base - 2) & 65535
            setattr(self, reg, addr)
            self.extra += 3
        elif kind == 4:
            addr = base
        elif kind in (5, 6):
            addr = base + signed(self.b if kind == 5 else self.a)
            self.extra += 1
        elif kind == 8:
            addr = base + signed(self.fetch())
            self.extra += 1
        elif kind == 9:
            addr = base + signed(self.fetch16(), 16)
            self.extra += 4
        elif kind == 11:
            addr = base + self.d
            self.extra += 4
        elif kind in (12, 13):
            offset = signed(self.fetch()) if kind == 12 else signed(self.fetch16(), 16)
            addr = self.pc + offset
            self.extra += 1 if kind == 12 else 5
        elif kind == 15 and indirect:
            addr = self.fetch16()
            self.extra += 2
        else:
            self.illegal(post, 'indexed postbyte')
        addr &= 65535
        if kind in (0, 1, 2, 3, 4, 5, 6, 8, 11):
            self.dummy(self.pc)
        if kind == 11:
            self.dummy((self.pc + 1) & 65535)
        idle = {0: 2, 1: 3, 2: 2, 3: 3, 5: 1, 6: 1, 9: 3, 11: 3, 12: 1, 13: 4, 15: 1}.get(kind, 0)
        self.internal(idle)
        if indirect:
            self.extra += 3
            addr = self.read16(addr)
            self.internal()
        return addr

    def address(self, mode):
        if mode == 1:
            addr = self.dp << 8 | self.fetch()
            self.internal()
            return addr
        if mode == 2:
            return self.indexed()
        if mode == 3:
            addr = self.fetch16()
            self.internal()
            return addr
        raise ValueError('Immediate operand has no effective address')

    def branch(self, cond):
        n, z, v, c = bool(self.cc & N), bool(self.cc & Z), bool(self.cc & V), bool(self.cc & C)
        return (True, False, not (c or z), c or z, not c, c, not z, z,
                not v, v, not n, n, n == v, n != v,
                not z and n == v, z or n != v)[cond]

    def stack_mask(self, stack, mask, pull=False):
        other = 'u' if stack == 's' else 's'
        regs = ('cc', 'a', 'b', 'dp', 'x', 'y', other, 'pc')
        size = 0
        for bit in (range(8) if pull else range(7, -1, -1)):
            if mask & (1 << bit):
                reg = regs[bit]
                wide = bit >= 4
                size += 2 if wide else 1
                if pull:
                    value = self.pull16(stack) if wide else self.pull8(stack)
                    setattr(self, reg, value)
                elif wide:
                    self.push16(stack, getattr(self, reg))
                else:
                    self.push8(stack, getattr(self, reg))
        return size

    def interrupt(self, vector, fast=False, masks=I, prefix=0, software=False):
        already_stacked = self.waiting == 'cwai'
        self.waiting = None
        if not already_stacked:
            self.dummy(self.pc)
            if not software:
                self.dummy(self.pc)
            self.internal()
        if not already_stacked:
            if fast:
                self.cc &= ~E
                self.push16('s', self.pc)
                self.push8('s', self.cc)
            else:
                self.cc |= E
                self.stack_mask('s', 255)
        self.cc |= masks
        self.internal()
        self.pc = self.read16(vector)
        self.internal()
        return (4 if already_stacked else (10 if fast else 19)) + prefix

    def illegal(self, opcode, kind='opcode'):
        raise IllegalInstruction(f'Unsupported {kind} ${opcode:02X} near PC=${self.pc:04X}')

    def step(self):
        if self.waiting == 'sync' and (self.irq or self.firq or self.nmi):
            self.waiting = None
        if self.nmi and self.nmi_armed:
            self.nmi = False
            cycles = self.interrupt(0xFFFC, masks=I | F)
        elif self.firq and not self.cc & F:
            cycles = self.interrupt(0xFFF6, fast=True, masks=I | F)
        elif self.irq and not self.cc & I:
            cycles = self.interrupt(0xFFF8)
        elif self.waiting:
            self.wait_cycle()
            cycles = 1
        else:
            self.extra = 0
            opcode = self.fetch()
            cycles = self.execute(opcode) + self.extra
            self.instructions += 1
        self.cycles += cycles
        return cycles

    def execute(self, op):
        if op in (0x10, 0x11):
            return self.prefixed(op, self.fetch())
        if 0x20 <= op <= 0x2F:
            offset = signed(self.fetch())
            self.internal()
            if self.branch(op & 15):
                self.pc = (self.pc + offset) & 65535
            return 3
        if op >= 0x80:
            return self.group(op)
        if op < 0x10 or 0x40 <= op <= 0x7F:
            return self.unary(op)
        if op == 0x12:
            self.dummy(self.pc)
            return 2
        if op == 0x13:
            self.dummy(self.pc)
            self.wait_cycle(2)
            self.waiting = 'sync'
            return 4
        if op in (0x16, 0x17):
            offset = signed(self.fetch16(), 16)
            if op == 0x17:
                self.internal(4)
                self.push16('s', self.pc)
            else:
                self.internal(2)
            self.pc = (self.pc + offset) & 65535
            return 9 if op == 0x17 else 5
        if op == 0x19:
            self.dummy(self.pc)
            value = self.a
            adjust = 6 if (value & 15) > 9 or self.cc & H else 0
            if value > 0x99 or self.cc & C or ((value & 0xF0) > 0x80 and (value & 15) > 9):
                adjust |= 0x60
            result = value + adjust
            if result > 255:
                self.cc |= C
            self.a = self.nz(result, clear_v=False)
            return 2
        if op == 0x1A:
            self.cc |= self.fetch()
            self.dummy(self.pc)
            return 3
        if op == 0x1C:
            self.cc &= self.fetch()
            self.dummy(self.pc)
            return 3
        if op == 0x1D:
            self.dummy(self.pc)
            self.a = 255 if self.b & 128 else 0
            self.nz(self.d, 16, False)
            return 2
        if op in (0x1E, 0x1F):
            post = self.fetch()
            regs = {0: 'd', 1: 'x', 2: 'y', 3: 'u', 4: 's', 5: 'pc',
                    8: 'a', 9: 'b', 10: 'cc', 11: 'dp'}
            src, dst = post >> 4, post & 15
            if src not in regs or dst not in regs:
                self.illegal(post, 'transfer register')
            left, right = getattr(self, regs[src]), getattr(self, regs[dst])
            # Mixed-width transfers are undefined; use the common NMOS behavior.
            if (src < 8) != (dst < 8):
                left = (left | 0xFF00) if src >= 8 else left & 255
                right = (right | 0xFF00) if dst >= 8 else right & 255
            setattr(self, regs[dst], left)
            if op == 0x1E:
                setattr(self, regs[src], right)
            if dst == 4 and op == 0x1F:
                self.nmi_armed = True
            self.internal(6 if op == 0x1E else 4)
            return 8 if op == 0x1E else 6
        if 0x30 <= op <= 0x33:
            value = self.indexed()
            reg = ('x', 'y', 's', 'u')[op - 0x30]
            setattr(self, reg, value)
            if op < 0x32:
                self.cc = (self.cc & ~Z) | (Z if not value else 0)
            if reg == 's':
                self.nmi_armed = True
            self.internal()
            return 4
        if 0x34 <= op <= 0x37:
            mask = self.fetch()
            stack = 'u' if op & 2 else 's'
            self.internal(2)
            if not op & 1:
                self.dummy(getattr(self, stack))
            size = self.stack_mask(stack, mask, bool(op & 1))
            if op & 1:
                self.dummy(getattr(self, stack))
            return 5 + size
        if op == 0x39:
            self.dummy(self.pc)
            self.pc = self.pull16('s')
            self.dummy(self.s)
            return 5
        if op == 0x3A:
            self.dummy(self.pc)
            self.internal()
            self.x = (self.x + self.b) & 65535
            return 3
        if op == 0x3B:
            self.dummy(self.pc)
            self.cc = self.pull8('s')
            full = bool(self.cc & E)
            if full:
                self.stack_mask('s', 0x7E, True)
            self.pc = self.pull16('s')
            self.dummy(self.s)
            return 15 if full else 6
        if op == 0x3C:
            self.cc &= self.fetch()
            self.cc |= E
            self.dummy(self.pc)
            self.internal()
            self.stack_mask('s', 255)
            self.waiting = 'cwai'
            # CWAI's documented 20 cycles span entry (16) and vectoring (4).
            # Waiting time between them releases the bus and is counted separately.
            return 16
        if op == 0x3D:
            self.dummy(self.pc)
            self.internal(9)
            self.d = self.a * self.b
            self.cc = (self.cc & ~(Z | C)) | (Z if self.d == 0 else 0) | (C if self.b & 128 else 0)
            return 11
        if op == 0x3F:
            return self.interrupt(0xFFFA, masks=I | F, software=True)
        self.illegal(op)

    def unary(self, op):
        kind, high = op & 15, op >> 4
        if kind not in (0, 3, 4, 6, 7, 8, 9, 10, 12, 13, 14, 15):
            self.illegal(op)
        register = high in (4, 5)
        if register:
            if kind == 14:
                self.illegal(op)
            reg = 'a' if high == 4 else 'b'
            value = getattr(self, reg)
            cycles = 2
        else:
            addr = self.address({0: 1, 6: 2, 7: 3}[high])
            if kind == 14:
                self.pc = addr
                return 4 if high == 7 else 3
            cycles = 7 if high == 7 else 6
            value = self.read(addr)
        if kind == 0:
            result = self.sub(0, value)
        elif kind == 3:
            result = self.nz(value ^ 255)
            self.cc |= C
        elif kind in (4, 6, 7):
            carry = self.cc & C
            result = value >> 1
            if kind == 6:
                result |= carry << 7
            elif kind == 7:
                result |= value & 128
            self.cc = (self.cc & ~C) | (value & 1)
            result = self.nz(result, clear_v=False)
        elif kind in (8, 9):
            result = (value << 1) | ((self.cc & C) if kind == 9 else 0)
            self.cc &= ~(C | V)
            if value & 128:
                self.cc |= C
            if ((result >> 7) ^ (result >> 8)) & 1:
                self.cc |= V
            result = self.nz(result, clear_v=False)
        elif kind == 10:
            result = self.nz(value - 1)
            if value == 128:
                self.cc |= V
        elif kind == 12:
            result = self.nz(value + 1)
            if value == 127:
                self.cc |= V
        elif kind == 13:
            self.nz(value)
            self.internal(1 if register else 2)
            return cycles
        else:
            result = 0
            self.cc = (self.cc & ~(N | V | C)) | Z
        if register:
            self.dummy(self.pc)
            setattr(self, reg, result)
        else:
            self.dummy(self.pc)
            self.write(addr, result)
        return cycles

    def group(self, op):
        mode = (op >> 4) & 3
        kind = op & 15
        group_b = bool(op & 0x40)
        reg = 'b' if group_b else 'a'
        if kind == 13 and not group_b:
            if mode == 0:
                offset = signed(self.fetch())
                addr = (self.pc + offset) & 65535
                cycles = 7
                self.internal(3)
            else:
                addr = self.address(mode)
                cycles = 8 if mode == 3 else 7
                self.dummy(self.pc)
                self.internal()
            self.push16('s', self.pc)
            self.pc = addr
            return cycles
        wide = kind in (3, 12, 14, 15) or (group_b and kind == 13)
        store = kind in (7, 15) or (group_b and kind == 13)
        cycles = (2, 4, 4, 5)[mode] + int(wide)
        if kind == 3 or (kind == 12 and not group_b):
            cycles += 1
        if mode == 0:
            if store:
                self.illegal(op)
            value = self.fetch16() if wide else self.fetch()
        else:
            addr = self.address(mode)
            value = 0 if store else (self.read16(addr) if wide else self.read(addr))
        if kind == 3 or (kind == 12 and not group_b):
            self.dummy(self.pc)
        if kind in (0, 1, 2):
            result = self.sub(getattr(self, reg), value, (self.cc & C) if kind == 2 else 0)
            if kind != 1:
                setattr(self, reg, result)
        elif kind == 3:
            self.d = self.add(self.d, value, bits=16) if group_b else self.sub(self.d, value, bits=16)
        elif kind in (4, 5, 8, 10):
            left = getattr(self, reg)
            result = left & value if kind in (4, 5) else (left ^ value if kind == 8 else left | value)
            result = self.nz(result)
            if kind != 5:
                setattr(self, reg, result)
        elif kind == 6:
            setattr(self, reg, self.nz(value))
        elif kind == 7:
            self.write(addr, self.nz(getattr(self, reg)))
        elif kind in (9, 11):
            setattr(self, reg, self.add(getattr(self, reg), value, (self.cc & C) if kind == 9 else 0))
        elif kind == 12:
            if group_b:
                self.d = self.nz(value, 16)
            else:
                self.sub(self.x, value, bits=16)
        elif kind == 13:
            self.write16(addr, self.nz(self.d, 16))
        elif kind == 14:
            setattr(self, 'u' if group_b else 'x', self.nz(value, 16))
        elif kind == 15:
            self.write16(addr, self.nz(self.u if group_b else self.x, 16))
        return cycles

    def prefixed(self, page, op):
        if page == 0x10 and 0x21 <= op <= 0x2F:
            offset = signed(self.fetch16(), 16)
            take = self.branch(op & 15)
            self.internal(2 if take else 1)
            if take:
                self.pc = (self.pc + offset) & 65535
            return 6 if take else 5
        if op == 0x3F:
            return self.interrupt(0xFFF4 if page == 0x10 else 0xFFF2, masks=0, prefix=1, software=True)
        mode = (op >> 4) & 3
        kind = op & 15
        reg = None
        store = False
        compare = False
        if page == 0x10:
            if 0x80 <= op < 0xC0 and kind in (3, 12):
                reg = 'd' if kind == 3 else 'y'
                compare = True
            elif op >= 0x80 and kind in (14, 15):
                reg = 's' if op & 0x40 else 'y'
                store = kind == 15
        elif 0x80 <= op < 0xC0 and kind in (3, 12):
            reg = 'u' if kind == 3 else 's'
            compare = True
        if reg is None or (mode == 0 and store):
            self.illegal((page << 8) | op)
        cycles = (4, 6, 6, 7)[mode] + int(compare)
        if mode == 0:
            value = self.fetch16()
        else:
            addr = self.address(mode)
            value = 0 if store else self.read16(addr)
        if compare:
            self.dummy(self.pc)
            self.sub(getattr(self, reg), value, bits=16)
        elif store:
            self.write16(addr, self.nz(getattr(self, reg), 16))
        else:
            setattr(self, reg, self.nz(value, 16))
            if reg == 's':
                self.nmi_armed = True
        return cycles
