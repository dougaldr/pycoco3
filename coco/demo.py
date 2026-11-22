"""Framework-independent test picture, not an emulated GIME or CPU.

The eventual machine will expose a framebuffer to the frontend in the same way.
"""

WIDTH = 640
HEIGHT = 480
PALETTE = (
    (235, 235, 235), (235, 235, 30), (30, 220, 220), (30, 220, 60),
    (220, 40, 220), (230, 45, 45), (45, 60, 220), (25, 25, 30),
)


class Demo:
    def __init__(self):
        self.pixels = bytearray(WIDTH * HEIGHT * 3)
        self.keys: set[str] = set()
        self.x = WIDTH // 2
        self.y = HEIGHT // 2
        self.phase = 0.0
        self.paused = False
        self._background = self._make_background()
        self.render()

    @staticmethod
    def _make_background() -> bytes:
        image = bytearray(bytes((12, 19, 30)) * (WIDTH * HEIGHT))
        bars = b"".join(bytes(color) * (WIDTH // len(PALETTE)) for color in PALETTE)
        for y in range(96, 256):
            start = y * WIDTH * 3
            image[start:start + WIDTH * 3] = bars
        for y in range(280, 328):
            # Fine stripes make scaling and interpolation problems visible.
            row = b"".join(bytes((210, 220, 230) if (x // 2) % 2 else (20, 30, 40))
                           for x in range(WIDTH))
            start = y * WIDTH * 3
            image[start:start + WIDTH * 3] = row
        return bytes(image)

    def reset(self):
        self.x = WIDTH // 2
        self.y = HEIGHT // 2
        self.phase = 0.0
        self.paused = False
        self.keys.clear()
        self.render()

    def advance(self, seconds: float):
        """Animate the test picture; this is not the future machine clock."""
        if not self.paused:
            self.phase = (self.phase + seconds * 160) % WIDTH
            self.x += 240 * seconds * (("right" in self.keys) - ("left" in self.keys))
            self.y += 240 * seconds * (("down" in self.keys) - ("up" in self.keys))
            self.x = max(8, min(WIDTH - 9, self.x))
            self.y = max(8, min(HEIGHT - 9, self.y))
        self.render()

    def render(self):
        # Preserve buffer identity: the frontend holds a shared view of it.
        self.pixels[:] = self._background
        self._rect(int(self.phase), 342, 3, 24, (255, 180, 50))
        self._rect(int(self.x) - 8, int(self.y) - 8, 17, 17, (0, 0, 0))
        self._rect(int(self.x) - 6, int(self.y) - 6, 13, 13, (255, 255, 255))

    def _rect(self, x, y, width, height, color):
        left, right = max(0, x), min(WIDTH, x + width)
        if right <= left:
            return
        row = bytes(color) * (right - left)
        for line in range(max(0, y), min(HEIGHT, y + height)):
            start = (line * WIDTH + left) * 3
            self.pixels[start:start + len(row)] = row
