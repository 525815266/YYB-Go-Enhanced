"""Local PCMgr 18.2.30865.218 Windows x64 protocol-lab bridge.

--import-native copies the current user's login into the private PCMgr profile.
--renew-key rotates only that profile's key via native command 20042.
--fetch-ticket uses that profile via command 20138. With no action, only reads
native status. Never writes PCMgr configuration or opens a YYB database.
"""
import argparse
from contextlib import contextmanager
import csv
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time


HASHES = {
    'QMCommon64.dll': '307f596fe65f7012dad5ba83ed8f14cdff426c743a3d38e1993e062d1e189b6a',
    'QMGuid64.dll': '216098312549bce826d59fb2aa696233443745fa51be6fc03688fbfb6c72d858',
    'QMNetworkMgr64.dll': 'e9b54b7f7dba30a0aa65b8da47f96ce7552ce3bf57c23625ca0491ebe89294e4',
}


class ServiceRejected(Exception):
    def __init__(self, code):
        self.code = code


def field(number, value):
    def varint(v):
        out = bytearray()
        while v > 127:
            out.append((v & 127) | 128)
            v >>= 7
        out.append(v)
        return bytes(out)
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    raw = value.encode() if isinstance(value, str) else value
    return varint(number << 3 | 2) + varint(len(raw)) + raw


def decode_response(raw, types):
    if not raw or len(raw) > 2_000_000:
        raise ValueError('invalid response size')
    at, fields = 0, {}

    def number():
        nonlocal at
        value = 0
        for shift in range(0, 70, 7):
            if at >= len(raw):
                raise ValueError('truncated varint')
            b = raw[at]
            at += 1
            if shift == 63 and b > 1:
                raise ValueError('overflow varint')
            value |= (b & 127) << shift
            if b < 128:
                return value
        raise ValueError('oversized varint')

    while at < len(raw):
        tag = number()
        num, wire = tag >> 3, tag & 7
        if not 0 < num < (1 << 29) or wire not in (0, 2) or num in fields or (num in types and types[num] != wire):
            raise ValueError('invalid response field')
        value = number()
        if wire == 2:
            end = at + value
            if end > len(raw):
                raise ValueError('truncated bytes')
            value, at = raw[at:end], end
        fields[num] = value
    if fields.get(1, 0) != 0:
        raise ServiceRejected(fields[1])
    return fields


def renewed_credentials(raw, previous):
    values = decode_response(raw, {1: 0, 2: 0, 3: 0, 4: 2, 5: 0, 6: 2})
    if values.get(2) != 2 or values.get(3) != previous['account_id']:
        raise ValueError('renewal identity mismatch')
    key = values.get(4, b'').decode('ascii')
    interval = values.get(5, 0)
    if not 1 <= len(key) <= 1024 or not 0 < interval <= 0x7fffffff:
        raise ValueError('incomplete renewal')
    return {**previous, 'loginkey': key, 'renewed_at': int(time.time()), 'next_renew_len': interval}


def private_write(path, raw):
    fd, name = tempfile.mkstemp(prefix='.pcmgr-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def protect_directory(private):
    private.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True, check=True)
    sid = next(csv.reader(io.StringIO(result.stdout.decode(errors='replace'))))[1]
    if not sid.startswith('S-1-') or not all(c.isdigit() or c in 'S-' for c in sid):
        raise ValueError('current user SID unavailable')
    subprocess.run(['icacls', str(private), '/inheritance:r', '/grant:r', f'*{sid}:(OI)(CI)F'],
                   capture_output=True, check=True)


@contextmanager
def profile_lock(private):
    import msvcrt
    with (private / '.bridge.lock').open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'\0')
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def read_native(root):
    common = ctypes.WinDLL(str(root / 'QMCommon64.dll'))
    getter = common.QMGetQMLoginInfoFromPCMgr
    getter.argtypes, getter.restype = [ctypes.c_void_p], ctypes.c_int
    info = ctypes.create_string_buffer(0x3604)
    if not getter(info) or struct.unpack_from('<I', info.raw)[0] != 16:
        raise ValueError('requires a current PCMgr WeChat login')
    account = info.raw[4:516].decode('utf-16-le').split('\0', 1)[0]
    key = info.raw[9732:].split(b'\0', 1)[0].decode('ascii')
    if not account.isdecimal() or not 0 < int(account) < (1 << 63) or not 1 <= len(key) <= 1024:
        raise ValueError('invalid native login structure')
    guid_dll = ctypes.WinDLL(str(root / 'QMGuid64.dll'))
    factory = guid_dll.GetGuidReader
    factory.argtypes, factory.restype = [], ctypes.c_void_p
    reader = factory()
    if not reader:
        raise ValueError('GUID reader unavailable')
    vt = ctypes.cast(reader, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    read_guid = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)(vt[4])
    guid = ctypes.create_string_buffer(16)
    if read_guid(reader, guid, 16) != 1 or not any(guid.raw):
        raise ValueError('GUID unavailable')
    creds = {'schema': 1, 'provider': 'pcmgr', 'account_id': int(account), 'loginkey': key,
             'client_guid': guid.raw.hex(), 'imported_at': int(time.time())}
    read_config = common.QMReadConfig
    read_config.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
    read_config.restype = ctypes.c_int
    for name, label in [('defRefreshTimeStamp', 'next_renew_len'), ('defWXLoginTimeStamp', 'native_login_at')]:
        value, size = ctypes.c_uint32(), ctypes.c_uint32()
        if read_config(name, ctypes.byref(value), 4, ctypes.byref(size)) and size.value == 4:
            creds[label] = value.value
    return creds


class NativeTransport:
    def __init__(self, root):
        self.dll = ctypes.WinDLL(str(root / 'QMNetworkMgr64.dll'))
        create = self.dll.CreateQMNetworkMgr
        create.argtypes, create.restype = [], ctypes.c_void_p
        self.obj = create()
        if not self.obj:
            raise ValueError('native network manager unavailable')
        self.destroy = self.dll.DestroyQMNetworkMgr
        self.destroy.argtypes, self.destroy.restype = [ctypes.c_void_p], ctypes.c_int
        self.alive = []

    def close(self):
        self.destroy(self.obj)

    def send(self, command, request):
        if command not in (20042, 20138):
            raise ValueError('command not inspected')
        complete, response = threading.Event(), {}
        callback_type = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                          ctypes.c_uint32, ctypes.c_int, ctypes.c_void_p)

        @callback_type
        def callback(this, seq, data, size, error, context):
            response['native_error'] = error
            if data and 0 < size <= 2_000_000:
                response['raw'] = ctypes.string_at(data, size)
            complete.set()

        cb_vt = (ctypes.c_void_p * 1)(ctypes.cast(callback, ctypes.c_void_p))
        cb = (ctypes.c_void_p * 1)(ctypes.cast(cb_vt, ctypes.c_void_p))
        options = ctypes.create_string_buffer(struct.pack('<6IQ', 1000 if command == 20042 else 5000, 3, 10000, 2, 1, 1, 0))
        vt = ctypes.cast(self.obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        send = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                 ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_uint64, ctypes.c_uint64)(vt[4])
        buf = ctypes.create_string_buffer(request)
        # Keep all callbacks and buffers alive until the manager joins its threads.
        self.alive.extend([callback, cb_vt, cb, options, buf])
        seq = send(self.obj, command, buf, len(request), cb, options, 20042 if command == 20042 else 30003, 0)
        if seq < 0 or not complete.wait(25) or response.get('native_error') != 0 or not response.get('raw'):
            raise ValueError('native transport failed')
        return response['raw']


def validate_credentials(creds):
    if creds.get('schema') != 1 or creds.get('provider') != 'pcmgr' or type(creds.get('account_id')) is not int or not 0 < creds['account_id'] < (1 << 63):
        raise ValueError('private profile identity invalid')
    if not isinstance(creds.get('loginkey'), str) or not 1 <= len(creds['loginkey']) <= 1024 or not creds['loginkey'].isascii():
        raise ValueError('private profile key invalid')
    guid = creds.get('client_guid', '')
    if not isinstance(guid, str) or len(guid) != 32 or len(bytes.fromhex(guid)) != 16 or guid != guid.lower():
        raise ValueError('private profile GUID invalid')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-dir', type=Path, default=Path('C:/Program Files (x86)/Tencent/QQPCMgr/18.2.30865.218'))
    parser.add_argument('--state-root', type=Path, required=True)
    parser.add_argument('--import-native', action='store_true')
    parser.add_argument('--renew-key', action='store_true')
    parser.add_argument('--fetch-ticket', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'win32' or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise ValueError('requires 64-bit Python on Windows')
    root = args.install_dir.resolve(strict=True)
    if not args.state_root.is_absolute() or (args.import_native and args.renew_key):
        raise ValueError('invalid lab arguments')
    for name, expected in HASHES.items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise ValueError('PCMgr binary changed; recheck native ABI')
    private = args.state_root / 'pcmgr'
    protect_directory(private)
    with profile_lock(private):
        run_profile(args, root, private)


def run_profile(args, root, private):
    directories = [os.add_dll_directory(str(root)), os.add_dll_directory(str(root / 'Qt64'))]
    creds_path = private / 'credentials.json'
    if args.import_native or not (args.renew_key or args.fetch_ticket):
        creds = read_native(root)
        validate_credentials(creds)
        if args.import_native:
            if creds_path.exists():
                raise FileExistsError('private PCMgr credentials already exist')
            private_write(creds_path, json.dumps(creds).encode())
        fingerprint = hashlib.sha256(creds['loginkey'].encode()).hexdigest()
        path = private / 'native-baseline.json'
        previous = json.loads(path.read_text(encoding='utf8')) if path.exists() else {}
        status = {'provider': 'pcmgr', 'at': int(time.time()), 'logged_in': True,
                  'renew_interval_seconds': creds.get('next_renew_len'), 'native_login_at': creds.get('native_login_at'),
                  'key_changed_since_previous_read': bool(previous) and previous.get('key_sha256') != fingerprint,
                  'native_login_time_unchanged': bool(previous) and previous.get('native_login_at') == creds.get('native_login_at')}
        private_write(path, json.dumps({**status, 'key_sha256': fingerprint}).encode())
        print(json.dumps(status), flush=True)
    else:
        creds = json.loads(creds_path.read_text(encoding='utf8'))
        validate_credentials(creds)
    if not (args.renew_key or args.fetch_ticket):
        return
    response_path = private / 'ticket-response.pb'
    if args.fetch_ticket:
        response_path.unlink(missing_ok=True)
    transport = NativeTransport(root)
    try:
        if args.renew_key:
            raw = transport.send(20042, field(1, 2) + field(2, creds['account_id']) +
                                 field(3, creds['loginkey']) + field(4, creds['client_guid']))
            updated = renewed_credentials(raw, creds)
            # Persist the rotation before ticket acquisition, even if it fails.
            private_write(creds_path, json.dumps(updated).encode())
            print(json.dumps({'provider': 'pcmgr', 'command': 20042, 'result': 0,
                              'key_changed': updated['loginkey'] != creds['loginkey'],
                              'next_renew_len': updated['next_renew_len'], 'native_configuration_written': False}), flush=True)
            creds = updated
        if args.fetch_ticket:
            raw = transport.send(20138, field(1, creds['account_id']) + field(2, creds['loginkey']) + field(3, creds['client_guid']))
            values = decode_response(raw, {1: 0, 2: 2, 3: 2, 4: 2, 5: 2})
            if not values.get(3):
                raise ValueError('ticket response has no buffer')
            # Strict base64/ticket schema and PCMgr HostAppID checked by Go.
            private_write(response_path, raw)
            print(json.dumps({'provider': 'pcmgr', 'command': 20138, 'result': 0, 'response_saved': True}), flush=True)
    finally:
        transport.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        result = {'provider': 'pcmgr', 'success': False, 'error_type': type(exc).__name__}
        if isinstance(exc, ServiceRejected):
            result['upstream_code'] = exc.code
        print(json.dumps(result), file=sys.stderr)
        sys.exit(1)
