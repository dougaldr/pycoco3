"""Pygame host frontend. Run on the main thread for macOS window events."""

import argparse
import os
from pathlib import Path

from .demo import Demo, WIDTH, HEIGHT


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="CoCo 3 display/input development demo")
    parser.add_argument("--headless", action="store_true", help="use SDL's dummy display")
    parser.add_argument("--frames", type=int, help="exit after this many frames")
    parser.add_argument("--screenshot", type=Path, help="save the last displayed frame as PNG")
    args = parser.parse_args(argv)
    if args.frames is not None and args.frames <= 0:
        parser.error("--frames must be positive")
    if args.headless and args.frames is None:
        parser.error("--headless requires --frames to bound the run")
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.headless:
        os.environ["SDL_VIDEODRIVER"] = "dummy"
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    import pygame

    if not getattr(pygame, "IS_CE", False):
        raise SystemExit("Install pygame-ce in this Python environment.")

    pygame.display.init()
    pygame.font.init()
    try:
        screen = pygame.display.set_mode((960, 720), pygame.RESIZABLE)
        pygame.display.set_caption("Python CoCo 3 — display/input demo")
        demo = Demo()
        source = pygame.image.frombuffer(demo.pixels, (WIDTH, HEIGHT), "RGB")
        # Draw host labels into a separate surface, leaving the core buffer intact.
        composed = pygame.Surface((WIDTH, HEIGHT)).convert()
        font = pygame.font.Font(None, 24)
        small_font = pygame.font.Font(None, 20)
        clock = pygame.time.Clock()
        held = {}
        count = 0
        running = True
        scaled = None
        while running:
            elapsed = min(clock.tick(60) / 1000, 0.05)
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE or (
                        event.key == pygame.K_q and event.mod & pygame.KMOD_GUI
                    ):
                        running = False
                    elif not getattr(event, "repeat", False):
                        if event.key == pygame.K_SPACE:
                            demo.paused = not demo.paused
                        elif event.key == pygame.K_F5:
                            demo.reset()
                            held.clear()
                    held[event.key] = pygame.key.name(event.key)
                elif event.type == pygame.KEYUP:
                    held.pop(event.key, None)
                elif event.type == pygame.WINDOWFOCUSLOST:
                    held.clear()
            if not running:
                break
            demo.keys = set(held.values())
            demo.advance(elapsed)
            composed.blit(source, (0, 0))
            labels = (
                (font, "PYTHON COCO 3 / FRONTEND CHECK", 20, 20),
                (small_font, "Test picture only - CPU, ROM and GIME emulation are not implemented.", 20, 54),
                (small_font, "Arrow keys: move marker   Space: pause   F5: reset   Esc: quit", 20, 390),
                (small_font, "Keys: " + (", ".join(sorted(demo.keys)) or "none"), 20, 419),
                (small_font, ("PAUSED" if demo.paused else "RUNNING") +
                 f"   |   {clock.get_fps():.1f} FPS   |   SDL {pygame.display.get_driver()}", 20, 448),
            )
            for face, label, x, y in labels:
                composed.blit(face.render(label, True, (225, 234, 242)), (x, y))

            # 4:3 test canvas with letterboxing. Integer upscale where it fits.
            window_w, window_h = screen.get_size()
            factor = min(window_w / WIDTH, window_h / HEIGHT)
            if factor >= 1:
                factor = int(factor)
            size = (max(1, int(WIDTH * factor)), max(1, int(HEIGHT * factor)))
            if scaled is None or scaled.get_size() != size:
                scaled = pygame.Surface(size).convert()
            pygame.transform.scale(composed, size, scaled)
            screen.fill((3, 7, 12))
            screen.blit(scaled, ((window_w - size[0]) // 2, (window_h - size[1]) // 2))
            pygame.display.flip()
            count += 1
            if args.frames is not None and count >= args.frames:
                break
        if args.screenshot:
            pygame.image.save(screen, str(args.screenshot))
        print(f"Frontend finished: {count} frames; pygame-ce {pygame.version.ver}; "
              f"SDL {pygame.get_sdl_version()}; video={pygame.display.get_driver()}")
    finally:
        pygame.quit()
    return 0
