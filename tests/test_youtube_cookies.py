"""Admin upload of the YouTube cookies file: accepted when it is a real cookies export, refused
otherwise, and never shown back in full."""

COOKIES = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1800000000\tSID\tsecret-value-123\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1800000000\tLOGIN_INFO\tsecret-value-456\n"
    ".example.com\tTRUE\t/\tFALSE\t1800000000\tother\tx\n"
)


def test_upload_saves_a_cookies_file_and_reports_only_a_count(client):
    res = client.post("/api/admin/youtube-cookies", files={"file": ("cookies.txt", COOKIES.encode(), "text/plain")})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["saved"] is True and body["youtube_cookies"] == 2
    assert "secret-value" not in res.text
    assert client.get("/api/admin/youtube-cookies").json()["youtube_cookies"] == 2


def test_wrong_files_are_refused(client):
    not_cookies = client.post("/api/admin/youtube-cookies", files={"file": ("a.txt", b"hello world", "text/plain")})
    assert not_cookies.status_code == 400
    no_youtube = client.post(
        "/api/admin/youtube-cookies",
        files={"file": ("c.txt", b"# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tFALSE\t1\tx\ty\n", "text/plain")},
    )
    assert no_youtube.status_code == 400 and "no YouTube" in no_youtube.json()["detail"]
    binary = client.post("/api/admin/youtube-cookies", files={"file": ("b.txt", b"\xff\xfe\x00binary", "text/plain")})
    assert binary.status_code == 400


def test_remove_deletes_the_file(client):
    client.post("/api/admin/youtube-cookies", files={"file": ("cookies.txt", COOKIES.encode(), "text/plain")})
    assert client.delete("/api/admin/youtube-cookies").json()["saved"] is False
