import os
os.environ['SDL_VIDEODRIVER'] = 'dummy'
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1'
from pathlib import Path
import tempfile
import unittest
import pygame
from coco.disk import DiskImage
from coco.machine import Machine
from coco.menu import DiskMenu
from test_hardware import test_rom


class MenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.display.init()
        cls.screen = pygame.display.set_mode((960,782))

    @classmethod
    def tearDownClass(cls):
        pygame.quit()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root/'my disk.dsk'
        self.image = DiskImage.create(self.path)
        self.m = Machine(test_rom(),disk_rom=bytes(8192),drives=[self.image],video=False,audio=False)
        self.ui = DiskMenu(pygame,self.m)
        self.ui.draw(self.screen)

    def click(self,action):
        self.ui.draw(self.screen)
        rect = next(rect for rect, target in self.ui.targets if target == action)
        return self.ui.handle(pygame.event.Event(pygame.MOUSEBUTTONDOWN,button=1,pos=rect.center))

    def key(self,key,mod=0):
        return self.ui.handle(pygame.event.Event(pygame.KEYDOWN,key=key,mod=mod))

    def test_mouse_menu_browser_and_readonly_load(self):
        self.assertTrue(self.click(('menu',0))[0])
        self.assertTrue(self.ui.active)
        self.click(('load',0))
        self.assertEqual(self.ui.directory,self.root)
        index=self.ui.entries.index(self.path)
        self.click(('row',index))
        self.click(('open',None))
        self.assertFalse(self.ui.active)
        self.assertEqual(self.m.drives[0].path,self.path)
        self.assertFalse(self.m.drives[0].writable)
        self.assertFalse(self.ui.error)

    def test_command_o_path_entry_and_cancel(self):
        self.key(pygame.K_o,pygame.KMOD_GUI)
        self.assertTrue(self.ui.active)
        self.key(pygame.K_l,pygame.KMOD_GUI)
        self.ui.handle(pygame.event.Event(pygame.TEXTINPUT,text=str(self.path)))
        self.key(pygame.K_RETURN)
        self.assertFalse(self.ui.active)
        self.assertEqual(self.m.drives[0].path,self.path)
        self.key(pygame.K_o,pygame.KMOD_CTRL)
        self.key(pygame.K_ESCAPE)
        self.assertFalse(self.ui.active)

    def test_second_click_cannot_activate_closed_dialog_targets(self):
        self.ui.begin_load(0)
        self.ui.selected=self.ui.entries.index(self.path)
        self.ui.draw(self.screen)
        rect=next(r for r,a in self.ui.targets if a==('open',None))
        event=pygame.event.Event(pygame.MOUSEBUTTONDOWN,button=1,pos=rect.center)
        self.ui.handle(event)
        self.assertFalse(self.ui.active)
        self.ui.handle(event)  # another queued click before the next draw
        self.assertEqual(self.m.drives[0].path,self.path)

    def test_modal_keys_consumed_and_directory_navigation(self):
        child=self.root/'subfolder'
        child.mkdir()
        self.ui.begin_load(1)
        self.ui.change_directory(child)
        self.assertTrue(self.key(pygame.K_a)[0])
        self.key(pygame.K_BACKSPACE)
        self.assertEqual(self.ui.directory,self.root)
        self.key(pygame.K_ESCAPE)
        self.assertFalse(self.key(pygame.K_a)[0])

    def test_failed_load_keeps_previous_disk_and_reports_error(self):
        bad=self.root/'bad.dsk'
        bad.write_bytes(b'bad')
        self.ui.begin_load(0)
        self.ui.load_path(bad)
        self.assertTrue(self.ui.error)
        self.assertTrue(self.ui.active)
        self.assertIs(self.m.drives[0],self.image)
        self.ui.load_path(self.root/'missing.dsk')
        self.assertTrue(self.ui.error)

    def test_busy_controller_cannot_eject_or_replace(self):
        self.m.fdc.write(0x40,0x29)
        self.m.fdc.write(0x48,0xA0)
        self.assertTrue(self.m.fdc.busy)
        self.ui.action(('eject',0))
        self.assertTrue(self.ui.error)
        self.assertIs(self.m.drives[0],self.image)
        self.ui.begin_load(0)
        self.ui.load_path(self.path)
        self.assertTrue(self.ui.error)
        self.assertIs(self.m.drives[0],self.image)

    def test_eject_and_mount_signal_forced_ready_interrupts(self):
        f=self.m.fdc
        f.write(0x40,0x29)
        f.write(0x48,0xD2)  # interrupt on ready -> not ready
        self.ui.action(('eject',0))
        self.assertTrue(f.intrq)
        f.read(0x48)
        f.write(0x48,0xD1)  # interrupt on not ready -> ready
        self.ui.begin_load(0)
        self.ui.load_path(self.path)
        self.assertTrue(f.intrq)

    def test_write_protection_eject_reset_and_other_drives(self):
        self.ui.action(('write',0))
        self.assertFalse(self.m.drives[0].writable)
        self.ui.action(('write',0))
        self.assertTrue(self.m.drives[0].writable)
        self.ui.action(('eject',0))
        self.assertIsNone(self.m.drives[0])
        self.ui.begin_load(3)
        self.ui.load_path(self.path)
        self.assertIs(self.m.drives[3],self.m.fdc.drives[3])
        self.m.reset()
        self.assertEqual(self.m.fdc.drives[3].path,self.path)

    def test_duplicate_writable_mount_refused(self):
        self.ui.begin_load(1)
        self.ui.load_path(self.path)
        self.assertTrue(self.ui.error)
        self.assertIsNone(self.m.drives[1])

    def test_missing_rom_can_be_loaded_from_machine_menu(self):
        m=Machine(test_rom(),video=False,audio=False)
        ui=DiskMenu(pygame,m)
        ui.begin_load(0)
        ui.load_path(self.path)
        self.assertTrue(ui.error)
        rom=self.root/'disk.rom'
        rom.write_bytes(bytes(8192))
        ui.begin_load('rom')
        ui.load_path(rom)
        self.assertFalse(ui.error)
        self.assertIsNotNone(m.fdc)
        ui.begin_load(0)
        ui.load_path(self.path)
        self.assertEqual(m.drives[0].path,self.path)

    def test_layout_and_scrolling_small_window(self):
        for i in range(40): (self.root/f'{i:03}.dsk').write_bytes(b'')
        self.ui.begin_load(0)
        self.ui.draw(pygame.Surface((640,480)))
        for _ in range(20): self.key(pygame.K_DOWN)
        self.assertGreater(self.ui.scroll,0)
        self.ui.draw(pygame.Surface((640,480)))
        self.assertLess(self.ui.selected,self.ui.scroll+self.ui.row_count)
        self.assertTrue(all(rect.right<=640 and rect.bottom<=480 for rect,_ in self.ui.targets))
