import io
import json
import stat
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import crashcustomer.app as app_module
from crashcustomer.attachments import AttachmentError, inspect_upload
from crashcustomer.store import Store


SPEC = (Path(__file__).parent / "fixtures/sample-openapi.yaml").read_bytes()


def zipped(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return out.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "store", Store(tmp_path))
    with TestClient(app_module.app) as client:
        yield client


def create_project(client, name="테스트 프로젝트"):
    response = client.post("/api/projects", json={"name": name, "base_url": "http://localhost:10000"})
    assert response.status_code == 201
    return response.json()["id"]


def test_openapi_summary_handles_auth_and_required_parameters():
    result = inspect_upload("api.yaml", SPEC)
    assert result["category"] == "openapi"
    assert result["summary"]["operation_count"] == 3
    operations = result["summary"]["operations"]
    assert operations[0]["authenticated"] is False
    assert operations[1]["authenticated"] is True
    assert operations[0]["parameters"][0] == {"name": "Device-Id", "in": "header", "required": True}


@pytest.mark.parametrize("name,content", [
    ("../api.yaml", SPEC), ("AuthKey.p8", b"test"), (".env", b"test"),
    ("user_accessKeys.csv", b"test"), ("broken.json", b"{broken"),
    ("empty.md", b""), ("run.exe", b"binary"),
    ("private.txt", b"-----BEGIN PRIVATE KEY-----\nnot-real-test-key"),
    ("aliases.yaml", b"data: &data [hello]\nrepeat: *data"),
    ("unsafe.yaml", b"!!python/object/apply:os.system ['echo test']"),
])
def test_invalid_and_sensitive_attachments_are_rejected(name, content):
    with pytest.raises(AttachmentError):
        inspect_upload(name, content)


@pytest.mark.parametrize("entry", ["../escape.py", "/absolute.py", "C:/drive.py", "source/.env", "source/.git/config", "source/AuthKey.p8", "source/accessKeys.csv", "nested.zip"])
def test_archive_paths_and_sensitive_entries_are_rejected(entry):
    with pytest.raises(AttachmentError):
        inspect_upload("source.zip", zipped({entry: "test"}))


def test_zip_is_indexed_without_extracting_or_executing(tmp_path):
    result = inspect_upload("source.zip", zipped({"src/main.py": "raise RuntimeError('must not execute')", "README.md": "Example"}))
    assert result["summary"]["file_count"] == 2
    assert result["summary"]["files"][0]["path"] == "src/main.py"


def test_symlink_archive_is_rejected():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        entry = zipfile.ZipInfo("link")
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(entry, "/etc/passwd")
    with pytest.raises(AttachmentError):
        inspect_upload("source.zip", out.getvalue())


def test_project_and_attachment_roundtrip_is_isolated_and_persistent(client):
    first, second = create_project(client), create_project(client, "다른 프로젝트")
    uploaded = client.post(f"/api/projects/{first}/attachments", data={"category": "auto"}, files={"file": ("명세.yaml", SPEC, "text/yaml")})
    assert uploaded.status_code == 201
    file = uploaded.json()
    attachment_id = file["id"]
    assert file["summary"]["operation_count"] == 3
    assert client.get(f"/api/projects/{second}").json()["attachments"] == []
    assert client.get(f"/api/projects/{second}/attachments/{attachment_id}/download").status_code == 404
    response = client.get(f"/api/projects/{first}/attachments/{attachment_id}/download")
    assert response.content == SPEC
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"
    preview = client.get(f"/api/projects/{first}/attachments/{attachment_id}/preview").json()
    assert "Sample Food API" in preview["text"]
    reopened = Store(app_module.store.directory)
    assert reopened.get_project(first)["name"] == "테스트 프로젝트"
    assert reopened.list_attachments(first)[0]["id"] == attachment_id
    assert client.delete(f"/api/projects/{first}/attachments/{attachment_id}").status_code == 204
    assert client.get(f"/api/projects/{first}").json()["attachments"] == []
    assert not (reopened.uploads / attachment_id).exists()


def test_upload_rejection_does_not_save_partial_files(client):
    project_id = create_project(client)
    response = client.post(f"/api/projects/{project_id}/attachments", files={"file": ("secret.p8", b"not-a-real-key")})
    assert response.status_code == 400
    assert list(app_module.store.uploads.iterdir()) == []
    assert client.get(f"/api/projects/{project_id}").json()["attachments"] == []


def test_project_update_and_external_run_guard(client):
    project_id = create_project(client)
    response = client.patch(f"/api/projects/{project_id}", json={"name": "수정된 이름", "browser_url": "http://localhost:10000/simulator/"})
    assert response.status_code == 200
    assert response.json()["name"] == "수정된 이름"
    assert response.json()["base_url"] == "http://localhost:10000"
    assert response.json()["browser_url"] == "http://localhost:10000/simulator/"
    assert response.json()["execution"]["status"] == "ready"
    assert client.post("/api/runs", json={"project_id": project_id}).status_code == 409


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///tmp/file", "https://user:password@example.com", "https://example.com?token=secret", "http://localhost:99999"])
def test_project_addresses_are_validated_without_fetching(client, url):
    assert client.post("/api/projects", json={"name": "project", "base_url": url}).status_code == 422


def test_category_validation_and_template(client):
    project_id = create_project(client)
    response = client.post(f"/api/projects/{project_id}/attachments", data={"category": "openapi"}, files={"file": ("note.md", b"notes")})
    assert response.status_code == 400
    response = client.get("/api/attachment-templates/test-plan")
    assert response.status_code == 200
    assert b"concurrent_requests: 70" in response.content


def test_per_project_file_limit_rolls_back_disk_file(tmp_path):
    store = Store(tmp_path)
    project = store.create_project({"name": "Limit"})
    analysis = inspect_upload("note.md", b"reference")
    for _ in range(30):
        store.add_attachment(project["id"], "note.md", b"reference", analysis)
    with pytest.raises(ValueError, match="30"):
        store.add_attachment(project["id"], "note.md", b"reference", analysis)
    assert len(store.list_attachments(project["id"])) == 30
    assert len(list(store.uploads.iterdir())) == 30


def test_streamed_upload_limit_and_declared_request_limit(client, monkeypatch):
    project_id = create_project(client)
    monkeypatch.setattr(app_module, "MAX_FILE_BYTES", 3)
    response = client.post(f"/api/projects/{project_id}/attachments", files={"file": ("note.md", b"four")})
    assert response.status_code == 413
    response = client.post(f"/api/projects/{project_id}/attachments", content=b"", headers={"content-length": "999999999"})
    assert response.status_code == 413
    assert list(app_module.store.uploads.iterdir()) == []


def test_archive_expansion_limit(monkeypatch):
    import crashcustomer.attachments as attachments

    monkeypatch.setattr(attachments, "MAX_ARCHIVE_BYTES", 5)
    with pytest.raises(AttachmentError, match="50MB"):
        inspect_upload("reference.zip", zipped({"readme.md": "more than five bytes"}))


def test_test_plan_is_stored_without_interpreting_as_commands(client):
    project_id = create_project(client)
    body = b"name: example\nload:\n  concurrent_requests: 70\n"
    response = client.post(f"/api/projects/{project_id}/attachments", data={"category": "test_plan"}, files={"file": ("plan.yaml", body)})
    assert response.status_code == 201
    assert response.json()["category"] == "test_plan"
    assert response.json()["summary"]["sections"] == ["name", "load"]
    assert app_module.store.list_runs() == []
