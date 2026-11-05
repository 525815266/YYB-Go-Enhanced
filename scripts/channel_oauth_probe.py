#!/usr/bin/env python3
"""Inspect QQBrowser's public OAuth entry without logging in or exchanging codes.

Prints a fresh authorization URL by default. --fetch reads the unauthenticated
page and reports only its shape; a QR appearing does NOT prove runtime scope,
desktop fast authorization, login_buffer issuance or mini-program getCode.
Does not read browser profiles, use existing cookies, follow a user callback,
poll QR login, or print QR UUIDs/cookies/codes.
"""
import argparse
import json
import re
import secrets
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', choices=('official', 'runtime-test'), default='official')
    parser.add_argument('--fast-login-test', action='store_true', help='Test page rendering with fast_login=1; not a successful desktop authorization test')
    parser.add_argument('--fetch', action='store_true')
    args = parser.parse_args()
    scope = 'snsapi_login' + (',snsapi_runtime_pcsdk' if args.scope == 'runtime-test' else '')
    query = dict(appid='wx0dbc1ae97f156c38', response_type='code', scope=scope,
                 redirect_uri='https://go.browser.qq.com/wx-login.html', state=secrets.token_urlsafe(18), self_redirect='true')
    if args.fast_login_test:
        query['fast_login'] = '1'
    url = 'https://open.weixin.qq.com/connect/qrconnect?' + urlencode(query)
    out = dict(channel='qqbrowser', requested_scope=scope, experimental_scope=args.scope != 'official',
               fast_login_test=args.fast_login_test, authorize_url=url,
               runtime_ticket_verified=False, desktop_authorization_verified=False)
    if args.fetch:
        try:
            with urlopen(Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=25) as response:
                html = response.read(2_000_000).decode('utf-8', 'replace')
                out.update(http_status=response.status, qr_element_present=bool(re.search(r'/connect/qrcode/[^\s\x22\x27<>]+', html)),
                           page_mentions_fast_login='fast_login' in html,
                           error_page='错误提示' in html or '无效的scope' in html)
        except Exception as exc:
            out['request_error_type'] = type(exc).__name__
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
