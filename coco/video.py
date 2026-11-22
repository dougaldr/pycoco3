"""Scanline RGB renderer for baseline GIME modes (GPL-3.0-or-later).

The framebuffer is a 640x480 presentation raster. The scheduler flushes scanned
spans before visible writes. Character prefetch and analog composite are not modeled.
"""
from functools import lru_cache
from .fonts import LEGACY_FONT, GIME_FONT

WIDTH, HEIGHT = 640, 480
RGB = tuple(bytes(((((c >> 5) & 1) * 2 + ((c >> 2) & 1)) * 85,
                   (((c >> 4) & 1) * 2 + ((c >> 1) & 1)) * 85,
                   (((c >> 3) & 1) * 2 + (c & 1)) * 85)) for c in range(64))


@lru_cache(maxsize=16384)
def glyph_row(bits, fg, bg, stretch):
    return b''.join((fg if bits & (128 >> x) else bg) * stretch for x in range(8))


@lru_cache(maxsize=8192)
def pixel_byte(value, palette, bpp, stretch):
    mask = (1 << bpp) - 1
    return b''.join(palette[(value >> shift) & mask] * stretch
                    for shift in range(8 - bpp, -1, -bpp))


@lru_cache(maxsize=4096)
def legacy_text_pixels(chars, line, vdg, pal):
    fg_index = 14 if vdg & 8 else 12
    bg_index = fg_index + 1
    if vdg & 32:
        fg_index, bg_index = bg_index, fg_index
    output = []
    for char in chars:
        fg, bg = pal[fg_index], pal[bg_index]
        if char & 128:
            bits = LEGACY_FONT[(64 + (char & 15)) * 12 + line]
            fg, bg = pal[(char >> 4) & 7], pal[8]
        else:
            code = char & 63
            if char < 32 and vdg & 16:
                bits = LEGACY_FONT[(code + 80) * 12 + line]
            else:
                bits = LEGACY_FONT[code * 12 + line]
                if not char & 64:
                    bits ^= 255
        output.append(glyph_row(bits, fg, bg, 2))
    return b''.join(output)


class Video:
    def __init__(self, memory):
        self.memory = memory
        self.pixels = bytearray(WIDTH * HEIGHT * 3)
        self.mode_name = 'GIME'
        self._palette_key = None
        self._palette = ()
        self.latched_offset = None
        self.timer_blink = None

    def begin_frame(self):
        r = self.memory.regs
        self.latched_offset = r[13] << 8 | r[14]

    def reads_address(self, address, y):
        """Whether this physical RAM write can affect the current visible line."""
        m, r = self.memory, self.memory.regs
        compat = bool(r[0] & 128)
        height = 192 if compat else (192, 200, 225, 225)[(r[9] >> 5) & 3]
        y -= (240 - height) // 2
        if not 0 <= y < height:
            return False
        voff = self.latched_offset if self.latched_offset is not None else (r[13] << 8 | r[14])
        if compat:
            base = (voff & 0xE0FF) * 8 + ((m.sam >> 3) & 127) * 512
            vdg = m.pia1.data[1] & m.pia1.ddr[1]
            if vdg & 128:
                count = (16, 16, 32, 16, 32, 16, 32, 32)[(vdg >> 4) & 7]
                lines = (12, 3, 3, 2, 2, 1, 1, 1)[m.sam & 7]
            else:
                count, lines = 32, 12
            base += y // lines * count
            return (address - base) & m.mask < count
        lpr = (1, 1, 2, 8, 9, 10, 11, 16)[r[8] & 7]
        scroll = r[12] & 15
        row = (y + (scroll if scroll < lpr else 0)) // lpr
        if r[8] & 7 == 7:
            row = 0
        hres = (r[9] >> 2) & 7
        count = ((16, 20, 32, 40, 64, 80, 128, 160)[hres] if r[8] & 128
                 else (32, 40, 32, 40, 64, 80, 64, 80)[hres] * (2 if r[9] & 1 else 1))
        base = voff * 8 + row * (256 if r[15] & 128 else count)
        # Horizontal offset wraps inside the source row's 256-byte window.
        relative = (address - base) & m.mask
        return relative < 256 and ((relative - (r[15] & 127) * 2) & 255) < count

    def render_span(self, y, first, last, frame=0):
        if first == 0 and last == WIDTH:
            self.render_line(y, frame)
            return
        start = y * 2 * WIDTH * 3
        previous = bytes(self.pixels[start:start + WIDTH * 6])
        self.render_line(y, frame)
        segment = self.pixels[start + first*3:start + last*3]
        self.pixels[start:start + WIDTH*6] = previous
        for duplicate in (0, 1):
            dest = start + duplicate * WIDTH*3 + first*3
            self.pixels[dest:dest + len(segment)] = segment

    def palette(self):
        key = bytes(self.memory.regs[32:48])
        if self._palette_key != key:
            self._palette_key = key
            self._palette = tuple(RGB[c & 63] for c in key)
        return self._palette

    def render_line(self, y, frame=0):
        if not 0 <= y < 240:
            return
        m, r = self.memory, self.memory.regs
        compat = bool(r[0] & 128)
        vdg = m.pia1.data[1] & m.pia1.ddr[1]
        graphics = bool(vdg & 128) if compat else bool(r[8] & 128)
        height = 192 if compat else (192, 200, 225, 225)[(r[9] >> 5) & 3]
        top = (240 - height) // 2
        border = RGB[0 if compat and not graphics else r[10] & 63]
        if compat and graphics:
            border = RGB[63 if vdg & 8 else 18]
        row = bytearray(border * WIDTH)
        if top <= y < top + height:
            active_y = y - top
            pal = self.palette()
            if compat:
                voff = self.latched_offset if self.latched_offset is not None else (r[13] << 8 | r[14])
                offset = ((voff & 0xE0FF) * 8
                          + ((m.sam >> 3) & 127) * 512)
                if graphics:
                    data = self.legacy_graphics(active_y, offset, vdg, pal)
                else:
                    data = self.legacy_text(active_y, offset, vdg, pal)
                left = 64
            else:
                offset = (self.latched_offset if self.latched_offset is not None else (r[13] << 8 | r[14])) * 8
                data, left = self.native(active_y, offset, graphics, pal, frame)
            row[left * 3:left * 3 + len(data)] = data
        start = y * 2 * WIDTH * 3
        self.pixels[start:start + WIDTH * 6] = row * 2

    def byte(self, address):
        return self.memory.ram[address & self.memory.mask]

    def legacy_text(self, y, offset, vdg, pal):
        self.mode_name = '32-column compatibility text'
        base = (offset + (y // 12) * 32) & self.memory.mask
        if base + 32 <= len(self.memory.ram):
            chars = bytes(self.memory.ram[base:base+32])
        else:
            chars = bytes(self.byte(base+x) for x in range(32))
        return legacy_text_pixels(chars, y % 12, vdg, pal)

    def legacy_graphics(self, y, offset, vdg, pal):
        mode = (vdg >> 4) & 7
        bpp = 2 if mode % 2 == 0 else 1
        byte_count = (16, 16, 32, 16, 32, 16, 32, 32)[mode]
        lines = (12, 3, 3, 2, 2, 1, 1, 1)[self.memory.sam & 7]
        color_set = (vdg >> 3) & 1
        palette_start = (4 * color_set) if bpp == 2 else (8 + color_set * 2)
        palette = pal[palette_start:palette_start + (1 << bpp)]
        stretch = 512 // (byte_count * (8 // bpp))
        base = offset + (y // lines) * byte_count
        self.mode_name = f'Compatibility graphics ({byte_count * 8 // bpp} pixels)'
        return b''.join(pixel_byte(self.byte(base + x), palette, bpp, stretch)
                        for x in range(byte_count))

    def native(self, y, offset, graphics, pal, frame):
        r = self.memory.regs
        hres = (r[9] >> 2) & 7
        width = 640 if hres & 1 else 512
        left = (640 - width) // 2
        lpr = (1, 1, 2, 8, 9, 10, 11, 16)[r[8] & 7]
        scroll = r[12] & 15
        # Defined smooth-scroll values; special out-of-range hardware patterns omitted.
        scan = y + (scroll if scroll < lpr else 0)
        source_row, line = divmod(scan, lpr)
        if r[8] & 7 == 7:
            source_row = 0  # hardware infinite-row mode
        hoff = (r[15] & 127) * 2
        if graphics:
            count = (16, 20, 32, 40, 64, 80, 128, 160)[hres]
            bpp = (1, 2, 4, 4)[r[9] & 3]
            logical_width = count * (8 // bpp)
            stretch = max(1, width // logical_width)
            palette = pal[:1 << bpp]
            pitch = 256 if r[15] & 128 else count
            base = offset + source_row * pitch
            data = b''.join(pixel_byte(self.byte(base + ((hoff + x) & 255)), palette, bpp, stretch)
                            for x in range(count))
            # Reserved combinations can request more than the active raster width.
            self.mode_name = f'GIME {logical_width}x{(192,200,225,225)[(r[9] >> 5) & 3] // lpr}, {1 << bpp} colors'
            return data[:width * 3].ljust(width * 3, b'\0'), left
        columns = (32, 40, 32, 40, 64, 80, 64, 80)[hres]
        stride = 2 if r[9] & 1 else 1
        pitch = 256 if r[15] & 128 else columns * stride
        base = offset + source_row * pitch
        underline = {3: 7, 4: 8, 5: 8, 6: 9}.get(r[8] & 7, -1)
        stretch = width // (columns * 8)
        output = []
        for col in range(columns):
            addr = base + ((hoff + col * stride) & 255)
            char = self.byte(addr)
            attr = self.byte(addr + 1) if stride == 2 else 0
            bits = GIME_FONT[char * 12 + line] if line < 12 else 0
            if attr & 64 and line == underline:
                bits = 255
            blink = self.timer_blink if self.timer_blink is not None else bool((frame // 16) & 1)
            if attr & 128 and blink:
                bits = 0
            output.append(glyph_row(bits, pal[8 + ((attr >> 3) & 7)], pal[attr & 7], stretch))
        self.mode_name = f'{columns}-column GIME text'
        return b''.join(output), left

    def render(self, frame=0):
        self.begin_frame()
        for y in range(240):
            self.render_line(y, frame)
