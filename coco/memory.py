"""CoCo 3 memory, PIA and GIME registers. GPL-3.0-or-later; see NOTICE.

Mapping/register behavior follows VCC; peripherals not installed return $FF.
"""
from pathlib import Path

KEY_ROWS = (
    ('@', 'a', 'b', 'c', 'd', 'e', 'f', 'g'),
    ('h', 'i', 'j', 'k', 'l', 'm', 'n', 'o'),
    ('p', 'q', 'r', 's', 't', 'u', 'v', 'w'),
    ('x', 'y', 'z', 'up', 'down', 'left', 'right', 'space'),
    ('0', '1', '2', '3', '4', '5', '6', '7'),
    ('8', '9', ':', ';', ',', '-', '.', '/'),
    ('return', 'clear', 'break', 'alt', 'ctrl', 'f1', 'f2', 'shift'),
)
KEY_POSITION = {key: (row, col) for row, keys in enumerate(KEY_ROWS)
                for col, key in enumerate(keys)}
SHIFTED = dict(zip('!"#$%&\'()*+<=>?', '123456789:;,-./'))
# BASIC uses the arrow keys for these characters. Ctrl-7 for '^' belongs
# to OS-9's keyboard driver and is not a BASIC character sequence.
BASIC_SYMBOLS = {'^': {'up'}, '_': {'shift', 'up'},
                 '[': {'shift', 'down'}, ']': {'shift', 'right'}}


def character_keys(char):
    """Translate text into actual keyboard matrix switches for paste/tests."""
    if char in ('\r', '\n'):
        return {'return'}
    if char == ' ':
        return {'space'}
    if char in SHIFTED:
        return {SHIFTED[char], 'shift'}
    if char in BASIC_SYMBOLS:
        return set(BASIC_SYMBOLS[char])
    if char.lower() in KEY_POSITION:
        return {char.lower()}
    raise ValueError(f'Character not available on the CoCo keyboard: {char!r}')


def load_rom(path):
    path = Path(path).expanduser()
    data = path.read_bytes()
    if len(data) != 32768:
        raise ValueError(f'CoCo 3 system ROM must be exactly 32768 bytes; {path} has {len(data)}')
    vector = int.from_bytes(data[-2:], 'big')
    if not 0x8000 <= vector < 0xFF00:
        raise ValueError(f'Invalid CoCo 3 reset vector ${vector:04X} in {path}')
    return data


class PIA:
    def __init__(self):
        self.data = [0, 0]
        self.ddr = [0, 0]
        self.control = [0, 0]

    @property
    def irq(self):
        return (self.control[0] & 0x81 == 0x81 or self.control[1] & 0x81 == 0x81)

    def edge(self, side, rising):
        if bool(self.control[side] & 2) == rising:
            self.control[side] |= 0x80

    def read(self, port, pins=255):
        side = port >> 1
        if port & 1:
            return self.control[side]
        if not self.control[side] & 4:
            return self.ddr[side]
        self.control[side] &= 63
        return (self.data[side] & self.ddr[side]) | (pins & ~self.ddr[side] & 255)

    def write(self, port, value):
        side = port >> 1
        if port & 1:
            self.control[side] = (self.control[side] & 0xC0) | (value & 63)
        elif self.control[side] & 4:
            self.data[side] = value
        else:
            self.ddr[side] = value


class Memory:
    def __init__(self, rom, ram_kb=128, cart_rom=None):
        if ram_kb not in (128, 512):
            raise ValueError('Supported RAM sizes: 128 or 512 KB')
        if len(rom) != 32768:
            raise ValueError('Expected a 32 KB system ROM')
        self.rom = bytes(rom)
        self.cart_rom = bytes(cart_rom) if cart_rom else None
        self.fdc = None
        self.on_write = None
        self.before_write = None
        self.ram = bytearray(ram_kb * 1024)
        self.mask = len(self.ram) - 1
        self.bank_mask = self.mask >> 13
        self.reset()

    def reset(self, cold=False):
        if cold:
            self.ram[:] = bytes(len(self.ram))
        self.regs = bytearray(48)  # $FF90-$FFBF
        self.regs[16:32] = bytes(range(0x38, 0x40)) * 2
        self.pia0, self.pia1 = PIA(), PIA()
        self.sam = 0
        self.keys = set()
        self.irq_pending = self.firq_pending = 0
        self.last_bus = 255

    @property
    def fast(self):
        return bool(self.sam & 0x1800)

    def cycle_ticks(self, address):
        speed = (self.sam >> 11) & 3
        if speed == 0:
            return 16
        if speed >= 2:
            return 8
        # SAM address-dependent mode: high address space fast, RAM low space slow.
        return 8 if address is not None and address >= 0x8000 else 16

    @property
    def irq(self):
        return bool(self.pia0.irq or (self.regs[0] & 0x20 and self.irq_pending))

    @property
    def firq(self):
        return bool(self.pia1.irq or (self.regs[0] & 0x10 and self.firq_pending))

    def signal(self, mask):
        self.irq_pending |= mask & self.regs[2]
        self.firq_pending |= mask & self.regs[3]

    def set_keys(self, keys):
        keys = set(keys)
        if keys - self.keys:
            self.signal(2)
        self.keys = keys

    def keyboard_rows(self):
        columns = self.pia0.data[1] | (~self.pia0.ddr[1] & 255)
        pins = 127
        for key in self.keys:
            pos = KEY_POSITION.get(key)
            if pos is not None:
                row, col = pos
                if not columns & (1 << col):
                    pins &= ~(1 << row)
        # Centered, disconnected analog joystick: DAC comparator input.
        if self.pia1.data[0] < 128:
            pins |= 128
        return pins

    def physical(self, addr):
        if 0xFE00 <= addr <= 0xFEFF and self.regs[0] & 8:
            return (self.bank_mask << 13) | (addr & 8191), False
        if self.regs[0] & 64:
            task = self.regs[1] & 1
            bank = self.regs[16 + task * 8 + (addr >> 13)] & self.bank_mask
        else:
            bank = (0x38 + (addr >> 13)) & self.bank_mask
        physical = bank * 8192 + (addr & 8191)
        is_rom = not self.sam & 0x8000 and self.bank_mask - 3 <= bank <= self.bank_mask
        return physical, is_rom

    def read8(self, addr):
        addr &= 65535
        if addr >= 0xFF00:
            value = self.read_io(addr & 255)
        else:
            physical, is_rom = self.physical(addr)
            if is_rom:
                offset = physical - ((self.bank_mask - 3) << 13)
                mapping = self.regs[0] & 3
                if mapping == 2 or (mapping < 2 and offset < 16384):
                    value = self.rom[offset]
                elif self.cart_rom:
                    cart_offset = offset - 16384 if mapping < 2 else (offset ^ 16384)
                    value = self.cart_rom[cart_offset % len(self.cart_rom)]
                else:
                    value = 255
            else:
                value = self.ram[physical]
        self.last_bus = value
        return value

    def write8(self, addr, value):
        addr &= 65535
        value &= 255
        self.last_bus = value
        if self.before_write is not None:
            self.before_write(addr, value)
        if addr >= 0xFF00:
            self.write_io(addr & 255, value)
        else:
            physical, is_rom = self.physical(addr)
            if not is_rom:
                self.ram[physical] = value

    def read_io(self, port):
        if port < 0x20:
            index = port & 3
            return self.pia0.read(index, self.keyboard_rows() if index == 0 else 255)
        if port < 0x40:
            return self.pia1.read(port & 3, 0)
        if self.fdc is not None and 0x40 <= port <= 0x4F:
            return self.fdc.read(port)
        if port >= 0xF0:
            return self.rom[0x7F00 + port]
        if port == 0x92:
            value, self.irq_pending = self.irq_pending, 0
            return value
        if port == 0x93:
            value, self.firq_pending = self.firq_pending, 0
            return value
        if 0xA0 <= port <= 0xBF:
            return (self.regs[port - 0x90] & 63) | (self.last_bus & 192)
        if 0x90 <= port <= 0x9F:
            return self.last_bus  # write-only registers; approximate floating bus
        return 255

    def write_io(self, port, value):
        if port < 0x20:
            self.pia0.write(port & 3, value)
        elif port < 0x40:
            self.pia1.write(port & 3, value)
        elif self.fdc is not None and 0x40 <= port <= 0x4F:
            self.fdc.write(port, value)
        elif 0x90 <= port <= 0xBF:
            index = port - 0x90
            self.regs[index] = value
            if port == 0x92:
                self.irq_pending &= value
            elif port == 0x93:
                self.firq_pending &= value
        elif 0xC0 <= port <= 0xDF:
            bit = (port - 0xC0) >> 1
            self.sam = (self.sam & ~(1 << bit)) | ((port & 1) << bit)
        if self.on_write is not None:
            self.on_write(port, value)
