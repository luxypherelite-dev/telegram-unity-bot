# Telegram Unity Texture Bot

This repository contains an asynchronous Python Telegram bot inspired by [NOT UABE](https://github.com/jackolegamer1/not-uabe). It focuses on Unity `Texture2D` assets and lets each Telegram user maintain multiple named workspaces for uploaded bundles, PNG exports, raw stream dumps, and texture replacements.

## Security notice

The bot token must be supplied through the `TELEGRAM_BOT_TOKEN` environment variable. It is intentionally not included in this repository. Since a token was exposed during setup, revoke it and create a new one with BotFather before deploying.

## Features

| Area | Behavior |
| --- | --- |
| Sessions | Each Telegram user has isolated named sessions. The active session receives all bundle and asset commands. |
| Viewing | `/view` lists readable Texture2D names, path IDs, dimensions, and formats. |
| PNG export | `/export` creates and sends `textures.zip`. |
| Raw export | `/export_raw` creates and sends `raw_assets.zip` containing uncompressed asset streams where UnityPy exposes them. |
| Batch replacement | `/replace` arms the bot for a ZIP of PNGs. Each filename should match a texture name, optionally with `_pathid`. |
| Single replacement | Reply to a PNG document with `/replace_one <name>`. The name can be an exact Texture2D name or path ID. |
| Cleanup | `/clear` removes the active session’s bundle, input files, and generated exports. |

The implementation uses `UnityPy.load(...)` to read bundles, filters objects whose type is `Texture2D`, uses Pillow for PNG conversion, and serializes modifications with UnityPy’s `env.file.save()` as in the reference project.[^1]

## Commands

Use `/start` or `/help` to display the command guide. Session commands are `/session create <name>`, `/session switch <name>`, `/session list`, and `/session delete <name>`. Asset commands are `/view`, `/export`, `/export_raw`, `/replace`, `/replace_one <name>`, and `/clear`.

To upload a bundle, first create or switch to a session, then send a `.bundle`, `.assets`, `.unity3d`, `.resS`, or `.resource` document. The upload is stored in the active session. The bot does not execute uploaded files; it only parses them with UnityPy and Pillow.

## Render deployment with Docker

Create a Render **Background Worker** using this repository and choose the Docker runtime. Set the required environment variable shown below. A background worker is appropriate because Telegram long polling requires a continuously running process rather than an HTTP request handler.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Yes | None | Token issued by BotFather. |
| `BOT_DATA_DIR` | No | `/tmp/telegram-unity-bot` | Root directory for user sessions. |
| `MAX_UPLOAD_BYTES` | No | `209715200` | Maximum Telegram document size accepted by the bot. |
| `MAX_SESSIONS` | No | `25` | Maximum number of sessions per user. |
| `MAX_VIEW_ITEMS` | No | `200` | Maximum number of asset rows shown by `/view`. |

The default Render filesystem is ephemeral. If workspaces must survive restarts or redeploys, attach a persistent disk and set `BOT_DATA_DIR` to its mount path, such as `/var/data/telegram-unity-bot`. Uploaded bundles and generated ZIPs may contain sensitive game assets, so restrict access to the bot and use an appropriate retention policy.

## Local development

```bash
python3.10 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN='replace-with-a-new-token'
python bot.py
```

To build and run the container locally:

```bash
docker build -t telegram-unity-bot .
docker run --rm \
  -e TELEGRAM_BOT_TOKEN='replace-with-a-new-token' \
  -e BOT_DATA_DIR=/var/lib/telegram-unity-bot \
  telegram-unity-bot
```

## Limitations and operational notes

Only `Texture2D` assets are supported. A texture may be listed but not exportable if the installed UnityPy decoder cannot decode its format. Batch replacement deliberately reads ZIP members in memory and does not extract arbitrary paths, which prevents ZIP path traversal. Session names and uploaded filenames are sanitized, and all session file paths are constrained beneath the user’s own workspace.

The bot uses in-memory asyncio locks to serialize operations within a running process. Because Render runs one worker instance for this deployment pattern, the design keeps session metadata on disk but does not implement distributed locking or a database. If the service is later scaled horizontally, move session metadata and binary workspaces to shared object storage and add a shared lock or job queue.

[^1]: [NOT UABE reference repository](https://github.com/jackolegamer1/not-uabe), including its [README](https://raw.githubusercontent.com/jackolegamer1/not-uabe/main/README.md) and [UnityPy implementation](https://raw.githubusercontent.com/jackolegamer1/not-uabe/main/uabe.py).
