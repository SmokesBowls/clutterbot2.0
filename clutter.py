#!/usr/bin/env python3
"""
Clutter - Zero-cost indexer with copy-on-demand workspace
Usage: ./clutter scan ~/Downloads ~/Projects
       ./clutter find "unity project"
       ./clutter watch ~/Downloads
       ./clutter track ~/Projects/my_project my_project
       ./clutter pull my_project
       ./clutter commit my_project
       ./clutter work my_project
       ./clutter resume
       ./clutter status
       ./clutter doctor
"""
import sqlite3
import os
import sys
import time
import json
import argparse
import subprocess
import shutil
import tempfile
from pathlib import Path
from datetime import datetime
from typing import List, Tuple, Optional
from contextlib import contextmanager

# ============================================================================
# Configuration
# ============================================================================
VERSION = "0.4.0"
BASE_PATH = Path.home() / '.clutter'
DB_PATH = BASE_PATH / 'clutter.db'
RESUME_STATE_PATH = BASE_PATH / 'last_worked'
IGNORE_DIRS = {'.git', '.venv', '__pycache__', 'node_modules', '.idea', '.vscode'}
IGNORE_EXTS = {'.pyc', '.pyo', '.so', '.o', '.a', '.dll', '.exe'}


# ============================================================================
# Core Clutter Class
# ============================================================================
class Clutter:
    def __init__(self, db_path: str = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(exist_ok=True)
        self.monitoring = False
        self.change_log = []
        self.init_db()

    # ------------------------------------------------------------------------
    # Database connection management (clean, leak‑proof)
    # ------------------------------------------------------------------------
    @contextmanager
    def get_conn(self):
        """Yield a fresh database connection and always close it."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys = ON')
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------------
    # Schema initialization
    # ------------------------------------------------------------------------
    def init_db(self):
        """Initialize database schema (idempotent)."""
        with self.get_conn() as conn:
            # Main files table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY,
                    path TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    ext TEXT,
                    size INTEGER,
                    mtime REAL,
                    indexed_at REAL DEFAULT (strftime('%s', 'now'))
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_name ON files(name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ext ON files(ext)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mtime ON files(mtime DESC)")

            # FTS5 full‑text search (optional)
            try:
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS files_fts
                    USING fts5(name, path, content='files', content_rowid='id')
                """)
                conn.execute("""
                    CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files
                    BEGIN
                        INSERT INTO files_fts(rowid, name, path) VALUES (new.id, new.name, new.path);
                    END
                """)
                conn.execute("""
                    CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files
                    BEGIN
                        DELETE FROM files_fts WHERE rowid = old.id;
                    END
                """)
            except sqlite3.OperationalError as e:
                if "fts5" in str(e):
                    pass  # silently ignore, fallback works

            # Symlinks table (manual symlink tracking)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS symlinks (
                    symlink_path TEXT PRIMARY KEY,
                    target_path TEXT NOT NULL,
                    created_at REAL,
                    last_verified REAL
                )
            """)

            # Tracked items table (zero‑copy workspace management)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tracked_items (
                    path TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    status TEXT DEFAULT 'tracked',
                    last_pulled REAL,
                    last_committed REAL,
                    snapshot_path TEXT,
                    created_at REAL DEFAULT (strftime('%s', 'now'))
                )
            """)

            # Changes log table (for watch command)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS changes (
                    id INTEGER PRIMARY KEY,
                    timestamp REAL,
                    change_type TEXT,
                    path TEXT,
                    dest_path TEXT,
                    is_green INTEGER,
                    handled INTEGER DEFAULT 0
                )
            """)
            conn.commit()

    # ------------------------------------------------------------------------
    # Resume state persistence (concierge)
    # ------------------------------------------------------------------------
    def _save_resume_state(self, sandbox_name: str):
        """Remember the last sandbox the user worked in."""
        try:
            RESUME_STATE_PATH.write_text(sandbox_name)
        except Exception:
            pass

    def _load_resume_state(self) -> Optional[str]:
        """Return the name of the last sandbox, if any."""
        if RESUME_STATE_PATH.exists():
            return RESUME_STATE_PATH.read_text().strip()
        return None

    # ------------------------------------------------------------------------
    # Canonical deletion handler (single, testable, non‑blocking)
    # ------------------------------------------------------------------------
    def handle_tracked_deletion(self, path: str) -> bool:
        """
        Handle deletion of a tracked original.
        Returns True if handled, False if not tracked or no sandbox.
        This is synchronous and never runs inside the watchdog event loop.
        """
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name, snapshot_path FROM tracked_items WHERE path = ?",
                (path,)
            )
            row = cursor.fetchone()

        if not row:
            return False  # Not tracked

        name, snapshot_path = row
        sandbox_path = BASE_PATH / 'sandboxes' / name

        has_ghost = any(
            f.name != '.clutter_sandbox'
            for f in sandbox_path.iterdir()
        ) if sandbox_path.exists() else False

        print(f"\n⚠️  TRACKED ITEM DELETED: {path}")
        print(f"   Alias: '{name}'")
        if has_ghost:
            print(f"   Ghost available in: {sandbox_path}\n")
            print(f"   [R] Restore — recover from sandbox")
            print(f"   [G] Keep ghost — decide later")
            print(f"   [D] Delete for real — remove tracking")
            choice = input("   Choice [R/g/d]: ").strip().lower()

            if choice == 'd':
                with self.get_conn() as conn:
                    conn.execute(
                        "UPDATE tracked_items SET status = 'ghost' WHERE path = ?",
                        (path,)
                    )
                    conn.commit()
                print("   Marked as ghost. Use 'clutter untrack' to fully remove.")
                return True
            elif choice == 'g':
                with self.get_conn() as conn:
                    conn.execute(
                        "UPDATE tracked_items SET status = 'ghost' WHERE path = ?",
                        (path,)
                    )
                    conn.commit()
                print("   👻 Ghost preserved. Restore later with 'clutter commit'.")
                return True
            else:  # Restore
                try:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    if os.path.isdir(sandbox_path):
                        shutil.copytree(
                            str(sandbox_path), path,
                            ignore=shutil.ignore_patterns('.clutter_sandbox')
                        )
                    else:
                        shutil.copy2(str(sandbox_path), path)
                    with self.get_conn() as conn:
                        conn.execute(
                            "UPDATE tracked_items SET status = 'tracked' WHERE path = ?",
                            (path,)
                        )
                        conn.commit()
                    print(f"   ✅ Restored to {path}")
                    return True
                except Exception as e:
                    print(f"   ❌ Restore failed: {e}")
                    return False
        else:
            print(f"   ⚠️  No ghost available (never pulled). Cannot recover.")
            with self.get_conn() as conn:
                conn.execute(
                    "UPDATE tracked_items SET status = 'ghost' WHERE path = ?",
                    (path,)
                )
                conn.commit()
            return False

    # ------------------------------------------------------------------------
    # Ignore rules
    # ------------------------------------------------------------------------
    def should_ignore(self, path: Path, name: str) -> bool:
        """Check if file/directory should be ignored."""
        if name.startswith('.'):
            return True
        for part in Path(path).parts:
            if part in IGNORE_DIRS:
                return True
        ext = Path(name).suffix.lower()
        if ext in IGNORE_EXTS:
            return True
        return False

    # ------------------------------------------------------------------------
    # Scan / Index
    # ------------------------------------------------------------------------
    def scan(self, paths: List[str], verbose: bool = False):
        """Index directories."""
        if not paths:
            print("Error: No paths provided")
            return

        total_files = 0
        total_size = 0
        start_time = time.time()

        for root_path in paths:
            root_path = Path(root_path).expanduser().resolve()
            if not root_path.exists():
                print(f"Warning: Path '{root_path}' doesn't exist")
                continue
            print(f"Indexing: {root_path}")

            for root, dirs, files in os.walk(root_path):
                root = Path(root)
                dirs[:] = [d for d in dirs if not self.should_ignore(root / d, d)]
                for file in files:
                    if self.should_ignore(root, file):
                        continue
                    full_path = root / file
                    try:
                        stat = full_path.stat()
                        size = stat.st_size
                        mtime = stat.st_mtime
                        ext = full_path.suffix.lower()
                        with self.get_conn() as conn:
                            conn.execute("""
                                INSERT OR REPLACE INTO files (path, name, ext, size, mtime)
                                VALUES (?, ?, ?, ?, ?)
                            """, (str(full_path), file, ext, size, mtime))
                        total_files += 1
                        total_size += size
                        if verbose and total_files % 1000 == 0:
                            print(f"  Indexed {total_files:,} files...")
                    except (OSError, PermissionError):
                        continue

        elapsed = time.time() - start_time
        size_mb = total_size / (1024 * 1024)
        print(f"\n✓ Indexed {total_files:,} files")
        print(f"  Total size: {size_mb:.1f} MB")
        print(f"  Time: {elapsed:.1f}s")
        print(f"  Database: {self.db_path}")
        print(f"  DB size: {self.db_path.stat().st_size / (1024*1024):.1f} MB")

    # ------------------------------------------------------------------------
    # Find / Search
    # ------------------------------------------------------------------------
    def find(self, query: str, limit: int = 20, use_ai: bool = False):
        """Search for files."""
        if use_ai:
            return self.find_with_ai(query, limit)

        with self.get_conn() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    SELECT f.path, f.name, f.size, f.mtime
                    FROM files f
                    JOIN files_fts ft ON f.id = ft.rowid
                    WHERE files_fts MATCH ?
                    ORDER BY f.mtime DESC
                    LIMIT ?
                """, (f"{query}*", limit))
            except sqlite3.OperationalError:
                cursor.execute("""
                    SELECT path, name, size, mtime
                    FROM files
                    WHERE name LIKE ?
                    ORDER BY mtime DESC
                    LIMIT ?
                """, (f'%{query}%', limit))
            results = cursor.fetchall()

        if not results:
            print(f"No files matching '{query}'")
            return []
        print(f"Found {len(results)} files matching '{query}':\n")
        self._print_results(results)
        return results

    def find_with_ai(self, query: str, limit: int = 20):
        """AI‑enhanced search using Ollama."""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT path, name, size, mtime
                FROM files
                WHERE name LIKE ?
                ORDER BY mtime DESC
                LIMIT 100
            """, (f'%{query}%',))
            candidates = cursor.fetchall()

        if not candidates:
            print(f"No files matching '{query}'")
            return []

        try:
            ranked_indices = self._ask_ollama(query, candidates)
            if ranked_indices:
                results = [candidates[i] for i in ranked_indices if i < len(candidates)]
                results = results[:limit]
            else:
                results = candidates[:limit]
        except Exception as e:
            print(f"AI search failed: {e}, falling back to basic search")
            results = candidates[:limit]

        print(f"AI found {len(results)} files matching '{query}':\n")
        self._print_results(results)
        return results

    def _ask_ollama(self, query: str, candidates: List[Tuple]) -> Optional[List[int]]:
        """Call Ollama to rank candidates."""
        try:
            subprocess.run(["ollama", "--version"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            print("Ollama not installed. Install from https://ollama.com")
            return None

        file_list = "\n".join(
            f"{i+1}. {name}" for i, (_, name, _, _) in enumerate(candidates[:50])
        )
        prompt = f"""Given the query "{query}", rank these files by relevance.
Return ONLY a JSON list of indices (1‑based) in order of relevance.

Files:
{file_list}

JSON:"""

        try:
            result = subprocess.run(
                ["ollama", "run", "llama3.2", prompt],
                capture_output=True, text=True, timeout=30
            )
            import re
            json_match = re.search(r'\[.*\]', result.stdout)
            if json_match:
                indices = json.loads(json_match.group())
                return [i-1 for i in indices if 1 <= i <= len(candidates)][:20]
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            print(f"Ollama error: {e}")
        return None

    def _print_results(self, results: List[Tuple]):
        """Pretty‑print search results."""
        for i, (path, name, size, mtime) in enumerate(results, 1):
            if size < 1024:
                size_str = f"{size} B"
            elif size < 1024 * 1024:
                size_str = f"{size/1024:.1f} KB"
            else:
                size_str = f"{size/(1024*1024):.1f} MB"
            time_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            display_path = path if len(path) <= 80 else "..." + path[-77:]
            print(f"{i:3}. {name}")
            print(f"     {display_path}")
            print(f"     {size_str:>8} • {time_str}\n")

    # ------------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------------
    def stats(self):
        """Show database statistics."""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM files")
            total_files = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(DISTINCT ext) FROM files WHERE ext != ''")
            unique_exts = cursor.fetchone()[0]
            cursor.execute("SELECT SUM(size) FROM files")
            total_size = cursor.fetchone()[0] or 0
            cursor.execute("""
                SELECT ext, COUNT(*) as count
                FROM files
                WHERE ext != ''
                GROUP BY ext
                ORDER BY count DESC
                LIMIT 10
            """)
            common_exts = cursor.fetchall()
            cursor.execute("""
                SELECT strftime('%Y-%m-%d', mtime, 'unixepoch') as day,
                       COUNT(*) as count
                FROM files
                GROUP BY day
                ORDER BY day DESC
                LIMIT 7
            """)
            recent_days = cursor.fetchall()

        print(f"Clutter v{VERSION}")
        print(f"Database: {self.db_path}")
        print(f"Total files indexed: {total_files:,}")
        print(f"Total size: {total_size/(1024**3):.1f} GB")
        print(f"Unique file types: {unique_exts}")

        if common_exts:
            print("\nMost common file types:")
            for ext, count in common_exts:
                percentage = (count / total_files) * 100
                print(f"  {ext or '(no ext)':8} {count:8,} ({percentage:.1f}%)")
        if recent_days:
            print("\nRecent activity:")
            for day, count in recent_days:
                print(f"  {day}: {count:,} files")

    def clear(self):
        """Clear the index."""
        confirm = input("Are you sure you want to clear the index? (y/N): ")
        if confirm.lower() == 'y':
            self.db_path.unlink(missing_ok=True)
            print("Index cleared")
        else:
            print("Cancelled")

    # ------------------------------------------------------------------------
    # Watch / Monitor (with delegated deletion handler)
    # ------------------------------------------------------------------------
    def watch(self, paths, sandbox_path=None):
        """Monitor directories for changes with color‑coded warnings."""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            print("Error: watchdog module required")
            print("Install with: pip install watchdog")
            return

        class ClutterEventHandler(FileSystemEventHandler):
            def __init__(self, outer, sandbox_path):
                self.outer = outer
                self.sandbox_path = sandbox_path

            def _get_color(self, path):
                if self.outer._is_under_symlink(path):
                    return "\033[92m"  # GREEN
                else:
                    return "\033[91m"  # RED

            def _reset_color(self):
                return "\033[0m"

            def on_created(self, event):
                if not event.is_directory:
                    color = self._get_color(event.src_path)
                    reset = self._reset_color()
                    print(f"{color}[+] {event.src_path}{reset}")
                    is_green = self.outer._is_under_symlink(event.src_path) or bool(self.sandbox_path)
                    self.outer._log_change('created', event.src_path, is_green=is_green)

            def on_deleted(self, event):
                path = os.path.abspath(event.src_path)
                color = self._get_color(path)
                reset = self._reset_color()
                print(f"{color}[-] {path}{reset}")

                # --- DELEGATE to the canonical handler ---
                self.outer.handle_tracked_deletion(path)

                is_green = self.outer._is_under_symlink(path) or bool(self.sandbox_path)
                self.outer._log_change('deleted', path, is_green=is_green)

            def on_moved(self, event):
                src = os.path.abspath(event.src_path)
                dest = os.path.abspath(event.dest_path)
                color_src = self._get_color(src)
                color_dest = self._get_color(dest)
                reset = self._reset_color()
                print(f"{color_src}[→] {src}{reset}")
                print(f"{color_dest}    → {dest}{reset}")

                with self.outer.get_conn() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT name FROM tracked_items WHERE path = ?", (src,)
                    )
                    row = cursor.fetchone()
                    if row:
                        name = row[0]
                        print(f"\n⚠️  TRACKED ITEM MOVED: '{name}'")
                        print(f"   From: {src}")
                        print(f"   To:   {dest}")
                        print()
                        print("   [F] Follow — update tracking to new location")
                        print("   [G] Ghost — keep old path, mark as ghost")
                        print("   [C] Cancel — this was an accident (cannot undo move)")
                        choice = input("   Choice [F/g/c]: ").strip().lower()

                        if choice == 'g':
                            conn.execute(
                                "UPDATE tracked_items SET status = 'ghost' WHERE path = ?",
                                (src,)
                            )
                            conn.commit()
                            print("   👻 Marked as ghost at old location")
                        elif choice == 'c':
                            print("   ⚠️  Clutter cannot undo the move.")
                            print("       Move it back manually, then run 'clutter verify'")
                        else:  # Follow
                            conn.execute(
                                "UPDATE tracked_items SET path = ? WHERE path = ?",
                                (dest, src)
                            )
                            ref_path = self.outer.db_path.parent / 'refs' / name
                            if os.path.lexists(str(ref_path)):
                                os.unlink(str(ref_path))
                            os.symlink(dest, str(ref_path), target_is_directory=os.path.isdir(dest))
                            conn.execute(
                                "UPDATE symlinks SET target_path = ? WHERE target_path = ?",
                                (dest, src)
                            )
                            conn.commit()
                            print(f"   ✅ Tracking updated to: {dest}")

                is_green = self.outer._is_under_symlink(src) or bool(self.sandbox_path)
                self.outer._log_change('moved', src, dest_path=dest, is_green=is_green)

            def on_modified(self, event):
                if not event.is_directory:
                    color = self._get_color(event.src_path)
                    reset = self._reset_color()
                    print(f"{color}[~] {event.src_path}{reset}")
                    is_green = self.outer._is_under_symlink(event.src_path) or bool(self.sandbox_path)
                    self.outer._log_change('modified', event.src_path, is_green=is_green)

        observer = Observer()
        event_handler = ClutterEventHandler(self, sandbox_path)
        for path in paths:
            path = os.path.expanduser(path)
            if os.path.exists(path):
                observer.schedule(event_handler, path, recursive=True)
                sandbox_status = "(sandbox)" if sandbox_path else ""
                print(f"👁️  Watching {path} {sandbox_status}")
            else:
                print(f"Warning: Path '{path}' doesn't exist")

        print("\n" + "=" * 60)
        print("🟢 Green = Sandbox (Clutter‑managed)")
        print("🔴 Red = External (outside Clutter control)")
        print("=" * 60 + "\n")
        print("Press Ctrl+C to stop monitoring\n")

        self.monitoring = True
        observer.start()
        try:
            while self.monitoring:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            observer.stop()
            observer.join()
            self.monitoring = False
            print("\n📝 Monitoring stopped")
            if self.change_log:
                print(f"\nRecorded {len(self.change_log)} changes")
                self._save_change_log()

    def _log_change(self, change_type, path, dest_path=None, is_green=0):
        """Log a change to the database."""
        with self.get_conn() as conn:
            conn.execute("""
                INSERT INTO changes (timestamp, change_type, path, dest_path, is_green)
                VALUES (?, ?, ?, ?, ?)
            """, (time.time(), change_type, path, dest_path, 1 if is_green else 0))
            conn.commit()
        self.change_log.append({
            'timestamp': time.time(),
            'type': change_type,
            'path': path,
            'dest': dest_path,
            'is_green': bool(is_green)
        })

    def _save_change_log(self):
        """Save change log to JSON file."""
        log_file = self.db_path.parent / 'change_log.json'
        with open(log_file, 'w') as f:
            json.dump(self.change_log, f, indent=2, default=str)
        print(f"Change log saved to: {log_file}")

    def changes(self, limit=10):
        """Show recent changes."""
        with self.get_conn() as conn:
            try:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT timestamp, change_type, path, dest_path, is_green
                    FROM changes
                    ORDER BY timestamp DESC
                    LIMIT ?
                """, (limit,))
                changes = cursor.fetchall()
            except sqlite3.OperationalError:
                print("No changes recorded yet")
                return

        if not changes:
            print("No changes recorded")
            return

        print(f"Recent changes (last {len(changes)}):\n")
        for ts, change_type, path, dest_path, is_green in changes:
            time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            color = "🟢" if is_green else "🔴"
            symbol = {
                'created': '[+]', 'deleted': '[-]', 'moved': '[→]', 'modified': '[~]'
            }.get(change_type, '[?]')
            print(f"{color} {time_str} {symbol} {path}")
            if dest_path:
                print(f"      → {dest_path}")
            print()

    # ------------------------------------------------------------------------
    # Sandbox management
    # ------------------------------------------------------------------------
    def sandbox(self, name):
        """Create an empty Clutter‑managed sandbox."""
        sandbox_root = BASE_PATH / 'sandboxes'
        sandbox_root.mkdir(exist_ok=True)
        sandbox_path = sandbox_root / name
        if sandbox_path.exists():
            print(f"⚠️  Sandbox '{name}' already exists at {sandbox_path}")
            return sandbox_path
        sandbox_path.mkdir(exist_ok=True)
        print(f"📁 Created empty sandbox: {sandbox_path}")
        meta_file = sandbox_path / '.clutter_sandbox'
        with open(meta_file, 'w') as f:
            json.dump({
                'name': name,
                'created': time.time(),
                'clutter_version': VERSION
            }, f, indent=2)
        return sandbox_path

    def list_sandboxes(self):
        """List all sandboxes."""
        sandbox_root = BASE_PATH / 'sandboxes'
        if not sandbox_root.exists():
            print("No sandboxes created yet")
            return
        print("Available sandboxes:")
        print("-" * 50)
        for sandbox_dir in sorted(sandbox_root.iterdir()):
            if sandbox_dir.is_dir():
                meta_file = sandbox_dir / '.clutter_sandbox'
                if meta_file.exists():
                    try:
                        with open(meta_file, 'r') as f:
                            meta = json.load(f)
                        created = datetime.fromtimestamp(meta['created']).strftime("%Y-%m-%d %H:%M")
                        original = meta.get('original_path', meta.get('source', 'N/A'))
                        print(f"📁 {sandbox_dir.name}")
                        print(f"   Path: {sandbox_dir}")
                        print(f"   Original: {original}")
                        print(f"   Created: {created}")
                    except:
                        print(f"📁 {sandbox_dir.name} (metadata incomplete)")
                else:
                    print(f"📁 {sandbox_dir.name} (no metadata)")
                print()

    # ------------------------------------------------------------------------
    # Symlink tracking
    # ------------------------------------------------------------------------
    def link(self, target, symlink):
        """Create and track a symlink."""
        target = os.path.abspath(os.path.expanduser(target))
        symlink = os.path.abspath(os.path.expanduser(symlink))
        os.makedirs(os.path.dirname(symlink), exist_ok=True)
        if os.path.lexists(symlink):
            os.unlink(symlink)
        target_is_dir = os.path.isdir(target)
        os.symlink(target, symlink, target_is_directory=target_is_dir)
        with self.get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO symlinks (symlink_path, target_path, created_at)
                VALUES (?, ?, ?)
            """, (symlink, target, time.time()))
            conn.commit()
        print(f"✅ Linked {symlink} → {target}")

    def verify(self):
        """Check health of all tracked items + manual symlinks."""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            print("🔍 Verifying tracked items...")
            cursor.execute("SELECT path, name, status FROM tracked_items")
            for path, name, status in cursor.fetchall():
                ref_path = BASE_PATH / 'refs' / name
                exists = os.path.exists(path)
                ref_exists = os.path.lexists(ref_path)
                if not exists:
                    print(f"❌ Ghost: {name} (Original missing at {path})")
                    if status != 'ghost':
                        conn.execute("UPDATE tracked_items SET status = 'ghost' WHERE name = ?", (name,))
                elif not ref_exists:
                    print(f"⚠️  Missing ref: {name} → {path}")
                    if input("   Recreate ref symlink? [Y/n] ").lower() != 'n':
                        os.symlink(path, str(ref_path), target_is_directory=os.path.isdir(path))
                        print("   ✅ Recreated")
                else:
                    print(f"✅ Tracked: {name} → {path}")

            print("\n🔍 Verifying manual symlinks...")
            cursor.execute("SELECT symlink_path, target_path FROM symlinks")
            for symlink, target in cursor.fetchall():
                if not os.path.lexists(symlink):
                    print(f"❌ Missing symlink: {symlink}")
                    if input("   Recreate? [Y/n] ").lower() != 'n':
                        target_is_dir = os.path.isdir(target)
                        os.symlink(target, symlink, target_is_directory=target_is_dir)
                        print(f"   ✅ Recreated")
                elif not os.path.exists(target):
                    print(f"⚠️  Broken symlink: {symlink} → {target} (target missing)")
                else:
                    real_target = os.path.abspath(os.path.join(os.path.dirname(symlink), os.readlink(symlink)))
                    if real_target != target:
                        print(f"⚠️  Mismatch: {symlink} points to {real_target} instead of {target}")
                    else:
                        print(f"✅ {symlink} → {target}")
            conn.commit()

    # ------------------------------------------------------------------------
    # Zero‑copy tracking and workspace commands
    # ------------------------------------------------------------------------
    def track(self, path, name):
        """Register an original path for Clutter awareness. Zero copies."""
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.exists(path):
            print(f"Error: Path '{path}' does not exist")
            return
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT path FROM tracked_items WHERE name = ?", (name,))
            if cursor.fetchone():
                print(f"Error: Name '{name}' already in use")
                return
            conn.execute("""
                INSERT INTO tracked_items (path, name, status)
                VALUES (?, ?, 'tracked')
            """, (path, name))
            conn.commit()

        ref_dir = BASE_PATH / 'refs'
        ref_dir.mkdir(exist_ok=True)
        ref_path = ref_dir / name
        if os.path.lexists(ref_path):
            os.unlink(ref_path)
        os.symlink(path, str(ref_path), target_is_directory=os.path.isdir(path))

        sandbox_path = BASE_PATH / 'sandboxes' / name
        sandbox_path.mkdir(parents=True, exist_ok=True)
        meta_file = sandbox_path / '.clutter_sandbox'
        with open(meta_file, 'w') as f:
            json.dump({
                'name': name,
                'original_path': path,
                'created': time.time(),
                'clutter_version': VERSION
            }, f, indent=2)

        print(f"✅ Now tracking: {path}")
        print(f"   Alias: {name}")
        print(f"   Ref: {ref_path} → {path}")
        print(f"   Run 'clutter pull {name}' when ready to work")

    def pull(self, name_or_path):
        """Create a working copy in the sandbox. Preserve previous as snapshot."""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT path, name, status FROM tracked_items WHERE name = ? OR path = ?",
                (name_or_path, name_or_path)
            )
            row = cursor.fetchone()
        if not row:
            print(f"Error: '{name_or_path}' is not tracked")
            return
        original_path, name, status = row
        sandbox_path = BASE_PATH / 'sandboxes' / name

        # Snapshot existing sandbox if it has content
        has_content = any(
            f.name != '.clutter_sandbox'
            for f in sandbox_path.iterdir()
        ) if sandbox_path.exists() else False
        snapshot_dest = None
        if has_content:
            snapshot_root = BASE_PATH / 'snapshots' / name
            snapshot_root.mkdir(parents=True, exist_ok=True)
            snapshot_dest = snapshot_root / f"pre_pull_{int(time.time())}"
            print(f"📸 Preserving previous sandbox as snapshot...")
            shutil.copytree(str(sandbox_path), str(snapshot_dest))
            for item in sandbox_path.iterdir():
                if item.name == '.clutter_sandbox':
                    continue
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()

        if not os.path.exists(original_path):
            print(f"⚠️  Original missing at {original_path}")
            print(f"   Status: ghost")
            with self.get_conn() as conn:
                conn.execute("UPDATE tracked_items SET status = 'ghost' WHERE name = ?", (name,))
                conn.commit()
            if has_content:
                print(f"   Previous snapshot preserved at: {snapshot_dest}")
                print(f"   Use 'clutter commit {name}' to restore from snapshot")
            return

        print(f"📥 Pulling {original_path} → sandbox/{name}...")
        sandbox_path.mkdir(parents=True, exist_ok=True)
        if os.path.isdir(original_path):
            for item in os.listdir(original_path):
                src = os.path.join(original_path, item)
                dst = sandbox_path / item
                if os.path.isdir(src):
                    shutil.copytree(src, str(dst))
                else:
                    shutil.copy2(src, str(dst))
        else:
            shutil.copy2(original_path, str(sandbox_path / os.path.basename(original_path)))

        with self.get_conn() as conn:
            conn.execute("""
                UPDATE tracked_items
                SET last_pulled = ?, status = 'pulled', snapshot_path = ?
                WHERE name = ?
            """, (time.time(), str(snapshot_dest) if snapshot_dest else None, name))
            conn.commit()

        self._save_resume_state(name)
        print(f"✅ Pull complete")
        print(f"   Working copy: {sandbox_path}")
        if snapshot_dest:
            print(f"   Previous version: {snapshot_dest}")

    def commit(self, name_or_path):
        """Sync sandbox changes back to original with safety snapshots."""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT path, name, status FROM tracked_items WHERE name = ? OR path = ?",
                (name_or_path, name_or_path)
            )
            row = cursor.fetchone()
        if not row:
            print(f"Error: '{name_or_path}' is not tracked")
            return
        original_path, name, status = row
        sandbox_path = BASE_PATH / 'sandboxes' / name

        has_content = any(
            f.name != '.clutter_sandbox'
            for f in sandbox_path.iterdir()
        ) if sandbox_path.exists() else False
        if not has_content:
            print(f"Error: Sandbox '{name}' is empty. Nothing to commit.")
            print(f"   Run 'clutter pull {name}' first.")
            return

        # Snapshot original
        snapshot_dest = None
        if os.path.exists(original_path):
            snapshot_root = BASE_PATH / 'snapshots' / name
            snapshot_root.mkdir(parents=True, exist_ok=True)
            snapshot_dest = snapshot_root / f"pre_commit_{int(time.time())}"
            print(f"📸 Snapshotting original before commit...")
            if os.path.isdir(original_path):
                shutil.copytree(original_path, str(snapshot_dest))
            else:
                snapshot_dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original_path, str(snapshot_dest / os.path.basename(original_path)))
        else:
            print(f"⚠️  Original not found at {original_path}")
            print(f"   Will create it from sandbox copy.")

        # Copy sandbox → original
        print(f"📤 Committing sandbox/{name} → {original_path}...")
        items_to_copy = [f for f in sandbox_path.iterdir() if f.name != '.clutter_sandbox']

        if os.path.isdir(original_path) or not os.path.exists(original_path) or any(item.is_dir() for item in items_to_copy):
            temp_path = str(original_path) + '.clutter_temp'
            if os.path.exists(temp_path):
                shutil.rmtree(temp_path) if os.path.isdir(temp_path) else os.remove(temp_path)
            os.makedirs(temp_path, exist_ok=True)
            for item in items_to_copy:
                dst = os.path.join(temp_path, item.name)
                if item.is_dir():
                    shutil.copytree(str(item), dst)
                else:
                    shutil.copy2(str(item), dst)
            bak_path = str(original_path) + '.clutter_bak'
            if os.path.exists(original_path):
                os.rename(original_path, bak_path)
            os.rename(temp_path, original_path)
            if os.path.exists(bak_path):
                shutil.rmtree(bak_path) if os.path.isdir(bak_path) else os.remove(bak_path)
        else:
            src_file = sandbox_path / os.path.basename(original_path)
            if src_file.exists():
                shutil.copy2(str(src_file), original_path)

        with self.get_conn() as conn:
            conn.execute("""
                UPDATE tracked_items
                SET last_committed = ?, status = 'committed', snapshot_path = ?
                WHERE name = ?
            """, (time.time(), str(snapshot_dest) if snapshot_dest else None, name))
            conn.commit()
        print(f"✅ Commit complete")
        if snapshot_dest:
            print(f"   Previous original saved: {snapshot_dest}")

    # ------------------------------------------------------------------------
    # Concierge commands (Tier 1 UX)
    # ------------------------------------------------------------------------
    def work(self, name_or_path: str = None):
        """
        One‑command entry point: track if needed, pull if needed, print path.
        """
        if name_or_path is None:
            last = self._load_resume_state()
            if last:
                name_or_path = last
            else:
                print("Error: No previous project. Specify a project name or path.")
                return

        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT path, name, status FROM tracked_items WHERE name = ? OR path = ?",
                (name_or_path, name_or_path)
            )
            row = cursor.fetchone()

        if not row:
            # Not tracked yet – try to auto‑track
            path = os.path.abspath(os.path.expanduser(name_or_path))
            if not os.path.exists(path):
                print(f"Error: '{name_or_path}' is not tracked and path doesn't exist.")
                return
            name = Path(path).name.lower().replace(' ', '_')
            with self.get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT path FROM tracked_items WHERE name = ?", (name,))
                if cursor.fetchone():
                    i = 1
                    while True:
                        trial = f"{name}_{i}"
                        cursor.execute("SELECT path FROM tracked_items WHERE name = ?", (trial,))
                        if not cursor.fetchone():
                            name = trial
                            break
                        i += 1
            print(f"📎 Auto‑tracking as '{name}'...")
            self.track(path, name)

        self.pull(name_or_path if 'name' not in locals() else name)
        sandbox_path = BASE_PATH / 'sandboxes' / name
        print(f"\n🚀 Working on: {sandbox_path}")
        print(f"   Original: {path if 'path' in locals() else row[0]}")

    def resume(self):
        """Re‑enter the last sandbox you worked on."""
        last = self._load_resume_state()
        if not last:
            print("Error: No previous session found.")
            print("   Run 'clutter work <project>' first.")
            return
        self.work(last)

    def status(self):
        """Show a summary of all tracked projects."""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT path, name, status, last_pulled, last_committed
                FROM tracked_items
                ORDER BY last_pulled DESC
            """)
            items = cursor.fetchall()
        if not items:
            print("No tracked items.")
            return

        print("Tracked Projects:")
        print("-" * 70)
        for path, name, status, last_pulled, last_committed in items:
            sandbox_path = BASE_PATH / 'sandboxes' / name
            status_icon = {
                'ghost': '👻',
                'pulled': '📥',
                'committed': '✅',
                'tracked': '📌'
            }.get(status, '📌')
            last_pulled_str = datetime.fromtimestamp(last_pulled).strftime("%Y-%m-%d %H:%M") if last_pulled else "never"
            print(f"{status_icon} {name}")
            print(f"   Alias: {name}")
            print(f"   Original: {path}")
            print(f"   Sandbox:  {sandbox_path}")
            print(f"   Status:   {status} | Pulled: {last_pulled_str}")
            print()

    def doctor(self):
        """Check system capabilities and report issues."""
        print("🔍 Clutter System Check")
        print("=" * 50)
        print(f"Python: {sys.version.split()[0]}")
        db_ok = self.db_path.exists()
        print(f"Database: {self.db_path}")
        print(f"  Exists: {'✅' if db_ok else '❌'}")
        if db_ok:
            try:
                with self.get_conn() as conn:
                    cursor = conn.cursor()
                    cursor.execute("PRAGMA integrity_check")
                    integrity = cursor.fetchone()[0]
                    print(f"  Integrity: {integrity}")
                    cursor.execute("SELECT COUNT(*) FROM tracked_items")
                    tracked = cursor.fetchone()[0]
                    cursor.execute("SELECT COUNT(*) FROM files")
                    indexed = cursor.fetchone()[0]
                    print(f"  Tracked items: {tracked}")
                    print(f"  Indexed files: {indexed}")
            except Exception as e:
                print(f"  Error: {e}")

        print("\nDirectories:")
        for subdir in ['sandboxes', 'snapshots', 'refs']:
            d = BASE_PATH / subdir
            exists = d.exists()
            print(f"  {subdir}/: {'✅' if exists else '❌'} ({d})")

        print("\nSymlink Support:")
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                test_target = Path(tmpdir) / 'target'
                test_target.mkdir()
                test_link = Path(tmpdir) / 'link'
                os.symlink(str(test_target), str(test_link), target_is_directory=True)
                print("  Symlinks: ✅ Available")
        except (OSError, NotImplementedError) as e:
            print(f"  Symlinks: ❌ Not available ({e})")

        try:
            import watchdog
            print(f"  Watchdog: ✅ Available (v{watchdog.__version__})")
        except ImportError:
            print("  Watchdog: ❌ Not installed (pip install watchdog)")

        try:
            subprocess.run(["ollama", "--version"], capture_output=True, check=True)
            print("  Ollama: ✅ Available")
        except (subprocess.CalledProcessError, FileNotFoundError):
            print("  Ollama: ❌ Not installed (optional)")

        print("\n" + "=" * 50)
        print("💡 Run 'clutter verify' to fix any issues.")

    # ------------------------------------------------------------------------
    # Symlink detection helpers
    # ------------------------------------------------------------------------
    def _is_under_symlink(self, path):
        """Check if path is a symlink or inside a symlinked directory."""
        path = os.path.abspath(path)
        with self.get_conn() as conn:
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT symlink_path FROM symlinks")
                symlinks = [row[0] for row in cursor.fetchall()]
            except sqlite3.OperationalError:
                symlinks = []
        for s in symlinks:
            if path == s or path.startswith(s + os.sep):
                return True
        return False

    def _is_inside_sandbox(self, path):
        """Check if path is inside any known symlink target (bypass)."""
        path = os.path.abspath(path)
        with self.get_conn() as conn:
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT target_path FROM symlinks")
                targets = [row[0] for row in cursor.fetchall()]
            except sqlite3.OperationalError:
                targets = []
        for t in targets:
            if path == t or path.startswith(t + os.sep):
                return True
        return False


# ============================================================================
# Command Line Interface
# ============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Clutter - Zero‑friction safety concierge for your files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  clutter scan ~/Downloads ~/Projects          # Index directories
  clutter find "unity project"                  # Search indexed files
  clutter track ~/Projects/my_game my_game      # Start tracking a project
  clutter work my_game                          # Track + pull in one command
  clutter resume                                # Resume last worked project
  clutter status                                # View all tracked projects
  clutter watch ~/Projects                     # Monitor for changes
  clutter doctor                               # Check system capabilities
        """
    )
    parser.add_argument('--version', action='version', version=f'Clutter v{VERSION}')

    subparsers = parser.add_subparsers(dest='command', help='Command')

    # Legacy / utility commands
    scan_parser = subparsers.add_parser('scan', help='Index directories')
    scan_parser.add_argument('paths', nargs='+', help='Paths to index')
    scan_parser.add_argument('--verbose', '-v', action='store_true', help='Verbose output')

    find_parser = subparsers.add_parser('find', help='Search for files')
    find_parser.add_argument('query', help='Search query')
    find_parser.add_argument('--limit', '-l', type=int, default=20, help='Max results')
    find_parser.add_argument('--ai', action='store_true', help='Use AI search')

    subparsers.add_parser('stats', help='Show statistics')
    subparsers.add_parser('clear', help='Clear index')

    watch_parser = subparsers.add_parser('watch', help='Monitor directories')
    watch_parser.add_argument('paths', nargs='+', help='Paths to monitor')
    watch_parser.add_argument('--sandbox', '-s', action='store_true',
                              help='Treat as sandbox directory (green)')

    sandbox_parser = subparsers.add_parser('sandbox', help='Create a sandbox')
    sandbox_parser.add_argument('name', help='Sandbox name')

    track_parser = subparsers.add_parser('track', help='Register an item for tracking')
    track_parser.add_argument('path', help='Path to original item')
    track_parser.add_argument('name', help='Sandbox name')

    pull_parser = subparsers.add_parser('pull', help='Copy from original to sandbox')
    pull_parser.add_argument('item', help='Path or sandbox name')

    commit_parser = subparsers.add_parser('commit', help='Sync changes back to original')
    commit_parser.add_argument('item', help='Path or sandbox name')

    changes_parser = subparsers.add_parser('changes', help='Show recent changes')
    changes_parser.add_argument('--limit', '-l', type=int, default=10, help='Number of changes')

    subparsers.add_parser('sandboxes', help='List all sandboxes')

    link_parser = subparsers.add_parser('link', help='Manual symlink tracking')
    link_parser.add_argument('target', help='Target path')
    link_parser.add_argument('symlink', help='Symlink path')

    subparsers.add_parser('verify', help='Verify/heal symlinks')

    # NEW Concierge commands (Tier 1 UX)
    work_parser = subparsers.add_parser('work', help='Track and pull in one command')
    work_parser.add_argument('name_or_path', nargs='?', help='Project name or path')

    subparsers.add_parser('resume', help='Resume last worked project')
    subparsers.add_parser('status', help='Show sandbox vs original summary')
    subparsers.add_parser('doctor', help='Check system capabilities')

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    clutter = Clutter()

    # Dispatch
    if args.command == 'scan':
        clutter.scan(args.paths, args.verbose)
    elif args.command == 'find':
        clutter.find(args.query, args.limit, args.ai)
    elif args.command == 'stats':
        clutter.stats()
    elif args.command == 'clear':
        clutter.clear()
    elif args.command == 'watch':
        sandbox_path = args.paths[0] if args.sandbox and args.paths else None
        clutter.watch(args.paths, sandbox_path)
    elif args.command == 'sandbox':
        clutter.sandbox(args.name)
    elif args.command == 'track':
        clutter.track(args.path, args.name)
    elif args.command == 'pull':
        clutter.pull(args.item)
    elif args.command == 'commit':
        clutter.commit(args.item)
    elif args.command == 'work':
        clutter.work(args.name_or_path)
    elif args.command == 'resume':
        clutter.resume()
    elif args.command == 'status':
        clutter.status()
    elif args.command == 'doctor':
        clutter.doctor()
    elif args.command == 'changes':
        clutter.changes(args.limit)
    elif args.command == 'sandboxes':
        clutter.list_sandboxes()
    elif args.command == 'link':
        clutter.link(args.target, args.symlink)
    elif args.command == 'verify':
        clutter.verify()
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
