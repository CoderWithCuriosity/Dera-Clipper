"""Phase 3 tests: accounts and sessions, admin rules, quotas, fair job claiming, S3 storage
(mocked with moto), backups, AWS Transcribe parsing, and retention cleanup."""
import dataclasses
import json
import os
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from conftest import FakeEngine, wait_for
from clipper import auth
from clipper.config import load_settings


# -- fixtures ----------------------------------------------------------------------

@pytest.fixture()
def auth_env(tmp_path, monkeypatch):
    from clipper.config import ROOT

    monkeypatch.setenv("CLIPPER_MODE", "vps")
    monkeypatch.setenv("CLIPPER_REQUIRE_AUTH", "true")
    monkeypatch.setenv("CLIPPER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLIPPER_EFFECTS_DIR", str(ROOT / "effects"))
    monkeypatch.setenv("CLIPPER_RUN_WORKER", "true")
    monkeypatch.setenv("CLIPPER_JOB_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("CLIPPER_MASTER_KEY", "test-master-key-for-tests")
    monkeypatch.setenv("CLIPPER_COOKIE_SECURE", "false")
    monkeypatch.setenv("CLIPPER_LOGIN_ATTEMPTS_PER_15_MIN", "3")
    monkeypatch.setenv("CLIPPER_BACKUP_EVERY_HOURS", "0")
    monkeypatch.delenv("CLIPPER_DATABASE_URL", raising=False)
    monkeypatch.delenv("CLIPPER_STORAGE", raising=False)
    return tmp_path


@pytest.fixture()
def auth_client(auth_env, monkeypatch):
    from fastapi.testclient import TestClient

    import clipper.pipeline as pipeline
    from clipper.api import create_app

    monkeypatch.setattr(pipeline, "build_transcription_chain",
                        lambda svc, user_id, mode: [("local", FakeEngine(duration=40.0))])
    with TestClient(create_app(load_settings())) as c:
        yield c


PASSWORD = "correct horse battery"


def _register(client, email, password=PASSWORD):
    return client.post("/api/auth/register", json={"email": email, "password": password})


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


# -- passwords and tokens -----------------------------------------------------------

def test_password_hash_round_trip_and_rules():
    stored = auth.hash_password(PASSWORD)
    assert stored.startswith("scrypt$") and PASSWORD not in stored
    assert auth.verify_password(PASSWORD, stored)
    assert not auth.verify_password("wrong password!!", stored)
    assert not auth.verify_password(PASSWORD, "not-a-hash")
    with pytest.raises(auth.AuthError):
        auth.check_password_rules("short")


def test_access_tokens_are_signed_and_expire(app_env, monkeypatch):
    settings = load_settings()
    token = auth.issue_access_token(settings, {"id": "u1", "role": "user"})
    assert auth.read_access_token(settings, token)["sub"] == "u1"
    tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    assert auth.read_access_token(settings, tampered) is None
    real_time = auth.time.time
    monkeypatch.setattr(auth.time, "time", lambda: real_time() + auth.ACCESS_TTL_SECONDS + 60)
    assert auth.read_access_token(settings, token) is None


# -- accounts and sessions -----------------------------------------------------------

def test_first_account_is_admin_and_others_are_users(auth_client):
    first = _register(auth_client, "owner@example.com")
    assert first.status_code == 201
    assert first.json()["user"]["role"] == "admin"
    second = _register(auth_client, "friend@example.com")
    assert second.json()["user"]["role"] == "user"
    assert _register(auth_client, "FRIEND@example.com").status_code == 409


def test_pages_require_sign_in(auth_client):
    assert auth_client.get("/api/projects").status_code == 401
    assert auth_client.get("/api/auth/me").status_code == 401


def test_login_wrong_password_and_rate_limit(auth_client):
    _register(auth_client, "a@example.com")
    auth_client.cookies.clear()
    assert auth_client.post("/api/auth/login", json={"email": "a@example.com", "password": "nope-nope-nope"}).status_code == 401
    assert auth_client.post("/api/auth/login", json={"email": "a@example.com", "password": "nope-nope-nope"}).status_code == 401
    assert auth_client.post("/api/auth/login", json={"email": "a@example.com", "password": "nope-nope-nope"}).status_code == 401
    blocked = auth_client.post("/api/auth/login", json={"email": "a@example.com", "password": PASSWORD})
    assert blocked.status_code == 429


def test_refresh_rotates_and_reuse_revokes_every_session(auth_client):
    body = _register(auth_client, "rot@example.com").json()
    first_cookie = auth_client.cookies.get("clipper_refresh")
    assert first_cookie

    rotated = auth_client.post("/api/auth/refresh")
    assert rotated.status_code == 200
    assert rotated.json()["access_token"] != body["access_token"]
    second_cookie = auth_client.cookies.get("clipper_refresh")
    assert second_cookie and second_cookie != first_cookie

    # Replaying the retired token is treated as theft: every session is revoked.
    replay = auth_client.post("/api/auth/refresh", cookies={"clipper_refresh": first_cookie})
    assert replay.status_code == 401
    assert auth_client.post("/api/auth/refresh", cookies={"clipper_refresh": second_cookie}).status_code == 401


def test_users_only_see_their_own_projects(auth_client, sample_video):
    a = _register(auth_client, "alice@example.com").json()["access_token"]
    auth_client.cookies.clear()
    b = _register(auth_client, "bob@example.com").json()["access_token"]
    with open(sample_video, "rb") as fh:
        pid = auth_client.post("/api/projects", headers=_bearer(a),
                               files={"file": ("s.mp4", fh, "video/mp4")}).json()["id"]
    assert auth_client.get(f"/api/projects/{pid}", headers=_bearer(b)).status_code == 404
    assert auth_client.get("/api/projects", headers=_bearer(b)).json()["projects"] == []
    assert len(auth_client.get("/api/projects", headers=_bearer(a)).json()["projects"]) == 1


def test_admin_can_manage_users_and_others_cannot(auth_client):
    admin = _register(auth_client, "boss@example.com").json()
    auth_client.cookies.clear()
    user = _register(auth_client, "staff@example.com").json()
    auth_client.cookies.clear()

    assert auth_client.get("/api/admin/users", headers=_bearer(user["access_token"])).status_code == 403
    listing = auth_client.get("/api/admin/users", headers=_bearer(admin["access_token"])).json()["users"]
    staff_id = next(u["id"] for u in listing if u["email"] == "staff@example.com")

    assert auth_client.patch(f"/api/admin/users/{staff_id}", headers=_bearer(admin["access_token"]),
                             json={"disabled": True}).status_code == 200
    # The disabled user's session stops working and they cannot sign in again.
    assert auth_client.get("/api/projects", headers=_bearer(user["access_token"])).status_code == 401
    login = auth_client.post("/api/auth/login", json={"email": "staff@example.com", "password": PASSWORD})
    assert login.status_code == 403

    # The last administrator cannot be demoted.
    admin_id = admin["user"]["id"]
    demote = auth_client.patch(f"/api/admin/users/{admin_id}", headers=_bearer(admin["access_token"]),
                               json={"role": "user"})
    assert demote.status_code == 400
    assert auth_client.patch(f"/api/admin/users/{admin_id}", headers=_bearer(admin["access_token"]),
                             json={"disabled": True}).status_code == 400

    stats = auth_client.get("/api/admin/stats", headers=_bearer(admin["access_token"])).json()
    assert stats["users"] == 2


def test_change_password_requires_current_password(auth_client):
    token = _register(auth_client, "pw@example.com").json()["access_token"]
    bad = auth_client.post("/api/auth/change-password", headers=_bearer(token),
                           json={"current_password": "wrong-wrong-wrong", "new_password": "brand-new-password"})
    assert bad.status_code == 400
    ok = auth_client.post("/api/auth/change-password", headers=_bearer(token),
                          json={"current_password": PASSWORD, "new_password": "brand-new-password"})
    assert ok.status_code == 200


def test_registration_can_be_closed(auth_env, monkeypatch):
    from fastapi.testclient import TestClient

    from clipper.api import create_app

    monkeypatch.setenv("CLIPPER_ALLOW_REGISTRATION", "false")
    with TestClient(create_app(load_settings())) as c:
        assert c.post("/api/auth/register", json={"email": "x@example.com", "password": PASSWORD}).status_code == 403


def test_storage_quota_blocks_uploads(auth_env, monkeypatch, sample_video):
    from fastapi.testclient import TestClient

    import clipper.pipeline as pipeline
    from clipper.api import create_app

    monkeypatch.setenv("CLIPPER_USER_STORAGE_QUOTA_MB", "0")
    monkeypatch.setattr(pipeline, "build_transcription_chain",
                        lambda svc, user_id, mode: [("local", FakeEngine(duration=40.0))])
    with TestClient(create_app(load_settings())) as c:
        token = _register(c, "quota@example.com").json()["access_token"]
        with open(sample_video, "rb") as fh:
            res = c.post("/api/projects", headers=_bearer(token), files={"file": ("s.mp4", fh, "video/mp4")})
        assert res.status_code == 413


# -- fair job claiming -------------------------------------------------------------------

def test_one_heavy_user_cannot_hold_every_worker(app_env, monkeypatch):
    from clipper.config import load_settings as load
    from clipper.db import now_iso
    from clipper.services import Services

    monkeypatch.setenv("CLIPPER_MAX_RUNNING_JOBS_PER_USER", "1")
    svc = Services.create(load())
    now = now_iso()
    for user, label in [("heavy", "h1"), ("heavy", "h2"), ("light", "l1")]:
        svc.db.run("INSERT INTO jobs (id, user_id, project_id, type, status, progress, attempts, payload_json, "
                   "run_after, created_at, updated_at) VALUES (?, ?, NULL, 'download', 'queued', 0, 0, '{}', ?, ?, ?)",
                   (label, user, now, now + label, now))
    first = svc.queue.claim()
    assert first["id"] == "h1"
    second = svc.queue.claim()
    assert second["id"] == "l1", "the light user should not wait behind the heavy user's second job"
    assert svc.queue.claim() is None  # heavy user is at the limit; nothing else is queued


# -- S3 storage (moto) ----------------------------------------------------------------------

@pytest.fixture()
def s3_bucket(monkeypatch):
    moto = pytest.importorskip("moto")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with moto.mock_aws():
        import boto3

        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="clipper-test")
        yield "clipper-test"


def test_s3_storage_operations(app_env, monkeypatch, s3_bucket, tmp_path):
    from clipper.storage_s3 import S3Storage

    monkeypatch.setenv("CLIPPER_STORAGE", "s3")
    monkeypatch.setenv("CLIPPER_S3_BUCKET", s3_bucket)
    monkeypatch.setenv("CLIPPER_S3_REGION", "us-east-1")
    store = S3Storage(load_settings())
    store.check()
    src = tmp_path / "file.mp4"
    src.write_bytes(b"0123456789")
    store.put_file("clips/p1/clip_01.mp4", src)
    assert store.exists("clips/p1/clip_01.mp4") and not store.exists("clips/p1/nope.mp4")
    local = store.local_path("clips/p1/clip_01.mp4")
    assert local.read_bytes() == b"0123456789"
    assert store.list_keys("clips/p1") == ["clips/p1/clip_01.mp4"]
    assert "X-Amz-Signature" in store.download_url("clips/p1/clip_01.mp4", "clip.mp4")
    store.delete_prefix("clips/p1")
    assert not store.exists("clips/p1/clip_01.mp4")
    with pytest.raises(FileNotFoundError):
        store.local_path("clips/p1/clip_01.mp4")


def test_full_run_on_s3_storage(app_env, monkeypatch, s3_bucket, sample_video):
    from fastapi.testclient import TestClient

    import clipper.pipeline as pipeline
    from clipper.api import create_app

    monkeypatch.setenv("CLIPPER_MODE", "local")
    monkeypatch.setenv("CLIPPER_STORAGE", "s3")
    monkeypatch.setenv("CLIPPER_S3_BUCKET", s3_bucket)
    monkeypatch.setenv("CLIPPER_S3_REGION", "us-east-1")
    monkeypatch.setattr(pipeline, "build_transcription_chain",
                        lambda svc, user_id, mode: [("local", FakeEngine(duration=40.0))])
    with TestClient(create_app(load_settings())) as c:
        with open(sample_video, "rb") as fh:
            pid = c.post("/api/projects", files={"file": ("s.mp4", fh, "video/mp4")}).json()["id"]
        wait_for(c, f"/api/projects/{pid}", lambda p: p["status"] == "awaiting_settings")
        c.post(f"/api/projects/{pid}/generate", json={"theme": "Funny", "clip_length": 8, "clip_count": 1})
        done = wait_for(c, f"/api/projects/{pid}", lambda p: p["status"] in ("done", "failed"))
        assert done["status"] == "done", done
        clip = done["clips"][0]
        assert clip["status"] == "done"
        redirect = c.get(clip["video_url"], follow_redirects=False)
        assert redirect.status_code in (302, 307)
        assert "X-Amz-Signature" in redirect.headers["location"]
        import boto3

        keys = [o["Key"] for o in boto3.client("s3", region_name="us-east-1")
                .list_objects_v2(Bucket=s3_bucket)["Contents"]]
        assert any(k.endswith(f"clips/{pid}/clip_01.mp4") for k in keys)
        assert any(k.endswith(f"uploads/{pid}/source.mp4") for k in keys)


# -- backups and maintenance ------------------------------------------------------------------

def test_backup_contains_database_settings_and_manifests_but_no_key(app_env):
    from clipper.backup import create_backup, list_backups
    from clipper.services import Services

    settings = load_settings()
    Services.create(settings)  # creates the database
    archive = create_backup(settings)
    assert archive.exists()
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
    assert "database.sqlite3" in names and "settings.json" in names
    assert "effects/funny/manifest.json" in names
    assert not any("master" in n.lower() for n in names)
    assert json.loads(zipfile.ZipFile(archive).read("settings.json"))["database"] == "sqlite"
    assert list_backups(settings)[0]["name"] == archive.name


def test_backups_are_pruned_to_the_keep_count(app_env, monkeypatch):
    from clipper import backup
    from clipper.services import Services

    monkeypatch.setenv("CLIPPER_BACKUP_KEEP", "2")
    settings = load_settings()
    Services.create(settings)
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    for stamp in ["20200101-000000", "20200102-000000", "20200103-000000"]:
        (settings.backups_dir / f"{backup.PREFIX}{stamp}.zip").write_bytes(b"x")
    backup._prune(settings)
    left = sorted(p.name for p in settings.backups_dir.glob(f"{backup.PREFIX}*.zip"))
    assert left == [f"{backup.PREFIX}20200102-000000.zip", f"{backup.PREFIX}20200103-000000.zip"]


def test_expired_uploads_are_removed_but_clips_kept(app_env, sample_video):
    from clipper.maintenance import cleanup_expired_uploads
    from clipper.services import Services

    settings = load_settings()
    svc = Services.create(settings)
    key = "uploads/old-project/source.mp4"
    svc.storage.put_file(key, sample_video)
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    svc.db.run(
        "INSERT INTO projects (id, user_id, title, source_type, source_location, source_key, mode, status, progress, "
        "transcription_mode, created_at, updated_at) VALUES ('old-project', 'local', 'Old', 'upload', 'x.mp4', ?, "
        "'local', 'done', 1, 'local', ?, ?)", (key, old, old))
    assert cleanup_expired_uploads(svc) == 1
    assert not svc.storage.exists(key)
    assert svc.db.one("SELECT source_key FROM projects WHERE id = 'old-project'")["source_key"] is None


# -- AWS Transcribe parsing ---------------------------------------------------------------------

def test_aws_transcribe_document_becomes_segments_and_words():
    from clipper.transcription.aws import parse_transcribe_document

    doc = {"results": {"language_code": "en-US", "items": [
        {"type": "pronunciation", "start_time": "0.0", "end_time": "0.4", "alternatives": [{"content": "Hello"}]},
        {"type": "pronunciation", "start_time": "0.4", "end_time": "0.8", "alternatives": [{"content": "there"}]},
        {"type": "punctuation", "alternatives": [{"content": "."}]},
        {"type": "pronunciation", "start_time": "3.0", "end_time": "3.4", "alternatives": [{"content": "Next"}]},
        {"type": "pronunciation", "start_time": "3.4", "end_time": "3.9", "alternatives": [{"content": "line"}]},
    ]}}
    transcript = parse_transcribe_document(doc)
    assert transcript.engine == "aws_transcribe" and transcript.language == "en-US"
    assert [s["text"] for s in transcript.segments] == ["Hello there.", "Next line"]
    assert transcript.words[0]["word"] == " Hello" and transcript.words[-1]["end"] == 3.9


# -- PostgreSQL (runs only when a test database is provided) ---------------------------------------

@pytest.mark.skipif(not os.environ.get("CLIPPER_TEST_PG_URL"),
                    reason="set CLIPPER_TEST_PG_URL to a scratch PostgreSQL database to run this")
def test_postgres_schema_and_queue(app_env, monkeypatch):
    from clipper.db import Database, now_iso

    url = os.environ["CLIPPER_TEST_PG_URL"]
    db = Database(url)
    db.run("INSERT INTO users (id, email, role, disabled, created_at) VALUES ('pg-user', 'pg@example.com', 'user', 0, ?) "
           "ON CONFLICT (id) DO NOTHING", (now_iso(),))
    assert db.one("SELECT email FROM users WHERE id = ?", ("pg-user",))["email"] == "pg@example.com"
    db.run("DELETE FROM users WHERE id = 'pg-user'")
