"""
Web Extract - Download a web page via the Lyrebird browser extension.

Extracted from the standalone Swagman project into Kelpie. This module asks
the Lyrebird browser extension (running in Edge) to load a page and download a
self-contained capture of it. Rather than cloning the site directly, the
request is relayed to the extension through the Lyrebird native-messaging
bridge, which listens on a local TCP socket.

In short:

    your Python program  <--TCP 127.0.0.1:8787-->  bridge host  <--stdio-->  Lyrebird extension

The client sends one newline-terminated JSON object::

    { "url": "https://example.com", "filename": "example-page.html" }

and the extension downloads the captured page under the given filename into the
browser's default downloads directory. The ``filename`` is used verbatim as the
download name (the bridge replaces illegal filename characters and any path
separators with ``_``), so it is a filename only, not a folder path.

The public entry point is ``run_web_extract_task(task)``, where ``task`` is a
dict with the shape::

    {
        "name": "Extract_Coles_Menu",
        "web_url": "https://www.coles.com.au/browse",
        "destination_folder_name": "coles_item_categories"
    }
"""

import os
import sys
import re
import json
import time
import shutil
import socket
from datetime import datetime
from urllib.parse import urlparse

# Resolve paths relative to the Kelpie project root (two levels up from
# Kelpie/Swagman/) so the Kelpie logger can be imported regardless of the
# working directory the caller runs from.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
_LOGGER_DIR = os.path.join(PROJECT_ROOT, "Logger")
if _LOGGER_DIR not in sys.path:
    sys.path.insert(0, _LOGGER_DIR)

import kelpie_logger as logger

SCRIPT_NAME = "web_extract.py"

# Lyrebird native-messaging bridge endpoint. The bridge host listens only on
# the loopback interface. Change the port here if you changed PORT in
# lyrebird_bridge_host.py.
BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 8787
BRIDGE_TIMEOUT_SECONDS = 15

# How long to wait for the browser extension to finish downloading the file
# into the user's Downloads folder, and how often to poll for it.
DOWNLOAD_WAIT_TIMEOUT_SECONDS = 45
DOWNLOAD_POLL_INTERVAL_SECONDS = 1

# Root folder for locally stored captures when the task does not provide a full
# destination path. Lives at the Kelpie project root (two levels up).
WEB_ROOT_DIR = os.path.join(PROJECT_ROOT, ".web")


def _sanitize_name(name: str, fallback: str) -> str:
    """Return a filesystem-safe version of a name.

    Strips characters that are invalid on common filesystems and collapses
    whitespace to underscores. Falls back to ``fallback`` if nothing remains.
    """
    if not name:
        return fallback
    # Replace path separators and illegal characters with underscores.
    safe = re.sub(r'[<>:"/\\|?*]', "_", str(name).strip())
    safe = re.sub(r"\s+", "_", safe)
    safe = safe.strip("._")
    return safe or fallback


def _timestamp() -> str:
    """Return a filesystem-safe timestamp for the current date and time."""
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def _build_download_filename(web_url: str, destination_folder_name: str) -> str:
    """Build the download filename for a captured page.

    The name combines the destination folder label, the URL host + path slug and
    a timestamp so that repeated captures stay unique and human-readable, e.g.::

        2026-09-09_14-06-11_coles_item_categories_www.coles.com.au_browse.html

    The bridge treats this value as a filename only (no folders), so the result
    intentionally contains no path separators.
    """
    label = _sanitize_name(destination_folder_name, "web_extract")

    parsed = urlparse(web_url)
    host = parsed.netloc or "page"
    path_slug = (parsed.path or "").strip("/")
    url_part = f"{host}_{path_slug}" if path_slug else host
    url_part = _sanitize_name(url_part, "page")

    filename = f"{_timestamp()}_{label}_{url_part}.html"
    # Final safety pass in case sanitization above still left path separators.
    return _sanitize_name(filename, f"web_extract_{_timestamp()}.html")


def _send_to_bridge(web_url: str, filename: str) -> dict:
    """Send a capture request to the Lyrebird bridge and return its response.

    Opens a short-lived TCP connection to the bridge, sends a single
    newline-terminated JSON request and parses the JSON acknowledgement.

    Returns:
        The parsed response dict on success. On failure it returns a dict of the
        form ``{"status": "error", "error": "<message>"}`` so callers can handle
        the outcome uniformly without needing to catch exceptions themselves.
    """
    try:
        request = json.dumps({"url": web_url, "filename": filename}) + "\n"
        with socket.create_connection(
            (BRIDGE_HOST, BRIDGE_PORT), timeout=BRIDGE_TIMEOUT_SECONDS
        ) as sock:
            sock.sendall(request.encode("utf-8"))
            response = sock.recv(4096).decode("utf-8").strip()
        return json.loads(response) if response else {}
    except (OSError, ConnectionRefusedError, socket.timeout) as e:
        return {
            "status": "error",
            "error": (
                f"Could not reach the Lyrebird bridge at "
                f"{BRIDGE_HOST}:{BRIDGE_PORT}: {type(e).__name__}: {e}. "
                f"Is Edge running with the Lyrebird extension and the bridge "
                f"host registered?"
            ),
        }
    except (ValueError, json.JSONDecodeError) as e:
        return {
            "status": "error",
            "error": f"Invalid response from the Lyrebird bridge: {type(e).__name__}: {e}",
        }


def extract_web_content(web_url: str, destination_folder_name: str) -> str | None:
    """Ask the Lyrebird extension to download ``web_url`` as a single file.

    A download filename is generated from ``destination_folder_name``, the URL
    and a timestamp, then the request is relayed to the Lyrebird extension via
    the local bridge. The extension loads the page in Edge and downloads the
    captured page under that filename into the browser's default downloads
    directory.

    Args:
        web_url: The URL whose content should be downloaded.
        destination_folder_name: Label used as part of the download filename.

    Returns:
        The download filename on success, or None on failure. Note this is a
        filename only (no folder); the browser saves it to its default downloads
        directory, and may append a suffix like ``(1)`` if the name already
        exists.
    """
    if not web_url:
        logger.log_error(SCRIPT_NAME, "No web_url provided for web extraction.")
        return None

    filename = _build_download_filename(web_url, destination_folder_name)

    logger.log_info(
        SCRIPT_NAME,
        f"Requesting Lyrebird capture of '{web_url}' as '{filename}' "
        f"via bridge {BRIDGE_HOST}:{BRIDGE_PORT}.",
    )

    result = _send_to_bridge(web_url, filename)

    status = result.get("status")
    if status == "forwarded":
        logger.log_info(
            SCRIPT_NAME,
            f"Lyrebird capture of '{web_url}' forwarded to the extension. "
            f"Download filename: '{filename}'.",
        )
        return filename

    error = result.get("error", "unknown error")
    logger.log_error(
        SCRIPT_NAME,
        f"Lyrebird bridge rejected the request for '{web_url}': {error}",
    )
    return None


def _get_downloads_dir() -> str:
    """Return the current Windows user's Downloads folder path."""
    return os.path.join(os.path.expanduser("~"), "Downloads")


def _wait_for_download(filename: str, downloads_dir: str) -> tuple[str | None, float]:
    """Poll the Downloads folder until ``filename`` appears or a timeout elapses.

    The browser may append a suffix such as `` (1)`` to the name if a file with
    the same name already exists, so the match also accepts
    ``<stem> (n)<ext>`` variants. A file is only considered done when no
    matching in-progress ``.crdownload`` partial file exists for it.

    Args:
        filename: The expected download filename (no folder).
        downloads_dir: The Downloads folder to watch.

    Returns:
        A tuple of ``(path, elapsed_seconds)`` where ``path`` is the full path to
        the downloaded file, or ``None`` if the wait timed out. ``elapsed_seconds``
        is how long the wait took.
    """
    stem, _ext = os.path.splitext(filename)
    # The extension always saves the capture as a .html file, so anchor the
    # match to a .html extension regardless of the requested name's extension.
    # Matches the exact name or a browser de-duplicated variant like
    # "name (1).html".
    pattern = re.compile(
        rf"^{re.escape(stem)}(?: \(\d+\))?\.html$",
        re.IGNORECASE,
    )

    start = time.monotonic()
    deadline = start + DOWNLOAD_WAIT_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        try:
            entries = os.listdir(downloads_dir)
        except OSError:
            entries = []

        # Skip while an in-progress partial download for our file still exists.
        in_progress = any(
            name.lower().endswith(".crdownload") and pattern.match(name[: -len(".crdownload")])
            for name in entries
        )

        if not in_progress:
            for name in entries:
                if pattern.match(name):
                    candidate = os.path.join(downloads_dir, name)
                    if os.path.isfile(candidate):
                        return candidate, time.monotonic() - start

        time.sleep(DOWNLOAD_POLL_INTERVAL_SECONDS)

    return None, time.monotonic() - start


def _resolve_destination_dir(destination_folder: str) -> str:
    """Resolve the folder the captured file should be moved into.

    If ``destination_folder`` is an absolute path (a full folder path), it is
    used as-is. Otherwise it is treated as a folder name to find or create under
    the ``.web`` folder at the project root. The chosen directory is created if
    it does not already exist.
    """
    if destination_folder and os.path.isabs(destination_folder):
        target = destination_folder
    else:
        target = os.path.join(WEB_ROOT_DIR, destination_folder or "web_extract")

    os.makedirs(target, exist_ok=True)
    return target


def run_web_extract_task(task: dict) -> str | None:
    """Execute a ``web_extract`` task.

    Sends a capture request to the Lyrebird extension, then waits for the
    extension to finish downloading the file into the user's Downloads folder.
    Once the file appears, it is moved into the requested destination folder.

    Args:
        task: The task definition dict. Expected keys:
            - web_url: URL to download.
            - destination_folder_name (or destination_folder): label used in the
              download filename and as the destination folder. A full folder
              path is used as-is; a plain name is created under ``.web``.
            - name: (optional) task name, used for logging and as a fallback
              label.

    Returns:
        The full path of the moved file on success, or None on failure/timeout.
    """
    task_name = task.get("name", "<unnamed>")
    web_url = task.get("web_url")
    destination_folder = (
        task.get("destination_folder_name")
        or task.get("destination_folder")
        or task_name
    )

    logger.log_info(SCRIPT_NAME, f"Running web_extract task '{task_name}'.")

    if not web_url:
        logger.log_error(SCRIPT_NAME, f"web_extract task '{task_name}' has no 'web_url'. Skipping.")
        return None

    # Step 1: send the capture request. The returned filename is what the
    # extension will save into the Downloads folder. A None result means the
    # request was not successfully sent to the extension.
    filename = extract_web_content(web_url, destination_folder)
    if not filename:
        logger.log_error(
            SCRIPT_NAME,
            f"web_extract task '{task_name}': request was not sent to the Lyrebird extension.",
        )
        return None

    logger.log_info(
        SCRIPT_NAME,
        f"web_extract task '{task_name}': request successfully sent to the extension "
        f"for filename '{filename}'. Waiting up to {DOWNLOAD_WAIT_TIMEOUT_SECONDS}s "
        f"for the download to complete.",
    )

    # Step 2: wait for the extension to finish the download into Downloads.
    downloads_dir = _get_downloads_dir()
    downloaded_path, elapsed = _wait_for_download(filename, downloads_dir)

    if not downloaded_path:
        logger.log_error(
            SCRIPT_NAME,
            f"web_extract task '{task_name}': timed out after "
            f"{DOWNLOAD_WAIT_TIMEOUT_SECONDS}s waiting for '{filename}' in "
            f"'{downloads_dir}'.",
        )
        return None

    logger.log_info(
        SCRIPT_NAME,
        f"web_extract task '{task_name}': file '{os.path.basename(downloaded_path)}' "
        f"downloaded in {elapsed:.1f}s.",
    )

    # Step 3: move the downloaded file into the destination folder.
    try:
        destination_dir = _resolve_destination_dir(destination_folder)
        final_path = os.path.join(destination_dir, os.path.basename(downloaded_path))
        shutil.move(downloaded_path, final_path)
    except (OSError, shutil.Error) as e:
        logger.log_error(
            SCRIPT_NAME,
            f"web_extract task '{task_name}': failed to move '{downloaded_path}' "
            f"to destination: {type(e).__name__}: {e}",
        )
        return None

    logger.log_info(
        SCRIPT_NAME,
        f"web_extract task '{task_name}': moved capture to '{final_path}'. Done.",
    )
    return final_path
