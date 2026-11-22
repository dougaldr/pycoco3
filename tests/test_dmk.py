import binascii
from pathlib import Path
import tempfile
import unittest
from coco.clock import Clock, MASTER_HZ
from coco.disk import FDC, open_disk, REVOLUTION, CRC, RNF, WP
from coco.dmk import DMKImage, TrackEncoder
import test_devices as devices


def make_dmk(path, records, tracks=1, protected=False, track_length=6250):
    """Build independent byte-level fixtures; no copyrighted disks required.

    records: (physical track, position, C, H, R, N, data byte, options).
    """
    size = track_length + 128
    header = bytearray(16)
    header[0], header[1], header[4] = 255 if protected else 0, tracks, 16
    header[2:4] = size.to_bytes(2, 'little')
    result = header
    for track in range(tracks):
        block = bytearray(128) + bytearray(b'\x4e'*track_length)
        index = 0
        for physical, position, c, h, r, n, fill, options in records:
            if physical != track: continue
            double = not options.get('single', False)
            stride = 1 if double else 2
            prefix = b'\xa1'*3 if double else b''
            ident = b'\xfe' + bytes((c,h,r,n))
            crc = binascii.crc_hqx(prefix+ident,0xFFFF) ^ int(options.get('bad_id',False))
            ident += crc.to_bytes(2,'big')
            mark = 0xF8 if options.get('deleted') else 0xFB
            data = bytes([mark]) + bytes([fill])*(128<<n)
            crc = binascii.crc_hqx(prefix+data,0xFFFF) ^ int(options.get('bad_data',False))
            stream = prefix + ident + b'\x4e'*(22 if double else 11) + b'\0'*(12 if double else 6)
            if not options.get('missing'):
                stream += prefix + data + crc.to_bytes(2,'big')
            offset = 128 + position
            pointer = offset + len(prefix)*stride
            block[index*2:index*2+2] = (pointer | (0x8000 if double else 0)).to_bytes(2,'little')
            index += 1
            for i,value in enumerate(stream):
                for duplicate in range(stride):
                    block[128+(position+i*stride+duplicate)%track_length] = value
        result += block
    path.write_bytes(result)
    return path


class DMKTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'protected.dmk'
        self.clock = Clock()

    def mount(self, records, **options):
        make_dmk(self.path,records,**options)
        self.image = open_disk(self.path,True)
        self.fdc = FDC(self.clock,[self.image])
        self.fdc.write(0x40,0x29)
        return self.image

    def transfer(self, command, payload=None):
        f = self.fdc
        f.write(0x48,command)
        data = bytearray()
        for _ in range(20000):
            if not f.busy: break
            self.assertIsNotNone(self.clock.next_event)
            self.clock.advance(self.clock.next_event)
            if f.drq:
                if payload is None: data.append(f.read(0x4B))
                else:
                    f.write(0x4B,payload[len(data)])
                    data.append(0)
        self.assertFalse(f.busy)
        return bytes(data)

    def test_duplicate_ids_selected_by_rotation_not_logical_sector(self):
        self.mount([(0,100,0,0,1,0,0x11,{}),(0,2500,0,0,1,0,0x22,{})])
        records=self.image.records(0,0)
        self.assertEqual(len(records),2)
        self.assertEqual(self.transfer(0x80),b'\x11'*128)
        self.assertEqual(self.transfer(0x80),b'\x22'*128)
        self.assertEqual(self.transfer(0x80),b'\x11'*128)

    def test_bad_data_crc_and_deleted_mark_reach_controller_status(self):
        self.mount([(0,100,0,0,1,1,0x5A,{'bad_data':True,'deleted':True})])
        self.assertEqual(self.transfer(0x80),b'\x5a'*256)
        self.assertEqual(self.fdc.read(0x48),CRC|32)

    def test_bad_id_crc_is_readable_but_not_accepted_for_sector_read(self):
        self.mount([(0,100,0,0,1,0,1,{'bad_id':True})])
        raw=self.image.records(0,0)[0].ident
        self.assertEqual(self.transfer(0xC0),raw)
        self.assertEqual(self.fdc.status,CRC)
        self.fdc.sector=1
        start=self.clock.now
        self.assertEqual(self.transfer(0x80),b'')
        self.assertEqual(self.clock.now, (start//REVOLUTION+5)*REVOLUTION)
        self.assertEqual(self.fdc.status,CRC|RNF)

    def test_nonstandard_id_size_and_missing_data_mark(self):
        self.mount([(0,100,7,1,77,2,0x33,{}),(0,2000,7,1,88,0,0,{'missing':True})])
        self.fdc.track,self.fdc.sector=7,77
        self.assertEqual(self.transfer(0x8A),b'\x33'*512)  # side ID compare against 1
        self.fdc.sector=88
        self.assertEqual(self.transfer(0x80),b'')
        self.assertEqual(self.fdc.status,RNF)

    def test_density_selection_and_single_density_byte_pacing(self):
        self.mount([(0,100,0,0,1,0,0x44,{'single':True})])
        self.assertEqual(self.transfer(0x80),b'')  # wrong density
        self.fdc.write(0x40,9)
        self.fdc.write(0x48,0x80)
        self.clock.advance(self.clock.next_event)
        first=self.clock.now
        self.assertEqual(self.fdc.read(0x4B),0x44)
        self.clock.advance(self.clock.next_event)
        self.assertEqual(self.clock.now-first,MASTER_HZ*8//125000)
        self.assertEqual(self.fdc.read(0x4B),0x44)

    def test_read_track_returns_recorded_bytes_and_one_revolution(self):
        self.mount([(0,100,0,0,1,0,0x31,{'bad_data':True})],track_length=6500)
        start=(self.clock.now//REVOLUTION+1)*REVOLUTION
        data=self.transfer(0xE0)
        self.assertEqual(data,self.image.track_stream(0,0))
        self.assertEqual(len(data),6500)
        self.assertEqual(self.clock.now,start+REVOLUTION)

    def test_sector_write_changes_only_selected_duplicate_and_crc(self):
        self.mount([(0,100,0,0,1,0,1,{'bad_data':True}),(0,2500,0,0,1,0,2,{})])
        self.transfer(0xA1,b'\x55'*128)
        reopened=DMKImage(self.path)
        records=reopened.records(0,0)
        self.assertEqual(records[0].payload,b'\x55'*128)
        self.assertTrue(records[0].data_crc_ok)
        self.assertEqual(records[0].mark,0xF8)
        self.assertEqual(records[1].payload,b'\x02'*128)

    def test_header_write_protection_and_dsk_extension_detection(self):
        self.mount([(0,100,0,0,1,0,1,{})],protected=True)
        self.assertFalse(self.image.writable)
        self.transfer(0xA0,bytes(128))
        self.assertEqual(self.fdc.status,WP)
        renamed=self.path.with_suffix('.dsk')
        renamed.write_bytes(self.path.read_bytes())
        self.assertIsInstance(open_disk(renamed),DMKImage)

    def test_seek_verify_checks_recorded_id_not_physical_track_number(self):
        self.mount([(0,100,7,0,1,0,1,{})])
        self.transfer(0x04)  # restore + verify: physical track zero carries ID 7
        self.assertEqual(self.fdc.status,RNF)

    def test_invalid_pointer_and_truncation_rejected(self):
        self.mount([(0,100,0,0,1,0,1,{})])
        data=bytearray(self.path.read_bytes())
        data[16:18]=b'\xff\xbf'
        self.path.write_bytes(data)
        with self.assertRaises(ValueError):open_disk(self.path)
        self.path.write_bytes(data[:-1])
        with self.assertRaises(ValueError):open_disk(self.path)

    def test_crc_token_expands_two_cells_and_creates_valid_track(self):
        self.mount([])
        stream=b'\x4e'*80+b'\xf5'*3+b'\xfe\0\0\x29\0\xf7'
        stream+=b'\x4e'*22+b'\0'*12+b'\xf5'*3+b'\xf8'+b'\xe5'*128+b'\xf7'
        self.transfer(0xF0,stream.ljust(6250,b'\x4e'))
        records=DMKImage(self.path).records(0,0)
        self.assertEqual(len(records),1)
        self.assertEqual(records[0].ident[:4],b'\0\0\x29\0')
        self.assertTrue(records[0].id_crc_ok and records[0].data_crc_ok)
        self.assertEqual(records[0].mark,0xF8)
        self.assertEqual(self.clock.now,2*REVOLUTION)

    def test_force_interrupt_cancels_pending_track_transfer(self):
        self.mount([(0,100,0,0,1,0,1,{})])
        original=self.path.read_bytes()
        self.fdc.write(0x48,0xF0)
        self.clock.advance(self.clock.next_event)
        self.fdc.write(0x4B,0xF5)
        self.fdc.write(0x48,0xD8)
        self.assertTrue(self.fdc.intrq)
        self.assertFalse(self.fdc.busy)
        self.assertIsNone(self.clock.next_event)
        self.assertEqual(self.path.read_bytes(),original)

    def test_6809_program_observes_protection_status_and_payload(self):
        from coco.machine import Machine
        self.mount([(0,100,0,0,1,0,0x5A,{'bad_data':True,'deleted':True})])
        program=bytearray.fromhex('8629 b7ff40 8601 b7ff4a 8680 b7ff48 8e0200 c680')
        labels,branches={},[]
        def branch(opcode,label):
            program.extend((opcode,0));branches.append((len(program)-1,label))
        labels['byte']=len(program)
        program.extend(bytes.fromhex('b6ff48 8502'))
        branch(0x27,'byte')
        program.extend(bytes.fromhex('b6ff4b a780 5a'))
        branch(0x26,'byte')
        labels['done']=len(program)
        program.extend(bytes.fromhex('b6ff48 8501'))
        branch(0x26,'done')
        program.extend(bytes.fromhex('b70300 8655 b70301 20fe'))
        for offset,label in branches: program[offset]=(labels[label]-offset-1)&255
        rom=bytearray(32768)
        rom[:len(program)]=program
        rom[-2:]=b'\x80\x00'
        m=Machine(rom,disk_rom=bytes(8192),drives=[self.image],video=False,audio=False)
        # Reset masks interrupts and NMI remains inhibited until LDS. This probe
        # deliberately polls DRQ/status, just as a protection loader can.
        for _ in range(3):m.run_frame()
        self.assertEqual(m.memory.ram[0x10200:0x10280],b'\x5a'*128)
        self.assertEqual(m.memory.read8(0x300),CRC|32)
        self.assertEqual(m.memory.read8(0x301),0x55)


class DMKBootTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        devices.DiskBootTests.setUpClass()
        cls.rom,cls.diskrom=devices.DiskBootTests.rom,devices.DiskBootTests.diskrom

    boot=devices.DiskBootTests.boot
    command=devices.DiskBootTests.command

    def test_disk_basic_save_reopen_load_run_on_dmk(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'rsdos.dmk'
            records=[(t,80+(s-1)*342,t,0,s,1,255,{}) for t in range(35) for s in range(1,19)]
            make_dmk(path,records,tracks=35)
            m=self.boot(open_disk(path,True))
            self.command(m,'10 PRINT "DMK WORKS"')
            self.command(m,'SAVE "PROBE"',360)
            m=self.boot(open_disk(path))
            self.command(m,'LOAD "PROBE"',360)
            self.command(m,'RUN',90)
            self.assertIn('\nDMK WORKS ',m.text_screen())
