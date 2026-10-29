"""Compare the verified PCMgr Hybrid encoder using synthetic inputs only.

Windows x64, cryptography. Executes only the inspected crypto constructor and
encoder in this process; no account/config reads, transport or production DB.
Do not pass real credentials. The generated ephemeral key is discarded here.
"""
import argparse
import ctypes as c
import hashlib
import json
import os
from pathlib import Path
import struct

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


EXPECTED = '46a0ac2eb34ef2e781a68aa4c6bbe5bd5ed1860218c6b4cbdf51ce81d78247c6'
PUBLIC = bytes.fromhex(
    '04ef87876d6478b15f1796eab12068610541173b7176b67f1dcc86683e901acd44'
    'd18b4ac36938251d0812dd0cf842aa2d6cbb8115712d1c0087dcefc14a44cd58')


def protobuf(raw):
    at, fields = 0, {}

    def number():
        nonlocal at
        value = 0
        for shift in range(0, 70, 7):
            if at >= len(raw):
                raise ValueError('truncated synthetic protobuf')
            b = raw[at]
            at += 1
            value |= (b & 127) << shift
            if b < 128:
                return value
        raise ValueError('oversized synthetic varint')

    while at < len(raw):
        tag = number()
        if tag & 7 not in (0, 2) or tag >> 3 in fields:
            raise ValueError('unsupported synthetic protobuf')
        value = number()
        if tag & 7 == 2:
            end = at + value
            if end > len(raw):
                raise ValueError('truncated synthetic bytes')
            value, at = raw[at:end], end
        fields[tag >> 3] = value
    return fields


def std_string(raw=b''):
    value = c.create_string_buffer(32)
    backing = None
    if len(raw) < 16:
        c.memmove(value, raw, len(raw))
        capacity = 15
    else:
        backing = c.create_string_buffer(raw)
        struct.pack_into('<Q', value, 0, c.addressof(backing))
        capacity = len(raw)
    struct.pack_into('<QQ', value, 16, len(raw), capacity)
    return value, backing


def read_string(address):
    raw = c.string_at(address, 32)
    size, capacity = struct.unpack_from('<QQ', raw, 16)
    if size > 65536 or capacity < size:
        raise ValueError('synthetic std::string shape changed')
    return c.string_at(struct.unpack_from('<Q', raw)[0], size) if capacity >= 16 else raw[:size]


def decrypt(key, aad, sealed):
    return AESGCM(key).decrypt(sealed[-28:-16], sealed[:-28] + sealed[-16:], aad)


def compare(dll, enc_key):
    # Constructor parameters are C++ strings passed by value: its epilogue
    # destroys them. Allocate their backing with this DLL's own allocator.
    assign = c.WINFUNCTYPE(c.c_void_p, c.c_void_p, c.c_void_p,
                          c.c_size_t)(dll._handle + 0x0b0e10)
    def owned_string(raw):
        value = std_string()[0]
        if raw:
            backing = c.create_string_buffer(raw)
            assign(value, backing, len(raw))
        return value
    static_key = owned_string(PUBLIC)
    signature_key = owned_string(b'')
    enc = owned_string(enc_key)
    instance = c.create_string_buffer(0x98)
    output = std_string()[0]
    ctor = c.WINFUNCTYPE(c.c_void_p, c.c_void_p, c.c_uint32,
                        c.c_void_p, c.c_void_p, c.c_void_p)(dll._handle + 0x17b3c0)
    encrypt = c.WINFUNCTYPE(c.c_int, c.c_void_p, c.c_void_p,
                           c.c_size_t, c.c_void_p)(dll._handle + 0x17c460)
    ctor(instance, 415, static_key, signature_key, enc)
    plain = b'\xf0\x0e' + b'synthetic-autoauth-layout-only'
    source = c.create_string_buffer(plain)
    rc = encrypt(instance, source, len(plain), output)
    if rc != 0:
        raise ValueError(f'native synthetic encryption failed: {rc}')
    fields = protobuf(read_string(c.addressof(output)))
    key = protobuf(fields[2])
    if fields[1] != 1 or key[1] != 415:
        raise ValueError('native Hybrid version or curve differs')
    # This is our isolated generated key, never a key from a live process.
    private_der = read_string(c.addressof(instance) + 0x68)
    private = serialization.load_der_private_key(private_der, password=None)
    pub = private.public_key().public_bytes(serialization.Encoding.X962,
                                           serialization.PublicFormat.UncompressedPoint)
    if key[2] != pub:
        raise ValueError('native ephemeral identity mismatch')
    secret = hashlib.sha256(private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), PUBLIC))).digest()
    hash1 = hashlib.sha256(b'1' + b'415' + pub).digest()
    cek = decrypt(secret[:24], hash1, fields[3])
    if len(cek) != 32:
        raise ValueError('native CEK shape differs')
    if enc_key:
        if decrypt(enc_key[:24], hash1, fields[4]) != cek:
            raise ValueError('native session CEK wrap differs')
    elif fields[4]:
        raise ValueError('native empty-key wrap differs')
    okm = HKDF(algorithm=hashes.SHA256(), length=56,
               salt=b'security hdkf expand\0', info=hash1).derive(cek)
    hash2 = hashlib.sha256(b'1' + b'415' + pub + fields[3] + fields[4]).digest()
    if decrypt(okm[:24], hash2, fields[5]) != plain:
        raise ValueError('native encrypted body/AAD/HKDF differs')
    return {'enc_key_length': len(enc_key), 'native_encoder_return': rc,
            'ecdh_cek_verified': True, 'session_cek_verified': True,
            'body_aad_hkdf_verified': True, 'synthetic_inputs_only': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    args = parser.parse_args()
    if os.name != 'nt' or c.sizeof(c.c_void_p) != 8:
        raise SystemExit('Windows x64 required')
    if hashlib.sha256(args.binary.read_bytes()).hexdigest() != EXPECTED:
        raise SystemExit('Sample changed; revalidate crypto ABI first')
    directory = os.add_dll_directory(str(args.binary.resolve().parent))
    dll = c.WinDLL(str(args.binary.resolve()))
    # Process-local objects are discarded on process exit. No native singleton
    # initialization, transport calls, application credentials or config reads.
    results = [compare(dll, key) for key in (b'', bytes(range(24)), bytes(range(32)))]
    print(json.dumps({'sample_sha256': EXPECTED, 'cases': results,
                      'network_requests': 0, 'server_acceptance_verified': False}))


if __name__ == '__main__':
    main()
