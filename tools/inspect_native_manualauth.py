"""Offline field-level comparison with a known official ilink2.dll sample.

Requires pefile and unicorn. Only synthetic inputs are used. No DLL is loaded
into Windows and no network, account database or live process is accessed.
Native serializers select the fields; known protobuf primitive writers are
substituted and traced. This verifies the message layout, not server acceptance.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

import pefile
from unicorn import Uc, UC_ARCH_X86, UC_MODE_64, UC_HOOK_CODE
from unicorn.x86_const import UC_X86_REG_RCX, UC_X86_REG_RDX, UC_X86_REG_R8, UC_X86_REG_RSP, UC_X86_REG_RIP


SAMPLE_SHA256 = '8b69772a79e5892ab48aff13a096a1a806219061a17395d14f311305d4a74d7a'
HEAP = 0x2000000
STACK = 0x3000000
STOP = 0x4000000


def varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def message_field(number, value):
    return varint(number * 8 + 2) + varint(len(value)) + value


def synthetic_objects(base):
    data = bytearray(0x4000)

    def put(offset, fmt, *values):
        struct.pack_into(fmt, data, offset, *values)

    def string(offset, value):
        pointer = HEAP + offset
        if len(value) < 16:
            data[offset:offset + len(value)] = value
            capacity = 15
        else:
            payload = offset + 0x40
            put(offset, '<Q', HEAP + payload)
            data[payload:payload + len(value)] = value
            capacity = len(value)
        put(offset + 0x10, '<QQ', len(value), capacity)
        return pointer

    device = b'0123456789abcdef'
    ticket = b'synthetic-ticket'
    # Layout confirmed by constructors, RTTI and the official request builder.
    put(0, '<QQ', base + 0x7e9718, HEAP + 0x100)
    put(0x10, '<Q', string(0x300, message_field(1, ticket)))
    put(0x18, '<II', 1901, 4)
    put(0x28, '<Q', string(0x400, b''))
    put(0x30, '<II', 0, 6)
    put(0x3c, '<I', 0xcf)  # present: 1,2,3,4,7,8; field 6 is absent
    put(0x100, '<QQ', base + 0x7e96b0, string(0x200, device))
    put(0x114, '<I', 1)
    return data, device, ticket


def serialize(image, base, memory, entry, object_offset, child_messages):
    emu = Uc(UC_ARCH_X86, UC_MODE_64)
    emu.mem_map(base, (len(image) + 4095) & ~4095)
    emu.mem_write(base, image)
    for address in (HEAP, STACK, STOP):
        emu.mem_map(address, 0x10000)
    emu.mem_write(HEAP, bytes(memory))
    stack_top = STACK + 0x8008
    emu.mem_write(stack_top, struct.pack('<Q', STOP))
    emu.reg_write(UC_X86_REG_RSP, stack_top)
    emu.reg_write(UC_X86_REG_RCX, HEAP + object_offset)
    emu.reg_write(UC_X86_REG_RDX, HEAP + 0x3000)
    output = bytearray()
    trace = []

    def hook(uc, address, size, user_data):
        rva = address - base
        if rva not in (0x1eeab0, 0x1eefc0, 0x1eed80):
            return
        field = uc.reg_read(UC_X86_REG_RCX)
        value = uc.reg_read(UC_X86_REG_RDX)
        if rva == 0x1eefc0:  # WireFormatLite::WriteUInt32
            output.extend(varint(field * 8) + varint(value & 0xffffffff))
            trace.append({'field': field, 'wire_type': 0, 'value': value & 0xffffffff})
        else:
            if rva == 0x1eed80:  # WireFormatLite::WriteMessage
                payload = child_messages[value]
            else:  # WireFormatLite::WriteBytes; MSVC std::string layout
                length, capacity = struct.unpack('<QQ', uc.mem_read(value + 0x10, 16))
                pointer = struct.unpack('<Q', uc.mem_read(value, 8))[0] if capacity >= 16 else value
                payload = bytes(uc.mem_read(pointer, length))
            output.extend(message_field(field, payload))
            trace.append({'field': field, 'wire_type': 2, 'length': len(payload)})
        sp = uc.reg_read(UC_X86_REG_RSP)
        destination = struct.unpack('<Q', uc.mem_read(sp, 8))[0]
        uc.reg_write(UC_X86_REG_RSP, sp + 8)
        uc.reg_write(UC_X86_REG_RIP, destination)

    emu.hook_add(UC_HOOK_CODE, hook)
    emu.emu_start(base + entry, STOP, count=10000)
    if emu.reg_read(UC_X86_REG_RIP) != STOP:
        raise RuntimeError('native serializer did not finish within instruction budget')
    return bytes(output), trace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True, type=Path)
    args = parser.parse_args()
    raw = args.binary.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != SAMPLE_SHA256:
        raise SystemExit('Sample changed; revalidate the RVAs before using this harness')
    pe = pefile.PE(data=raw)
    image = pe.get_memory_mapped_image()
    base = pe.OPTIONAL_HEADER.ImageBase
    memory, device, ticket = synthetic_objects(base)
    child, child_trace = serialize(image, base, memory, 0x168280, 0x100, {})
    root, root_trace = serialize(image, base, memory, 0x169ab0, 0, {HEAP + 0x100: child})
    expected = (message_field(1, message_field(1, device)) + varint(16) + varint(1901)
                + message_field(3, message_field(1, ticket)) + bytes.fromhex('200438004006'))
    assert root == expected, 'native field layout differs from the expected fixture'
    print(json.dumps({'sample_sha256': digest, 'synthetic_inputs_only': True,
                      'native_app_base_fields': child_trace, 'native_manual_auth_fields': root_trace,
                      'manual_auth_hex': root.hex(), 'wire_layout_matches_fixture': True}, indent=2))


if __name__ == '__main__':
    main()
