# PyCoCo3

A Python TRS-80 Color Computer 3 emulator using pygame-ce for the macOS frontend
and VCC as a hardware implementation reference. The current version boots a
user-supplied CoCo 3 ROM to BASIC, accepts keyboard input, runs GIME graphics,
plays DAC/single-bit sound, and reads and writes floppy sector images through an
emulated disk controller. Version 0.5.0 adds read-only HFE weak-bit and SCP flux
playback, recorded byte deadlines, and controller timing corrections.

**Acknowledgments:** Thank you to Joseph Forgione and the
[VCC (Virtual Color Computer) contributors](https://github.com/VCCE/VCC).
VCC provided the foundational hardware implementation reference for this
emulator, and the bitmap fonts were converted from VCC's font data. Original
attribution is retained in [NOTICE](NOTICE); this project is distributed under
[GPL-3.0-or-later](LICENSE).

## Getting started on macOS

Use Python 3.11 or newer and a user-supplied 32 KB CoCo 3 system ROM.

```sh
git clone https://github.com/dougaldr/pycoco3.git
cd pycoco3
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp config.example.toml config.local.toml
```

Edit `config.local.toml` to set `rom` to your system ROM's path, then run:

```sh
python -m coco
```

For disks, also set `disk_rom` to your separately supplied Disk BASIC ROM. You
can then load images from the drive menus. To create an empty writable work disk:

```sh
mkdir -p disks
python -m coco.disk disks/work.dsk
python -m coco --disk disks/work.dsk --write-disk 0
```

The disk creation command refuses to overwrite an existing file. System ROMs,
Disk BASIC ROMs, disk images and local configuration are not included in this
repository. To run the tests from the activated environment:

```sh
python -m unittest discover -s tests -v
```

ROM integration tests report a skip when the required ROMs are unavailable.
The `python-coco` command remains available after installation, and the Python
package is named `coco`.

## Existing development checkout

The original development checkout uses `~/Documents/.venv` and lives at
`~/Documents/python-coco`. Its untracked `config.local.toml` selects the local
ROMs and writable work disk. If you already use that checkout, launch it with:

```sh
cd ~/Documents/python-coco
../.venv/bin/python -m coco
```

Type `PRINT 2+2` and press Return. To try the native text screen, enter `WIDTH 80`.
Use `WIDTH 32` to return to the compatibility screen.

Try `SOUND 128,5` for a short tone and `DIR` for the disk directory. To check saving:

```basic
10 PRINT "HELLO FROM DISK"
SAVE "HELLO"
NEW
LOAD "HELLO"
RUN
```

Wait for `OK` after each disk command. The saved program survives emulator restarts.

## Load disks from the window

Click a drive in the top menu bar, then **Load disk image…**. Select a `.dsk`,
`.dmk`, `.vdk`, `.hfe`, or `.scp` file and click **Open** (or press Return). You can navigate folders
with the mouse, arrow keys, Up/Home buttons, or Backspace. **Command-O** opens
drive 0 directly; **F10** opens/closes the menus. **Command-L** in the browser lets
you enter a path, including `~`; Command-V pastes it. Control substitutes for
Command on other platforms. Escape cancels without changing the mounted disk.

The browser pauses emulation and consumes keyboard input. A failed load leaves
the old disk mounted and displays an error at the bottom of the window. Media
changes are refused while a controller command is busy; close the menu and let
the command finish first.

Newly opened disks are **read-only**. Choose **Enable disk writes** from the drive
menu when you want to save; **Make read-only** restores protection. The menu bar
shows `[RO]` or `[RW]`. HFE and SCP remain read-only; attempting to enable writes
explains this restriction. Each drive also has **Eject disk**. Writable duplicate mounts
of the same host file are refused. Menu changes last for the running session and
survive resets; startup defaults remain in `config.local.toml`.

The **Machine** menu provides pause/resume, reset, cold reset, disk-controller ROM
loading, and quit. Loading a controller ROM performs a **cold reset**, clearing
emulated RAM so BASIC can install its disk hooks. The menus use pygame-ce and
need no Tk/PyObjC installation.

| Host control | Action |
| --- | --- |
| Letters, numbers, punctuation, Space, Return | CoCo keyboard |
| Arrow keys | CoCo arrows |
| Backspace / Delete | CoCo Left (delete a character in BASIC) |
| Escape | CoCo BREAK (does **not** quit the emulator) |
| Home | CoCo CLEAR |
| Control / Option | CoCo CTRL / ALT |
| F1 / F2 | CoCo F1 / F2 |
| F5 | Reset, preserving RAM |
| Shift-F5 | Cold reset, clearing RAM |
| F6 | Pause / resume |
| Command-O / F10 | Open drive 0 / toggle disk menus |
| Command-Q or close window | Quit |

Mac keyboards may require Fn with function keys. Printable punctuation is mapped
by character, so host Shift-2 types the CoCo's `@`. Held keys are released when the
window loses focus. This is a text-friendly mapping; it is not a selectable
physical CoCo keyboard layout.

The shifted US number row types `!@#$%^&*()` both before and after opening disk
menus. Host Shift-6 maps to the CoCo Up key, BASIC's exponentiation character
(`^`). Underscore and square brackets use BASIC's shifted arrow combinations.

The original test picture is still available with `--demo` (its original controls
are unchanged). ROM mode uses a 4:3 window with nearest-neighbor scaling and
letterboxing. There are no host labels drawn over the emulated screen.

## ROM and RAM configuration

Use a raw **32768-byte CoCo 3 system ROM**. File size and reset-vector validation
run before opening the window. ZIP archives, CoCo 1/2 ROMs, and disk-controller
ROMs are not accepted as the system ROM.

Override the local configuration with command-line options:

```sh
../.venv/bin/python -m coco --rom /path/to/coco3.rom --ram 512
```

Or copy `config.example.toml` to `config.local.toml` and set `rom` and `ram_kb`.
A relative ROM path in a config file is resolved relative to that config file;
a relative command-line ROM path is resolved relative to the working directory.
`config.local.toml`, ROM images, and generated artifacts are ignored by Git.
On another machine, create a virtual environment, install `python -m pip install
-e .`, then run `python -m coco` or `python-coco`.

## Sound and disks

Sound uses the PIA-controlled 6-bit DAC and single-bit output, sampled at their
emulated write times. Integer-phase resampling produces 48 kHz mono PCM without
frame-rounding drift. Live output uses pygame-ce/SDL (CoreAudio on macOS), a short
prebuffer, and bounded buffering. `--volume 0.25` sets host volume; `--mute` disables
live playback. If the device cannot open, emulation continues with a diagnostic.
Headless and unthrottled runs do not play live audio, but can record it:

```sh
../.venv/bin/python -m coco --headless --frames 360 \
  --type-text $'SOUND 128,5\n' --record-wav /tmp/coco.wav
```

Scripted text must include Return to execute. For a complete sound example:

```sh
../.venv/bin/python -m coco --type-file examples/sound.bas
```

Once the program has finished typing, enter `RUN`. WAV capture records emulated
time, unaffected by host volume; pausing omits paused wall-clock time.

Mount up to four images with `--disk` (drive 0), `--disk1`, `--disk2`, and `--disk3`.
The disk controller needs a separately supplied 8/16 KB Disk BASIC ROM, or a ZIP
containing `disk11.rom`. Raw/JVC `.dsk`, VDK sector images, DMK track images,
HFE v1/v3 bitstreams and indexed SCP flux captures are supported within the
limits below. DMK, HFE and SCP content is detected by its header even with a
`.dsk` extension.
An image is read-only unless `--write-disk N` or `diskN_writable = true` explicitly
enables writes. A command-line image override does not inherit another image's
configured write permission.

```sh
../.venv/bin/python -m coco.disk disks/another.dsk
../.venv/bin/python -m coco --disk-rom /path/to/disk11.rom \
  --disk disks/another.dsk --write-disk 0
```

The creation command makes a formatted empty 35-track RSDOS image and refuses to
overwrite an existing file. Sector writes persist automatically using atomic
file replacement, with a check against external changes. `DSKINI 0` is supported
for normal RSDOS formatting and erases the contents of the selected writable
image. Formatting and verification take emulated disk time.

Headerless 35/40/80-track single-sided images and common double-sided sizes are
recognized; other sector geometries need a JVC/VDK header. Per-sector JVC
attributes are not supported.

## Copy-protected track images

DMK preserves information that ordinary sector DSK files discard. The controller
now reads recorded sector positions and selects the next matching ID as the disk
rotates. It supports duplicate sector IDs, nonstandard track/side/sector IDs and
sector sizes, missing data marks, bad ID/data CRCs, deleted-data marks, and FM/MFM
density selection. `READ ADDRESS` returns stored CHRN/CRC bytes; `READ TRACK`
returns recorded track bytes across one revolution. Seek verification checks
recorded IDs rather than assuming they match the physical track number.

Writable DMK sector updates preserve neighboring records, update the addressed
record's data CRC, and honor the deleted-data write flag. Write-track F5/F6/F7
tokens are decoded, including the extra byte-cell time consumed by F7; DMK track
formatting retains the generated ID table and data. Header write protection is
honored even if writes are requested on the command line.

Protection tests use synthetic tracks and an actual 6809 polling program, plus
a real Disk BASIC save/reopen/load/run on DMK. They do **not** establish that all
commercial protected games boot. Use a track-preserving dump of the original:
renaming or converting a sector-only DSK cannot recover protection information
already lost. DMK does not encode flux transitions, weak-bit probabilities, or
multiple revolutions; use an appropriate HFE or SCP capture for those features.

## Weak bits and flux captures

| Format | Supported protection information | Writes |
| --- | --- | --- |
| Raw/JVC DSK, VDK | Sector contents; angular positions are synthesized | Yes |
| DMK | Duplicate/nonstandard IDs, recorded positions, deleted/missing marks, bad CRC, FM/MFM | Yes |
| HFE v1 | Encoded MFM cells, missing-clock sync, recorded CRC and index placement | No |
| HFE v3 | HFE bitstreams plus RAND weak cells, rate changes, skipped bits and index opcode | No |
| SCP | Indexed 16-bit flux intervals, capture resolution and multiple captured revolutions | No |

HFE/SCP decoding currently targets **250 kbit/s double-density MFM**. A weak HFE
region is sampled anew for each emulated revolution, with repeatable noise keyed
by the image and revolution. Repeating a run reproduces the same results; reading
on successive revolutions can return different bytes and CRC errors. Ordinary
cells and stored CRCs are preserved. SCP replays its recorded revolutions in
sequence and repeats the capture set, retaining differences between captures.
It does not invent new noise where a capture contains no flux transitions.

Reads recognize missing-clock address marks and verify the recorded CRCs.
Sector searches advance through successive revolutions, so a changing ID can
be found on a later pass. Records can cross index into the next revolution.
READ ADDRESS, READ TRACK, DRQ deadlines, CRC completion, and index interrupts
use the recorded timeline. Source times are accumulated exactly in integer
units and converted at absolute master-clock deadlines, avoiding repeated
rounding drift. The SCP index duration remains in 25 ns units even when flux
samples use another capture resolution.

These readers are deliberately bounded: HFE v2, FM/GCR flux decoding, unindexed
SCP captures, extended SCP media, non-16-bit SCP samples, and flux writes are
not implemented. HFE requires a 250 kbit/s header and MFM/unknown encoding;
v3 bitrate divisors 36–144 are accepted. Tracks must span 100–333 ms; SCP
accepts 1–32 captured revolutions. Malformed headers, truncated data, unsupported
HFE opcodes and SCP checksum failures are rejected before mounting.

SCP uses a bounded digital data separator. Analog noise, VFO lock/loss behavior,
extreme rate changes, splice transients and every commercial protection scheme
are **not validated**. HFE is decoded at its supplied cell boundaries. Passing
synthetic protection tests establishes those cases, not universal protected-game
compatibility. No user disk images are converted or rewritten by these readers.

Format references: [HxC HFE specification](https://hxc2001.com/floppy_drive_emulator/HFE-file-format.html),
[FlashFloppy's HFE opcode implementation](https://github.com/keirf/FlashFloppy/blob/master/src/image/hfe.c),
and [SuperCard Pro specification](https://www.cbmstuff.com/downloads/scp/scp_image_specs.txt).

## Implemented

- Motorola 6809 documented instructions, addressing modes, flags, stacks,
  instruction cycle counts, IRQ/FIRQ/NMI, SWI/SWI2/SWI3, SYNC, and CWAI.
  Unsupported/undocumented instructions fail with an explicit error.
- Explicit E-cycle sequences for the supported opcodes and indexed postbytes:
  dummy PC/stack reads, internal `$FFFF` cycles, read-modify-write ordering,
  indirect pointer fetches, stack transfers and interrupt vector reads. Dummy
  reads retain memory-mapped I/O side effects. CWAI entry and vectoring account
  for 16 + 4 active cycles; waiting releases the bus instead of reading memory.
- 128 KB and 512 KB physical RAM, two MMU tasks with eight 8 KB banks each,
  ROM/RAM switching, ROM protection, fixed vectors, and SAM register writes.
- PIA direction/data/control registers and active-low keyboard scanning.
- GIME register writes, IRQ/FIRQ masks and acknowledgement, line/3.58 MHz timer
  clocks with 1986/1987 offsets (`--gime`), palette and CPU speed switching.
- Compatibility 32-column text, semigraphics-4, and baseline bitmap modes.
- Native 32/40/64/80-column text with colors, underline, and blink; baseline
  packed 1/2/4-bit graphics, display pitch, and video offsets.
- A shared 640×480 RGB presentation buffer. Video fetches physical RAM separately
  from the CPU's MMU mapping. Scanned horizontal spans are preserved before
  visible RAM/palette/register writes; video base is latched per frame.
- Integer NTSC master-clock events: 14,318,180 ticks/second, 912 ticks/line,
  263 lines/frame (about 59.69 Hz), independent of host frame pacing.
- Timed CPU memory accesses, sync edges, GIME interrupts, timer reload/IRQ delays,
  and SAM slow/fast/address-dependent speed selection.
- WD1793-style seek, sector read/write, multi-sector transfers, read address,
  synthesized read track, normal write-track formatting and force interrupt.
  DRQ/HALT/NMI handshakes, lost-data deadlines, step delays, 300 RPM rotation and
  rational DD/SD byte pacing all share the machine timeline.
- Read-only HFE weak-bit and SCP multi-revolution MFM playback, retaining recorded
  byte timing and index intervals. The disk menus and CLI accept both formats.
- DAC and single-bit sound, CoreAudio playback through SDL, and WAV recording.

## Current limits

This is **not VCC compatibility parity or verified full-machine bus accuracy**.
The supported CPU instructions now emit explicit bus sequences, with event timing
at each E-cycle boundary. Hardware traces are still needed to verify E/Q pin
phases, interrupt sampling, SYNC entry/release and reset-release timing. GIME
video prefetch, memory refresh/arbitration, floating-bus behavior and unusual
scroll/reserved modes remain approximate. HALT is honored at instruction
boundaries, as specified for the 6809, rather than interrupting a memory transfer.

Sector-only images still use synthesized angular positions. DMK retains recorded
positions, but mixed-density READ TRACK decoding, partial writes interrupted
mid-command, overlapping-field writes and analog drive behavior are not fully
modeled. Interrupted writes are discarded rather than partially damaging a host
image. The timer model follows the selected VCC reference, including zero
disabling reloads; further hardware test-ROM work is needed for timer quirks and
raster tricks.

Controller timing now includes the head-settle delay **before** searching for an
ID/index, once per command; assembled-byte timing for sector reads; CRC trailer
time before BUSY/INTRQ completion; and recurring force-index interrupts until
cancelled. The DMK data-mark search uses the WD179x's density-specific window.
Write-sector preamble/initial DRQ timing and Type I head-load/verify settling
remain approximate. These fixes do not establish pin-level WD179x accuracy.

The palette is digital RGB. Analog composite/artifact colors, PAL timing, 6309
instructions, cassette, expansion sound, general cartridges, external joysticks
and save states are not implemented. Timing-sensitive games and OS-9 have not
been validated. Host playback can underrun if emulation cannot keep real time;
the deterministic WAV capture is useful for separating that from emulated sound.

## Diagnostics and tests

```sh
../.venv/bin/python -m unittest discover -s tests -v
../.venv/bin/python -m coco --headless --frames 120 --screenshot /tmp/coco-basic.png
```

The tests include exhaustive 8-bit add/subtract operand/carry combinations,
decimal arithmetic, CPU addressing and interrupt cases, memory banking, keyboard,
video pixels, and integration tests against the user-supplied ROM. ROM tests use
`COCO_ROM` or `config.local.toml`; without either they report a skip. The ROM tests
boot both RAM sizes, execute arithmetic/loops, switch to 80 columns, and exercise
HSCREEN/HCLS. Device tests check timer deadlines and revisions, sync spacing,
mid-line palette changes, frame-latched video base, exact audio sample counts,
disk geometry, write protection, persistence and DRQ/HALT/NMI handshakes.
Disk ROM tests use `COCO_DISK_ROM` or the config's `disk_rom`, create temporary
images, then save, reopen, load and run a BASIC program. They also execute SOUND.
Without the required ROMs those integration tests report a skip.

On this Mac the native Cocoa/CoreAudio smoke test boots Disk BASIC and executes
`SOUND 128,5`. A separate scratch-image check completed `DSKINI 0`, including its
verification pass. These checks do not establish compatibility with every program.

Bus tests check explicit cycle counts across the supported opcode and indexed
postbyte space, selected reference address sequences, I/O side effects of dummy
reads, and events between a read-modify-write's read and write. DMK tests cover
the protection cases above. Menu tests cover mouse/keyboard loading, navigation,
write protection, busy-drive errors, ejection, cancellation and invalid files.
The native macOS check opens the browser and loads a DSK through the frontend.

Timing/protection regressions additionally check 1,000 raster frames, the minimum
fast-timer periods for both GIME revisions, one second of DD/SD byte pacing,
100 fractional-duration SCP index intervals, CRC/trailing-byte deadlines,
head-settle/index ordering, and interrupt cancellation. Independent encoded
fixtures cover HFE weak cells and rate changes, SCP jitter/overflow samples,
multi-revolution differences, index-crossing records, invalid-file rejection,
and read-only menu loading. A 6809 polling loader checks actual CPU-visible
data/status for both formats. Full-suite ROM tests still exercise BASIC boot,
sound, and save/reopen/load/run on the existing writable disk formats.

The controller checks use the command rules in the
[Western Digital FD179x data sheet](https://www.bitsavers.org/components/westernDigital/FD179X-01_Data_Sheet_Oct1979.pdf).
They are reference-based regression tests; a real CoCo logic-analyzer trace has
not yet been compared against the emulator.

Useful options:

- `--frames N`: stop after N emulated frames.
- `--headless`: run without a window (requires `--frames`; runs unthrottled).
- `--unthrottled`: disable real-time pacing in the windowed frontend.
- `--screenshot PATH`: save the final displayed image as PNG.
- `--type-text TEXT`: type using matrix key presses, beginning at frame 90.
- `--type-file PATH`: type a UTF-8 BASIC/text file through the same keyboard matrix.
- `--type-after N`: change when scripted typing begins.
- `--text-dump PATH`: save the **compatibility 32-column** text screen for diagnosis;
  it is not a decoder for native graphics/80-column screens.
- `--trace`: retain the last 64 CPU states and print recent states on failure.
- `--bus-trace PATH`: save the last 100,000 bus cycles as JSON on exit, including
  master tick, access kind, address and value. `wait` entries have no address or
  data because the CPU released the bus. Tracing adds overhead; resets begin a new
  trace. Example: `--headless --frames 120 --bus-trace /tmp/coco-bus.json`.

Scripted typing has a short delay after Return so BASIC can finish short commands.
Long-running commands may need separate runs or manual typing. It does not patch
ROM code, inject BASIC input buffers, or fake screen output.

## Code map

| File | Responsibility |
| --- | --- |
| `coco/cpu6809.py` | CPU interpreter |
| `coco/memory.py` | Memory bus, MMU, PIA, keyboard matrix, GIME registers |
| `coco/machine.py` | CPU/device integration and scripted key presses |
| `coco/clock.py` | Master-clock event queue and timed CPU bus |
| `coco/timing.py` | GIME raster, sync edges and timer events |
| `coco/audio.py` | DAC/bit output, resampling, playback and WAV capture |
| `coco/disk.py` | Sector images, Disk BASIC ROM loading and floppy controller |
| `coco/dmk.py` | DMK track records, CRCs, density and write-track tokens |
| `coco/video.py` | Physical-RAM scanline/span rendering |
| `coco/fonts.py` | Converted VCC bitmap glyphs |
| `coco/frontend.py` | Window, host input, configuration and CLI |
| `coco/menu.py` | Drive menus, file browser and media controls |
| `coco/demo_frontend.py` / `coco/demo.py` | Original test picture |

Core modules do not import Pygame. The macOS window and event loop run on the main
thread. `tools/import_vcc_fonts.py /path/to/VCC` reproduces the converted font data.

## License and references

GPL-3.0-or-later. See `LICENSE` and `NOTICE` for full terms and VCC attribution.
`LICENSE-MAME` retains the BSD-3-Clause attribution for the MAME cycle/format
references used for this milestone.
ROMs are supplied separately by the user. Reference VCC revision:
`c7046c2e48d5056409fc22897df7b22193d666fa`.

- [VCC CPU](https://github.com/VCCE/VCC/blob/c7046c2e48d5056409fc22897df7b22193d666fa/mc6809.cpp)
- [VCC MMU](https://github.com/VCCE/VCC/blob/c7046c2e48d5056409fc22897df7b22193d666fa/tcc1014mmu.cpp)
- [VCC GIME registers](https://github.com/VCCE/VCC/blob/c7046c2e48d5056409fc22897df7b22193d666fa/tcc1014registers.cpp)
- [VCC video](https://github.com/VCCE/VCC/blob/c7046c2e48d5056409fc22897df7b22193d666fa/tcc1014graphics.cpp)
- [VCC timing constants](https://github.com/VCCE/VCC/blob/c7046c2e48d5056409fc22897df7b22193d666fa/tcc1014graphics.h)
- [VCC disk controller](https://github.com/VCCE/VCC/blob/c7046c2e48d5056409fc22897df7b22193d666fa/FD502/wd1793.cpp)
- [MAME GIME behavior cross-check](https://github.com/mamedev/mame/blob/master/src/mame/trs/gime.cpp)
- [MAME CoCo disk interface cross-check](https://github.com/mamedev/mame/blob/master/src/devices/bus/coco/coco_fdc.cpp)
- [MAME 6809 bus sequences](https://github.com/mamedev/mame/blob/master/src/devices/cpu/m6809/m6809.lst)
- [MAME shared instruction sequences](https://github.com/mamedev/mame/blob/master/src/devices/cpu/m6809/base6x09.lst)
- [MAME DMK format implementation](https://github.com/mamedev/mame/blob/master/src/lib/formats/dmk_dsk.cpp)
- [Motorola 6809 programming manual](https://www.maddes.net/m6809pm/sections.htm)
- [Pygame CE](https://pyga.me/docs/)
