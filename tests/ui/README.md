# Interface smoke tests

These load the real interface (`web/index.html` and `web/app.js`) in jsdom and visit each page.
They need a running server, so they are not part of `pytest`.

1. Start the app in local mode on port 8011:

       CLIPPER_PORT=8011 python -m clipper serve

2. In this folder, install and run (pass a short speech video so the transcript has real text):

       npm install
       node smoke.js /path/to/speech.mp4

3. For sign-in, start a second server with `CLIPPER_MODE=vps` on port 8012, then run `node auth-smoke.js`.

The scripts print one line per page and exit with a non-zero code on any runtime error.
