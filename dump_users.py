# -*- coding: utf-8 -*-
"""
Diagnostics + repair tool.
- Lists engineers with linked user_id and flags duplicates.
- If --fix is passed, clears every duplicate user_id (keeps the first by id).
"""
import asyncio, aiosqlite, os, sys
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
FIX = "--fix" in sys.argv


async def main():
    print(f"ADMIN_ID from .env: {ADMIN_ID}")
    print(f"Mode: {'FIX (will modify DB)' if FIX else 'DIAGNOSE only'}")
    print()
    async with aiosqlite.connect("duty_bot.db") as db:
        async with db.execute(
            "SELECT id, full_name, telegram_tag, user_id "
            "FROM engineers WHERE user_id IS NOT NULL ORDER BY user_id, id"
        ) as cursor:
            rows = await cursor.fetchall()

        print(f"Engineers with linked user_id: {len(rows)}")
        print(f"{'id':<5} {'full_name':<30} {'tag':<25} {'user_id':<12} note")
        by_uid: dict[int, list[tuple]] = defaultdict(list)
        for r in rows:
            by_uid[r[3]].append(r)

        for r in rows:
            uid = r[3]
            note = []
            if uid == ADMIN_ID:
                note.append("=ADMIN")
            if len(by_uid[uid]) > 1:
                note.append(f"DUPLICATE x{len(by_uid[uid])}")
            print(f"{r[0]:<5} {(r[1] or ''):<30} {(r[2] or ''):<25} {uid:<12} {' '.join(note)}")

        # Find duplicates (multiple engineers sharing the same user_id)
        duplicates = {uid: lst for uid, lst in by_uid.items() if len(lst) > 1}
        if duplicates:
            print()
            print(f"Found {len(duplicates)} duplicate user_id(s) covering {sum(len(v) for v in duplicates.values())} records.")
            if FIX:
                # Strategy: keep none. Clear all, force everyone to /start again.
                print("FIXING: clearing user_id for ALL records that share a user_id with another record.")
                for uid, lst in duplicates.items():
                    ids = [r[0] for r in lst]
                    await db.executemany(
                        "UPDATE engineers SET user_id=NULL WHERE id=?",
                        [(i,) for i in ids],
                    )
                    print(f"  cleared user_id={uid} from engineer ids={ids}")
                await db.commit()
                print("Done. Affected users must run /start again.")
            else:
                print("Run with --fix to clear them.")
        else:
            print()
            print("No duplicates.")


asyncio.run(main())
