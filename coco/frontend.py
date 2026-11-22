"""macOS-friendly Pygame frontend for the CoCo 3 machine."""
import argparse
import os
from pathlib import Path
import sys
import time
import tomllib
import zipfile
import json

from .machine import Machine, FRAME_HZ
from .memory import load_rom, character_keys
from .video import WIDTH, HEIGHT
from .disk import open_disk, load_disk_rom
from .audio import Playback, WaveRecorder
from .menu import DiskMenu, BAR_HEIGHT, FOOTER_HEIGHT


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Python CoCo 3 emulator')
    parser.add_argument('--rom', type=Path, help='user-supplied 32 KB CoCo 3 ROM')
    parser.add_argument('--config', type=Path, default=Path('config.local.toml'))
    parser.add_argument('--ram', type=int, choices=(128, 512), help='RAM size in KB')
    parser.add_argument('--demo', action='store_true', help='run the original framebuffer demo')
    parser.add_argument('--headless', action='store_true', help='use an offscreen SDL display')
    parser.add_argument('--frames', type=int, help='exit after this many emulated frames')
    parser.add_argument('--screenshot', type=Path, help='save final display as PNG')
    parser.add_argument('--text-dump', type=Path, help='save compatibility text screen')
    parser.add_argument('--unthrottled', action='store_true', help='run without real-time pacing')
    parser.add_argument('--type-text', help='type text through the keyboard matrix')
    parser.add_argument('--type-after', type=int, default=90, help='frames before typing (default 90)')
    parser.add_argument('--trace', action='store_true', help='retain recent CPU states for errors')
    parser.add_argument('--bus-trace', type=Path, help='save the last 100,000 bus cycles as JSON')
    parser.add_argument('--disk-rom', type=Path, help='Disk BASIC ROM or ZIP containing disk11.rom')
    for drive in range(4):
        flags = ['--disk', '--disk0'] if drive == 0 else [f'--disk{drive}']
        parser.add_argument(*flags, dest=f'disk{drive}', type=Path, help=f'image for drive {drive}')
    parser.add_argument('--write-disk', type=int, choices=range(4), action='append', default=[],
                        help='enable writes to this drive (repeat for multiple drives)')
    parser.add_argument('--mute', action='store_true', help='disable live sound playback')
    parser.add_argument('--volume', type=float, default=0.35, help='sound volume, 0 to 1')
    parser.add_argument('--record-wav', type=Path, help='capture emulated audio to a 48 kHz mono WAV')
    parser.add_argument('--type-file', type=Path, help='type a UTF-8 text/BASIC file through the keyboard')
    parser.add_argument('--gime', type=int, choices=(1986, 1987), default=1987, help='GIME timer revision')
    args = parser.parse_args(argv)
    if args.frames is not None and args.frames <= 0:
        parser.error('--frames must be positive')
    if args.headless and args.frames is None:
        parser.error('--headless requires --frames')
    if args.type_after < 0:
        parser.error('--type-after must be nonnegative')
    if not 0 <= args.volume <= 1:
        parser.error('--volume must be between 0 and 1')
    if args.type_file and args.type_text:
        parser.error('Use either --type-file or --type-text')
    if args.demo:
        return args
    config = {}
    try:
        if args.config.exists():
            config = tomllib.loads(args.config.read_text())
        explicit_rom = args.rom is not None
        rom = args.rom or config.get('rom')
        if rom is None:
            parser.error('Specify --rom /path/to/coco3.rom, set rom in config.local.toml, or use --demo.')
        args.rom = Path(rom).expanduser()
        if not args.rom.is_absolute():
            args.rom = (Path.cwd() if explicit_rom else args.config.parent) / args.rom
        args.ram = args.ram or config.get('ram_kb', 128)
        args.rom_data = load_rom(args.rom)
        if args.ram not in (128, 512):
            parser.error('ram_kb must be 128 or 512')
        def configured_path(explicit, key):
            value = explicit if explicit is not None else config.get(key)
            if not value:
                return None
            path = Path(value).expanduser()
            return path if path.is_absolute() else (Path.cwd() if explicit is not None else args.config.parent) / path
        disk_rom = configured_path(args.disk_rom, 'disk_rom')
        args.disk_rom_data = load_disk_rom(disk_rom) if disk_rom else None
        args.drives = []
        for drive in range(4):
            explicit = getattr(args, f'disk{drive}')
            path = configured_path(explicit, f'disk{drive}')
            # A CLI image override does not inherit write permission from a different configured image.
            writable = drive in args.write_disk or (explicit is None and config.get(f'disk{drive}_writable', False))
            args.drives.append(open_disk(path, writable=bool(writable)) if path else None)
        if any(args.drives) and args.disk_rom_data is None:
            parser.error('Mounting a disk requires --disk-rom or disk_rom in the config')
        if args.type_file:
            args.type_text = args.type_file.read_text()
        if args.type_text:
            for char in args.type_text.replace('\r\n', '\n'):
                character_keys(char)
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile) as error:
        parser.error(str(error))
    return args


class HostKeyboard:
    """Translate host characters and special keys into CoCo matrix switches."""
    # Fallback for SDL events without translated text (standard US host layout).
    # Properly translated Unicode always takes precedence for other layouts.
    shifted_host = dict(zip('1234567890-=[]\\;\',./`', '!@#$%^&*()_+{}|:"<>?~'))
    def __init__(self, pygame):
        self.pg = pygame
        self.held = {}
        self.special = {
            pygame.K_RETURN: 'return', pygame.K_KP_ENTER: 'return',
            pygame.K_BACKSPACE: 'left', pygame.K_DELETE: 'left',
            pygame.K_ESCAPE: 'break', pygame.K_HOME: 'clear',
            pygame.K_UP: 'up', pygame.K_DOWN: 'down',
            pygame.K_LEFT: 'left', pygame.K_RIGHT: 'right',
            pygame.K_LCTRL: 'ctrl', pygame.K_RCTRL: 'ctrl',
            pygame.K_LALT: 'alt', pygame.K_RALT: 'alt',
            pygame.K_F1: 'f1', pygame.K_F2: 'f2',
            pygame.K_LSHIFT: 'shift', pygame.K_RSHIFT: 'shift',
        }
        self.printable = set()

    def clear(self):
        self.held.clear()
        self.printable.clear()

    def down(self, event):
        if event.key in self.special:
            self.held[event.key] = {self.special[event.key]}
        else:
            char = getattr(event, 'unicode', '')
            base = self.pg.key.name(event.key)
            mods = getattr(event, 'mod', 0)
            if not char or not char.isprintable():
                char = base
            shifted = bool(mods & self.pg.KMOD_SHIFT or
                           self.pg.K_LSHIFT in self.held or self.pg.K_RSHIFT in self.held)
            if shifted and char == base and not mods & (self.pg.KMOD_CTRL | self.pg.KMOD_ALT | self.pg.KMOD_MODE):
                char = self.shifted_host.get(char, char)
            if len(char) == 1:
                try:
                    self.held[event.key] = character_keys(char)
                    self.printable.add(event.key)
                except ValueError:
                    pass

    def up(self, event):
        self.held.pop(event.key, None)
        self.printable.discard(event.key)

    def keys(self):
        result = set()
        for key, switches in self.held.items():
            # Host Shift-2 produces @; @ has its own unshifted CoCo key.
            if self.printable and key in (self.pg.K_LSHIFT, self.pg.K_RSHIFT):
                continue
            result.update(switches)
        return result


def main(argv=None):
    args = parse_args(argv)
    if args.demo:
        from .demo_frontend import main as demo_main
        forwarded = []
        if args.headless:
            forwarded.append('--headless')
        if args.frames:
            forwarded += ['--frames', str(args.frames)]
        if args.screenshot:
            forwarded += ['--screenshot', str(args.screenshot)]
        return demo_main(forwarded)
    if args.headless:
        os.environ['SDL_VIDEODRIVER'] = 'dummy'
    os.environ.setdefault('PYGAME_HIDE_SUPPORT_PROMPT', '1')
    import pygame
    if not getattr(pygame, 'IS_CE', False):
        raise SystemExit('Install pygame-ce in this Python environment.')
    live_audio = not (args.mute or args.headless or args.unthrottled)
    machine = Machine(args.rom_data, args.ram, trace=args.trace, disk_rom=args.disk_rom_data,
                      drives=args.drives, audio=live_audio or bool(args.record_wav), gime_revision=args.gime,
                      bus_trace=bool(args.bus_trace))
    playback = recorder = None
    pygame.display.init()
    try:
        screen = pygame.display.set_mode((960, 720+BAR_HEIGHT+FOOTER_HEIGHT), pygame.RESIZABLE)
        pygame.key.start_text_input()
        pygame.display.set_caption('Python CoCo 3 | Booting | F5 reset · F6 pause')
        if args.record_wav:
            recorder = WaveRecorder(args.record_wav)
        if live_audio:
            try:
                playback = Playback(pygame, args.volume)
            except pygame.error as error:
                print(f'Audio device could not be opened: {error}', file=sys.stderr)
        source = pygame.image.frombuffer(machine.video.pixels, (WIDTH, HEIGHT), 'RGB')
        keyboard = HostKeyboard(pygame)
        menu = DiskMenu(pygame, machine)
        paused = False
        running = True
        count = 0
        scaled = None
        deadline = time.perf_counter()
        started = deadline
        pending_text = args.type_text
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                    continue
                if event.type == pygame.KEYDOWN and event.key == pygame.K_q and event.mod & pygame.KMOD_GUI:
                    running = False
                    continue
                consumed, action = menu.handle(event)
                if consumed:
                    keyboard.clear()
                    machine.host_keys.clear()
                    machine.memory.set_keys(set())
                    if playback:
                        playback.clear()
                    if action == 'quit':
                        running = False
                    elif action == 'pause':
                        paused = not paused
                    elif action in ('reset', 'cold'):
                        machine.reset(cold=action == 'cold')
                    deadline = time.perf_counter()
                    continue
                if event.type == pygame.WINDOWFOCUSLOST:
                    keyboard.clear()
                elif event.type == pygame.KEYUP:
                    keyboard.up(event)
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_q and event.mod & pygame.KMOD_GUI:
                        running = False
                    elif event.key == pygame.K_F5:
                        machine.reset(cold=bool(event.mod & pygame.KMOD_SHIFT))
                        keyboard.clear()
                        if playback:
                            playback.clear()
                    elif event.key == pygame.K_F6:
                        paused = not paused
                        if playback:
                            playback.clear()
                    elif not event.mod & pygame.KMOD_GUI:
                        keyboard.down(event)
            if not running:
                break
            if not paused and not menu.active:
                if pending_text and count >= args.type_after:
                    machine.queue_text(pending_text)
                    pending_text = None
                machine.host_keys = keyboard.keys()
                machine.run_frame()
                if recorder:
                    recorder.write(machine.audio_frame)
                if playback:
                    playback.feed(machine.audio_frame)
                count += 1
            w, h = screen.get_size()
            # Fractional nearest-neighbor scaling fills a 4:3 window; no filtering.
            available_h = max(1, h-BAR_HEIGHT-FOOTER_HEIGHT)
            factor = min(w / WIDTH, available_h / HEIGHT)
            size = (max(1, round(WIDTH * factor)), max(1, round(HEIGHT * factor)))
            if scaled is None or scaled.get_size() != size:
                scaled = pygame.Surface(size, depth=source.get_bitsize(), masks=source.get_masks())
            pygame.transform.scale(source, size, scaled)
            screen.fill((0, 0, 0))
            screen.blit(scaled, ((w - size[0]) // 2, BAR_HEIGHT+(available_h - size[1]) // 2))
            menu.draw(screen, paused)
            pygame.display.flip()
            if count % 30 == 0 or paused:
                status = 'PAUSED' if paused or menu.active else machine.video.mode_name
                pygame.display.set_caption(f'Python CoCo 3 | {args.ram} KB | {status} | F5 reset · F6 pause')
            if args.frames is not None and count >= args.frames:
                break
            if not args.unthrottled and not args.headless:
                deadline += 1 / FRAME_HZ
                delay = deadline - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                elif delay < -0.25:
                    deadline = time.perf_counter()
            elif paused or menu.active:
                time.sleep(0.01)
        if args.screenshot:
            pygame.image.save(screen, str(args.screenshot))
        if args.text_dump:
            args.text_dump.write_text(machine.text_screen() + '\n')
        elapsed = time.perf_counter() - started
        print(f'CoCo finished: {count} frames in {elapsed:.2f}s; PC=${machine.cpu.pc:04X}; '
              f'{machine.cpu.instructions:,} instructions; SDL={pygame.display.get_driver()}')
        if playback:
            print(f'Audio: {pygame.mixer.get_driver()}; underruns={playback.underruns}; overruns={playback.overruns}')
        if machine.fdc and machine.fdc.last_error:
            print(f'Disk error: {machine.fdc.last_error}', file=sys.stderr)
    except Exception:
        if machine.trace:
            for state in list(machine.trace)[-16:]:
                print(' '.join(f'{k}={v:04X}' for k, v in state.items()), file=sys.stderr)
        raise
    finally:
        if recorder:
            recorder.close()
        pygame.quit()
        if args.bus_trace:
            args.bus_trace.write_text(json.dumps({'master_hz': 14_318_180,
                'columns': ['tick', 'kind', 'address', 'value'], 'cycles': list(machine.bus.events)}, indent=2) + '\n')
    return 0
