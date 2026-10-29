"""Inspect authentication field tables in a verified official ilink2.dll.

Requires pefile. Reads static WXMessageLiteCPP metadata only, never account data,
process memory, configuration files or the network. Does not execute the DLL.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

import pefile


SAMPLE_SHA256 = '8b69772a79e5892ab48aff13a096a1a806219061a17395d14f311305d4a74d7a'
SCHEMAS = {
    'AppBaseRequest': (0x7e5520, 0x9efcf8),
    'ManualAuthRequest': (0x7d91c8, 0x9f03c8),
    'ManualAuthResponse': (0x7d9df0, 0x9f0430),
    'AutoAuthRequest': (0x7d4110, 0x9f04f0),
    'AutoAuthResponse': (0x7d9e60, 0x9f0560),
    'UnifyAuthResponse': (0x7da560, 0x9f01b0),
    'AuthSectResp': (0x7e5600, 0x9effc8),
    'IlinkAppCommSessionInfo': (0x7d4088, 0x9ec4b8),
    'IlinkRequestInfo': (0x7d4f50, 0x9ec810),
    'IlinkLoginRequest': (0x7dae98, 0x9eb318),
}
TYPES = {
    1: 'double', 2: 'float', 3: 'int64', 4: 'uint64', 5: 'int32',
    6: 'fixed64', 7: 'fixed32', 8: 'bool', 9: 'string', 10: 'group',
    11: 'message', 12: 'bytes', 13: 'uint32', 14: 'enum',
    15: 'sfixed32', 16: 'sfixed64', 17: 'sint32', 18: 'sint64',
}


def inspect(pe, vtable, descriptor):
    base = pe.OPTIONAL_HEADER.ImageBase
    count, _, fields = struct.unpack('<IIQ', pe.get_data(descriptor, 16))
    if not 0 < count <= 128 or not base <= fields < base + pe.OPTIONAL_HEADER.SizeOfImage:
        raise ValueError('invalid static field table')
    output = []
    for index in range(count):
        raw = pe.get_data(fields - base + index * 40, 40)
        number, label, kind, packed, indirect, offset, _ = struct.unpack_from('<IBBBBII', raw)
        if number < 1 or kind not in TYPES:
            raise ValueError('invalid static field entry')
        output.append({'index': index, 'number': number, 'type': TYPES[kind],
                       'label': label, 'packed': bool(packed),
                       'indirect_storage': bool(indirect), 'object_offset': hex(offset)})
    return {'vtable_rva': hex(vtable), 'descriptor_rva': hex(descriptor), 'fields': output}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True, type=Path)
    args = parser.parse_args()
    raw = args.binary.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != SAMPLE_SHA256:
        raise SystemExit('Sample changed; revalidate the metadata locations first')
    pe = pefile.PE(data=raw)
    print(json.dumps({'sample_sha256': digest, 'static_metadata_only': True,
                      'messages': {name: inspect(pe, *locations)
                                   for name, locations in SCHEMAS.items()}}, indent=2))


if __name__ == '__main__':
    main()
