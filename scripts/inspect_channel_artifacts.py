"""Read public executable strings/resources; never reads browser profiles."""
import argparse
import re
import struct
import gzip
import zlib
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('files', nargs='+')
p.add_argument('--pattern', default=r'wx[0-9a-f]{16}|https?://[^\s\x00"<>]{4,220}')
p.add_argument('--filter', default='')
p.add_argument('--context', type=int, default=0)
p.add_argument('--pak-out')
p.add_argument('--nsis-header', action='store_true')
a = p.parse_args()
for name in a.files:
    path = Path(name)
    data = path.read_bytes()
    print(f'FILE {path} ({len(data)} bytes)')
    if a.nsis_header:
        offset = data.index(bytes.fromhex('efbeadde4e756c6c736f6674496e7374'))
        size = struct.unpack_from('<I', data, offset+24)[0]
        payload = data[offset+28:offset+28+(size & 0x7fffffff)]
        data = zlib.decompress(payload, -15) if size & 0x80000000 else payload
        print(f'NSIS HEADER: {len(data)} bytes')
    if a.pak_out:
        version = struct.unpack_from('<I', data)[0]
        if version == 5:
            count = struct.unpack_from('<H', data, 8)[0]
            table = 12
        elif version == 4:
            count = struct.unpack_from('<I', data, 4)[0]
            table = 9
        else:
            raise ValueError(f'unsupported pak {version}')
        out = Path(a.pak_out)
        out.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            ident, start = struct.unpack_from('<HI', data, table + i * 6)
            end = struct.unpack_from('<HI', data, table + (i + 1) * 6)[1]
            chunk = data[start:end]
            if chunk.startswith(b'\x1f\x8b'):
                chunk = gzip.decompress(chunk)
            elif chunk.startswith(b'\x1e\x9b'):
                import brotli
                chunk = brotli.decompress(chunk[8:])
            # Chromium marks compressed resources with a short prefix; static
            # plaintext entries still give us the native login page wiring.
            if re.search(a.pattern.encode(), chunk, re.I):
                target = out / f'{ident}.bin'
                target.write_bytes(chunk)
                print(f'RESOURCE {ident}: {len(chunk)} bytes -> {target}')
        continue
    for encoding in ('utf-8', 'utf-16le'):
        value = data.decode(encoding, errors='replace')
        hits = []
        for match in re.finditer(a.pattern, value, re.I):
            s = value[max(0, match.start()-a.context):match.end()+a.context]
            s = re.sub(r'[^\x20-\x7e\u4e00-\u9fff]', ' ', s)
            if a.filter and not re.search(a.filter, s, re.I):
                continue
            if s not in hits:
                hits.append(s)
        print(encoding, *hits[:100], sep='\n')
