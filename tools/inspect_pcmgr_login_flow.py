"""Verify selected PCMgr V3 login control-flow edges in the static MAL sample.

No client configuration, process memory, credentials or network access is used.
This proves caller paths only, not upstream acceptance or renewal lifetime.
"""
import argparse
import hashlib
import json
from pathlib import Path

import capstone
from capstone.x86_const import X86_OP_MEM, X86_REG_RIP
import pefile

EXPECTED = 'ce4b4acd5b7b472819f98eb321eedd44d9a4aa15b81f2ad85e5c50d791bd55bb'
EDGES = [
    ('get_login_params_to_applet_login', 0x1526ad, 0x46050),
    ('applet_login_checks_cached_token', 0x462ac, 0x47a10),
    ('applet_login_uses_cached_token', 0x463ce, 0x47eb0),
    ('merge_auth_fetches_buffer', 0x157941, 0x47420),
    ('require_auto_login_uses_manual_flow', 0x6c6d2, 0x69ba0),
    ('wechat_scheme_opens_url', 0x14b0e2, 0x156d0),
    ('wechat_scheme_success_callback', 0x14b1ca, 0x1554e0),
    ('ams_launch_checks_wechat_login', 0x14e17f, 0x14cc60),
    ('launch_entry_prefers_wechat_scheme', 0x14e96e, 0x14a7f0),
]

# Exact instructions in HandleLaunchAppletByWeChat. These guards all fall
# back when scheme support / PC login is missing or WMPF is already logged in.
GUARDS = [(0x14adf1, 'je'), (0x14adf9, 'je'), (0x14ae02, 'jne')]


def inspect(path):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != EXPECTED:
        raise ValueError('Sample changed; revalidate control-flow offsets')
    pe = pefile.PE(data=raw)
    base = pe.OPTIONAL_HEADER.ImageBase
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    out = []
    for label, caller, target in EDGES:
        instruction = next(md.disasm(pe.get_data(caller, 8), base + caller, count=1))
        if instruction.mnemonic != 'call' or int(instruction.op_str, 16) != base + target:
            raise ValueError('Inspected call edge changed')
        out.append({'edge': label, 'caller_rva': hex(caller), 'target_rva': hex(target), 'verified': True})
    marker = 'kIlinkRequireAutoLogin - directly go to manual login (AutoLogin disabled)'
    if not any(marker.encode(encoding) in raw for encoding in ('ascii', 'utf-16-le')):
        raise ValueError('Expected V3 state annotation missing')
    guards = []
    for at, mnemonic in GUARDS:
        ins = next(md.disasm(pe.get_data(at, 8), base + at, count=1))
        if ins.mnemonic != mnemonic or int(ins.op_str, 16) != base + 0x14b364:
            raise ValueError('WeChat launch fallback guard changed')
        guards.append({'rva': hex(at), 'branch': mnemonic, 'fallback_rva': '0x14b364'})
    skip = next(md.disasm(pe.get_data(0x14e975, 8), base + 0x14e975, count=1))
    if skip.mnemonic != 'jne' or int(skip.op_str, 16) != base + 0x14ea41:
        raise ValueError('Launch entry no longer skips WMPF on scheme success')
    markers = [
        'weixin://dl/business/?appid=',
        'PC WeChat is logged in but WMPF is not logged in, using WeChat scheme to launch',
        'Successfully launched applet %s via WeChat scheme, skip WMPF login check',
        'MinimizeWeChatWindow set wechat Minimize!',
        'MinimizeWeChatWindow set wechat Hide!',
    ]
    for text in markers:
        if text.encode('ascii') not in raw:
            raise ValueError('WeChat launch annotation changed')
    return {'sample_sha256': EXPECTED, 'edges': out, 'auto_login_disabled_annotation': True,
            'wechat_scheme_fallback_guards': guards, 'wechat_launch_annotations': markers,
            'scheme_success_skips_wmpf_check_rva': '0x14e975',
            'reads_client_state': False, 'network_requests': 0, 'server_renewal_proven': False}


def inspect_yyb(path):
    raw = path.read_bytes()
    expected = 'dc32d301e15e7e7ed8e5de5ec03067f1b031f6e7333b28bc3adb3703084d9727'
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('YYB sample changed; revalidate control-flow offsets')
    pe = pefile.PE(data=raw)
    base = pe.OPTIONAL_HEADER.ImageBase
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    imports = {item.address: item.name for library in pe.DIRECTORY_ENTRY_IMPORT for item in library.imports}
    call = next(md.disasm(pe.get_data(0x178114, 8), base + 0x178114, count=1))
    operand = call.operands[0]
    if call.mnemonic != 'call' or operand.type != X86_OP_MEM or operand.mem.base != X86_REG_RIP:
        raise ValueError('Expected indirect scheme launch call')
    if imports.get(call.address + call.size + operand.mem.disp) != b'ShellExecuteW':
        raise ValueError('Scheme launch no longer calls ShellExecuteW')
    check = next(md.disasm(pe.get_data(0x178175, 8), base + 0x178175, count=1))
    if check.mnemonic != 'cmp' or check.op_str != 'esi, 0x20':
        raise ValueError('Expected ShellExecute status check')
    for marker in ('url_scheme', '[LaunchWXAppWithSchema] failed: empty url_scheme'):
        if marker.encode('ascii') not in raw:
            raise ValueError('YYB scheme annotation changed')
    return {'sample_sha256': expected, 'scheme_launcher_rva': '0x178114',
            'import': 'ShellExecuteW', 'status_check_rva': '0x178175',
            'returned_status_is_wx_login_code': False, 'network_requests': 0}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--yyb-binary', type=Path, help='Optional AndrowsStore static sample')
    args = parser.parse_args()
    result = inspect(args.binary)
    if args.yyb_binary:
        result['yyb_scheme'] = inspect_yyb(args.yyb_binary)
    print(json.dumps(result, indent=2))
