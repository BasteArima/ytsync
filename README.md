<div align="center">

# ytsync

**Self-hosted YouTube playlist archiver that respects the library you already have.**

[![Publish image](https://github.com/BasteArima/ytsync/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/BasteArima/ytsync/actions/workflows/docker-publish.yml)
[![Image](https://img.shields.io/badge/ghcr.io-bastearima%2Fytsync-2f6fd0)](https://github.com/BasteArima/ytsync/pkgs/container/ytsync)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

[English](README.md) · [Русский](README.ru.md)

</div>

---

Watches your playlists on a schedule, downloads what is new, and tells you what
disappeared from YouTube — so you can see which videos your archive saved before
they were taken down.

Built on `yt-dlp`. Deliberately small: Flask + SQLite, no Elasticsearch, no
message broker, one download at a time. Idles at a few tens of MB of RAM.

## Why another one

Most YouTube archivers insist on owning your library. Pinchflat
[states plainly](https://github.com/kieraneglin/pinchflat/wiki/Frequently-Asked-Questions)
that importing an existing library *"is not currently possible and is not a
planned feature"*; TubeSync and ChannelTube do not document it either. If you
already have hundreds of videos on disk, those tools will download them all over
again.

> **ytsync treats `yt-dlp`'s own `--download-archive` file as the source of truth.**

Point it at a folder that already contains an `archive.txt` and it picks up
exactly where you left off. It also recovers video IDs from filenames shaped like
`Title [dQw4w9WgXcQ].mkv`, so a folder without an archive file is usually still
recognised. Nothing about your existing files is modified.

## Features

| | |
|---|---|
| **Scheduled sync** | Checks each playlist at the hours you choose. |
| **Honest bookkeeping** | Every video is *downloaded*, *queued*, *rescued* (gone from YouTube, still on your disk) or *missed* (gone before you got it). |
| **Ghost-entry detection** | Deleted videos stay listed in a YouTube playlist forever. ytsync recognises them instead of reporting them as permanently missing. |
| **Live progress** | Current file, percentage, pause and stop. Stopping leaves a `.part` file that resumes later instead of starting over. |
| **A queue you can see** | One download at a time by design; the queue is visible and editable, and clicking sync twice never queues the same playlist twice. |
| **Disk guard** | Refuses to start when free space drops below your threshold. |
| **Quiet hours** | Never downloads during hours you reserve for other work. Indexing still runs. |
| **Retry the unavailable** | Some failures are temporary, not deletions. |
| **Reconciliation wizard** | For files whose names carry no video ID, suggests matches by title and writes the ones you confirm into `archive.txt`. |
| **Password login** | Set on first launch, remembered for six months per browser. |
| **Russian and English** | Switchable in settings, along with theme and interface scale. |

Media-server friendly: files land in your existing folder layout and are merged
into `mkv` without re-encoding.

## Quick start

```yaml
# docker-compose.yml
services:
  ytsync:
    image: ghcr.io/bastearima/ytsync:latest
    container_name: ytsync
    restart: unless-stopped
    user: "1000:1000"          # must be able to write to your media folder
    ports:
      - "8099:8099"
    environment:
      TZ: "Europe/Moscow"
    volumes:
      - /srv/media/YouTube:/media
      - ytsync_config:/config

volumes:
  ytsync_config:
```

```sh
docker compose up -d
```

Open `http://<host>:8099`, set a password, add a playlist.

> Adding a playlist only **indexes** it. Nothing is downloaded until you press
> **Sync**, so the first run is always safe to try.

Sub-folders of `/media` are playlists. Point a new playlist at an existing folder
and its `archive.txt` is reused as-is.

### Permissions

The container writes as the `user:` you give it. Use the same UID:GID your media
server already uses, otherwise it will not be able to read what ytsync downloads.

## Configuration

Everything lives in the web UI and is stored in `/config/config.json`.

| Setting | Default | Notes |
|---|---|---|
| Run at hours | `4` | comma separated, local time (set `TZ`) |
| Quiet hours | — | no downloading during these hours |
| Minimum free space | `100` GB | sync refuses to start below this |
| Rate limit | — | passed to `yt-dlp --limit-rate`, e.g. `5M` |
| Format | — | empty means best video + best audio |
| Filename template | `%(playlist_index)s - %(title)s [%(id)s].%(ext)s` | keep `[%(id)s]` so files stay identifiable |

Cookies — only needed for private or age-restricted videos — are uploaded through
the UI. Anything outside YouTube and Google domains is stripped before saving: a
full browser cookie jar holds sessions to every site you use and has no business
on a server.

## Security

There is no TLS; this is a LAN tool. Login is a single password stored as a
PBKDF2-SHA256 hash with a random salt, and the session cookie is HMAC-signed and
`HttpOnly`. If you expose it beyond your network, put it behind a reverse proxy
that terminates TLS.

## Notes worth knowing

- **A JavaScript runtime is required** and is bundled in the image (Deno).
  YouTube challenges must be solved to obtain stream URLs; without a runtime
  `yt-dlp` sees only storyboards and reports `Requested format is not available`,
  which looks exactly like a deleted video but is not one. The `yt-dlp[default]`
  extra pulls the companion `yt-dlp-ejs` scripts.
- **Deleted videos cannot be matched by content.** There is no hash registry for
  YouTube as there is for anime; the 11-character video ID is the only reliable
  identifier. That is why the reconciliation wizard asks you to confirm rather
  than deciding on its own.
- **One download at a time**, deliberately, to stay gentle on slow home servers.
- **Server-side log lines are currently Russian only.** The interface is fully
  translated; the diagnostic log is not yet.

## Development

```sh
docker build -t ytsync .
docker run --rm -p 8099:8099 -v ./config:/config -v ./media:/media ytsync
```

- `core.py` — yt-dlp, the archive logic and playlist statistics
- `extras.py` — authentication and the reconciliation matcher
- `app.py` — queue, scheduler and HTTP layer

## Licence

MIT — see [LICENSE](LICENSE). Bundled IBM Plex fonts are licensed separately
under the SIL Open Font License 1.1.
