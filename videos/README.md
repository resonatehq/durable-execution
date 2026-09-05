# videos

Source video files.

## Embedding in the README

GitHub does **not** play a video referenced by repo path — `![demo](videos/demo.mp4)`
renders as a dead link, not a player. To embed one:

1. Open any issue or PR in this repo (it does not need to be submitted).
2. Drag the video file into the comment box and wait for the upload to finish.
3. GitHub replies with a `https://github.com/user-attachments/assets/...` URL.
4. Paste that URL on its own line in the README. It renders as a player.

Keep the source file here so it is versioned; use the attachment URL for display.

## Size

Anything over 50 MB triggers a GitHub warning and 100 MB is a hard push limit.
Compress before committing, or set up Git LFS if the raw file has to live here.
