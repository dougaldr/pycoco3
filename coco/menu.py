"""Pygame disk menus and file browser; no additional GUI toolkit required."""
from pathlib import Path
from zipfile import BadZipFile
from .disk import open_disk, load_disk_rom

BAR_HEIGHT = 36
FOOTER_HEIGHT = 26


class DiskMenu:
    def __init__(self, pygame, machine):
        self.pg, self.machine = pygame, machine
        pygame.font.init()
        self.font = pygame.font.Font(None, 23)
        self.small = pygame.font.Font(None, 20)
        self.menu = None
        self.browser = None
        self.directory = next((d.path.parent for d in machine.drives if d), Path.home())
        self.entries = []
        self.selected = self.scroll = 0
        self.path_text = ''
        self.editing = False
        self.message = 'Click a drive to load a disk.  Command-O: open drive 0  |  F10: menus'
        self.error = False
        self.targets = []
        self.row_count = 12

    @property
    def active(self):
        return self.menu is not None or self.browser is not None

    def close(self):
        self.menu = self.browser = None
        self.targets = []
        self.editing = False
        # The emulator also needs SDL's layout-aware character translation.
        # Disabling it leaves KEYDOWN.unicode with unshifted number/punctuation
        # keycodes on SDL2, breaking symbols after a file dialog is closed.
        self.pg.key.start_text_input()

    def notice(self, text, error=False):
        self.message, self.error = str(text), error

    def begin_load(self, drive=0):
        self.targets = []
        self.browser = drive
        self.menu = None
        image = self.machine.drives[drive] if isinstance(drive, int) else None
        self.change_directory(image.path.parent if image else self.directory)

    def change_directory(self, path):
        try:
            path = Path(path).expanduser().resolve()
            extensions = ('.rom', '.zip') if self.browser == 'rom' else ('.dsk', '.vdk', '.dmk', '.hfe', '.scp')
            entries = [p for p in path.iterdir() if not p.name.startswith('.')
                       and (p.is_dir() or p.suffix.lower() in extensions)]
            entries.sort(key=lambda p: (not p.is_dir(), p.name.casefold()))
            self.entries = ([path.parent] if path != path.parent else []) + entries
            self.targets = []
            self.directory = path
            self.path_text = str(path)
            self.selected = self.scroll = 0
            self.editing = False
            self.pg.key.stop_text_input()
            self.notice('Select a folder or image. Disks open read-only; enable writes from the drive menu.')
        except OSError as error:
            self.notice(error, True)

    def load_path(self, path):
        try:
            path = Path(path).expanduser()
            if path.is_dir():
                self.change_directory(path)
                return
            if self.browser == 'rom':
                self.machine.load_disk_controller(load_disk_rom(path))
                message = f'Loaded disk controller ROM: {path.name}. Machine cold-reset.'
            else:
                image = open_disk(path)
                self.machine.mount_disk(self.browser, image)
                message = f'Drive {self.browser}: {path.name} ({image.format}, read-only)'
            self.directory = path.resolve().parent
            self.close()
            self.notice(message)
        except (OSError, ValueError, KeyError, BadZipFile) as error:
            self.notice(error, True)

    def activate_selected(self):
        if self.editing:
            path = Path(self.path_text).expanduser()
            self.load_path(path if path.is_absolute() else self.directory / path)
        elif self.entries:
            self.load_path(self.entries[self.selected])

    def action(self, action):
        kind, value = action
        try:
            if kind == 'menu':
                self.menu = None if self.menu == value else value
            elif kind == 'load':
                self.begin_load(value)
            elif kind == 'eject':
                self.machine.mount_disk(value, None)
                self.close()
                self.notice(f'Drive {value} ejected.')
            elif kind == 'write':
                image = self.machine.drives[value]
                self.machine.set_disk_writable(value, not (image and image.writable))
                enabled = self.machine.drives[value].writable
                self.close()
                self.notice(f'Drive {value}: {"writes enabled" if enabled else "read-only"}.')
            elif kind == 'row':
                self.selected = value
            elif kind == 'open':
                self.activate_selected()
            elif kind == 'parent':
                self.change_directory(self.directory.parent)
            elif kind == 'home':
                self.change_directory(Path.home())
            elif kind == 'path':
                self.editing = True
                self.path_text = ''
                self.pg.key.start_text_input()
            elif kind == 'cancel':
                self.close()
            else:
                self.close()
                return kind  # pause/reset/quit handled by frontend
        except (OSError, ValueError) as error:
            self.notice(error, True)
        return None

    def handle(self, event):
        """Return (consumed, frontend action). Modal input never reaches CoCo keys."""
        pg = self.pg
        if event.type == pg.KEYDOWN:
            if event.key == pg.K_o and event.mod & (pg.KMOD_GUI | pg.KMOD_CTRL):
                self.begin_load(0)
                return True, None
            if event.key == pg.K_F10:
                if self.active: self.close()
                else: self.menu = 0
                return True, None
        if event.type == pg.MOUSEBUTTONDOWN and event.button == 1:
            for rect, action in reversed(self.targets):
                if rect.collidepoint(event.pos):
                    result = self.action(action)
                    if action[0] == 'row' and getattr(event, 'clicks', 1) >= 2:
                        self.activate_selected()
                    return True, result
            if self.menu is not None:
                self.close()
                return True, None
        if not self.active:
            return False, None
        if event.type == pg.TEXTINPUT and self.editing:
            self.path_text += event.text
        elif event.type == pg.MOUSEWHEEL and self.browser is not None:
            self.scroll = max(0, min(max(0, len(self.entries)-self.row_count), self.scroll-event.y*3))
        elif event.type == pg.KEYDOWN:
            if event.key == pg.K_ESCAPE:
                self.close()
            elif self.browser is not None:
                if event.key == pg.K_l and event.mod & (pg.KMOD_GUI | pg.KMOD_CTRL):
                    self.action(('path', None))
                elif self.editing and event.key == pg.K_v and event.mod & (pg.KMOD_GUI | pg.KMOD_CTRL):
                    try:
                        self.path_text += pg.scrap.get_text().replace('\r','').replace('\n','')
                    except pg.error as error:
                        self.notice(error, True)
                elif event.key == pg.K_BACKSPACE:
                    if self.editing: self.path_text = self.path_text[:-1]
                    else: self.change_directory(self.directory.parent)
                elif event.key == pg.K_RETURN:
                    self.activate_selected()
                elif event.key in (pg.K_UP, pg.K_DOWN, pg.K_PAGEUP, pg.K_PAGEDOWN) and not self.editing:
                    delta = {pg.K_UP: -1, pg.K_DOWN: 1, pg.K_PAGEUP: -self.row_count, pg.K_PAGEDOWN: self.row_count}[event.key]
                    self.selected = max(0, min(len(self.entries)-1, self.selected+delta))
                    self.scroll = max(0, min(self.scroll, self.selected))
                    self.scroll = max(self.scroll, self.selected-self.row_count+1)
            elif event.key in (pg.K_LEFT, pg.K_RIGHT):
                self.menu = (self.menu + (1 if event.key == pg.K_RIGHT else -1)) % 5
            elif event.key == pg.K_RETURN and self.menu < 4:
                self.begin_load(self.menu)
        return True, None

    def draw(self, screen, paused=False):
        pg = self.pg
        w, h = screen.get_size()
        self.targets = []
        fg, muted, background, highlight = (234, 239, 247), (169, 184, 203), (27, 34, 45), (49, 78, 110)
        def label(text, rect, color=fg, font=None):
            font = font or self.font
            # Clip each label without disturbing the other drawing regions.
            old = screen.get_clip()
            screen.set_clip(rect)
            surface = font.render(text, True, color)
            screen.blit(surface, (rect.x+10, rect.y+(rect.height-surface.get_height())//2))
            screen.set_clip(old)
        def button(text, rect, action, selected=False):
            pg.draw.rect(screen, highlight if selected else background, rect)
            label(text, rect)
            self.targets.append((rect, action))
        pg.draw.rect(screen, background, (0, 0, w, BAR_HEIGHT))
        slot = max(1, w // 5)
        for n in range(4):
            image = self.machine.drives[n]
            name = f'{image.path.name} [{"RW" if image.writable else "RO"}]' if image else 'empty'
            button(f'{n}: {name}', pg.Rect(n*slot, 0, slot-1, BAR_HEIGHT), ('menu', n), self.menu == n)
        button('Machine', pg.Rect(slot*4, 0, w-slot*4, BAR_HEIGHT), ('menu', 4), self.menu == 4)
        if self.active:
            shade = pg.Surface((w, max(1, h-BAR_HEIGHT)), pg.SRCALPHA)
            shade.fill((0, 0, 0, 150))
            screen.blit(shade, (0, BAR_HEIGHT))
        if self.menu is not None:
            n = self.menu
            x = min(n*slot, max(0, w-310))
            if n < 4:
                image = self.machine.drives[n]
                rows = [('Load disk image…', ('load', n)), ('Eject disk', ('eject', n)),
                        ('Make read-only' if image and image.writable else 'Enable disk writes', ('write', n))]
            else:
                rows = [('Resume' if paused else 'Pause', ('pause', None)), ('Reset', ('reset', None)),
                        ('Cold reset', ('cold', None)), ('Load Disk ROM & cold reset…', ('load', 'rom')), ('Quit', ('quit', None))]
            for i, (text, action) in enumerate(rows):
                button(text, pg.Rect(x, BAR_HEIGHT+i*36, min(310, w), 36), action)
        if self.browser is not None:
            # Exclude underlying menu-bar hit targets while the dialog is modal.
            self.targets = []
            panel = pg.Rect(24, 54, max(240, w-48), max(200, h-100))
            pg.draw.rect(screen, background, panel, border_radius=10)
            title = 'Load Disk BASIC ROM' if self.browser == 'rom' else f'Load disk into drive {self.browser}'
            label(title, pg.Rect(panel.x+10, panel.y+4, panel.w-20, 32))
            for text, left, action in [('Up', 12, ('parent', None)), ('Home', 78, ('home', None))]:
                button(text, pg.Rect(panel.x+left, panel.y+42, 64, 30), action)
            button(self.path_text, pg.Rect(panel.x+148, panel.y+42, panel.w-160, 30), ('path', None), self.editing)
            self.row_count = max(1, (panel.h-128)//28)
            self.scroll = min(self.scroll, max(0, len(self.entries)-self.row_count))
            for row, i in enumerate(range(self.scroll, min(len(self.entries), self.scroll+self.row_count))):
                p = self.entries[i]
                name = '../' if p == self.directory.parent else p.name + ('/' if p.is_dir() else '')
                button(name, pg.Rect(panel.x+12, panel.y+82+row*28, panel.w-24, 28), ('row', i), i == self.selected)
            if not self.entries:
                label('No matching files in this folder.', pg.Rect(panel.x+12, panel.y+82, panel.w-24, 28), muted)
            button('Cancel', pg.Rect(panel.right-198, panel.bottom-38, 86, 30), ('cancel', None))
            button('Open', pg.Rect(panel.right-104, panel.bottom-38, 92, 30), ('open', None), True)
            label('Enter: open   Backspace: up   Cmd/Ctrl-L: enter path', pg.Rect(panel.x+8, panel.bottom-38, max(0,panel.w-212), 30), muted, self.small)
        pg.draw.rect(screen, background, (0, h-FOOTER_HEIGHT, w, FOOTER_HEIGHT))
        label(self.message, pg.Rect(0, h-FOOTER_HEIGHT, w, FOOTER_HEIGHT), (255, 159, 144) if self.error else muted, self.small)
