#!/usr/bin/env python3
import sqlite3, os, sys, time
from pathlib import Path

DB = Path.home() / ".clutter" / "test.db"
DB.parent.mkdir(exist_ok=True)

def scan(paths):
    conn = sqlite3.connect(str(DB))
    conn.execute("CREATE TABLE IF NOT EXISTS files (path TEXT, name TEXT)")
    for root_path in paths:
        for root, dirs, files in os.walk(root_path):
            for f in files:
                full = os.path.join(root, f)
                conn.execute("INSERT OR REPLACE INTO files VALUES (?, ?)", (full, f))
    conn.commit()
    conn.close()
    print("Indexed successfully")

def find(query):
    conn = sqlite3.connect(str(DB))
    for (path,) in conn.execute("SELECT path FROM files WHERE name LIKE ?", (f"%{query}%",)):
        print(path)
    conn.close()

if __name__ == "__main__":
    if sys.argv[1] == "scan":
        scan(sys.argv[2:])
    elif sys.argv[1] == "find":
        find(sys.argv[2])
