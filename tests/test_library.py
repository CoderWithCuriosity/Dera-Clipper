"""Library management from the website: themes, uploads, removal, and the rules that protect them.
Every test works on its own copy of the effects folder, so the real library is never changed."""
import shutil
import subprocess

import pytest

from conftest import FakeEngine
from test_phase3 import _bearer, _register, auth_client, auth_env  # noqa: F401  (fixtures)


def _wav(tmp_path, ffmpeg_bin, seconds=1):
    path = tmp_path / "tone.wav"
    subprocess.run([ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"sine=frequency=600:duration={seconds}", str(path)], check=True)
    return path.read_bytes()


@pytest.fixture()
def client(app_env, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import clipper.pipeline as pipeline
    from clipper.api import create_app
    from clipper.config import ROOT, load_settings

    copy = tmp_path / "effects-copy"
    shutil.copytree(ROOT / "effects", copy)
    monkeypatch.setenv("CLIPPER_EFFECTS_DIR", str(copy))
    monkeypatch.setattr(pipeline, "build_transcription_chain",
                        lambda svc, user_id, mode: [("local", FakeEngine(duration=40.0))])
    with TestClient(create_app(load_settings())) as c:
        yield c


@pytest.fixture()
def auth_lib_client(auth_env, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import clipper.pipeline as pipeline
    from clipper.api import create_app
    from clipper.config import ROOT, load_settings

    copy = tmp_path / "effects-copy"
    shutil.copytree(ROOT / "effects", copy)
    monkeypatch.setenv("CLIPPER_EFFECTS_DIR", str(copy))
    monkeypatch.setattr(pipeline, "build_transcription_chain",
                        lambda svc, user_id, mode: [("local", FakeEngine(duration=40.0))])
    with TestClient(create_app(load_settings())) as c:
        yield c


def test_create_upload_and_remove_a_theme_effect(client, ffmpeg_bin, tmp_path):
    theme = client.post("/api/admin/themes", json={"name": "Laughter Test", "description": "Laughs"})
    assert theme.status_code == 201, theme.text
    folder = theme.json()["folder"]
    assert folder == "laughter-test"
    assert "Laughter Test" in [t["name"] for t in client.get("/api/effects").json()["themes"]]

    entry = client.post(
        f"/api/admin/themes/{folder}/effects",
        files={"file": ("my laugh!.wav", _wav(tmp_path, ffmpeg_bin), "audio/wav")},
        data={"kind": "sound", "name": "Big Laugh", "tags": "laugh, loud", "license": "CC0-1.0",
              "source": "recorded by the owner"},
    )
    assert entry.status_code == 201, entry.text
    file_rel = entry.json()["file"]
    assert file_rel == "sfx/big-laugh.wav"

    laugh_theme = next(t for t in client.get("/api/effects").json()["themes"] if t["name"] == "Laughter Test")
    effect = laugh_theme["effects"][0]
    assert effect["license"].startswith("CC0-1.0") and effect["tags"] == ["laugh", "loud"]
    assert effect["url"].startswith(f"/api/effects/{effect['id']}/file?t=")

    # The preview link plays the file without a sign-in header.
    preview = client.get(effect["url"])
    assert preview.status_code == 200 and len(preview.content) > 1000

    assert client.delete(f"/api/admin/themes/{folder}/effects", params={"file": file_rel}).status_code == 200
    after = next(t for t in client.get("/api/effects").json()["themes"] if t["name"] == "Laughter Test")
    assert after["effects"] == []

    assert client.delete(f"/api/admin/themes/{folder}").status_code == 200
    assert "Laughter Test" not in [t["name"] for t in client.get("/api/effects").json()["themes"]]


def test_uploads_are_checked_and_mistakes_are_explained(client, tmp_path, ffmpeg_bin):
    client.post("/api/admin/themes", json={"name": "Checks"})
    not_media = client.post("/api/admin/themes/checks/effects",
                            files={"file": ("fake.wav", b"this is only text", "audio/wav")},
                            data={"kind": "sound"})
    assert not_media.status_code == 400 and "could not be read" in not_media.json()["detail"]

    wrong_kind = client.post("/api/admin/themes/checks/effects",
                             files={"file": ("x.wav", _wav(tmp_path, ffmpeg_bin), "audio/wav")},
                             data={"kind": "broll"})
    assert wrong_kind.status_code == 400

    duplicate = client.post("/api/admin/themes", json={"name": "checks"})
    assert duplicate.status_code == 400


def test_names_cannot_escape_the_library_folder(client, tmp_path):
    res = client.post("/api/admin/themes", json={"name": "../../etc"})
    assert res.status_code == 201
    assert res.json()["folder"] == "etc"  # the name is reduced to a safe folder name
    assert client.post("/api/admin/themes", json={"name": "x"}).status_code == 422
    assert client.delete("/api/admin/themes/..%2F..%2Fetc").status_code in (400, 404, 405)


def test_only_admins_can_change_the_library(auth_lib_client):
    admin = _register(auth_lib_client, "lib-admin@example.com").json()["access_token"]
    auth_lib_client.cookies.clear()
    member = _register(auth_lib_client, "lib-member@example.com").json()["access_token"]
    res = auth_lib_client.post("/api/admin/themes", headers=_bearer(member), json={"name": "Sneaky"})
    assert res.status_code == 403
    res = auth_lib_client.post("/api/admin/themes", headers=_bearer(admin), json={"name": "Official"})
    assert res.status_code == 201
