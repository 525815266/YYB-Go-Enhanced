#!/usr/bin/env python3
"""Isolated Windows LDPlayer SDK control; never reads the installed profile.

Default: initialize a new private root without login. --ticket-file consumes a
fresh project-issued ticket once. --auto-login resumes only a root created by
this tool after a successful login, with no ticket or manual-login fallback.
Only numeric callback results are emitted; SDK state and logs stay private.
Requires the hash-pinned 64-bit SDK bundled with LDPlayer 14.
"""
import argparse
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path
import threading
import time


HOST_SHA256 = '09784efeba7d487dc04b824a38f2876994e70b7150b996b735a0495074116ee3'
RUNTIME_SHA256 = '46a0ac2eb34ef2e781a68aa4c6bbe5bd5ed1860218c6b4cbdf51ce81d78247c6'
FLUE_SHA256 = '0248de2199ec48db63fe182854e3f58e5d799322558f2e1b5e8fe552b5467615'


def record_once(path, fields):
    with path.open('x', encoding='utf-8') as out:
        json.dump(fields, out)
        out.flush()
        os.fsync(out.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', type=Path, required=True)
    parser.add_argument('--state-root', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--ticket-file', type=Path)
    mode.add_argument('--auto-login', action='store_true')
    parser.add_argument('--hold-seconds', type=int, default=5)
    args = parser.parse_args()
    if os.name != 'nt' or C.sizeof(C.c_void_p) != 8:
        parser.error('requires 64-bit Windows Python')
    if not args.state_root.is_absolute() or not 0 <= args.hold_seconds <= 7200:
        parser.error('requires an absolute private root and hold-seconds 0..7200')
    sdk, root = args.sdk.resolve(), args.state_root.resolve()
    host_path = sdk / 'host/wmpf_host_export_x64.dll'
    runtime_path = sdk / 'runtime/ilink2.dll'
    flue_path = sdk / 'runtime/flue.dll'
    if (hashlib.sha256(host_path.read_bytes()).hexdigest() != HOST_SHA256 or
            hashlib.sha256(runtime_path.read_bytes()).hexdigest() != RUNTIME_SHA256 or
            hashlib.sha256(flue_path.read_bytes()).hexdigest() != FLUE_SHA256):
        parser.error('SDK changed; revalidate ABI before execution')
    if args.auto_login:
        observed = json.loads((root / 'sdk-third-app-result.json').read_text('utf-8'))
        if observed.get('error_code') != 0:
            parser.error('requires a successful isolated SDK baseline')
    else:
        root.mkdir(parents=True, exist_ok=False)
        record_once(root / 'control-owner.json', {'schema': 1, 'provider': 'ldplayer'})
    owner = json.loads((root / 'control-owner.json').read_text('utf-8'))
    if owner != {'schema': 1, 'provider': 'ldplayer'}:
        parser.error('not an isolated SDK control root')
    # This is a dedicated child directory, never LDPlayer/weixinsmallgameData.
    profile = root / 'sdk-profile'
    profile.mkdir(exist_ok=args.auto_login)
    mode_name = 'auto' if args.auto_login else 'third-app'
    if args.ticket_file or args.auto_login:
        record_once(root / ('sdk-' + mode_name + '-intent.json'), {'at': int(time.time())})

    def emit(stage, **fields):
        print(json.dumps(dict(stage=stage, at=int(time.time()), **fields)), flush=True)

    paths = [os.add_dll_directory(str(sdk / name)) for name in ('host', 'runtime')]
    dll = C.WinDLL(str(host_path), winmode=0x1100)
    get = dll.GetBrowsingService
    get.restype, get.argtypes = C.c_void_p, []
    service = get()
    if not service:
        raise RuntimeError('SDK service unavailable')
    table = C.cast(service, C.POINTER(C.POINTER(C.c_void_p))).contents
    query = C.WINFUNCTYPE(C.c_int, C.c_void_p, C.c_char_p, C.POINTER(C.c_void_p))(table[0])
    auth = C.c_void_p()
    if query(service, b'IILinkAuthManager', C.byref(auth)) != 0 or not auth:
        raise RuntimeError('SDK auth interface unavailable')
    initialized, finished, disconnected = threading.Event(), threading.Event(), threading.Event()

    def ready(*unused):
        initialized.set()
        emit('context_initialized')

    def disconnect(*unused):
        disconnected.set()
        emit('service_disconnected')

    def noop(*unused):
        pass

    handler_type = C.WINFUNCTYPE(None, C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p)
    callbacks = [handler_type(noop), handler_type(noop), handler_type(noop),
                 handler_type(ready), handler_type(disconnect), handler_type(noop)]
    handler_vtable = (C.c_void_p * 6)(*[C.cast(cb, C.c_void_p).value for cb in callbacks])
    handler = (C.c_void_p * 1)(C.cast(handler_vtable, C.c_void_p).value)
    options = json.dumps({'product-id': 3000, 'enable-applet-v3': True,
                          'wmpf_root_dir': str(profile)}).encode()
    init = C.WINFUNCTYPE(C.c_int, C.c_void_p, C.c_char_p, C.c_char_p, C.c_void_p)(table[5])
    # The official caller passes the SDK root, not its runtime/ subdirectory.
    ret = init(service, options, (str(sdk) + os.sep).encode(), C.cast(handler, C.c_void_p))
    emit('init_returned', return_code=ret)
    user32, message = C.WinDLL('user32', use_last_error=True), W.MSG()

    def pump_until(event, seconds):
        deadline = time.monotonic() + seconds
        while not event.is_set() and not disconnected.is_set() and time.monotonic() < deadline:
            while user32.PeekMessageW(C.byref(message), None, 0, 0, 1):
                user32.TranslateMessage(C.byref(message))
                user32.DispatchMessageW(C.byref(message))
            event.wait(.02)
        return event.is_set()

    if not pump_until(initialized, 35):
        emit('context_not_ready')
        os._exit(1)
    if not args.ticket_file and not args.auto_login:
        emit('init_only_completed')
        os._exit(0)
    result = {}
    callback_lock = threading.Lock()

    def login_callback(error_type, error_code, data, data_length, userdata):
        # Host callback ABI verified at RVA 0x1eeec0. Never dereference data.
        with callback_lock:
            if finished.is_set():
                return
            result.update(error_type=error_type, error_code=error_code, data_length=data_length)
            try:
                record_once(root / ('sdk-' + mode_name + '-result.json'), result)
                emit('login_callback', mode=mode_name, **result)
                result['persisted'] = True
            except Exception as exc:
                emit('result_save_failed', error_type=type(exc).__name__)
            finally:
                finished.set()

    callback_type = C.WINFUNCTYPE(None, C.c_int, C.c_int, C.c_void_p, C.c_int, C.c_void_p)
    login_cb = callback_type(login_callback)
    methods = C.cast(auth, C.POINTER(C.POINTER(C.c_void_p))).contents
    login = C.WINFUNCTYPE(None, C.c_void_p, C.c_char_p, callback_type, C.c_void_p)(methods[5 if args.auto_login else 4])
    ticket = b''
    if args.ticket_file:
        ticket = args.ticket_file.read_bytes().strip()
        if not ticket or len(ticket) > 100000 or b'\0' in ticket:
            raise ValueError('invalid private ticket')
    login(auth, ticket, login_cb, None)
    emit('login_dispatched', mode=mode_name)
    if not pump_until(finished, 60):
        emit('login_outcome_unknown', mode=mode_name)
        os._exit(1)
    if result.get('error_code') != 0 or not result.get('persisted'):
        os._exit(1)
    pump_until(threading.Event(), args.hold_seconds)
    emit('hold_completed', mode=mode_name, seconds=args.hold_seconds,
         disconnected=disconnected.is_set(), business_code_verified=False)
    # Avoid native destructor races while leaving the isolated SDK-owned state
    # in place. A successful callback alone does not prove restart persistence.
    os._exit(0 if not disconnected.is_set() else 1)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'stage': 'control_failed', 'error_type': type(exc).__name__}), flush=True)
        os._exit(1)
