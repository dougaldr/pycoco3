"""GIME raster and timer events on integer master ticks (NTSC, 1986/1987)."""
from collections import deque
from .clock import LINE_TICKS, LINES


class GimeTiming:
    def __init__(self, clock, memory, video=None, revision=1987, trace=False):
        if revision not in (1986, 1987):
            raise ValueError('GIME revision must be 1986 or 1987')
        self.clock, self.memory, self.video = clock, memory, video
        self.revision = revision
        self.events = deque(maxlen=4096) if trace else None
        self.line = -1
        self.frame = -1
        self.start = 0
        self.pixel_start = 183
        self.drawn = 0
        self.counter = 0
        self.timer_ready = False
        self.timer_fast = False
        self.blink = False
        if self.video is not None:
            self.video.timer_blink = False
        self.clock.at(0, self._line_start)

    def _record(self, name):
        if self.events is not None:
            self.events.append((self.clock.now, name, self.line))

    def _line_start(self):
        self.line = (self.line + 1) % LINES
        self.start = self.clock.now
        self.drawn = 0
        if self.line == 0:
            self.frame += 1
            if self.video:
                self.video.begin_frame()
            self.clock.after(225, lambda: self._vsync(False))
            self.clock.after(4 * LINE_TICKS + 225, lambda: self._vsync(True))
        self._record('hsync-fall')
        self.memory.pia0.edge(0, False)
        r = self.memory.regs
        graphics = bool(self.memory.pia1.data[1] & 128) if r[0] & 128 else bool(r[8] & 128)
        hr = (r[9] >> 2) & 7
        hr0 = 0 if r[0] & 128 else hr & 1
        hr2 = (hr >> 2) & 1
        left = 80 + (61, 57, 63, 63)[hr2 * 2 + graphics]
        left += (106, 110, 44, 48, 104, 104, 42, 42)[hr2 * 4 + hr0 * 2 + graphics]
        right = left + (640 if hr0 else 512)
        self.pixel_start = left if hr0 else left - 64
        self.clock.after(80, self._hsync_rise)
        self.clock.after(right, self._horizontal_irq)
        self.clock.after(self.pixel_start + 640, self.flush)
        self.clock.after(LINE_TICKS - 16, self._timer_line)
        self.clock.after(LINE_TICKS, self._line_start)
        if self.timer_ready:
            self.timer_ready = False
            self._timer_irq()

    def _hsync_rise(self):
        self._record('hsync-rise')
        self.memory.pia0.edge(0, True)

    def _vsync(self, rising):
        self._record('vsync-rise' if rising else 'vsync-fall')
        self.memory.pia0.edge(1, rising)

    def _horizontal_irq(self):
        self._record('gime-horizontal')
        self.memory.signal(16)
        r = self.memory.regs
        height = 192 if r[0] & 128 else (192, 200, 225, 225)[(r[9] >> 5) & 3]
        bottom = 18 + (240 - height) // 2 + height - 1
        if self.line == bottom:
            self._record('gime-vertical')
            self.memory.signal(8)

    def flush(self):
        end = max(0, min(640, self.clock.now - self.start - self.pixel_start))
        y = self.line - 18
        if self.video is not None and end > self.drawn and 0 <= y < 240:
            self.video.render_span(y, self.drawn, end, self.frame)
        self.drawn = max(self.drawn, end)

    def _reload_value(self):
        value = (self.memory.regs[4] & 15) << 8 | self.memory.regs[5]
        return value + (1 if self.revision == 1987 else 2) if value else 0

    def register_write(self, port):
        fast = bool(self.memory.regs[1] & 32)
        if port == 0x94 or (port == 0x91 and fast != self.timer_fast):
            self.clock.cancel('gime-timer')
            self.clock.cancel('gime-timer-irq')
            self.timer_fast = fast
            self.counter = 0
            self.timer_ready = False
            self.clock.after(8, self._restart, 'gime-timer')

    def _restart(self):
        self._reload()
        # A register-triggered restart also clocks the line counter once.
        # Periodic reloads at HSYNC do not perform this extra decrement.
        self._timer_line()

    def _reload(self):
        self.counter = self._reload_value()
        if self.timer_fast and self.counter:
            # Align the 3.579545 MHz timer to the master-clock divide-by-four.
            first = 4 - (self.clock.now % 4)
            self.clock.after(first + (self.counter - 1) * 4, self._timer_expire, 'gime-timer')

    def _timer_expire(self):
        self._record('timer-expire')
        self.clock.after(16, self._timer_irq, 'gime-timer-irq')
        self._reload()

    def _timer_line(self):
        if not self.timer_fast and self.counter:
            self.counter -= 1
            if self.counter == 0:
                self.timer_ready = True

    def _timer_irq(self):
        self._record('timer-irq')
        self.memory.signal(32)
        self.blink = not self.blink
        if self.video:
            self.flush()
            self.video.timer_blink = self.blink
        if not self.timer_fast:
            self._reload()
