"""Verify selected PCMgr V3 login control-flow edges in the static MAL sample.

No client configuration, process memory, credentials or network access is used.
This proves caller paths only, not upstream acceptance or renewal lifetime.
"""
import argparse
import hashlib
import json
from pathlib import Path

import capstone
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
            'reads_client_state': False, 'network_requests': 0, 'server_renewal_proven': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect(args.binary), indent=2))
