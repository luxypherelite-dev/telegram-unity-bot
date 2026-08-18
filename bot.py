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
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx
import UnityPy
import texture2ddecoder  # noqa: F401  # registers supported texture decoders
from PIL import Image, UnidentifiedImageError
import traceback

from telegram import BotCommand, Document, Update
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
HEALTH_PORT = int(os.environ.get("PORT", "10000"))
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$")

# Regex to detect URLs in text messages (supports common file-hosting services)
URL_PATTERN = re.compile(
    r"https?://[^\s<>\"']+\.(?:bundle|assets|unity3d|resS|resource|zip)"
    r"|https?://(?:drive\.google\.com|docs\.google\.com|mega\.nz|github\.com"
    r"|raw\.githubusercontent\.com|dropbox\.com|mediafire\.com|cdn\.discordapp\.com"
    r"|discord\.com/attachments)[^\s<>\"']*",
    re.IGNORECASE,
)
MAX_DOWNLOAD_BYTES = int(os.environ.get("MAX_DOWNLOAD_BYTES", str(500 * 1024 * 1024)))  # 500 MB

# ---------------------------------------------------------------------------
# Structured logging configuration
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.environ.get(
    "LOG_FORMAT",
    "%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s",
)
logging.basicConfig(format=LOG_FORMAT, level=getattr(logging, LOG_LEVEL, logging.INFO))
logger = logging.getLogger("telegram-unity-bot")

# Reduce noise from third-party libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO)

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
    # Allow files anywhere within the session directory (including subdirs like input/)
    session_resolved = session.resolve()
    try:
        path.relative_to(session_resolved)
    except ValueError:
        return None
    return path if path.is_file() else None


def require_active(user_id: int) -> tuple[str, Path, Path]:
    name, session = active_session(user_id)
    if not name or not session:
        raise RuntimeError("No active session. Create or switch one with /session create <name>.")
    bundle = bundle_path(session)
    if not bundle:
        raise RuntimeError("The active session has no uploaded bundle. Send a .bundle or .assets file first.")
    return name, session, bundle


# ---------------------------------------------------------------------------
# Universal Unity version extraction engine (Unity 3.x through Unity 6+)
# Handles UnityFS/LZ4/LZMA compression, missing TypeTrees, nested containers
# ---------------------------------------------------------------------------
def load_unity_env(bundle_file: Path) -> Any:
    """Load a Unity bundle with UnityPy, handling all compression formats.

    UnityPy.load() automatically detects and decompresses UnityFS (LZ4/LZMA)
    bundles, raw .assets files, and split resource files. This wrapper ensures
    proper error reporting if the file cannot be parsed.
    """
    try:
        env = UnityPy.load(str(bundle_file))
    except Exception as exc:
        logger.error(
            "Failed to load bundle '%s': %s (%s)",
            bundle_file.name, exc, type(exc).__name__,
        )
        raise ValueError(
            f"Could not parse '{bundle_file.name}'. "
            f"Ensure it is a valid Unity bundle/assets file. Error: {exc}"
        ) from exc

    # Validate that UnityPy found content (empty = likely unsupported compression)
    obj_count = sum(1 for _ in env.objects)
    if obj_count == 0:
        # Attempt to check if files were at least detected
        file_count = len(getattr(env, "files", {}) or {})
        logger.warning(
            "Bundle '%s' loaded but contains 0 objects (%d files detected). "
            "Possibly unsupported compression or empty bundle.",
            bundle_file.name, file_count,
        )
    else:
        logger.info(
            "Loaded bundle '%s': %d objects across %d file(s)",
            bundle_file.name, obj_count, len(getattr(env, "files", {}) or {}),
        )
    return env


def _try_read_object(obj: Any) -> Any | None:
    """Attempt to read a Unity object with multiple fallback strategies.

    Strategy order:
    1. Standard obj.read() — works when TypeTree is present
    2. obj.read_typetree() — forces TypeTree-based reading
    3. Dictionary parsing — for stripped/missing TypeTrees
    """
    # Strategy 1: Standard read
    try:
        data = obj.read()
        if data is not None:
            return data
    except Exception as exc:
        logger.debug(
            "Standard read failed for object path_id=%s type=%s: %s",
            getattr(obj, "path_id", "?"), getattr(obj.type, "name", "?"), exc,
        )

    # Strategy 2: TypeTree read (some versions need explicit call)
    try:
        if hasattr(obj, "read_typetree"):
            data = obj.read_typetree()
            if data is not None:
                return data
    except Exception as exc:
        logger.debug(
            "TypeTree read failed for path_id=%s: %s",
            getattr(obj, "path_id", "?"), exc,
        )

    # Strategy 3: Dictionary/raw parsing for stripped TypeTrees
    try:
        if hasattr(obj, "parse_as_dict"):
            data = obj.parse_as_dict()
            if data is not None:
                return data
    except Exception as exc:
        logger.debug(
            "Dict parse failed for path_id=%s: %s",
            getattr(obj, "path_id", "?"), exc,
        )

    try:
        if hasattr(obj, "parse_as_object"):
            data = obj.parse_as_object()
            if data is not None:
                return data
    except Exception as exc:
        logger.debug(
            "Object parse failed for path_id=%s: %s",
            getattr(obj, "path_id", "?"), exc,
        )

    return None


def _is_texture2d(obj: Any) -> bool:
    """Check if an object is a Texture2D, handling various Unity versions."""
    try:
        type_name = getattr(obj.type, "name", None) or ""
        if type_name == "Texture2D":
            return True
        # Some versions use class_id instead of name
        class_id = getattr(obj, "class_id", None)
        if class_id == 28:  # Texture2D class ID in Unity
            return True
    except Exception:
        pass
    return False


def get_texture_entries(env: Any) -> list[tuple[Any, Any]]:
    """Recursively crawl all objects across all files and containers.

    Handles:
    - Top-level objects in env.objects
    - Nested assets in sub-containers (env.files)
    - Objects accessible via container paths
    - Missing TypeTree fallback parsing
    """
    entries = []
    seen_ids: set[int] = set()  # Avoid duplicates from multiple access paths

    def _process_object(obj: Any) -> None:
        """Process a single object, attempting to read it as Texture2D."""
        obj_id = id(obj)
        if obj_id in seen_ids:
            return
        seen_ids.add(obj_id)

        if not _is_texture2d(obj):
            return

        data = _try_read_object(obj)
        if data is None:
            logger.warning(
                "Could not read Texture2D path_id=%s (all parse strategies failed)",
                getattr(obj, "path_id", "unknown"),
            )
            return
        entries.append((obj, data))

    # Pass 1: Iterate all objects directly (covers most cases)
    for obj in env.objects:
        try:
            _process_object(obj)
        except Exception as exc:
            logger.debug("Error processing object: %s", exc)

    # Pass 2: Recursively crawl sub-files/containers for nested assets
    files = getattr(env, "files", None)
    if files:
        file_collection = files.values() if isinstance(files, dict) else files
        for sub_file in file_collection:
            sub_objects = getattr(sub_file, "objects", None)
            if sub_objects:
                obj_iter = sub_objects.values() if isinstance(sub_objects, dict) else sub_objects
                for obj in obj_iter:
                    try:
                        _process_object(obj)
                    except Exception as exc:
                        logger.debug("Error in sub-file object: %s", exc)

    # Pass 3: Check container paths (some Unity builds nest textures in asset maps)
    container = getattr(env, "container", None)
    if container:
        container_items = container.items() if isinstance(container, dict) else []
        for path, obj_info in container_items:
            try:
                obj = getattr(obj_info, "asset", None) or obj_info
                if hasattr(obj, "type"):
                    _process_object(obj)
            except Exception as exc:
                logger.debug("Error in container path '%s': %s", path, exc)

    if not entries:
        logger.warning(
            "No Texture2D assets found after exhaustive crawl "
            "(checked %d unique objects)",
            len(seen_ids),
        )

    return entries


def texture_name(obj: Any, data: Any) -> str:
    """Extract texture name with multiple fallback paths."""
    # Try standard name fields
    name = getattr(data, "m_Name", None) or getattr(data, "name", None)
    if name and str(name).strip():
        return str(name)

    # Try dictionary-style access (for dict-parsed objects)
    if isinstance(data, dict):
        name = data.get("m_Name") or data.get("name")
        if name and str(name).strip():
            return str(name)

    # Try container path as name source
    container_path = getattr(obj, "container", None)
    if container_path:
        return Path(str(container_path)).stem

    return f"Asset_{getattr(obj, 'path_id', 'unknown')}"


def decode_image(data: Any) -> Image.Image | None:
    """Decode a Texture2D to PIL Image with fallback for various formats."""
    # Standard .image property (works for most versions)
    try:
        image = data.image
        if image is not None:
            return image.convert("RGBA") if image.mode != "RGBA" else image
    except Exception as exc:
        logger.debug("Standard image decode failed: %s", exc)

    # Fallback: try to manually decode from raw image data
    try:
        width = getattr(data, "m_Width", 0) or 0
        height = getattr(data, "m_Height", 0) or 0
        image_data = getattr(data, "image_data", None)
        if width > 0 and height > 0 and image_data:
            # Attempt to create image from raw RGBA bytes
            if len(image_data) == width * height * 4:
                image = Image.frombytes("RGBA", (width, height), bytes(image_data))
                return image
    except Exception as exc:
        logger.debug("Manual image decode fallback failed: %s", exc)

    return None


def raw_asset_bytes(obj: Any, data: Any) -> bytes:
    """Extract raw binary data from a texture object with multiple strategies."""
    # Strategy 1: get_raw_data method
    getter = getattr(obj, "get_raw_data", None)
    if callable(getter):
        try:
            raw = getter()
            if isinstance(raw, (bytes, bytearray)) and len(raw) > 0:
                return bytes(raw)
        except Exception:
            pass

    # Strategy 2: image_data attribute
    raw = getattr(data, "image_data", None)
    if isinstance(raw, (bytes, bytearray)) and len(raw) > 0:
        return bytes(raw)

    # Strategy 3: m_StreamData or raw reader
    stream_data = getattr(data, "m_StreamData", None)
    if stream_data:
        raw = getattr(stream_data, "data", None)
        if isinstance(raw, (bytes, bytearray)) and len(raw) > 0:
            return bytes(raw)

    # Strategy 4: Read from object's raw bytes
    try:
        if hasattr(obj, "get_raw_data"):
            raw = obj.get_raw_data()
            if raw:
                return bytes(raw)
    except Exception:
        pass

    raise ValueError("No raw stream is available for this asset")


def find_texture(env: Any, requested: str) -> tuple[Any, Any] | None:
    """Find a texture by name or path_id with case-insensitive matching."""
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

You can also paste a direct download link (Google Drive, GitHub Releases, Dropbox, Mega, Discord CDN, etc.) to load bundles larger than 20 MB — bypasses Telegram's file size limit!

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


# ---------------------------------------------------------------------------
# URL-based file download (bypasses Telegram's 20 MB limit)
# ---------------------------------------------------------------------------
def extract_filename_from_url(url: str) -> str:
    """Try to extract a meaningful filename from a URL."""
    from urllib.parse import urlparse, unquote
    parsed = urlparse(url)
    path = unquote(parsed.path)
    name = Path(path).name if path else ""
    # Strip query params from name
    name = name.split("?")[0]
    if not name or "." not in name:
        return "download.bundle"
    return safe_filename(name, "download.bundle")


def resolve_google_drive_url(url: str) -> str:
    """Convert a Google Drive share/view link to a direct download URL."""
    # Handle /file/d/FILE_ID/ pattern
    match = re.search(r"/file/d/([a-zA-Z0-9_-]+)", url)
    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"
    # Handle ?id=FILE_ID pattern
    match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"
    return url


def resolve_dropbox_url(url: str) -> str:
    """Convert Dropbox share link to direct download."""
    if "dropbox.com" in url:
        url = re.sub(r"[?&]dl=0", "?dl=1", url)
        if "dl=1" not in url:
            url += "&dl=1" if "?" in url else "?dl=1"
    return url


def resolve_download_url(url: str) -> str:
    """Resolve known hosting services to direct download URLs."""
    if "drive.google.com" in url or "docs.google.com" in url:
        return resolve_google_drive_url(url)
    if "dropbox.com" in url:
        return resolve_dropbox_url(url)
    return url


async def download_from_url(url: str, destination: Path) -> int:
    """Download a file from a URL with streaming. Returns bytes written."""
    resolved_url = resolve_download_url(url)
    total_bytes = 0
    async with httpx.AsyncClient(follow_redirects=True, timeout=300.0) as client:
        async with client.stream("GET", resolved_url) as response:
            response.raise_for_status()
            # Check content-length if available
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > MAX_DOWNLOAD_BYTES:
                raise ValueError(
                    f"File too large: {int(content_length) // (1024*1024)} MB "
                    f"(limit: {MAX_DOWNLOAD_BYTES // (1024*1024)} MB)"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            with open(destination, "wb") as f:
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    total_bytes += len(chunk)
                    if total_bytes > MAX_DOWNLOAD_BYTES:
                        raise ValueError(
                            f"Download exceeded {MAX_DOWNLOAD_BYTES // (1024*1024)} MB limit"
                        )
                    f.write(chunk)
    return total_bytes


async def handle_url_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text messages containing URLs to bundle files."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not message.text:
        return

    # Find URLs in the message
    urls = URL_PATTERN.findall(message.text)
    if not urls:
        return

    url = urls[0]  # Process the first matched URL

    try:
        name, session, _ = require_active(user.id)
    except RuntimeError:
        # If no bundle exists yet, just need an active session
        session_name, session_dir = active_session(user.id)
        if not session_name or not session_dir:
            await send_text(update, "No active session. Create one first with /session create <name>.")
            return
        name = session_name
        session = session_dir

    filename = extract_filename_from_url(url)
    # Ensure it has a valid Unity extension, default to .bundle if not
    valid_extensions = (".bundle", ".assets", ".unity3d", ".ress", ".resource", ".zip")
    if not filename.lower().endswith(valid_extensions):
        filename = filename + ".bundle"

    await send_text(update, f"\u2b07\ufe0f Downloading from URL...\nFile: {filename}")
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)

    destination = session / "input" / filename
    try:
        total = await download_from_url(url, destination)
        size_mb = total / (1024 * 1024)
        logger.info(
            "Downloaded %.1f MB from URL for user %s session '%s': %s",
            size_mb, user.id, name, url[:100],
        )

        # If it's a zip for replacement context, handle differently
        if filename.lower().endswith(".zip") and context.user_data.get("pending_upload") == "replace":
            await send_text(update, f"\u2705 Downloaded {size_mb:.1f} MB. Processing as replacement archive...")
            # Simulate the replacement flow
            async with session_lock(user.id, name):
                bundle = bundle_path(session)
                if not bundle:
                    await send_text(update, "No bundle loaded in this session to replace textures in.")
                    return
                env = load_unity_env(bundle)
                textures = get_texture_entries(env)
                by_name = {texture_name(obj, data).casefold(): (obj, data) for obj, data in textures}
                by_id = {str(obj.path_id): (obj, data) for obj, data in textures}
                replaced: list[str] = []
                skipped: list[str] = []
                with zipfile.ZipFile(destination) as zf:
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
        else:
            # Treat as a bundle upload
            save_metadata(session, {
                **load_metadata(session),
                "bundle": destination.relative_to(session).as_posix(),
                "uploaded_at": now_iso(),
                "source_url": url[:500],
            })
            context.user_data.pop("pending_upload", None)
            await send_text(
                update,
                f"\u2705 Downloaded {size_mb:.1f} MB into session '{name}'.\n"
                f"File: {filename}\nUse /view, /export, or /replace."
            )
    except httpx.HTTPStatusError as exc:
        logger.error("HTTP error downloading %s: %s", url[:100], exc)
        await send_text(update, f"\u274c Download failed: HTTP {exc.response.status_code}. Check the URL is a direct download link.")
    except (httpx.RequestError, ValueError, OSError) as exc:
        logger.error("Download error for %s: %s", url[:100], exc)
        await send_text(update, f"\u274c Download failed: {exc}")


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
            env = load_unity_env(bundle)
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
            env = load_unity_env(bundle)
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
            env = load_unity_env(bundle)
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
            env = load_unity_env(bundle)
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
    """Log unhandled errors with full structured context for debugging."""
    error = context.error
    # Build structured log payload
    log_data = {
        "error_type": type(error).__name__ if error else "Unknown",
        "error_message": str(error) if error else "No error info",
        "user_id": None,
        "chat_id": None,
        "command": None,
        "message_text": None,
    }
    if isinstance(update, Update):
        if update.effective_user:
            log_data["user_id"] = update.effective_user.id
        if update.effective_chat:
            log_data["chat_id"] = update.effective_chat.id
        if update.effective_message:
            log_data["message_text"] = (update.effective_message.text or "")[:200]
            if update.effective_message.text and update.effective_message.text.startswith("/"):
                log_data["command"] = update.effective_message.text.split()[0]

    # Log full traceback with context
    tb_str = "".join(traceback.format_exception(type(error), error, error.__traceback__)) if error else "N/A"
    logger.error(
        "Unhandled exception | user=%(user_id)s | chat=%(chat_id)s | command=%(command)s | "
        "error_type=%(error_type)s | error_message=%(error_message)s",
        log_data,
    )
    logger.debug("Full traceback:\n%s", tb_str)

    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            f"\u26a0\ufe0f An error occurred: {type(error).__name__}. The issue has been logged."
        )


# ---------------------------------------------------------------------------
# Bot command definitions for Telegram auto-complete menu
# ---------------------------------------------------------------------------
BOT_COMMANDS = [
    BotCommand("start", "Show welcome message and usage guide"),
    BotCommand("help", "Display all available commands"),
    BotCommand("session", "Manage sessions: create, switch, list, delete"),
    BotCommand("view", "List Texture2D assets in the active bundle"),
    BotCommand("export", "Export all textures as PNG zip"),
    BotCommand("export_raw", "Dump raw binary asset streams as zip"),
    BotCommand("replace", "Batch-replace textures from a PNG zip"),
    BotCommand("replace_one", "Replace a single texture (reply to PNG)"),
    BotCommand("clear", "Clear cached files in the active session"),
]


async def post_init(application: Application) -> None:
    """Register bot commands with Telegram for auto-complete suggestions."""
    await application.bot.set_my_commands(BOT_COMMANDS)
    logger.info("Registered %d bot commands for auto-complete menu", len(BOT_COMMANDS))


def build_application() -> Application:
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(False)
        .post_init(post_init)
        .build()
    )
    application.add_handler(CommandHandler(["start", "help"], start))
    application.add_handler(CommandHandler("session", session_command))
    application.add_handler(CommandHandler("view", view_assets))
    application.add_handler(CommandHandler("export", export_textures))
    application.add_handler(CommandHandler("export_raw", export_raw))
    application.add_handler(CommandHandler("replace", replace_command))
    application.add_handler(CommandHandler("replace_one", replace_one))
    application.add_handler(CommandHandler("clear", clear_session))
    application.add_handler(MessageHandler(filters.Document.ALL, receive_document))
    # URL handler: detect links in text messages (must come after command handlers)
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(URL_PATTERN),
        handle_url_message,
    ))
    application.add_error_handler(error_handler)
    return application


# ---------------------------------------------------------------------------
# Lightweight HTTP keep-alive server for Render Web Service
# Uses stdlib http.server — no extra dependencies required.
# ---------------------------------------------------------------------------
def start_health_server() -> None:
    """Run a minimal HTTP server in a daemon thread so Render detects an active web service."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class KeepAliveHandler(BaseHTTPRequestHandler):
        """Responds 200 OK on any GET request (/, /health, etc.)."""

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, format: str, *args: Any) -> None:
            # Suppress default stderr request logs; use structured logger instead
            logger.debug("HTTP %s", args[0] if args else "")

    server = HTTPServer(("0.0.0.0", HEALTH_PORT), KeepAliveHandler)
    logger.info("Keep-alive HTTP server listening on 0.0.0.0:%d", HEALTH_PORT)
    server.serve_forever()


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required; set it as a Render environment variable.")
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    logger.info("Starting bot with data root %s", DATA_ROOT)

    # Start the health-check HTTP server in a background daemon thread
    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()
    logger.info("Health-check server listening on port %s", HEALTH_PORT)

    build_application().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
