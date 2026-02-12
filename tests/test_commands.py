import pytest
from pathlib import Path
from clutter import Clutter


@pytest.fixture
def temp_clutter(tmp_path):
    """Isolated Clutter instance with temp database and sandbox."""
    db_path = tmp_path / 'test.db'
    clutter = Clutter(str(db_path))
    clutter.db_path = db_path
    clutter.base_dir = tmp_path
    (clutter.base_dir / 'sandboxes').mkdir(exist_ok=True)
    (clutter.base_dir / 'refs').mkdir(exist_ok=True)
    (clutter.base_dir / 'snapshots').mkdir(exist_ok=True)
    yield clutter, tmp_path


class TestWorkCommand:
    def test_work_tracks_and_pulls_if_not_tracked(self, temp_clutter, monkeypatch):
        clutter, tmpdir = temp_clutter
        original = tmpdir / 'original'
        original.mkdir()
        (original / 'file.txt').write_text('hello')

        # Simulate user input for path and confirmation
        inputs = iter([str(original), 'y'])
        monkeypatch.setattr('builtins.input', lambda _: next(inputs))

        clutter.work('myproj')

        # Should be tracked
        with clutter.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT path FROM tracked_items WHERE name = ?", ('myproj',))
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == str(original)

        # Sandbox should have file
        sandbox = clutter.base_dir / 'sandboxes' / 'myproj'
        assert (sandbox / 'file.txt').exists()
        assert (sandbox / 'file.txt').read_text() == 'hello'

    def test_work_resume_state_saved(self, temp_clutter, monkeypatch):
        clutter, tmpdir = temp_clutter
        original = tmpdir / 'original'
        original.mkdir()
        (original / 'file.txt').write_text('hello')

        inputs = iter([str(original), 'y'])
        monkeypatch.setattr('builtins.input', lambda _: next(inputs))

        clutter.work('myproj')

        state = clutter._load_resume_state()
        assert state == 'myproj'


class TestStatusCommand:
    def test_status_shows_clean_when_synced(self, temp_clutter, capsys):
        clutter, tmpdir = temp_clutter
        original = tmpdir / 'original'
        original.mkdir()
        (original / 'file.txt').write_text('hello')

        clutter.track(str(original), 'proj')
        clutter.pull('proj')

        clutter.status('proj')
        captured = capsys.readouterr()
        assert "in sync" in captured.out

    def test_status_detects_modification(self, temp_clutter, capsys):
        clutter, tmpdir = temp_clutter
        original = tmpdir / 'original'
        original.mkdir()
        (original / 'file.txt').write_text('hello')

        clutter.track(str(original), 'proj')
        clutter.pull('proj')

        # Modify sandbox
        sandbox = clutter.base_dir / 'sandboxes' / 'proj'
        (sandbox / 'file.txt').write_text('world')

        clutter.status('proj')
        captured = capsys.readouterr()
        assert "Modified" in captured.out


class TestResumeCommand:
    def test_resume_prints_cd_command(self, temp_clutter, capsys, monkeypatch):
        clutter, tmpdir = temp_clutter
        original = tmpdir / 'original'
        original.mkdir()
        (original / 'file.txt').write_text('hello')

        clutter.track(str(original), 'proj')
        clutter._save_resume_state('proj')

        clutter.resume()
        captured = capsys.readouterr()
        sandbox_path = clutter.base_dir / 'sandboxes' / 'proj'
        assert f"cd {sandbox_path}" in captured.out
