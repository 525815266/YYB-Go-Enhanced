#!/usr/bin/env python3
"""Seeds fake accounts for tests/multi-channel-ui.cjs into an isolated service's database.

    python tests/seed-multi-channel-ui.py <resource-root>/db/yyb.db          # add the accounts
    python tests/seed-multi-channel-ui.py <resource-root>/db/yyb.db --reset  # remove them again

Start the service once first so it has migrated the database, and run it without
keepalive (-keepalive-interval 0) so the fake accounts are left alone. The accounts
carry no tokens; they are matched by their openid, never by id.

    Boom   oBoom... pcmgr:boom... oBoomGameAssist...   one WeChat, already one fleet (primary: the appstore one)
    Alice  oAlice... pcmgr:alice... oAliceGameAssist.. one WeChat, three independent accounts
    Carol  oCarol...                                   another WeChat
    Dave   oDave... pcmgr:dave...                      one WeChat without a unionid
"""
import json
import sqlite3
import sys
import time

if len(sys.argv) < 2:
    sys.exit(__doc__)
db = sqlite3.connect(sys.argv[1])
cur = db.cursor()
columns = {row[1] for row in cur.execute("PRAGMA table_info(wechat_accounts)")}
if "fleet_primary_id" not in columns:
    sys.exit("fleet_primary_id is missing: start the service once so it migrates the database")

ACCOUNTS = [
    # openid, nickname, status, unionid, provider
    ("oBoomAppStore000000000000001", "Boom", "alive", "u-boom", ""),
    ("pcmgr:boom00000000000000000002", "Boom", "alive", "u-boom", ""),
    ("oBoomGameAssist00000000000003", "Boom", "expired", "u-boom", "gameassist"),
    ("oAliceAppStore0000000000000004", "Alice", "alive", "u-alice", ""),
    ("pcmgr:alice0000000000000000005", "Alice", "alive", "u-alice", ""),
    ("oAliceGameAssist000000000000006", "Alice", "session_only", "u-alice", "gameassist"),
    ("oCarolAppStore0000000000000007", "Carol", "alive", "u-carol", ""),
    ("oDaveAppStore00000000000000008", "Dave", "alive", "", ""),
    ("pcmgr:dave00000000000000000009", "Dave", "alive", "", ""),
]
openids = [row[0] for row in ACCOUNTS]
marks = ",".join("?" * len(openids))

if "--reset" in sys.argv:
    cur.execute("DELETE FROM wechat_accounts WHERE openid IN (%s)" % marks, openids)
    db.commit()
    print("removed the seeded accounts")
    sys.exit(0)

if cur.execute("SELECT COUNT(*) FROM wechat_accounts WHERE openid IN (%s)" % marks, openids).fetchone()[0]:
    sys.exit("already seeded: run with --reset first")

now = int(time.time())
ids = []
for number, (openid, nick, status, union, provider) in enumerate(ACCOUNTS):
    credentials = {"expires_at": now + 6500, "expires_in": 7200}
    if provider:
        credentials["provider"] = provider
    cur.execute(
        "INSERT INTO wechat_accounts (openid, uin, alias, nickname, remark, status, user_info, credentials, login_buffer,"
        " created_at, updated_at, last_checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (openid, 30000000 + number, "wxid_" + nick.lower(), nick, nick if nick != "Dave" else "", status,
         json.dumps({"unionid": union} if union else {}), json.dumps(credentials), "", now - 3600, now - 60, now - 120),
    )
    ids.append(cur.lastrowid)
# Boom is a fleet already: the appstore account anchors it.
cur.execute("UPDATE wechat_accounts SET fleet_primary_id=? WHERE id IN (?,?,?)", (ids[0], ids[0], ids[1], ids[2]))
db.commit()
print("seeded", dict(zip(openids, ids)))
