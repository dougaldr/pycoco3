"""Host Shift translation and text-input restoration after disk menus."""
import os
os.environ['SDL_VIDEODRIVER'] = 'dummy'
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1'
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import pygame
from coco.frontend import HostKeyboard
from coco.machine import Machine
from coco.memory import character_keys
from coco.menu import DiskMenu
from test_hardware import test_rom, rom_for_tests


SYMBOLS = '!@#$%^&*()'
NUMBER_KEYS = '1234567890'
EXPECTED = ({'shift','1'}, {'@'}, {'shift','3'}, {'shift','4'}, {'shift','5'},
            {'up'}, {'shift','6'}, {'shift',':'}, {'shift','8'}, {'shift','9'})


class HostKeyboardTests(unittest.TestCase):
    def setUp(self):
        self.keyboard = HostKeyboard(pygame)

    def test_number_row_symbols_with_translated_missing_and_unshifted_unicode(self):
        for shift in (pygame.K_LSHIFT, pygame.K_RSHIFT):
            for mode in ('translated', 'empty', 'unshifted', 'absent'):
                for base, symbol, expected in zip(NUMBER_KEYS,SYMBOLS,EXPECTED):
                    with self.subTest(shift=shift,mode=mode,symbol=symbol):
                        k = self.keyboard
                        k.clear()
                        k.down(SimpleNamespace(key=shift))
                        event = SimpleNamespace(key=ord(base),mod=pygame.KMOD_SHIFT)
                        if mode != 'absent':
                            event.unicode = symbol if mode == 'translated' else base if mode == 'unshifted' else ''
                        k.down(event)
                        self.assertEqual(k.keys(),expected)
                        k.up(event)
                        self.assertEqual(k.keys(),{'shift'})
                        k.up(SimpleNamespace(key=shift))
                        self.assertEqual(k.keys(),set())

    def test_modifier_bitmask_works_without_separate_shift_event(self):
        k = self.keyboard
        k.down(SimpleNamespace(key=pygame.K_8,unicode='8',mod=pygame.KMOD_RSHIFT))
        self.assertEqual(k.keys(),{'shift',':'})
        k.clear()
        k.down(SimpleNamespace(key=pygame.K_8,unicode='8',mod=0))
        self.assertEqual(k.keys(),{'8'})

    def test_translated_layout_character_wins_over_us_fallback(self):
        self.keyboard.down(SimpleNamespace(key=pygame.K_2,unicode='"',mod=pygame.KMOD_SHIFT))
        self.assertEqual(self.keyboard.keys(),{'shift','2'})

    def test_control_shortcuts_and_shift_arrow_preserved(self):
        k = self.keyboard
        k.down(SimpleNamespace(key=pygame.K_LCTRL))
        k.down(SimpleNamespace(key=pygame.K_7,unicode='\x1f',mod=pygame.KMOD_CTRL))
        self.assertEqual(k.keys(),{'ctrl','7'})
        k.clear()
        k.down(SimpleNamespace(key=pygame.K_RSHIFT))
        k.down(SimpleNamespace(key=pygame.K_LEFT))
        self.assertEqual(k.keys(),{'shift','left'})

    def test_shifted_punctuation_and_basic_arrow_symbols(self):
        expected = {'_': {'shift','up'}, '+': {'shift',';'}, ':': {':'},
                    '"': {'shift','2'}, '<': {'shift',','}, '>': {'shift','.'},
                    '?': {'shift','/'}}
        for base,symbol in zip('-=;\',./','_+:"<>?'):
            self.keyboard.clear()
            self.keyboard.down(SimpleNamespace(key=ord(base),unicode=base,mod=pygame.KMOD_SHIFT))
            self.assertEqual(self.keyboard.keys(),expected[symbol])
        self.assertEqual(character_keys('^'),{'up'})
        self.assertEqual(character_keys('['),{'shift','down'})
        self.assertEqual(character_keys(']'),{'shift','right'})

    def test_menu_close_restores_translated_text_input(self):
        pygame.display.init()
        try:
            pygame.display.set_mode((640,480))
            m = Machine(test_rom(),video=False,audio=False)
            menu = DiskMenu(pygame,m)
            with patch.object(pygame.key,'start_text_input') as start, patch.object(pygame.key,'stop_text_input') as stop:
                menu.begin_load(0)
                stop.assert_called_once()
                menu.handle(pygame.event.Event(pygame.KEYDOWN,key=pygame.K_ESCAPE,mod=0))
                start.assert_called_once()
                self.assertFalse(menu.active)
                menu.begin_load('rom')
                menu.close()
                self.assertEqual(start.call_count,2)
        finally:
            pygame.quit()


class KeyboardROMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = rom_for_tests()
        if cls.rom is None:
            raise unittest.SkipTest('Supply COCO_ROM or config.local.toml')

    def test_host_number_symbols_echo_and_caret_computes_exponent(self):
        m = Machine(self.rom,video=False,audio=False)
        k = HostKeyboard(pygame)
        def frames(count):
            for _ in range(count):
                m.host_keys = k.keys()
                m.run_frame()
        def press(base, char, shifted=False):
            if shifted: k.down(SimpleNamespace(key=pygame.K_LSHIFT))
            event=SimpleNamespace(key=ord(base),unicode=base,mod=pygame.KMOD_SHIFT if shifted else 0)
            # Exercise SDL's fallback (untranslated unicode), not queue_text().
            k.down(event)
            frames(3)
            k.up(event)
            if shifted:k.up(SimpleNamespace(key=pygame.K_LSHIFT))
            frames(3)
        def enter():
            event=SimpleNamespace(key=pygame.K_RETURN)
            k.down(event);frames(3);k.up(event);frames(30)
        frames(60)
        for char in 'PRINT ':press(char.lower(),char)
        press("'",'"',True)
        for base,char in zip(NUMBER_KEYS,SYMBOLS):press(base,char,True)
        press("'",'"',True)
        enter()
        self.assertIn('\n'+SYMBOLS+' ',m.text_screen())
        for char in 'PRINT 2':press(char.lower(),char)
        press('6','^',True)
        press('3','3')
        enter()
        self.assertIn('\n 8 ',m.text_screen())
        self.assertNotIn('ERROR',m.text_screen())
