"""Reproduce coco/fonts.py from a local VCC checkout, preserving attribution."""
import argparse
from pathlib import Path
import re

parser = argparse.ArgumentParser()
parser.add_argument('vcc', type=Path)
args = parser.parse_args()
parts = ['"""Bitmap glyphs converted from VCC cc2font.h/cc3font.h.\n\n'
         'Copyright 2015 Joseph Forgione. GPL-3.0-or-later. See NOTICE and LICENSE.\n'
         'Source commit: c7046c2e48d5056409fc22897df7b22193d666fa\n"""\n']
for source, name, expected in [('cc2font.h', 'LEGACY_FONT', 112*12),
                                ('cc3font.h', 'GIME_FONT', 256*12)]:
    text = (args.vcc / source).read_text()
    text = re.sub(r'/\*.*?\*/|//[^\n]*', '', text, flags=re.S)
    body = text.split('{', 1)[1].split('}', 1)[0]
    values = bytes(int(s.strip(), 0) for s in body.split(',') if s.strip())
    if len(values) != expected:
        raise ValueError(f'{source}: expected {expected} bytes, found {len(values)}')
    parts.append(name + ' = bytes.fromhex(\n' + ''.join(
        '    "' + values[i:i+48].hex() + '"\n' for i in range(0, len(values), 48)) + ')\n')
(Path(__file__).resolve().parents[1] / 'coco/fonts.py').write_text('\n'.join(parts))
