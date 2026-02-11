# ClutterBot v0.3.0 — Testing Guide

## Prerequisites

```bash
# You need Python 3.7+ and watchdog for the watch command
pip install watchdog

# Optional: Ollama for AI search (not required for core tests)
# ollama pull llama3.2
```

---

## Test 1: Clean Start

Wipe any previous clutter data so you're starting fresh.

```bash
rm -rf ~/.clutter
python clutter.py --version
# Expected: Clutter v0.3.0
```

---

## Test 2: Scan & Find

Create some test files, index them, search them.

```bash
# Create test project structure
mkdir -p /tmp/clutter_test/project_alpha/src
mkdir -p /tmp/clutter_test/project_alpha/assets
mkdir -p /tmp/clutter_test/project_beta
echo "print('hello')" > /tmp/clutter_test/project_alpha/src/main.py
echo "readme content" > /tmp/clutter_test/project_alpha/README.md
echo "sprite data" > /tmp/clutter_test/project_alpha/assets/player.png
echo "beta code" > /tmp/clutter_test/project_beta/app.js
echo '{"name": "beta"}' > /tmp/clutter_test/project_beta/package.json

# Scan
python clutter.py scan /tmp/clutter_test
# Expected: ✓ Indexed 5 files (counts may vary)

# Find
python clutter.py find "main"
# Expected: shows main.py with path, size, date

python clutter.py find "alpha"
# Expected: shows files under project_alpha

# Stats
python clutter.py stats
# Expected: file count, size, common extensions
```

---

## Test 3: Track (Zero-Copy)

Track a project. Verify NOTHING gets copied.

```bash
# Check disk usage before
du -sh /tmp/clutter_test/project_alpha
du -sh ~/.clutter

# Track it
python clutter.py track /tmp/clutter_test/project_alpha alpha
# Expected:
#   ✅ Now tracking: /tmp/clutter_test/project_alpha
#   Alias: alpha
#   Ref: ~/.clutter/refs/alpha → /tmp/clutter_test/project_alpha
#   Run 'clutter pull alpha' when ready to work

# Verify zero-copy: sandbox should only have metadata file
ls -la ~/.clutter/sandboxes/alpha/
# Expected: ONLY .clutter_sandbox (a few bytes)

# Verify ref symlink exists and points correctly
ls -la ~/.clutter/refs/alpha
# Expected: alpha -> /tmp/clutter_test/project_alpha

# Verify disk usage barely changed
du -sh ~/.clutter
# Expected: tiny (just the database + metadata)

# Verify duplicate name is rejected
python clutter.py track /tmp/clutter_test/project_beta alpha
# Expected: Error: Name 'alpha' already tracks ...
```

---

## Test 4: Pull (Copy-on-Demand)

Pull creates the working copy. Now files get duplicated.

```bash
python clutter.py pull alpha
# Expected:
#   📥 Pulling /tmp/clutter_test/project_alpha → sandbox/alpha...
#   ✅ Pull complete
#   Working copy: ~/.clutter/sandboxes/alpha

# Verify sandbox now has actual content
ls -la ~/.clutter/sandboxes/alpha/
# Expected: src/, assets/, README.md, .clutter_sandbox

# Verify original is untouched
ls -la /tmp/clutter_test/project_alpha/
# Expected: same files as before, not moved

# Verify sandbox is a COPY not a symlink
file ~/.clutter/sandboxes/alpha/README.md
# Expected: regular file, NOT a symlink
```

---

## Test 5: Edit in Sandbox & Commit

Make changes in the sandbox, then commit back to original.

```bash
# Make changes in sandbox
echo "new feature code" >> ~/.clutter/sandboxes/alpha/src/main.py
echo "changelog" > ~/.clutter/sandboxes/alpha/CHANGELOG.md

# Verify original is still unchanged
cat /tmp/clutter_test/project_alpha/src/main.py
# Expected: just "print('hello')" — no "new feature code"

# Commit
python clutter.py commit alpha
# Expected:
#   📸 Snapshotting original before commit...
#   📤 Committing sandbox/alpha → /tmp/clutter_test/project_alpha...
#   ✅ Commit complete
#   Previous original saved: ~/.clutter/snapshots/alpha/pre_commit_...

# Verify changes are now in the original
cat /tmp/clutter_test/project_alpha/src/main.py
# Expected: "print('hello')" AND "new feature code"

ls /tmp/clutter_test/project_alpha/
# Expected: now includes CHANGELOG.md

# Verify snapshot exists (safety net of pre-commit original)
ls ~/.clutter/snapshots/alpha/
# Expected: pre_commit_<timestamp>/ directory
```

---

## Test 6: Pull Again (Snapshot Preservation)

Pull again — the previous sandbox content should be snapshotted first.

```bash
# Add something to sandbox so it has divergent content
echo "sandbox only" > ~/.clutter/sandboxes/alpha/scratch.txt

python clutter.py pull alpha
# Expected:
#   📸 Preserving previous sandbox as snapshot...
#   📥 Pulling /tmp/clutter_test/project_alpha → sandbox/alpha...
#   ✅ Pull complete
#   Previous version: ~/.clutter/snapshots/alpha/pre_pull_...

# Verify snapshot of previous sandbox was saved
ls ~/.clutter/snapshots/alpha/
# Expected: both pre_commit_... AND pre_pull_... directories

# Verify sandbox now matches current original (fresh pull)
cat ~/.clutter/sandboxes/alpha/src/main.py
# Expected: matches the committed version from Test 5

# Verify scratch.txt is gone from sandbox (it was in the old sandbox, now snapshotted)
ls ~/.clutter/sandboxes/alpha/scratch.txt 2>/dev/null
# Expected: No such file (it's in the snapshot, not active sandbox)
```

---

## Test 7: Verify Command

Check health of everything.

```bash
python clutter.py verify
# Expected:
#   🔍 Verifying tracked items...
#   ✅ Tracked: alpha → /tmp/clutter_test/project_alpha
#   🔍 Verifying manual symlinks...
```

Now break the ref symlink and re-verify:

```bash
rm ~/.clutter/refs/alpha
python clutter.py verify
# Expected:
#   ⚠️  Missing ref: alpha → /tmp/clutter_test/project_alpha
#   Recreate ref symlink? [Y/n]
# Type Y
# Expected: ✅ Recreated

# Confirm it's back
ls -la ~/.clutter/refs/alpha
```

---

## Test 8: Ghost Detection (Original Deleted)

This tests the safety net. Requires the watcher running in one terminal.

**Terminal 1:**
```bash
python clutter.py watch /tmp/clutter_test
# Leave this running
```

**Terminal 2:**
```bash
# Make sure alpha has been pulled (sandbox has content)
# If not: python clutter.py pull alpha

# Now delete the original
rm -rf /tmp/clutter_test/project_alpha
```

**Back in Terminal 1:**
```
# Expected output:
#   ⚠️  TRACKED ITEM DELETED: /tmp/clutter_test/project_alpha
#   This item is managed by Clutter as 'alpha'
#   Ghost copy available in sandbox: ~/.clutter/sandboxes/alpha
#
#   [R] Restore — copy ghost back to original location
#   [G] Keep ghost — mark as ghost, decide later
#   [D] Delete for real — remove tracking and ghost

# Type R to restore
# Expected: ✅ Restored to /tmp/clutter_test/project_alpha
```

**Terminal 2 — verify restoration:**
```bash
ls /tmp/clutter_test/project_alpha/
# Expected: src/, assets/, README.md, CHANGELOG.md — all back

python clutter.py verify
# Expected: ✅ Tracked: alpha → /tmp/clutter_test/project_alpha
```

---

## Test 9: Ghost Detection (Original Moved)

**Terminal 1:**
```bash
python clutter.py watch /tmp/clutter_test
```

**Terminal 2:**
```bash
mv /tmp/clutter_test/project_alpha /tmp/clutter_test/project_alpha_renamed
```

**Terminal 1:**
```
# Expected:
#   ⚠️  TRACKED ITEM MOVED: 'alpha'
#   From: /tmp/clutter_test/project_alpha
#   To:   /tmp/clutter_test/project_alpha_renamed
#
#   [F] Follow — update tracking to new location
#   [G] Ghost — keep old path, mark as ghost
#   [C] Cancel — this was an accident (cannot undo move)

# Type F to follow
# Expected: ✅ Tracking updated to: /tmp/clutter_test/project_alpha_renamed
```

**Terminal 2 — verify:**
```bash
python clutter.py verify
# Expected: ✅ Tracked: alpha → /tmp/clutter_test/project_alpha_renamed

ls -la ~/.clutter/refs/alpha
# Expected: points to /tmp/clutter_test/project_alpha_renamed
```

---

## Test 10: Ghost Without Prior Pull

Track something new but NEVER pull it, then delete the original.

**Terminal 1:**
```bash
# Stop any running watcher first (Ctrl+C), then restart
python clutter.py watch /tmp/clutter_test
```

**Terminal 2:**
```bash
python clutter.py track /tmp/clutter_test/project_beta beta
rm -rf /tmp/clutter_test/project_beta
```

**Terminal 1:**
```
# Expected:
#   ⚠️  TRACKED ITEM DELETED: /tmp/clutter_test/project_beta
#   This item is managed by Clutter as 'beta'
#   ⚠️  No ghost available (never pulled)
#   Cannot recover. Remove tracking with 'clutter untrack beta'
```

This confirms the safety model: **no pull = no safety net.** The system warned honestly.

---

## Test 11: Sandboxes & Changes

```bash
python clutter.py sandboxes
# Expected: lists alpha and beta with paths, originals, dates

python clutter.py changes
# Expected: recent change log with 🟢/🔴 color indicators
```

---

## Test 12: Empty Commit Blocked

```bash
# Track something fresh
mkdir -p /tmp/clutter_test/project_gamma
echo "gamma" > /tmp/clutter_test/project_gamma/data.txt
python clutter.py track /tmp/clutter_test/project_gamma gamma

# Try to commit without pulling first
python clutter.py commit gamma
# Expected: Error: Sandbox 'gamma' is empty. Nothing to commit.
#           Run 'clutter pull gamma' first.
```

---

## Test 13: Full Lifecycle (Happy Path)

Run the complete workflow end-to-end in one shot.

```bash
# Clean slate
rm -rf ~/.clutter /tmp/clutter_lifecycle

# Create project
mkdir -p /tmp/clutter_lifecycle/myapp/src
echo "v1" > /tmp/clutter_lifecycle/myapp/src/app.py

# Index
python clutter.py scan /tmp/clutter_lifecycle

# Track
python clutter.py track /tmp/clutter_lifecycle/myapp myapp

# Pull
python clutter.py pull myapp

# Edit in sandbox
echo "v2 with new feature" > ~/.clutter/sandboxes/myapp/src/app.py

# Commit back
python clutter.py commit myapp

# Verify original updated
cat /tmp/clutter_lifecycle/myapp/src/app.py
# Expected: "v2 with new feature"

# Verify snapshot of v1 exists
ls ~/.clutter/snapshots/myapp/
# Expected: pre_commit_<timestamp>/

cat ~/.clutter/snapshots/myapp/pre_commit_*/src/app.py
# Expected: "v1"

# Full health check
python clutter.py verify
# Expected: all green
```

---

## Cleanup

```bash
rm -rf /tmp/clutter_test /tmp/clutter_lifecycle ~/.clutter
```

---

## Quick Reference: What Each Test Proves

| Test | Proves |
|------|--------|
| 1 | Clean install, version correct |
| 2 | Indexing and search work |
| 3 | Track is zero-copy (Law 1 & 2) |
| 4 | Pull creates working copy on demand |
| 5 | Edit→commit syncs back safely |
| 6 | Re-pull preserves previous sandbox as snapshot |
| 7 | Verify detects and repairs broken refs |
| 8 | Ghost detection restores from sandbox on delete |
| 9 | Move detection offers follow/ghost/cancel |
| 10 | No pull = no ghost (honest failure) |
| 11 | Listing and change history work |
| 12 | Empty commit blocked with clear message |
| 13 | Full lifecycle end-to-end |
