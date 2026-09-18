# Magisk module

This package runs YYB Go directly from Magisk's late-start service stage. It
does not require Termux to remain open.

Starting with `0.1.1`, the ZIP contains Magisk's standard installer entry and
is explicitly marked as a service-only module. Install it directly from the
official Magisk app; do not extract it or install it through a third-party app.

Version `0.1.2` fixes the browser redirect loop when authentication is disabled.
Version `0.1.3` defaults browser authentication to local SQLite and adds an
explicit DNS resolver fallback for Android ROMs that expose an unavailable
`[::1]:53` resolver to static Go programs.
Version `0.1.4` repairs CRLF configuration files created by Windows packaging;
existing `/data/adb/yyb-go/config.conf` files are normalized automatically.
Version `0.2.0` includes the current account keepalive and multi-account
isolation fixes. Temporary network, proxy, and DNS failures remain retryable;
the console asks for a rescan only after a definitive refresh-token rejection.

## Current scope

- Android arm64 only.
- Magisk 20.4 or newer.
- The console listens on `127.0.0.1:8000` by default.
- Protocol accounts, logs, configuration, avatars, and QR files persist under
  `/data/adb/yyb-go` across module upgrades.
- Browser authentication uses local SQLite by default and can optionally use a
  reachable external MySQL server.

Version `0.1.4` installation was validated with the official Magisk installer
on a rooted ARM64 device. Version `0.2.0` keeps the same installer and persistent
data layout while updating the service binary and web resources.

## Build

The build host needs Go 1.23+, Bash, and `zip`.

```sh
VERSION=0.2.0 VERSION_CODE=2000 bash ./scripts/build-magisk.sh arm64
```

The ZIP is written to `dist/` and can be installed from the Magisk app.

If `0.1.0` was installed by a third-party module tool and the Magisk module
list keeps refreshing, remove that installation in the same tool and reboot
first. Then install the current release from the official Magisk app and
reboot again.

## Runtime

After reboot, open the Magisk app and press the module's Action button. It
opens `http://127.0.0.1:8000/` in the default browser.

Configuration is created on first start:

```text
/data/adb/yyb-go/config.conf
```

Edit that file as root, then stop and start the module or reboot. Runtime files
are stored here:

```text
/data/adb/yyb-go/resource/db
/data/adb/yyb-go/resource/avatars
/data/adb/yyb-go/resource/qr
/data/adb/yyb-go/yyb-go.log
```

## Network and authentication

Keep `HOST=127.0.0.1` for normal phone-only use. Android loopback is shared by
apps on the same device, so this prevents LAN access but is not an app-level
security boundary. Setting `HOST=0.0.0.0` also exposes the console and protocol
API to the local network. Do not do that without firewall rules or browser
authentication.

Browser login uses `/data/adb/yyb-go/resource/db/auth.db` by default. If no
initial password is configured, open `/register`; the first registered account
becomes administrator. Existing MySQL deployments remain compatible through
`YYB_AUTH_MYSQL_DSN`, or can use `YYB_AUTH_DRIVER=mysql` with `YYB_AUTH_DSN`.
Set `YYB_AUTH_DRIVER=none` only when intentionally disabling browser login.

The module defaults to `YYB_DNS_SERVERS=223.5.5.5:53,119.29.29.29:53`. This
avoids static Go resolving through Android's unavailable `[::1]:53` stub. The
setting accepts comma-separated IP addresses with optional ports. Remove it to
use the system resolver, or replace it when the current network blocks direct
DNS traffic.

## QingLong and daidai-panel

The phone can call a panel over the LAN when `QL_URL` or `DAIDAI_URL` points to
the panel's real LAN address. For panel scripts to call YYB Go back, loopback
listening is not sufficient. Set the phone's LAN address explicitly:

```sh
HOST=0.0.0.0
QL_URL=http://192.168.1.10:5700
QL_CLIENT_ID=replace-me
QL_CLIENT_SECRET=replace-me
YYB_QINGLONG_SERVER=192.168.1.20:8000
```

For daidai-panel, use `PANEL_TYPE=daidai`, `DAIDAI_URL`, `DAIDAI_APP_KEY`, and
`DAIDAI_APP_SECRET`. The panel and phone must be on the same reachable network,
and `YYB_QINGLONG_SERVER` must be the phone address as seen by the panel. Phone
DHCP addresses can change, so reserve the address in the router. Exposing
`HOST=0.0.0.0` also exposes protocol routes to the LAN; do not expose port 8000
to the public Internet.

## Uninstall behavior

Uninstalling stops the service but intentionally preserves `/data/adb/yyb-go`.
Delete that directory manually only after confirming the stored accounts are no
longer needed.
