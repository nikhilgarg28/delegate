"""Tests for the GET /teams/{team}/files and /teams/{team}/files/content API endpoints."""

import pytest
from fastapi.testclient import TestClient

from boss.web import create_app

TEAM = "testteam"


@pytest.fixture
def shared_tree(tmp_team):
    """Create a mock shared/ directory structure inside the bootstrapped team."""
    from boss.paths import shared_dir

    base = shared_dir(tmp_team, TEAM)
    base.mkdir(exist_ok=True)

    # Create subdirectories
    (base / "decisions").mkdir()
    (base / "specs").mkdir()
    (base / "guides").mkdir()

    # Create files
    (base / "README.md").write_text("# Shared Knowledge Base\n")
    (base / "decisions" / "2026-02-09-api-design.md").write_text(
        "# API Design Decision\nWe chose REST.\n"
    )
    (base / "specs" / "T0025-code-review-ux.md").write_text(
        "# Code Review UX Spec\nDetails here.\n"
    )
    (base / "guides" / "getting-started.md").write_text(
        "# Getting Started\nStep 1...\n"
    )

    return tmp_team


@pytest.fixture
def client(shared_tree):
    """Create a FastAPI test client with shared/ directory populated."""
    app = create_app(hc_home=shared_tree)
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /teams/{team}/files -- list shared directory
# ---------------------------------------------------------------------------


class TestListSharedFiles:
    def test_list_root_returns_dirs_and_files(self, client):
        """Listing root shared/ returns subdirectories and files."""
        resp = client.get(f"/teams/{TEAM}/files")
        assert resp.status_code == 200
        data = resp.json()
        files = data["files"]

        names = [f["name"] for f in files]
        assert "decisions" in names
        assert "specs" in names
        assert "guides" in names
        assert "README.md" in names

    def test_list_root_dirs_first(self, client):
        """Directories should appear before files in listing."""
        resp = client.get(f"/teams/{TEAM}/files")
        files = resp.json()["files"]

        dirs = [f for f in files if f["is_dir"]]
        non_dirs = [f for f in files if not f["is_dir"]]

        if dirs and non_dirs:
            last_dir_idx = max(files.index(d) for d in dirs)
            first_file_idx = min(files.index(f) for f in non_dirs)
            assert last_dir_idx < first_file_idx

    def test_list_subdirectory(self, client):
        """Listing a subdirectory returns its contents."""
        resp = client.get(f"/teams/{TEAM}/files", params={"path": "decisions"})
        assert resp.status_code == 200
        files = resp.json()["files"]

        assert len(files) == 1
        assert files[0]["name"] == "2026-02-09-api-design.md"
        assert files[0]["is_dir"] is False
        assert files[0]["path"] == "decisions/2026-02-09-api-design.md"

    def test_list_file_entry_has_required_fields(self, client):
        """Each file entry should have name, path, size, modified, is_dir."""
        resp = client.get(f"/teams/{TEAM}/files", params={"path": "decisions"})
        entry = resp.json()["files"][0]

        assert "name" in entry
        assert "path" in entry
        assert "size" in entry
        assert "modified" in entry
        assert "is_dir" in entry
        assert isinstance(entry["size"], int)
        assert entry["size"] > 0

    def test_list_nonexistent_subdir_404(self, client):
        """Listing a non-existent subdirectory returns 404."""
        resp = client.get(f"/teams/{TEAM}/files", params={"path": "nonexistent"})
        assert resp.status_code == 404

    def test_list_path_traversal_403(self, client):
        """Path traversal attempts return 403."""
        resp = client.get(f"/teams/{TEAM}/files", params={"path": "../../etc"})
        assert resp.status_code == 403

    def test_list_empty_shared_dir(self, tmp_team):
        """Listing when shared/ doesn't exist returns empty list."""
        app = create_app(hc_home=tmp_team)
        c = TestClient(app)
        resp = c.get(f"/teams/{TEAM}/files")
        assert resp.status_code == 200
        assert resp.json() == {"files": []}


# ---------------------------------------------------------------------------
# GET /teams/{team}/files/content -- read a specific file
# ---------------------------------------------------------------------------


class TestReadSharedFile:
    def test_read_file_returns_content(self, client):
        """Reading a file returns its content."""
        resp = client.get(
            f"/teams/{TEAM}/files/content",
            params={"path": "decisions/2026-02-09-api-design.md"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "API Design Decision" in data["content"]
        assert data["name"] == "2026-02-09-api-design.md"
        assert data["path"] == "decisions/2026-02-09-api-design.md"

    def test_read_file_has_required_fields(self, client):
        """Response should contain path, name, size, content, modified."""
        resp = client.get(
            f"/teams/{TEAM}/files/content",
            params={"path": "README.md"},
        )
        data = resp.json()
        assert "path" in data
        assert "name" in data
        assert "size" in data
        assert "content" in data
        assert "modified" in data

    def test_read_root_file(self, client):
        """Reading a file at the root of shared/ works."""
        resp = client.get(
            f"/teams/{TEAM}/files/content",
            params={"path": "README.md"},
        )
        assert resp.status_code == 200
        assert "Shared Knowledge Base" in resp.json()["content"]

    def test_read_nonexistent_file_404(self, client):
        """Reading a non-existent file returns 404."""
        resp = client.get(
            f"/teams/{TEAM}/files/content",
            params={"path": "does-not-exist.md"},
        )
        assert resp.status_code == 404

    def test_read_path_traversal_403(self, client):
        """Path traversal attempts return 403."""
        resp = client.get(
            f"/teams/{TEAM}/files/content",
            params={"path": "../../etc/passwd"},
        )
        assert resp.status_code == 403

    def test_read_large_file_truncated(self, shared_tree):
        """Files larger than 1MB should be truncated."""
        from boss.paths import shared_dir

        base = shared_dir(shared_tree, TEAM)
        large_file = base / "large.txt"
        large_file.write_text("x" * 1_500_000)

        app = create_app(hc_home=shared_tree)
        c = TestClient(app)
        resp = c.get(
            f"/teams/{TEAM}/files/content",
            params={"path": "large.txt"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["content"]) == 1_000_000
        assert data["size"] == 1_500_000
