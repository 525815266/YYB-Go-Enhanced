#!/usr/bin/env python3
"""Fresh LDPlayer OAuth lab. Never reads client profiles or production databases.

Requires --authorize and a new absolute private directory. Saves a QR image,
polls for at most five minutes, exchanges the confirmed code once, and saves the
private ticket. Does not print codes, tickets, identities or upstream errors.
The separate protocolaudit Go harness validates the ticket and calls wx.login.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import secrets
import time

import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', required=True, type=Path)
    parser.add_argument('--authorize', action='store_true', required=True)
    args = parser.parse_args()
    root = args.state_root
    if not root.is_absolute():
        parser.error('--state-root must be absolute')
    root.mkdir(mode=0o700, parents=True, exist_ok=False)

    def stage(name, **fields):
        result = dict(provider='ldplayer', stage=name, **fields)
        (root / 'status.json').write_text(json.dumps(result), encoding='utf-8')
        print(json.dumps(result), flush=True)

    try:
        with requests.Session() as session:
            session.headers['User-Agent'] = 'Mozilla/5.0'
            response = session.get('https://open.weixin.qq.com/connect/qrconnect', params={
                'appid': 'wx56135c126042c9e7',
                'redirect_uri': 'https://www.ldmnq.com/ldy/pcbox/tencent-login.html',
                'scope': 'snsapi_login,snsapi_runtime_pcsdk',
                'response_type': 'code', 'state': secrets.token_hex(16), 'self_redirect': 'true',
            }, timeout=25)
            response.raise_for_status()
            found = re.search(r'/connect/qrcode/([^"\s<>\x27]+)', response.text)
            if not found:
                stage('qr_missing')
                return 1
            uuid = found[1]
            image = session.get('https://open.weixin.qq.com/connect/qrcode/' + uuid, timeout=20)
            image.raise_for_status()
            if not image.headers.get('Content-Type', '').startswith('image/'):
                stage('invalid_qr_image')
                return 1
            (root / 'qrcode.png').write_bytes(image.content)
            stage('waiting_for_scan')
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                try:
                    poll = session.get('https://long.open.weixin.qq.com/connect/l/qrconnect',
                                       params={'uuid': uuid, '_': int(time.time()*1000)}, timeout=40)
                except requests.Timeout:
                    continue
                poll.raise_for_status()
                status = re.search(r'wx_errcode\s*=\s*(\d+)', poll.text)
                code = re.search(r"wx_code\s*=\s*'([^']*)'", poll.text)
                if status and status[1] == '405' and code and code[1]:
                    # Intent prevents accidental replay after a lost exchange response.
                    with (root / 'exchange-intent.json').open('x', encoding='utf-8') as out:
                        json.dump({'at': int(time.time())}, out)
                        out.flush()
                        os.fsync(out.fileno())
                    stage('exchanging')
                    result = session.post('https://game-service.ldmnq.com/mini-game/wechat/login',
                                          params={'code': code[1]}, timeout=25)
                    if result.status_code != 200:
                        stage('exchange_failed', http_status=result.status_code)
                        return 1
                    reply = result.json()
                    if type(reply.get('code')) is not int or reply['code'] != 200:
                        stage('exchange_rejected', business_code=reply.get('code') if type(reply.get('code')) is int else None)
                        return 1
                    ticket = reply.get('data')
                    if not isinstance(ticket, str) or not 1 <= len(ticket) <= 100000:
                        stage('invalid_ticket_type')
                        return 1
                    try:
                        raw = base64.b64decode(ticket, validate=True)
                    except ValueError:
                        stage('invalid_ticket_encoding')
                        return 1
                    (root / 'ticket.private.txt').write_text(ticket, encoding='ascii')
                    stage('ticket_received', ticket_bytes=len(raw), business_code=200)
                    return 0
                if status and status[1] in ('403', '400', '500'):
                    stage('scan_ended', qr_status=int(status[1]))
                    return 1
                time.sleep(1)
            stage('scan_timeout')
            return 1
    except Exception as exc:
        stage('failed', error_type=type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
