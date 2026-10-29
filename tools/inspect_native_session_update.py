"""Execute official session-type selection and copy with synthetic objects.

Requires pefile and unicorn. Verifies application/network storage separation,
not complete packet construction, server acceptance or credential renewal.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

import pefile
from unicorn import Uc, UC_ARCH_X86, UC_MODE_64, UC_HOOK_CODE
from unicorn.x86_const import (
    UC_X86_REG_RAX, UC_X86_REG_RCX, UC_X86_REG_RDX, UC_X86_REG_R8,
    UC_X86_REG_RDI, UC_X86_REG_RSI, UC_X86_REG_RSP, UC_X86_REG_RIP, UC_X86_REG_R15,
)

from inspect_native_auth_schema import SAMPLE_SHA256


def run_type_dispatch(pe):
    """Run only native enum selection, stopping before any protobuf parser."""
    base = pe.OPTIONAL_HEADER.ImageBase
    image = pe.get_memory_mapped_image()
    emu = Uc(UC_ARCH_X86, UC_MODE_64)
    emu.mem_map(base, (len(image) + 4095) & ~4095)
    emu.mem_write(base, image)
    branches = {
        0x170aa5: 'IlinkAppCommSessionInfo',
        0x170b8e: 'IlinkAppGetSessionInfo',
        0x170e68: 'app_auth_info',
        0x17107d: 'unsupported_type',
        0x171163: 'empty_session',
    }
    selected = []

    def hook(uc, address, size, _):
        if address - base in branches:
            selected.append((address - base, branches[address - base]))
            uc.emu_stop()

    emu.hook_add(UC_HOOK_CODE, hook)
    results = []
    for value in range(10):
        selected.clear()
        emu.reg_write(UC_X86_REG_RSI, 1)  # Synthetic nonempty session data.
        emu.reg_write(UC_X86_REG_R15, value)
        emu.emu_start(base + 0x170a72, base + 0x171274, count=40)
        assert len(selected) == 1
        address, branch = selected[0]
        expected = ('empty_session' if value == 0 else
                    'IlinkAppGetSessionInfo' if value == 4 else
                    'app_auth_info' if value == 8 else
                    'unsupported_type' if value > 8 else
                    'IlinkAppCommSessionInfo')
        assert branch == expected
        results.append({'session_type': value, 'native_branch': hex(address), 'parser_branch': branch})
    return results


def run(pe):
    base = pe.OPTIONAL_HEADER.ImageBase
    image = pe.get_memory_mapped_image()
    emu = Uc(UC_ARCH_X86, UC_MODE_64)
    emu.mem_map(base, (len(image) + 4095) & ~4095)
    emu.mem_write(base, image)
    heap, stack = 0x2000000, 0x3000000
    emu.mem_map(heap, 0x10000)
    emu.mem_map(stack, 0x10000)
    source = heap + 0x1000

    def put_string(address, value):
        assert len(value) < 16
        emu.mem_write(address, value.ljust(16, b'\0') + struct.pack('<QQ', len(value), 15))

    def get_string(address):
        size, capacity = struct.unpack('<QQ', emu.mem_read(address + 16, 16))
        assert capacity == 15 and size < 16
        return bytes(emu.mem_read(address, size))

    transport_offsets = (0x158, 0x178, 0x198, 0x390)
    for offset in transport_offsets:
        emu.mem_write(heap + offset, b'unchanged-net!!!')
    before = [bytes(emu.mem_read(heap + offset, 15)) for offset in transport_offsets]
    source_offsets = (8, 0x28, 0x48, 0x78, 0x98)
    target_offsets = (0x2d8, 0x2f8, 0x318, 0x348, 0x368)
    for index, (origin, target) in enumerate(zip(source_offsets, target_offsets)):
        put_string(source + origin, ('app-value-' + str(index)).encode())
        put_string(heap + target, b'old-app')
    emu.mem_write(source + 0x68, struct.pack('<QQ', 1234, 5678))
    emu.mem_write(source + 0xb8, b'\1')
    emu.reg_write(UC_X86_REG_RDI, heap)
    emu.reg_write(UC_X86_REG_RSI, source)
    emu.reg_write(UC_X86_REG_RSP, stack + 0x8000)
    writes = []

    def hook(uc, address, size, _):
        if address != base + 0x44050:
            return
        destination = uc.reg_read(UC_X86_REG_RCX)
        value = bytes(uc.mem_read(uc.reg_read(UC_X86_REG_RDX), uc.reg_read(UC_X86_REG_R8)))
        put_string(destination, value)
        writes.append(hex(destination - heap))
        sp = uc.reg_read(UC_X86_REG_RSP)
        target, = struct.unpack('<Q', uc.mem_read(sp, 8))
        uc.reg_write(UC_X86_REG_RAX, destination)
        uc.reg_write(UC_X86_REG_RSP, sp + 8)
        uc.reg_write(UC_X86_REG_RIP, target)

    emu.hook_add(UC_HOOK_CODE, hook)
    emu.emu_start(base + 0x1732f9, base + 0x1733dd, count=1000)
    assert emu.reg_read(UC_X86_REG_RIP) == base + 0x1733dd
    assert all(get_string(source + origin) == get_string(heap + target)
               for origin, target in zip(source_offsets, target_offsets))
    assert bytes(emu.mem_read(heap + 0x338, 16)) == struct.pack('<QQ', 1234, 5678)
    assert bytes(emu.mem_read(heap + 0x388, 1)) == b'\1'
    assert before == [bytes(emu.mem_read(heap + offset, 15)) for offset in transport_offsets]
    return {'native_slice': ['0x1732f9', '0x1733dd'], 'string_destinations': writes,
            'application_session_fields_copied': True, 'transport_session_unchanged': True,
            'complete_wire_or_renewal_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    args = parser.parse_args()
    raw = args.binary.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SAMPLE_SHA256:
        raise SystemExit('Sample changed; revalidate the native locations first')
    pe = pefile.PE(data=raw)
    print(json.dumps({'sample_sha256': SAMPLE_SHA256, 'synthetic_inputs_only': True,
                      'session_type_dispatch': run_type_dispatch(pe),
                      'session_update': run(pe)}, indent=2))


if __name__ == '__main__':
    main()
