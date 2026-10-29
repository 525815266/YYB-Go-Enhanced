"""Execute bounded official AutoAuth crypto-routing slices with synthetic data.

Requires pefile, unicorn and cryptography. AES-GCM is substituted with its
standard primitive; official instructions select the inputs and assign the
result to HybridEcdhRequest field 4. Native Req2Buf branches also select header
fields against a synthetic account. These slices do not verify a complete
network packet, server acceptance or OAuth renewal.
No DLL loading, live process, configuration, database or network access.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

import pefile
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from unicorn import Uc, UC_ARCH_X86, UC_MODE_64, UC_HOOK_CODE
from unicorn.x86_const import (
    UC_X86_REG_RAX, UC_X86_REG_RBP, UC_X86_REG_RBX, UC_X86_REG_RCX, UC_X86_REG_RDI,
    UC_X86_REG_RDX, UC_X86_REG_RIP, UC_X86_REG_RSI, UC_X86_REG_RSP,
    UC_X86_REG_R8, UC_X86_REG_R9, UC_X86_REG_R13, UC_X86_REG_R14, UC_X86_REG_R15,
)

from inspect_native_auth_schema import SAMPLE_SHA256, inspect

HEAP = 0x2000000
STACK = 0x3000000


def run_slices(pe, input_key):
    image = pe.get_memory_mapped_image()
    base = pe.OPTIONAL_HEADER.ImageBase
    emu = Uc(UC_ARCH_X86, UC_MODE_64)
    emu.mem_map(base, (len(image) + 4095) & ~4095)
    emu.mem_write(base, image)
    for address in (HEAP, STACK):
        emu.mem_map(address, 0x10000)
    frame = STACK + 0x5000
    stack = STACK + 0x1000
    allocation = HEAP + 0x6000
    trace = []

    def allocate(size):
        nonlocal allocation
        pointer = allocation
        allocation += (size + 32 + 15) & ~15
        if allocation >= HEAP + 0x10000:
            raise RuntimeError('synthetic allocation budget exceeded')
        return pointer

    def get_u64(address):
        return struct.unpack('<Q', emu.mem_read(address, 8))[0]

    def set_string(address, value):
        data = bytearray(32)
        if len(value) < 16:
            data[:len(value)] = value
            capacity = 15
        else:
            pointer = allocate(len(value))
            emu.mem_write(pointer, value + b'\0')
            struct.pack_into('<Q', data, 0, pointer)
            capacity = len(value)
        struct.pack_into('<QQ', data, 16, len(value), capacity)
        emu.mem_write(address, bytes(data))

    def get_string(address):
        size, capacity = struct.unpack('<QQ', emu.mem_read(address + 16, 16))
        return bytes(emu.mem_read(get_u64(address) if capacity >= 16 else address, size)) if size else b''

    def return_from_hook(value=0):
        sp = emu.reg_read(UC_X86_REG_RSP)
        destination = get_u64(sp)
        emu.reg_write(UC_X86_REG_RAX, value)
        emu.reg_write(UC_X86_REG_RSP, sp + 8)
        emu.reg_write(UC_X86_REG_RIP, destination)

    cek = bytes(range(32))
    aad = hashlib.sha256(b'synthetic-only-hybrid-aad').digest()
    iv = bytes(range(12))  # Deterministic fixture only; never a production nonce.
    enc_key = input_key[:24]
    set_string(HEAP + 0x48, b'')
    set_string(HEAP + 0x200, input_key)
    set_string(frame + 8, cek)
    set_string(frame + 0x28, aad)
    set_string(HEAP + 0x5000, b'')
    emu.mem_write(frame + 0x60, struct.pack('<Q', HEAP + 0x5000))
    for reg, value in ((UC_X86_REG_RSP, stack), (UC_X86_REG_RBP, frame),
                       (UC_X86_REG_RSI, HEAP), (UC_X86_REG_RDI, 0),
                       (UC_X86_REG_R14, base + 0x9f19d0)):
        emu.reg_write(reg, value)

    def hook(uc, address, size, _):
        rva = address - base
        if rva in (0x11a58a, 0x11bb04, 0x11bfd6):
            uc.emu_stop()
        elif rva == 0x708eec:  # allocator, bounded to this synthetic heap.
            return_from_hook(allocate(uc.reg_read(UC_X86_REG_RCX)))
        elif rva == 0x708f28:  # free of synthetic temporary storage.
            return_from_hook()
        elif rva == 0x1a0950:
            sp = uc.reg_read(UC_X86_REG_RSP)
            key = bytes(uc.mem_read(uc.reg_read(UC_X86_REG_RCX), uc.reg_read(UC_X86_REG_RDX)))
            actual_aad = bytes(uc.mem_read(uc.reg_read(UC_X86_REG_R8), uc.reg_read(UC_X86_REG_R9)))
            plain = bytes(uc.mem_read(get_u64(sp + 0x28), get_u64(sp + 0x30)))
            if key != enc_key or actual_aad != aad or plain != cek:
                raise RuntimeError('official AES-GCM argument selection differs from fixture')
            sealed = AESGCM(key).encrypt(iv, plain, actual_aad)
            wrapped = sealed[:-16] + iv + sealed[-16:]
            set_string(get_u64(sp + 0x38), wrapped)
            trace.append({'call_rva': '0x11ba6c', 'key_length': len(key),
                          'aad_length': len(actual_aad), 'plaintext_length': len(plain)})
            return_from_hook()
        elif rva == 0x44050:  # std::string assignment; no crypto decision here.
            dest = uc.reg_read(UC_X86_REG_RCX)
            value = bytes(uc.mem_read(uc.reg_read(UC_X86_REG_RDX), uc.reg_read(UC_X86_REG_R8)))
            set_string(dest, value)
            return_from_hook(dest)

    emu.hook_add(UC_HOOK_CODE, hook)
    # Constructor retains at most 24 bytes of the provided enc key.
    for reg, value in ((UC_X86_REG_RBX, HEAP + 0x200), (UC_X86_REG_RDI, HEAP + 0x48),
                       (UC_X86_REG_R13, 0), (UC_X86_REG_R15, HEAP)):
        emu.reg_write(reg, value)
    emu.emu_start(base + 0x11a4a7, base + 0x11a58a, count=2000)
    if emu.reg_read(UC_X86_REG_RIP) != base + 0x11a58a or get_string(HEAP + 0x48) != enc_key:
        raise RuntimeError('native enc-key constructor differs from fixture')
    emu.reg_write(UC_X86_REG_RBP, frame)
    emu.reg_write(UC_X86_REG_RDI, 0)
    # Run the official key-presence branch and its AES-GCM input selection.
    emu.emu_start(base + 0x11ba06, base + 0x11bb04, count=2000)
    if emu.reg_read(UC_X86_REG_RIP) != base + 0x11bb04:
        raise RuntimeError('native key-wrap slice did not finish')
    wrapped = get_string(frame - 0x38)
    if bool(enc_key) != bool(trace) or bool(enc_key) != bool(wrapped):
        raise RuntimeError('official optional-key behavior differs from fixture')
    # Run the official assignment into HybridEcdhRequest field 4 (object +0x18).
    emu.emu_start(base + 0x11bf7f, base + 0x11bfd6, count=2000)
    if emu.reg_read(UC_X86_REG_RIP) != base + 0x11bfd6:
        raise RuntimeError('native field-assignment slice did not finish')
    field4 = get_string(get_u64(frame + 0x60))
    if field4 != wrapped or not (get_u64(frame + 0x78) & 8):
        raise RuntimeError('wrapped CEK was not assigned to Hybrid field 4')
    if field4:
        restored = AESGCM(enc_key).decrypt(field4[-28:-16], field4[:-28] + field4[-16:], aad)
        if restored != cek:
            raise RuntimeError('synthetic wrapped CEK round trip failed')
    return {'input_key_length': len(input_key), 'enc_key_length': len(enc_key),
            'native_key_truncation_verified': True, 'native_key_wrap_calls': trace,
            'hybrid_field_4_present': True, 'hybrid_field_4_length': len(field4),
            'hybrid_field_4_matches_wrapped_cek': True}


def run_header_branch(pe, application_account, ignore_app_header=False,
                      transport_crypto=0, application_crypto=17):
    """Execute the two native Req2Buf branches, replacing only field writes."""
    image = pe.get_memory_mapped_image()
    base = pe.OPTIONAL_HEADER.ImageBase
    emu = Uc(UC_ARCH_X86, UC_MODE_64)
    emu.mem_map(base, (len(image) + 4095) & ~4095)
    emu.mem_write(base, image)
    for address in (HEAP, STACK):
        emu.mem_map(address, 0x10000)
    # Native getters execute against a synthetic account, with no INI access.
    account = HEAP + 0x1000
    emu.mem_write(HEAP + 0x90, struct.pack('<Q', account))
    emu.mem_write(HEAP + 0x108, bytes([ignore_app_header]))
    emu.mem_write(HEAP + 0x178, struct.pack('<I', transport_crypto))
    emu.mem_write(HEAP + 0x118, struct.pack('<I', application_crypto))
    emu.mem_write(account, struct.pack('<Q', base + 0x7ebea0))
    emu.mem_write(account + 0x2c8, struct.pack('<I', 1504))
    for register, value in ((UC_X86_REG_RSI, HEAP), (UC_X86_REG_RDI, 0),
                            (UC_X86_REG_RSP, STACK + 0x1000)):
        emu.reg_write(register, value)
    fields = {}

    def hook(uc, address, size, _):
        if address != base + 0x1d4b40:
            return
        number = uc.reg_read(UC_X86_REG_RDX)
        fields[number] = uc.reg_read(UC_X86_REG_R8)
        sp = uc.reg_read(UC_X86_REG_RSP)
        destination = struct.unpack('<Q', uc.mem_read(sp, 8))[0]
        uc.reg_write(UC_X86_REG_RSP, sp + 8)
        uc.reg_write(UC_X86_REG_RIP, destination)

    emu.hook_add(UC_HOOK_CODE, hook)
    # Field 25 is copied from the application request, independently of the
    # transport crypto at +0x178 which the app-account branch writes as field 7.
    emu.emu_start(base + 0x1a2cba, base + 0x1a2cd0, count=100)
    start, stop = (0x1a2d7c, 0x1a2e01) if application_account else (0x1a3897, 0x1a38f2)
    emu.emu_start(base + start, base + stop, count=1000)
    expected = {25: application_crypto, 20: 1504, 7: transport_crypto,
                8: 4 if ignore_app_header else 0, 17: 0, 18: 0}
    if not application_account:
        expected[7], expected[8], expected[18] = 0, 0, 1
    if emu.reg_read(UC_X86_REG_RIP) != base + stop or fields != expected:
        raise RuntimeError('native account header branch differs from fixture')
    return {'application_account': application_account, 'ignore_app_header': ignore_app_header,
            'native_integer_fields': fields}


def run_transport_crypto_dispatch(pe, mode):
    """Execute only native branch selection; do not substitute a full packet."""
    raw = pe.get_memory_mapped_image()
    base = pe.OPTIONAL_HEADER.ImageBase
    emu = Uc(UC_ARCH_X86, UC_MODE_64)
    emu.mem_map(base, (len(raw) + 4095) & ~4095)
    emu.mem_write(base, raw)
    emu.mem_map(HEAP, 0x10000)
    emu.mem_write(HEAP + 0x178, struct.pack('<I', mode))
    emu.reg_write(UC_X86_REG_RSI, HEAP)
    destinations = {0: (0x1a3686, 'copy'), 16: (0x1a3592, 'aes_gcm'),
                    17: (0x1a343d, 'hybrid_ecdh')}
    stop, primitive = destinations[mode]
    emu.emu_start(base + 0x1a336e, base + stop, count=20)
    if emu.reg_read(UC_X86_REG_RIP) != base + stop:
        raise RuntimeError('native transport crypto dispatch differs from fixture')
    return {'transport_crypto': mode, 'selected_primitive': primitive,
            'native_destination_rva': hex(stop), 'complete_packet_verified': False}


def run_instance_task_selection(pe, device_instance):
    """Run real provider virtual getters and task selection, not full Init."""
    base = pe.OPTIONAL_HEADER.ImageBase
    raw = pe.get_memory_mapped_image()
    emu = Uc(UC_ARCH_X86, UC_MODE_64)
    emu.mem_map(base, (len(raw) + 4095) & ~4095)
    emu.mem_write(base, raw)
    emu.mem_map(HEAP, 0x10000)
    emu.mem_map(STACK, 0x10000)
    network, provider, task = HEAP + 0x1000, HEAP + 0x2000, HEAP + 0x4000
    table = 0x7ee9a0 if device_instance else 0x7ee2d8
    locator, = struct.unpack('<Q', pe.get_data(table - 8, 8))
    signature, offset, _, descriptor, _, own = struct.unpack('<6I', pe.get_data(locator - base, 24))
    name = pe.get_data(descriptor + 16, 100).split(b'\0', 1)[0].decode('ascii')
    expected = '.?AVIlinkDeviceInstanceImpl@@' if device_instance else '.?AVIlinkAppInstanceImpl@@'
    assert signature == 1 and offset == 8 and own == locator - base and name == expected
    emu.mem_write(network + 0x20, struct.pack('<Q', provider))
    emu.mem_write(provider, struct.pack('<Q', base + table))
    emu.reg_write(UC_X86_REG_R13, network)
    emu.reg_write(UC_X86_REG_RBP, STACK + 0x8000)
    emu.reg_write(UC_X86_REG_RSP, STACK + 0x6000)
    selected = []

    def hook(uc, address, size, _):
        if address == base + 0x708eec:
            assert uc.reg_read(UC_X86_REG_RCX) == 0x190
            uc.reg_write(UC_X86_REG_RAX, task)
            sp = uc.reg_read(UC_X86_REG_RSP)
            destination, = struct.unpack('<Q', uc.mem_read(sp, 8))
            uc.reg_write(UC_X86_REG_RSP, sp + 8)
            uc.reg_write(UC_X86_REG_RIP, destination)
        elif address in (base + 0x17a740, base + 0x17a9a0):
            assert uc.reg_read(UC_X86_REG_RCX) == task
            assert uc.reg_read(UC_X86_REG_RDX) == provider + 0x190
            selected.append(address - base)
            uc.emu_stop()  # Do not claim that full constructor/Init ran.

    emu.hook_add(UC_HOOK_CODE, hook)
    emu.emu_start(base + 0x109749, base + 0x1097ac, count=100)
    assert selected == [0x17a9a0 if device_instance else 0x17a740]
    start, stop = (0x17abc3, 0x17abcd) if device_instance else (0x17a963, 0x17a96d)
    emu.reg_write(UC_X86_REG_RDI, task)
    emu.emu_start(base + start, base + stop, count=1)
    mode, = struct.unpack('<I', emu.mem_read(task + 0x178, 4))
    assert mode == (16 if device_instance else 0)
    return {'instance_type': name, 'provider_vtable_rva': hex(table),
            'selected_constructor_rva': hex(selected[0]), 'transport_crypto': mode,
            'allocator_substituted': True, 'native_virtual_getters_executed': True,
            'complete_constructor_or_init_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    args = parser.parse_args()
    raw = args.binary.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SAMPLE_SHA256:
        raise SystemExit('Sample changed; revalidate native slice locations first')
    pe = pefile.PE(data=raw)
    schema = inspect(pe, 0x7d4110, 0x9f04f0)
    actual = [(field['number'], field['type']) for field in schema['fields']]
    if actual != [(1, 'message'), (2, 'uint32'), (3, 'bytes'), (4, 'bytes')]:
        raise SystemExit('AutoAuth field schema differs from the verified fixture')
    results = [run_slices(pe, key) for key in (b'', bytes(range(16)), bytes(range(24)), bytes(range(32)))]
    print(json.dumps({'sample_sha256': SAMPLE_SHA256, 'synthetic_inputs_only': True,
                      'native_slices_executed': True, 'standard_aes_primitive_substituted': True,
                      'autoauth_schema_verified': True, 'cases': results,
                      'header_branches': [run_header_branch(pe, True), run_header_branch(pe, True, True),
                                          run_header_branch(pe, False)],
                      'independent_crypto_fields': [run_header_branch(pe, True, transport_crypto=mode)
                                                    for mode in (0, 16, 17)],
                      'transport_crypto_dispatch': [run_transport_crypto_dispatch(pe, mode)
                                                     for mode in (0, 16, 17)],
                      'instance_task_selection': [run_instance_task_selection(pe, device)
                                                  for device in (False, True)],
                      'network_or_server_acceptance_verified': False}, indent=2))


if __name__ == '__main__':
    main()
