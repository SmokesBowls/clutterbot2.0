#!/usr/bin/env python3
"""
Clutter - Zero-cost indexer with copy-on-demand workspace
Usage: ./clutter scan ~/Downloads ~/Projects
       ./clutter find "unity project"
       ./clutter watch ~/Downloads
       ./clutter track ~/Projects/my_project my_project
       ./clutter pull my_project
       ./clutter commit my_project
"""
import sqlite3
import os
import sys
import time
import json
import argparse
import subprocess
import shutil
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager
from typing import List, Tuple, Optional

# Configuration
VERSION = "0.3.0"
DB_PATH = Path.home() / '.clutter' / 'clutter.db'
DB_PATH.parent.mkdir(exist_ok=True)
IGNORE_DIRS = {'.git', '.venv', '__pycache__', 'node_modules', '.idea', '.vscode'}
IGNORE_EXTS = {'.pyc', '.pyo', '.so', '.o', '.a', '.dll', '.exe'}

class Clutter:
    def __init__(self, db_path: str = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.base_dir = self.db_path.parent
        self.db_path.parent.mkdir(exist_ok=True)
        self.conn = None
        self.monitoring = False
        self.change_log = []
        self.init_db()
        self.detect_capabilities()

    @contextmanager
    def get_conn(self):
        """Context manager for guaranteed database connection cleanup."""
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def detect_capabilities(self):
        """Detect database and system capabilities."""
        with self.get_conn() as conn:
            try:
                conn.execute("SELECT fts5_decode(NULL)")
                self.has_fts5 = True
            except sqlite3.OperationalError:
                self.has_fts5 = False
    
    def connect(self):
        """Connect to SQLite database"""
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute('PRAGMA journal_mode=WAL')
        return self.conn
    
    def init_db(self):
        """Initialize database schema"""
        conn = self.connect()
        
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
        
        # Create indexes
        conn.execute("CREATE INDEX IF NOT EXISTS idx_name ON files(name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ext ON files(ext)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mtime ON files(mtime DESC)")
        
        # FTS5 virtual table for full-text search
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS files_fts 
                USING fts5(name, path, content='files', content_rowid='id')
            """)
            
            # Trigger to keep FTS in sync
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
                print("Warning: FTS5 not available, using basic search")
        
        # Symlinks table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS symlinks (
                symlink_path TEXT PRIMARY KEY,
                target_path TEXT NOT NULL,
                created_at REAL,
                last_verified REAL
            )
        """)
        
        # Tracked items table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tracked_items (
                path TEXT PRIMARY KEY,          -- absolute path to original
                name TEXT NOT NULL UNIQUE,      -- sandbox name (user-chosen alias)
                status TEXT DEFAULT 'tracked',  -- tracked | pulled | working | committed | ghost
                last_pulled REAL,               -- timestamp of last pull
                last_committed REAL,            -- timestamp of last commit
                snapshot_path TEXT,             -- path to most recent snapshot
                created_at REAL DEFAULT (strftime('%s', 'now'))
            )
        """)

        # Changes table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS changes (
                id INTEGER PRIMARY KEY,
                timestamp REAL,
                change_type TEXT,     -- created | deleted | moved | modified
                path TEXT,
                dest_path TEXT,       -- only for moves
                is_green INTEGER,     -- 1 = clutter-managed action, 0 = external
                handled INTEGER DEFAULT 0
            )
        """)
        
        conn.commit()
        return conn
    
    def should_ignore(self, path: str, name: str) -> bool:
        """Check if file/directory should be ignored"""
        if name.startswith('.'):
            return True
        
        for part in Path(path).parts:
            if part in IGNORE_DIRS:
                return True
        
        ext = os.path.splitext(name)[1].lower()
        if ext in IGNORE_EXTS:
            return True
        
        return False
    
    def scan(self, paths: List[str], verbose: bool = False):
        """Index directories"""
        if not paths:
            print("Error: No paths provided")
            print("Usage: clutter scan ~/Downloads ~/Projects")
            return
        
        conn = self.init_db()
        cursor = conn.cursor()
        
        total_files = 0
        total_size = 0
        start_time = time.time()
        
        for root_path in paths:
            root_path = os.path.expanduser(root_path)
            if not os.path.exists(root_path):
                print(f"Warning: Path '{root_path}' doesn't exist")
                continue
            
            print(f"Indexing: {root_path}")
            
            for root, dirs, files in os.walk(root_path):
                dirs[:] = [d for d in dirs if not self.should_ignore(root, d)]
                
                for file in files:
                    if self.should_ignore(root, file):
                        continue
                    
                    full_path = os.path.join(root, file)
                    
                    try:
                        stat = os.stat(full_path)
                        size = stat.st_size
                        mtime = stat.st_mtime
                        ext = os.path.splitext(file)[1].lower()
                        
                        cursor.execute("""
                            INSERT OR REPLACE INTO files 
                            (path, name, ext, size, mtime) 
                            VALUES (?, ?, ?, ?, ?)
                        """, (full_path, file, ext, size, mtime))
                        
                        total_files += 1
                        total_size += size
                        
                        if verbose and total_files % 1000 == 0:
                            print(f"  Indexed {total_files:,} files...")
                            
                    except (OSError, PermissionError) as e:
                        if verbose:
                            print(f"  Skipping {file}: {e}")
                        continue
        
        conn.commit()
        conn.close()
        
        elapsed = time.time() - start_time
        size_mb = total_size / (1024 * 1024)
        
        print(f"\n✓ Indexed {total_files:,} files")
        print(f"  Total size: {size_mb:.1f} MB")
        print(f"  Time: {elapsed:.1f}s")
        print(f"  Database: {self.db_path}")
        print(f"  DB size: {self.db_path.stat().st_size / (1024*1024):.1f} MB")
    
    def find(self, query: str, limit: int = 20, use_ai: bool = False):
        """Search for files"""
        if use_ai:
            return self.find_with_ai(query, limit)
        
        conn = self.connect()
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
        conn.close()
        
        if not results:
            print(f"No files matching '{query}'")
            return []
        
        print(f"Found {len(results)} files matching '{query}':\n")
        self._print_results(results)
        return results
    
    def find_with_ai(self, query: str, limit: int = 20):
        """AI-enhanced search using Ollama"""
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT path, name, size, mtime 
            FROM files 
            WHERE name LIKE ? 
            ORDER BY mtime DESC 
            LIMIT 100
        """, (f'%{query}%',))
        
        candidates = cursor.fetchall()
        conn.close()
        
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
        """Ask Ollama to rank search results"""
        try:
            subprocess.run(["ollama", "--version"], 
                         capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            print("Ollama not found or not running")
            print("Install from: https://ollama.com")
            print("Then run: ollama pull llama3.2:3b")
            return None
        
        file_list = "\n".join([f"{i+1}. {name}" for i, (_, name, _, _) in enumerate(candidates[:50])])
        
        prompt = f"""Given the query "{query}", rank these files by relevance.
Return ONLY a JSON list of indices (1-based) in order of relevance.

Files:
{file_list}

JSON:"""
        
        try:
            result = subprocess.run(
                ["ollama", "run", "llama3.2", prompt],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            import re
            json_match = re.search(r'\[.*\]', result.stdout)
            if json_match:
                indices = json.loads(json_match.group())
                indices = [i-1 for i in indices if 1 <= i <= len(candidates)]
                return indices[:20]
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            print(f"Ollama error: {e}")
        
        return None
    
    def _print_results(self, results: List[Tuple]):
        """Print search results in a readable format"""
        for i, (path, name, size, mtime) in enumerate(results, 1):
            if size < 1024:
                size_str = f"{size} B"
            elif size < 1024 * 1024:
                size_str = f"{size/1024:.1f} KB"
            else:
                size_str = f"{size/(1024*1024):.1f} MB"
            
            time_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            
            display_path = path
            if len(path) > 80:
                display_path = "..." + path[-77:]
            
            print(f"{i:3}. {name}")
            print(f"     {display_path}")
            print(f"     {size_str:>8} • {time_str}\n")
    
    def stats(self):
        """Show database statistics"""
        conn = self.connect()
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
        
        conn.close()
        
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
        """Clear the index"""
        confirm = input("Are you sure you want to clear the index? (y/N): ")
        if confirm.lower() == 'y':
            self.db_path.unlink(missing_ok=True)
            print("Index cleared")
        else:
            print("Cancelled")
    
    def watch(self, paths, sandbox_path=None):
        """Monitor directories for changes with color-coded warnings"""
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
                """Determine if change is via symlink (green) or direct target access (red)"""
                if self.outer._is_under_symlink(path):
                    return "\033[92m"  # 🟢 GREEN
                elif self.outer._is_inside_sandbox(path):
                    return "\033[91m"  # 🔴 RED (bypass)
                else:
                    return "\033[91m"  # 🔴 RED (untracked)
            
            def _reset_color(self):
                return "\033[0m"

            def _is_green(self, path):
                """Check if a path change is clutter-managed (green) or external (red)."""
                return self.outer._is_under_symlink(path) or bool(self.sandbox_path)
            
            def on_created(self, event):
                if not event.is_directory:
                    color = self._get_color(event.src_path)
                    reset = self._reset_color()
                    print(f"{color}[+] {event.src_path}{reset}")
                    
                    is_green = self.outer._is_under_symlink(event.src_path) or bool(self.sandbox_path)
                    self.outer._log_change('created', event.src_path, is_green=is_green)
            
            def on_deleted(self, event):
                path = os.path.abspath(event.src_path)
                # Log the change
                self.outer._log_change('deleted', path, is_green=self._is_green(path))
                # Handle recovery (non-blocking in this context – but it's synchronous)
                self.outer.handle_tracked_deletion(path)
            
            def on_moved(self, event):
                src = os.path.abspath(event.src_path)
                dest = os.path.abspath(event.dest_path)
                color_src = self._get_color(src)
                color_dest = self._get_color(dest)
                reset = self._reset_color()
                print(f"{color_src}[→] {src}{reset}")
                print(f"{color_dest}    → {dest}{reset}")

                # Check if source was a tracked original
                conn = self.outer.connect()
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
                    print(f"   [F] Follow — update tracking to new location")
                    print(f"   [G] Ghost — keep old path, mark as ghost")
                    print(f"   [C] Cancel — this was an accident (cannot undo move)")
                    choice = input("   Choice [F/g/c]: ").strip().lower()

                    if choice == 'g':
                        conn.execute(
                            "UPDATE tracked_items SET status = 'ghost' WHERE path = ?",
                            (src,)
                        )
                        conn.commit()
                        print(f"   👻 Marked as ghost at old location")
                    elif choice == 'c':
                        print(f"   ⚠️  Clutter cannot undo the move.")
                        print(f"       Move it back manually, then run 'clutter verify'")
                    else:
                        # Follow the move
                        conn.execute(
                            "UPDATE tracked_items SET path = ? WHERE path = ?",
                            (dest, src)
                        )
                        # Update ref symlink
                        ref_path = self.outer.db_path.parent / 'refs' / name
                        if os.path.lexists(str(ref_path)):
                            os.unlink(str(ref_path))
                        os.symlink(dest, str(ref_path),
                                   target_is_directory=os.path.isdir(dest))

                        # Update symlinks table if present
                        conn.execute(
                            "UPDATE symlinks SET target_path = ? WHERE target_path = ?",
                            (dest, src)
                        )
                        conn.commit()
                        print(f"   ✅ Tracking updated to: {dest}")

                conn.close()
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
        
        print("\n" + "="*60)
        print("🟢 Green = Sandbox (Clutter-managed)")
        print("🔴 Red = External (outside Clutter control)")
        print("="*60 + "\n")
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
        """Log a change to the database"""
        conn = self.connect()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO changes (timestamp, change_type, path, dest_path, is_green)
            VALUES (?, ?, ?, ?, ?)
        """, (time.time(), change_type, path, dest_path, 1 if is_green else 0))
        
        conn.commit()
        conn.close()
        
        self.change_log.append({
            'timestamp': time.time(),
            'type': change_type,
            'path': path,
            'dest': dest_path,
            'is_green': bool(is_green)
        })
    
    def _save_change_log(self):
        """Save change log to JSON file"""
        log_file = self.db_path.parent / 'change_log.json'
        with open(log_file, 'w') as f:
            json.dump(self.change_log, f, indent=2, default=str)
        print(f"Change log saved to: {log_file}")
    
    def changes(self, limit=10):
        """Show recent changes"""
        conn = self.connect()
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                SELECT timestamp, change_type, path, dest_path, is_green
                FROM changes
                ORDER BY timestamp DESC
                LIMIT ?
            """, (limit,))
            
            changes = cursor.fetchall()
            
            if not changes:
                print("No changes recorded")
                return
            
            print(f"Recent changes (last {len(changes)}):\n")
            
            for ts, change_type, path, dest_path, is_green in changes:
                time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                color = "🟢" if is_green else "🔴"
                symbol = {
                    'created': '[+]',
                    'deleted': '[-]',
                    'moved': '[→]',
                    'modified': '[~]'
                }.get(change_type, '[?]')
                
                print(f"{color} {time_str} {symbol} {path}")
                if dest_path:
                    print(f"      → {dest_path}")
                print()
                
        except sqlite3.OperationalError:
            print("No changes recorded yet")
        
        conn.close()
    
    def sandbox(self, name):
        """Create a Clutter-managed sandbox"""
        sandbox_root = self.db_path.parent / 'sandboxes'
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
        """List all sandboxes"""
        sandbox_root = self.db_path.parent / 'sandboxes'
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

    def link(self, target, symlink):
        """Create and track a symlink"""
        target = os.path.abspath(os.path.expanduser(target))
        symlink = os.path.abspath(os.path.expanduser(symlink))
        
        # Create parent directories if needed
        os.makedirs(os.path.dirname(symlink), exist_ok=True)
        
        # Create the symlink (force overwrite)
        if os.path.lexists(symlink):
            os.unlink(symlink)
        
        target_is_dir = os.path.isdir(target)
        os.symlink(target, symlink, target_is_directory=target_is_dir)
        
        # Store in DB
        conn = self.connect()
        conn.execute("""
            INSERT OR REPLACE INTO symlinks (symlink_path, target_path, created_at)
            VALUES (?, ?, ?)
        """, (symlink, target, time.time()))
        conn.commit()
        conn.close()
        
        print(f"✅ Linked {symlink} → {target}")

    def verify(self):
        """Check health of all tracked items + manual symlinks."""
        conn = self.connect()
        cursor = conn.cursor()
        
        print("🔍 Verifying tracked items...")
        cursor.execute("SELECT path, name, status FROM tracked_items")
        for path, name, status in cursor.fetchall():
            ref_path = self.db_path.parent / 'refs' / name
            exists = os.path.exists(path)
            ref_exists = os.path.lexists(ref_path)
            
            if not exists:
                print(f"❌ Ghost: {name} (Original missing at {path})")
                if status != 'ghost':
                    conn.execute("UPDATE tracked_items SET status = 'ghost' WHERE name = ?", (name,))
            elif not ref_exists:
                print(f"⚠️  Missing ref: {name} → {path}")
                if input(f"   Recreate ref symlink? [Y/n] ").lower() != 'n':
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
                    print(f"⚠️  Mismatch: {symlink} pts to {real_target} instead of {target}")
                else:
                    print(f"✅ {symlink} → {target}")
        
        conn.commit()
        conn.close()

    def track(self, path, name):
        """Register an original path for Clutter awareness. Zero copies."""
        path = os.path.abspath(os.path.expanduser(path))

        # VALIDATE: path must exist
        if not os.path.exists(path):
            print(f"Error: Path '{path}' does not exist")
            return

        # VALIDATE: name must not already be in use
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT path FROM tracked_items WHERE name = ?", (name,))
            existing = cursor.fetchone()
            if existing:
                print(f"Error: Name '{name}' already tracks {existing[0]}")
                return

            # 1. Insert into tracked_items
            conn.execute("""
                INSERT OR REPLACE INTO tracked_items (path, name, status)
                VALUES (?, ?, 'tracked')
            """, (path, name))
            conn.commit()

        # 2. Create ref symlink (lightweight pointer)
        ref_dir = self.base_dir / 'refs'
        ref_dir.mkdir(exist_ok=True)
        ref_path = ref_dir / name
        if os.path.lexists(ref_path):
            os.unlink(ref_path)
        os.symlink(path, str(ref_path), target_is_directory=os.path.isdir(path))

        # 3. Create empty sandbox dir (placeholder, no content)
        sandbox_path = self.base_dir / 'sandboxes' / name
        sandbox_path.mkdir(parents=True, exist_ok=True)

        # 4. Write minimal metadata
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
        # Resolve the tracked item
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
        sandbox_path = self.base_dir / 'sandboxes' / name

        # STEP 1: Snapshot existing sandbox if it has content
        has_content = any(
            f.name != '.clutter_sandbox'
            for f in sandbox_path.iterdir()
        ) if sandbox_path.exists() else False

        snapshot_dest = None
        if has_content:
            snapshot_root = self.base_dir / 'snapshots' / name
            snapshot_root.mkdir(parents=True, exist_ok=True)
            snapshot_dest = snapshot_root / f"pre_pull_{int(time.time())}"
            print(f"📸 Preserving previous sandbox as snapshot...")
            shutil.copytree(str(sandbox_path), str(snapshot_dest))
            # Now clear sandbox (except metadata)
            for item in sandbox_path.iterdir():
                if item.name == '.clutter_sandbox':
                    continue
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()

        # STEP 2: Check original exists
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

        # STEP 3: Copy original into sandbox
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

        # STEP 4: Update database
        with self.get_conn() as conn:
            conn.execute("""
                UPDATE tracked_items
                SET last_pulled = ?, status = 'pulled', snapshot_path = ?
                WHERE name = ?
            """, (time.time(), str(snapshot_dest) if snapshot_dest else None, name))
            conn.commit()

        print(f"✅ Pull complete")
        print(f"   Working copy: {sandbox_path}")
        if snapshot_dest:
            print(f"   Previous version: {snapshot_dest}")

    def commit(self, name_or_path):
        """Sync sandbox changes back to original with safety snapshots."""
        # Resolve the tracked item
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
        sandbox_path = self.base_dir / 'sandboxes' / name

        # CHECK: sandbox must have content
        has_content = any(
            f.name != '.clutter_sandbox'
            for f in sandbox_path.iterdir()
        ) if sandbox_path.exists() else False

        if not has_content:
            print(f"Error: Sandbox '{name}' is empty. Nothing to commit.")
            print(f"   Run 'clutter pull {name}' first.")
            return

        # STEP 1: Snapshot ORIGINAL
        snapshot_dest = None
        if os.path.exists(original_path):
            snapshot_root = self.base_dir / 'snapshots' / name
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

        # STEP 2: Copy sandbox → original (safe swap)
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
            # Single file commit (assuming sandbox has one file matching original name)
            src_file = sandbox_path / os.path.basename(original_path)
            if src_file.exists():
                shutil.copy2(str(src_file), original_path)

        # STEP 3: Update DB
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

    def handle_tracked_deletion(self, path):
        """Handle deletion of a tracked original — interactive ghost recovery."""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name, snapshot_path FROM tracked_items WHERE path = ?",
                (path,)
            )
            row = cursor.fetchone()

            if not row:
                return  # Not a tracked item, nothing to do

            name, snapshot_path = row
            sandbox_path = self.base_dir / 'sandboxes' / name

            # Check if sandbox has a working copy (ghost candidate)
            has_ghost = any(
                f.name != '.clutter_sandbox'
                for f in sandbox_path.iterdir()
            ) if sandbox_path.exists() else False

            print(f"\n⚠️  TRACKED ITEM DELETED: {path}")
            print(f"   This item is managed by Clutter as '{name}'")

            if has_ghost:
                print(f"   Ghost copy available in sandbox: {sandbox_path}")
                print()
                print(f"   [R] Restore — copy ghost back to original location")
                print(f"   [G] Keep ghost — mark as ghost, decide later")
                print(f"   [D] Delete for real — remove tracking and ghost")
                choice = input("   Choice [R/g/d]: ").strip().lower()

                if choice == 'd':
                    conn.execute(
                        "UPDATE tracked_items SET status = 'ghost' WHERE path = ?",
                        (path,)
                    )
                    conn.commit()
                    print(f"   Marked as ghost. Run 'clutter untrack {name}' to fully remove.")
                elif choice == 'g':
                    conn.execute(
                        "UPDATE tracked_items SET status = 'ghost' WHERE path = ?",
                        (path,)
                    )
                    conn.commit()
                    print(f"   👻 Ghost preserved. Restore later with 'clutter commit {name}'")
                else:
                    # Restore from sandbox
                    if sandbox_path.is_dir():
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        shutil.copytree(str(sandbox_path), path,
                                       ignore=shutil.ignore_patterns('.clutter_sandbox'))
                    else:
                        src_file = sandbox_path / os.path.basename(path)
                        if src_file.exists():
                            shutil.copy2(str(src_file), path)

                    print(f"   ✅ Restored to {path}")
                    conn.execute(
                        "UPDATE tracked_items SET status = 'tracked' WHERE path = ?",
                        (path,)
                    )
                    conn.commit()
            else:
                print(f"   ⚠️  No ghost available (never pulled)")
                print(f"   Cannot recover. Remove tracking with 'clutter untrack {name}'")
                conn.execute(
                    "UPDATE tracked_items SET status = 'ghost' WHERE path = ?",
                    (path,)
                )
                conn.commit()

    def _is_under_symlink(self, path):
        """Check if path is a symlink or inside a symlinked directory"""
        path = os.path.abspath(path)
        conn = self.connect()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT symlink_path FROM symlinks")
            symlinks = [row[0] for row in cursor.fetchall()]
        except sqlite3.OperationalError:
            symlinks = []
        conn.close()
        
        for s in symlinks:
            if path == s or path.startswith(s + os.sep):
                return True
        return False

    def _is_inside_sandbox(self, path):
        """Check if path is inside any known symlink target (bypass)"""
        path = os.path.abspath(path)
        conn = self.connect()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT target_path FROM symlinks")
            targets = [row[0] for row in cursor.fetchall()]
        except sqlite3.OperationalError:
            targets = []
        conn.close()
        
        for t in targets:
            if path == t or path.startswith(t + os.sep):
                return True
        return False

def main():
    parser = argparse.ArgumentParser(description="Clutter - Zero-install file indexer")
    parser.add_argument('--version', action='store_true', help='Show version')
    
    subparsers = parser.add_subparsers(dest='command', help='Command', required=False)
    
    # Scan command
    scan_parser = subparsers.add_parser('scan', help='Index directories')
    scan_parser.add_argument('paths', nargs='+', help='Paths to index')
    scan_parser.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    
    # Find command
    find_parser = subparsers.add_parser('find', help='Search for files')
    find_parser.add_argument('query', help='Search query')
    find_parser.add_argument('--limit', '-l', type=int, default=20, help='Max results')
    find_parser.add_argument('--ai', action='store_true', help='Use AI search')
    
    # Stats command
    subparsers.add_parser('stats', help='Show statistics')
    
    # Clear command
    subparsers.add_parser('clear', help='Clear index')
    
    # Watch command
    watch_parser = subparsers.add_parser('watch', help='Monitor directories')
    watch_parser.add_argument('paths', nargs='+', help='Paths to monitor')
    watch_parser.add_argument('--sandbox', '-s', action='store_true', 
                            help='Treat as sandbox directory (green)')
    
    # Sandbox command
    sandbox_parser = subparsers.add_parser('sandbox', help='Create a sandbox')
    sandbox_parser.add_argument('name', help='Sandbox name')
    
    # Track command
    track_parser = subparsers.add_parser('track', help='Register an item for tracking')
    track_parser.add_argument('path', help='Path to original item')
    track_parser.add_argument('name', help='Sandbox name')
    
    # Pull command
    pull_parser = subparsers.add_parser('pull', help='Copy from original to sandbox')
    pull_parser.add_argument('item', help='Path or sandbox name')
    
    # Commit command
    commit_parser = subparsers.add_parser('commit', help='Sync changes back to original')
    commit_parser.add_argument('item', help='Path or sandbox name')
    
    # Changes command
    changes_parser = subparsers.add_parser('changes', help='Show recent changes')
    changes_parser.add_argument('--limit', '-l', type=int, default=10, help='Number of changes')
    
    # List sandboxes command
    subparsers.add_parser('sandboxes', help='List all sandboxes')
    
    # Link command
    link_parser = subparsers.add_parser('link', help='Manual symlink tracking')
    link_parser.add_argument('target', help='Target path')
    link_parser.add_argument('symlink', help='Symlink path')
    
    # Verify command
    subparsers.add_parser('verify', help='Verify/heal symlinks')
    
    args = parser.parse_args()
    
    if not args.command:
        if args.version:
            print(f"Clutter v{VERSION}")
            return
        else:
            parser.print_help()
            return
    
    clutter = Clutter()
    
    if args.command == 'scan':
        clutter.scan(args.paths, args.verbose)
    elif args.command == 'find':
        clutter.find(args.query, args.limit, args.ai)
    elif args.command == 'stats':
        clutter.stats()
    elif args.command == 'clear':
        clutter.clear()
    elif args.command == 'watch':
        sandbox_path = None
        if args.sandbox and args.paths:
            sandbox_path = args.paths[0]
        clutter.watch(args.paths, sandbox_path)
    elif args.command == 'sandbox':
        clutter.sandbox(args.name)
    elif args.command == 'track':
        clutter.track(args.path, args.name)
    elif args.command == 'pull':
        clutter.pull(args.item)
    elif args.command == 'commit':
        clutter.commit(args.item)
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