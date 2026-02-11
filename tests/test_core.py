import pytest
import tempfile
import shutil
import os
import time
from pathlib import Path
from clutter import Clutter

@pytest.fixture
def temp_clutter():
    """Isolated Clutter instance with temp database and sandbox."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / 'test.db'
        clutter = Clutter(str(db_path))
        # Override the base directory so sandboxes/refs/snapshots go inside tmpdir
        clutter.db_path = db_path
        clutter.base_dir = Path(tmpdir)
        # Ensure directories exist
        (clutter.base_dir / 'sandboxes').mkdir(exist_ok=True)
        (clutter.base_dir / 'refs').mkdir(exist_ok=True)
        (clutter.base_dir / 'snapshots').mkdir(exist_ok=True)
        yield clutter, Path(tmpdir)
        # Cleanup is automatic via TemporaryDirectory

class TestTrackPullCommit:
    """Core workflow: track → pull → commit"""

    def test_track_creates_metadata(self, temp_clutter):
        clutter, tmpdir = temp_clutter
        original = tmpdir / 'original'
        original.mkdir()
        (original / 'file.txt').write_text('hello')

        clutter.track(str(original), 'myproj')

        # Verify database entry
        with clutter.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT path, name FROM tracked_items WHERE name = ?",
                ('myproj',)
            )
            row = cursor.fetchone()

        assert row is not None, "Tracked item not found in DB"
        assert row[1] == 'myproj'
        assert row[0] == str(original)

    def test_track_creates_ref_symlink(self, temp_clutter):
        clutter, tmpdir = temp_clutter
        original = tmpdir / 'original'
        original.mkdir()
        (original / 'file.txt').write_text('hello')

        clutter.track(str(original), 'myproj')

        ref_path = clutter.base_dir / 'refs' / 'myproj'
        assert os.path.lexists(ref_path), "Ref symlink not created"
        assert os.path.realpath(ref_path) == str(original)

    def test_track_creates_sandbox_dir(self, temp_clutter):
        clutter, tmpdir = temp_clutter
        original = tmpdir / 'original'
        original.mkdir()
        (original / 'file.txt').write_text('hello')

        clutter.track(str(original), 'myproj')

        sandbox_path = clutter.base_dir / 'sandboxes' / 'myproj'
        assert sandbox_path.exists(), "Sandbox directory not created"
        meta = sandbox_path / '.clutter_sandbox'
        assert meta.exists(), "Sandbox metadata missing"

    def test_pull_copies_original_to_sandbox(self, temp_clutter):
        clutter, tmpdir = temp_clutter
        original = tmpdir / 'original'
        original.mkdir()
        (original / 'file.txt').write_text('hello')

        clutter.track(str(original), 'myproj')
        clutter.pull('myproj')

        sandbox = clutter.base_dir / 'sandboxes' / 'myproj'
        assert (sandbox / 'file.txt').exists(), "File not copied"
        assert (sandbox / 'file.txt').read_text() == 'hello'

    def test_commit_syncs_sandbox_back_to_original(self, temp_clutter):
        clutter, tmpdir = temp_clutter
        original = tmpdir / 'original'
        original.mkdir()
        (original / 'file.txt').write_text('hello')

        clutter.track(str(original), 'myproj')
        clutter.pull('myproj')

        sandbox = clutter.base_dir / 'sandboxes' / 'myproj'
        (sandbox / 'file.txt').write_text('world')

        clutter.commit('myproj')

        assert (original / 'file.txt').read_text() == 'world'


class TestDeletedFileRecovery:
    """The concierge feature: detect deleted tracked original, offer restore"""

    def test_deleted_original_marks_as_ghost_when_no_sandbox(self, temp_clutter, monkeypatch):
        """If no sandbox exists, deletion should mark as ghost without prompting."""
        clutter, tmpdir = temp_clutter
        original = tmpdir / 'original'
        original.mkdir()
        (original / 'file.txt').write_text('hello')

        clutter.track(str(original), 'myproj')
        # Do NOT pull – sandbox is empty

        # Simulate deletion
        shutil.rmtree(original)

        # Handle deletion – should mark as ghost
        result = clutter.handle_tracked_deletion(str(original))

        assert result is False  # No recovery possible
        with clutter.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM tracked_items WHERE name = ?", ('myproj',))
            status = cursor.fetchone()[0]
            assert status == 'ghost'

    def test_deleted_original_with_sandbox_offers_restore(self, temp_clutter, monkeypatch):
        """When sandbox exists, user should be prompted to restore."""
        clutter, tmpdir = temp_clutter
        original = tmpdir / 'original'
        original.mkdir()
        (original / 'file.txt').write_text('game code')

        clutter.track(str(original), 'game')
        clutter.pull('game')

        # Simulate deletion
        shutil.rmtree(original)

        # Simulate user choosing 'r' (restore)
        monkeypatch.setattr('builtins.input', lambda _: 'r')

        result = clutter.handle_tracked_deletion(str(original))

        assert result is True
        # Original should be restored
        assert original.exists()
        assert (original / 'file.txt').read_text() == 'game code'

        # Status should be 'tracked'
        with clutter.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM tracked_items WHERE name = ?", ('game',))
            status = cursor.fetchone()[0]
            assert status == 'tracked'

    def test_deleted_original_with_sandbox_user_chooses_ghost(self, temp_clutter, monkeypatch):
        """User can choose to keep ghost."""
        clutter, tmpdir = temp_clutter
        original = tmpdir / 'original'
        original.mkdir()
        (original / 'file.txt').write_text('game code')

        clutter.track(str(original), 'game')
        clutter.pull('game')
        shutil.rmtree(original)

        monkeypatch.setattr('builtins.input', lambda _: 'g')

        result = clutter.handle_tracked_deletion(str(original))

        assert result is True
        # Original should NOT be restored
        assert not original.exists()
        # Status should be 'ghost'
        with clutter.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM tracked_items WHERE name = ?", ('game',))
            status = cursor.fetchone()[0]
            assert status == 'ghost'

    def test_deleted_original_with_sandbox_user_chooses_delete(self, temp_clutter, monkeypatch):
        """User can choose to delete tracking entirely."""
        clutter, tmpdir = temp_clutter
        original = tmpdir / 'original'
        original.mkdir()
        (original / 'file.txt').write_text('game code')

        clutter.track(str(original), 'game')
        clutter.pull('game')
        shutil.rmtree(original)

        monkeypatch.setattr('builtins.input', lambda _: 'd')

        result = clutter.handle_tracked_deletion(str(original))

        assert result is True
        # Original should NOT be restored
        assert not original.exists()
        # Status should be 'ghost' (the spec says mark as ghost, not delete)
        with clutter.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM tracked_items WHERE name = ?", ('game',))
            status = cursor.fetchone()[0]
            assert status == 'ghost'
