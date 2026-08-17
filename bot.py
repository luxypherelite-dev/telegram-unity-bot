"""Telegram bot for inspecting and editing Unity Texture2D assets.

The bot intentionally reads TELEGRAM_BOT_TOKEN from the environment. Never commit a
Telegram token to source control. Persistent storage is controlled by BOT_DATA_DIR.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import UnityPy
import texture2ddecoder  # noqa: F401  # registers supported texture decoders
from PIL import Image, UnidentifiedImageError
from telegram import Document, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
DATA_ROOT = Path(os.environ.get("BOT_DATA_DIR", "/tmp/telegram-unity-bot")).resolve()
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(200 * 1024 * 1024)))
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "25"))
MAX_VIEW_ITEMS = int(os.environ.get("MAX_VIEW_ITEMS", "200"))
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logger = logging.getLogger("telegram-unity-bot")

# A lock prevents two commands from serializing the same UnityPy environment at once.
SESSION_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_filename(name: str, fallback: str = "asset.bin") -> str:
    name = Path(name or fallback).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name[:180] or fallback


def safe_session_name(name: str) -> str:
    if not SESSION_NAME_RE.fullmatch(name):
        raise ValueError("Session names must be 1–48 characters: letters, numbers, _, ., or -.")
    return name


def user_root(user_id: int) -> Path:
    root = DATA_ROOT / "users" / str(user_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def session_path(user_id: int, name: str) -> Path:
    path = user_root(user_id) / "sessions" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_path(user_id: int) -> Path:
    return user_root(user_id) / "state.json"


def read_state(user_id: int) -> dict[str, Any]:
    path = state_path(user_id)
    if not path.exists():
        return {"active": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"active": None}
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read state for user %s; rebuilding it", user_id)
        return {"active": None}


def write_state(user_id: int, state: dict[str, Any]) -> None:
    path = state_path(user_id)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temp.replace(path)


def list_sessions(user_id: int) -> list[str]:
    directory = user_root(user_id) / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    return sorted(p.name for p in directory.iterdir() if p.is_dir() and SESSION_NAME_RE.fullmatch(p.name))


def active_session(user_id: int) -> tuple[str | None, Path | None]:
    state = read_state(user_id)
    active = state.get("active")
    if not isinstance(active, str) or active not in list_sessions(user_id):
        return None, None
    return active, session_path(user_id, active)


def session_lock(user_id: int, name: str) -> asyncio.Lock:
    key = (user_id, name)
    SESSION_LOCKS.setdefault(key, asyncio.Lock())
    return SESSION_LOCKS[key]


def metadata_path(session: Path) -> Path:
    return session / "session.json"


def load_metadata(session: Path) -> dict[str, Any]:
    try:
        return json.loads(metadata_path(session).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_metadata(session: Path, metadata: dict[str, Any]) -> None:
    metadata_path(session).write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def bundle_path(session: Path) -> Path | None:
    value = load_metadata(session).get("bundle")
    if not isinstance(value, str):
        return None
    path = (session / value).resolve()
    return path if path.parent == session.resolve() and path.is_file() else None


def require_active(user_id: int) -> tuple[str, Path, Path]:
    name, session = active_session(user_id)
    if not name or not session:
        raise RuntimeError("No active session. Create or switch one with /session create <name>.")
    bundle = bundle_path(session)
    if not bundle:
        raise RuntimeError("The active session has no uploaded bundle. Send a .bundle or .assets file first.")
    return name, session, bundle


def get_texture_entries(env: Any) -> list[tuple[Any, Any]]:
    entries = []
    for obj in env.objects:
        try:
            if obj.type.name == "Texture2D":
                data = obj.read()
                entries.append((obj, data))
        except Exception:
            logger.exception("Skipping unreadable object")
    return entries


def texture_name(obj: Any, data: Any) -> str:
    name = getattr(data, "m_Name", None) or getattr(data, "name", None)
    return str(name) if name else f"Asset_{getattr(obj, 'path_id', 'unknown')}"


def decode_image(data: Any) -> Image.Image | None:
    try:
        image = data.image
        return image.convert("RGBA") if image.mode != "RGBA" else image
    except Exception:
        return None


def raw_asset_bytes(obj: Any, data: Any) -> bytes:
    getter = getattr(obj, "get_raw_data", None)
    if callable(getter):
        raw = getter()
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw)
    raw = getattr(data, "image_data", None)
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    raise ValueError("No raw stream is available for this asset")


def find_texture(env: Any, requested: str) -> tuple[Any, Any] | None:
    requested_lower = requested.casefold()
    matches = []
    for obj, data in get_texture_entries(env):
        name = texture_name(obj, data)
        if name.casefold() == requested_lower or str(getattr(obj, "path_id", "")) == requested:
            matches.append((obj, data))
    return matches[0] if matches else None


def save_png(image: Image.Image, path: Path) -> None:
    image.save(path, format="PNG", optimize=True)


async def send_text(update: Update, text: str) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_text(update, HELP_TEXT)


HELP_TEXT = """Unity Texture Bot

Upload a Unity .bundle or .assets file after creating or switching to a session. The bot works only with Texture2D assets, as in the reference NOT UABE project.

Session commands:
/session create <name> — create and select a workspace
/session switch <name> — select an existing workspace
/session list — list your workspaces
/session delete <name> — delete a workspace

Asset commands:
/view — list Texture2D names and IDs in the active bundle
/export — export all readable textures as textures.zip
/export_raw — dump raw Texture2D streams as raw_assets.zip
/replace — request a PNG zip, then upload it; filenames must match texture names
/replace_one <name> — reply to this command with one PNG document
/clear — remove the active session's uploaded and generated files

All sessions are isolated by Telegram user ID and session name. Use /help at any time for this guide."""


async def session_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    args = context.args
    if not args:
        await send_text(update, "Usage: /session create|switch|list|delete <name>")
        return
    action = args[0].casefold()
    if action == "list":
        sessions = list_sessions(user_id)
        active, _ = active_session(user_id)
        if not sessions:
            await send_text(update, "You have no sessions. Create one with /session create <name>.")
            return
        rows = [f"{'*' if name == active else '-'} {name}" for name in sessions]
        await send_text(update, "Your sessions (asterisk = active):\n" + "\n".join(rows))
        return
    if action not in {"create", "switch", "delete"} or len(args) != 2:
        await send_text(update, "Usage: /session create|switch|delete <name>")
        return
    try:
        name = safe_session_name(args[1])
    except ValueError as exc:
        await send_text(update, str(exc))
        return
    sessions = list_sessions(user_id)
    target = user_root(user_id) / "sessions" / name
    if action == "create":
        if name in sessions:
            await send_text(update, f"Session '{name}' already exists.")
            return
        if len(sessions) >= MAX_SESSIONS:
            await send_text(update, f"You have reached the session limit ({MAX_SESSIONS}).")
            return
        target.mkdir(parents=True, exist_ok=False)
        save_metadata(target, {"created_at": now_iso(), "bundle": None})
        state = read_state(user_id)
        state["active"] = name
        write_state(user_id, state)
        await send_text(update, f"Created and selected session '{name}'. Send a Unity bundle now.")
    elif action == "switch":
        if name not in sessions:
            await send_text(update, f"Session '{name}' does not exist.")
            return
        state = read_state(user_id)
        state["active"] = name
        write_state(user_id, state)
        await send_text(update, f"Active session: '{name}'.")
    else:
        if name not in sessions:
            await send_text(update, f"Session '{name}' does not exist.")
            return
        shutil.rmtree(target)
        state = read_state(user_id)
        if state.get("active") == name:
            state["active"] = None
        write_state(user_id, state)
        SESSION_LOCKS.pop((user_id, name), None)
        await send_text(update, f"Deleted session '{name}' and its files.")


async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    document = message.document if message else None
    if not message or not user or not document:
        return
    if document.file_size and document.file_size > MAX_UPLOAD_BYTES:
        await send_text(update, f"File is too large. The limit is {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB.")
        return
    pending = context.user_data.get("pending_upload")
    if pending == "replace":
        await process_replace_archive(update, context, document)
        return
    try:
        name, session, _ = require_active(user.id)
    except RuntimeError as exc:
        await send_text(update, str(exc))
        return
    filename = safe_filename(document.file_name or "input.bundle", "input.bundle")
    if not filename.lower().endswith((".bundle", ".assets", ".unity3d", ".resS", ".resource")):
        await send_text(update, "Please upload a Unity .bundle, .assets, .unity3d, .resS, or .resource file.")
        return
    destination = session / "input" / filename
    destination.parent.mkdir(exist_ok=True)
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)
    telegram_file = await document.get_file()
    await telegram_file.download_to_drive(custom_path=destination)
    save_metadata(session, {**load_metadata(session), "bundle": destination.relative_to(session).as_posix(), "uploaded_at": now_iso()})
    context.user_data.pop("pending_upload", None)
    await send_text(update, f"Loaded '{filename}' into session '{name}'. Use /view, /export, or /replace.")


async def receive_photo_or_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message and update.effective_message.document:
        await receive_document(update, context)


async def view_assets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    try:
        name, _, bundle = require_active(user_id)
    except RuntimeError as exc:
        await send_text(update, str(exc))
        return
    try:
        async with session_lock(user_id, name):
            env = UnityPy.load(str(bundle))
            entries = get_texture_entries(env)
            lines = []
            for obj, data in entries[:MAX_VIEW_ITEMS]:
                width = getattr(data, "m_Width", "?")
                height = getattr(data, "m_Height", "?")
                fmt = str(getattr(data, "m_TextureFormat", "?"))
                lines.append(f"{texture_name(obj, data)} | id={obj.path_id} | {width}x{height} | {fmt}")
        if not lines:
            await send_text(update, f"No readable Texture2D assets found in session '{name}'.")
            return
        suffix = f"\nShowing first {MAX_VIEW_ITEMS}." if len(entries) > MAX_VIEW_ITEMS else ""
        await send_text(update, f"Texture2D assets in '{name}' ({len(entries)} total):\n" + "\n".join(lines) + suffix)
    except Exception as exc:
        logger.exception("View failed")
        await send_text(update, f"Could not read the bundle: {exc}")


async def export_textures(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await export_assets(update, raw=False)


async def export_raw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await export_assets(update, raw=True)


async def export_assets(update: Update, raw: bool) -> None:
    user_id = update.effective_user.id
    try:
        name, session, bundle = require_active(user_id)
    except RuntimeError as exc:
        await send_text(update, str(exc))
        return
    work = session / "exports"
    work.mkdir(exist_ok=True)
    archive_name = "raw_assets.zip" if raw else "textures.zip"
    archive = work / archive_name
    try:
        async with session_lock(user_id, name):
            env = UnityPy.load(str(bundle))
            entries = get_texture_entries(env)
            count = 0
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                for obj, data in entries:
                    asset_name = safe_filename(texture_name(obj, data), f"Asset_{obj.path_id}")
                    if raw:
                        payload = raw_asset_bytes(obj, data)
                        zf.writestr(f"{asset_name}_{obj.path_id}.bin", payload)
                    else:
                        image = decode_image(data)
                        if image is None:
                            continue
                        buf = io.BytesIO()
                        image.save(buf, format="PNG")
                        zf.writestr(f"{asset_name}_{obj.path_id}.png", buf.getvalue())
                    count += 1
        if count == 0:
            await send_text(update, "No exportable Texture2D assets were found.")
            return
        await update.effective_message.reply_document(document=archive.open("rb"), filename=archive.name, caption=f"Exported {count} asset(s) from '{name}'.")
    except Exception as exc:
        logger.exception("Export failed")
        await send_text(update, f"Export failed: {exc}")


async def replace_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        name, _, _ = require_active(update.effective_user.id)
    except RuntimeError as exc:
        await send_text(update, str(exc))
        return
    context.user_data["pending_upload"] = "replace"
    await send_text(update, f"Upload a PNG zip for session '{name}'. Each PNG filename should match a Texture2D name, with an optional _<path_id> suffix.")


def archive_candidates(filename: str) -> Iterable[str]:
    stem = Path(filename).stem.casefold()
    yield stem
    match = re.match(r"^(.*?)(?:_(-?\d+))?$", stem)
    if match and match.group(1):
        yield match.group(1)


async def process_replace_archive(update: Update, context: ContextTypes.DEFAULT_TYPE, document: Document) -> None:
    user_id = update.effective_user.id
    try:
        name, session, bundle = require_active(user_id)
    except RuntimeError as exc:
        await send_text(update, str(exc))
        return
    if not (document.file_name or "").lower().endswith(".zip"):
        await send_text(update, "The batch replacement upload must be a .zip containing PNG files.")
        return
    archive_path = session / "input" / "replacement.zip"
    archive_path.parent.mkdir(exist_ok=True)
    try:
        telegram_file = await document.get_file()
        await telegram_file.download_to_drive(custom_path=archive_path)
        async with session_lock(user_id, name):
            env = UnityPy.load(str(bundle))
            textures = get_texture_entries(env)
            by_name = {texture_name(obj, data).casefold(): (obj, data) for obj, data in textures}
            by_id = {str(obj.path_id): (obj, data) for obj, data in textures}
            replaced: list[str] = []
            skipped: list[str] = []
            with zipfile.ZipFile(archive_path) as zf:
                for info in zf.infolist():
                    if info.is_dir() or not info.filename.casefold().endswith(".png"):
                        continue
                    target = None
                    for candidate in archive_candidates(Path(info.filename).name):
                        target = by_name.get(candidate) or by_id.get(candidate)
                        if target:
                            break
                    if not target:
                        skipped.append(Path(info.filename).name)
                        continue
                    try:
                        image = Image.open(io.BytesIO(zf.read(info))).convert("RGBA")
                        target[1].image = image
                        target[1].save()
                        replaced.append(texture_name(*target))
                    except (UnidentifiedImageError, OSError, ValueError) as exc:
                        logger.warning("Skipping replacement %s: %s", info.filename, exc)
                        skipped.append(Path(info.filename).name)
            if replaced:
                bundle.write_bytes(env.file.save())
        context.user_data.pop("pending_upload", None)
        report = f"Replaced {len(replaced)} texture(s) in '{name}'."
        if skipped:
            report += f" Skipped {len(skipped)} unmatched or invalid PNG(s)."
        await send_text(update, report)
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        await send_text(update, f"Batch replacement failed: {exc}")


async def replace_one(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.reply_to_message:
        await send_text(update, "Reply to a PNG document with /replace_one <texture name>.")
        return
    if not context.args:
        await send_text(update, "Usage: reply to a PNG with /replace_one <texture name>")
        return
    document = message.reply_to_message.document
    if not document or not (document.file_name or "").lower().endswith(".png"):
        await send_text(update, "The replied message must contain a PNG document.")
        return
    requested = " ".join(context.args)
    user_id = update.effective_user.id
    try:
        name, session, bundle = require_active(user_id)
    except RuntimeError as exc:
        await send_text(update, str(exc))
        return
    try:
        async with session_lock(user_id, name):
            env = UnityPy.load(str(bundle))
            target = find_texture(env, requested)
            if not target:
                await send_text(update, f"Texture '{requested}' was not found. Use /view for exact names.")
                return
            temp = session / "input" / "single_replacement.png"
            temp.parent.mkdir(exist_ok=True)
            tg_file = await document.get_file()
            await tg_file.download_to_drive(custom_path=temp)
            image = Image.open(temp).convert("RGBA")
            target[1].image = image
            target[1].save()
            bundle.write_bytes(env.file.save())
        await send_text(update, f"Replaced '{texture_name(*target)}' in session '{name}'.")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        await send_text(update, f"Single replacement failed: {exc}")


async def clear_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    try:
        name, session, _ = require_active(user_id)
    except RuntimeError as exc:
        await send_text(update, str(exc))
        return
    async with session_lock(user_id, name):
        for child in session.iterdir():
            if child.name != "session.json":
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)
        save_metadata(session, {**load_metadata(session), "bundle": None, "cleared_at": now_iso()})
    context.user_data.pop("pending_upload", None)
    await send_text(update, f"Cleared cached files in active session '{name}'.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled update error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("An unexpected error occurred. Check the bot logs for details.")


def build_application() -> Application:
    application = Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()
    application.add_handler(CommandHandler(["start", "help"], start))
    application.add_handler(CommandHandler("session", session_command))
    application.add_handler(CommandHandler("view", view_assets))
    application.add_handler(CommandHandler("export", export_textures))
    application.add_handler(CommandHandler("export_raw", export_raw))
    application.add_handler(CommandHandler("replace", replace_command))
    application.add_handler(CommandHandler("replace_one", replace_one))
    application.add_handler(CommandHandler("clear", clear_session))
    application.add_handler(MessageHandler(filters.Document.ALL, receive_document))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required; set it as a Render environment variable.")
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    logger.info("Starting bot with data root %s", DATA_ROOT)
    build_application().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
