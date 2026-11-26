"""Browser video and download requests cannot send the sign-in header. These tests check the signed
links the server gives each project: they work without a header, and only for the item they name."""
import time

from test_phase3 import _bearer, _register, auth_client, auth_env  # noqa: F401  (fixtures)


def _wait(client, token, pid, states, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/projects/{pid}", headers=_bearer(token)).json()
        if body["status"] in states:
            return body
        time.sleep(0.5)
    raise AssertionError("project did not reach " + "/".join(states))


def test_signed_links_play_and_download_without_a_sign_in_header(auth_client, sample_video):
    token = _register(auth_client, "media@example.com").json()["access_token"]
    with open(sample_video, "rb") as fh:
        pid = auth_client.post("/api/projects", headers=_bearer(token),
                               files={"file": ("s.mp4", fh, "video/mp4")}).json()["id"]
    _wait(auth_client, token, pid, ("awaiting_settings", "failed"))
    auth_client.post(f"/api/projects/{pid}/generate", headers=_bearer(token),
                     json={"theme": "Funny", "clip_length": 8, "clip_count": 1})
    project = _wait(auth_client, token, pid, ("done", "failed"))
    assert project["status"] == "done", project
    clip = project["clips"][0]

    # No sign-in header: the signed link is enough for playing, downloading, and the zip.
    preview = auth_client.get(clip["video_url"])
    assert preview.status_code == 200 and preview.headers["content-type"].startswith("video/mp4")
    download = auth_client.get(clip["download_url"])
    assert download.status_code == 200 and "attachment" in download.headers.get("content-disposition", "")
    assert auth_client.get(project["zip_url"]).status_code == 200

    # A link without its signature is refused.
    assert auth_client.get(clip["video_url"].split("?")[0]).status_code == 401
    # A tampered link is refused.
    assert auth_client.get(clip["video_url"] + "x").status_code == 401
    # A link for one clip does not open another clip.
    assert auth_client.get(clip["video_url"].replace(clip["id"], "0" * 32)).status_code == 401
