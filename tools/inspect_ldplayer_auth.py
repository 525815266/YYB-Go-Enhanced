"""Compare the LDPlayer SDK's native auth serializers using synthetic inputs.

Requires pefile and unicorn. Maps a hash-pinned public DLL into Unicorn only;
does not load it in Windows, read profiles, contact a server or use credentials.
Only protobuf primitive writers are substituted. This proves serialization,
not complete native login, header selection, key acceptance or persistence.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

import pefile
from unicorn import Uc, UC_ARCH_X86, UC_MODE_64, UC_HOOK_CODE
from unicorn.x86_const import (
    UC_X86_REG_RCX, UC_X86_REG_RDX, UC_X86_REG_R8,
    UC_X86_REG_RDI, UC_X86_REG_RBP, UC_X86_REG_RSP, UC_X86_REG_RIP,
    UC_X86_REG_RSI, UC_X86_REG_RAX,
)

from inspect_native_manualauth import HEAP, message_field, serialize, varint


SAMPLE_SHA256 = '46a0ac2eb34ef2e781a68aa4c6bbe5bd5ed1860218c6b4cbdf51ce81d78247c6'
WRITERS = (0x205cf0, 0x205fd0, 0x205ea0)


def inspect_autoauth_error_selection(pe):
    """Execute the native AutoAuth response check's final error selection.

    Synthetic response/error values only. The helper returns an incoming error
    first, otherwise the parsed response ret. This cannot identify which path
    produced an observed SDK callback without evidence from that invocation.
    """
    base = pe.OPTIONAL_HEADER.ImageBase
    image = pe.get_memory_mapped_image()
    emu = Uc(UC_ARCH_X86, UC_MODE_64)
    emu.mem_map(base, (len(image) + 4095) & ~4095)
    emu.mem_write(base, image)
    heap = 0x2000000
    emu.mem_map(heap, 0x10000)
    response, header = heap + 0x1000, heap + 0x2000
    emu.mem_write(response + 8, struct.pack('<Q', header))
    cases = []
    for incoming, response_ret, expected in ((-13, 0, -13), (0, -13, -13), (-7, -13, -7), (0, 0, 0)):
        emu.mem_write(header + 0x10, struct.pack('<i', response_ret))
        emu.reg_write(UC_X86_REG_RSI, incoming & 0xffffffff)
        emu.reg_write(UC_X86_REG_RDI, response)
        emu.emu_start(base + 0xa8661, base + 0xa8683, count=20)
        assert emu.reg_read(UC_X86_REG_RIP) == base + 0xa8683
        actual = struct.unpack('<i', struct.pack('<I', emu.reg_read(UC_X86_REG_RAX) & 0xffffffff))[0]
        assert actual == expected
        cases.append({'incoming_error': incoming, 'response_ret': response_ret, 'selected': actual})
    return {'selection_rva': '0xa8661', 'synthetic_cases': cases,
            'live_callback_source_verified': False}


def inspect_transport_psk_reads(pe):
    """Execute native access/refresh storage reads with synthetic objects.

    Both snippets stop immediately after the same virtual storage call, before
    its result is used. This verifies requested types, not successful refresh.
    """
    base = pe.OPTIONAL_HEADER.ImageBase
    image = pe.get_memory_mapped_image()
    emu = Uc(UC_ARCH_X86, UC_MODE_64)
    emu.mem_map(base, (len(image) + 4095) & ~4095)
    emu.mem_write(base, image)
    heap, stack = 0x2000000, 0x3000000
    emu.mem_map(heap, 0x10000)
    emu.mem_map(stack, 0x10000)
    channel, storage, vtable, getter = (heap + value for value in (0x1000, 0x2000, 0x3000, 0x4000))
    emu.mem_write(channel + 0x90, struct.pack('<Q', storage))
    emu.mem_write(storage, struct.pack('<Q', vtable))
    emu.mem_write(vtable + 0x30, struct.pack('<Q', getter))
    calls = []

    def hook(uc, address, size, _):
        if address == getter:
            assert uc.reg_read(UC_X86_REG_RCX) == storage
            assert stack <= uc.reg_read(UC_X86_REG_R8) < stack + 0x10000
            calls.append(uc.reg_read(UC_X86_REG_RDX) & 0xff)
            uc.emu_stop()

    emu.hook_add(UC_HOOK_CODE, hook)
    for start, origin in ((0x4abc6d, UC_X86_REG_RCX), (0x4ac68c, UC_X86_REG_RDI)):
        emu.reg_write(origin, channel)
        emu.reg_write(UC_X86_REG_RBP, stack + 0x8000)
        emu.reg_write(UC_X86_REG_RSP, stack + 0x7000)
        emu.emu_start(base + start, base + start + 64, count=30)
        assert emu.reg_read(UC_X86_REG_RIP) == getter
    assert calls == [1, 2], 'native channel no longer requests both PSK types'
    return {'native_getter_call_rvas': ['0x4abc7d', '0x4ac69c'],
            'requested_psk_types': calls, 'storage_method_offset': '0x30',
            'actual_psk_or_application_renewal_verified': False}


def compare(pe):
    memory = bytearray(0x4000)
    base = pe.OPTIONAL_HEADER.ImageBase

    def put(offset, fmt, *values):
        struct.pack_into(fmt, memory, offset, *values)

    def string(offset, value):
        if len(value) < 16:
            memory[offset:offset + len(value)] = value
            capacity = 15
        else:
            payload = offset + 0x40
            put(offset, '<Q', HEAP + payload)
            memory[payload:payload + len(value)] = value
            capacity = len(value)
        put(offset + 16, '<QQ', len(value), capacity)
        return HEAP + offset

    image = pe.get_memory_mapped_image()
    device, key = b'synthetic-device', b'synthetic-auto-key'
    third_app = message_field(1, b'synthetic-ticket')
    # Verified LD AppBaseRequest: device pointer +8, presence bits +0x14.
    put(0x100, '<QQ', base + 0x773428, string(0x200, device))
    put(0x114, '<I', 1)
    child, child_trace = serialize(image, base, memory, 0xe9be0, 0x100, {}, WRITERS)
    assert child == message_field(1, device)

    # ThirdApp builder 0xcff20: version 1901, login type 4, explicit empty
    # field 6 (helper 0xd35b0), and configured fields 7/8. Here use Go's 0/6.
    put(0, '<QQQII', base + 0x773768, HEAP + 0x100,
        string(0x300, third_app), 1901, 4)
    put(0x28, '<Q', string(0x400, b''))
    put(0x30, '<II', 0, 6)
    put(0x50, '<I', 0xef)
    manual, manual_trace = serialize(image, base, memory, 0x106fc0, 0,
                                     {HEAP + 0x100: child}, WRITERS)
    go_manual = (message_field(1, child) + varint(16) + varint(1901)
                 + message_field(3, third_app) + bytes.fromhex('200438004006'))
    expected = go_manual[:-4] + message_field(6, b'') + go_manual[-4:]
    assert manual == expected, 'LD manual serializer differs from verified layout'

    # AutoAuthRequest: base +8, key +0x10, context +0x18, version +0x20,
    # presence +0x28. Native builder 0xabc60 only assigns context when supplied.
    put(0x800, '<QQQQI', base + 0x773838, HEAP + 0x100,
        string(0x900, key), string(0xa00, b''), 1901)
    put(0x828, '<I', 7)
    auto, auto_trace = serialize(image, base, memory, 0x106320, 0x800,
                                 {HEAP + 0x100: child}, WRITERS)
    expected_auto = message_field(1, child) + varint(16) + varint(1901) + message_field(3, key)
    assert auto == expected_auto, 'LD auto serializer differs from Go fixture'
    context = b'synthetic-login-context'
    string(0xa00, context)
    put(0x828, '<I', 15)
    with_context, context_trace = serialize(image, base, memory, 0x106320, 0x800,
                                           {HEAP + 0x100: child}, WRITERS)
    assert with_context == expected_auto + message_field(4, context)
    return {'app_base_fields': child_trace, 'manual_auth_fields': manual_trace,
            'manual_go_difference': 'native third-app builder explicitly emits empty bytes field 6',
            'auto_auth_fields': auto_trace, 'auto_without_context_matches_go': True,
            'auto_with_context_fields': context_trace,
            'configured_session_type': 0, 'configured_session_module_type': 6,
            'configured_types_are_synthetic': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    args = parser.parse_args()
    raw = args.binary.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SAMPLE_SHA256:
        raise SystemExit('Sample changed; revalidate the LDPlayer locations first')
    pe = pefile.PE(data=raw)
    results = compare(pe)
    print(json.dumps({'sample_sha256': SAMPLE_SHA256, 'synthetic_inputs_only': True,
                      'native_serializers_executed': True, 'results': results,
                      'transport_psk_reads': inspect_transport_psk_reads(pe),
                      'autoauth_error_selection': inspect_autoauth_error_selection(pe),
                      'network_or_server_acceptance_verified': False}, indent=2))


if __name__ == '__main__':
    main()
