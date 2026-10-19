"""Read-only counts from the public copy's database, for a weekly digest (the app has no analytics).

The digest runs this on the server, with the start date as the one argument, and reads the one
line of JSON it prints. Counts only: no names, keys or photos leave the server.
"""

import json
import os
import sqlite3
import sys

since = sys.argv[1]
db = sqlite3.connect(f"file:{os.environ.get('TIDYRACK_DATA', 'data')}/tidyrack.db?mode=ro", uri=True)


def count(sql: str, *args) -> int:
    return db.execute(sql, args).fetchone()[0]


print(json.dumps({
    "People signed up, in all": count("select count(*) from users"),
    "New sign-ups": count("select count(*) from users where added_at >= ?", since),
    "Photos added": count("select count(*) from uploads where added_at >= ?", since),
    "People who added a photo": count("select count(distinct user_id) from uploads where added_at >= ?", since),
}))
