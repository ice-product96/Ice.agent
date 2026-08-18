"""Persist customer images and place them in the Cursor workspace."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .config import get_settings

logger = logging.getLogger(__name__)

IMAGE_KINDS = {"image"}
CUSTOMER_FILE_KINDS = frozenset({"image", "file", "document"})
_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "image/svg+xml": ".svg",
}
_WORKSPACE_KEYS = (
    "workspacePath",
    "workspace_path",
    "workspaceFolder",
    "workspace",
    "projectPath",
    "project_path",
    "rootPath",
    "root_path",
    "cwd",
    "folder",
    "path",
)
MAX_IMAGES = 8
MAX_CUSTOMER_FILES = 8


def customer_file_relative_path(
    work_item_id: Any,
    attachment: dict[str, Any],
    *,
    index: int,
    digest: str,
) -> str:
    case = str(work_item_id or "inbox").strip() or "inbox"
    filename = (
        f"{index:02d}_{digest[:8]}_{_safe_stem(str(attachment.get('filename') or 'file'))}"
        f"{_ext_for(attachment)}"
    )
    return f"from-customer/case-{case}/{filename}"


def asset_access_token(work_item_id: Any, digest: str, secret: str) -> str:
    payload = f"{work_item_id}:{digest}".encode()
    key = (secret or "change-me").encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()[:40]


def verify_asset_access_token(
    work_item_id: Any,
    digest: str,
    token: str,
    secret: str,
) -> bool:
    if not token:
        return False
    expected = asset_access_token(work_item_id, digest, secret)
    return hmac.compare_digest(expected, token)


def asset_download_url(
    work_item_id: Any,
    digest: str,
    attachment: dict[str, Any],
    *,
    public_base_url: str,
    secret_key: str,
) -> str | None:
    base = (public_base_url or "").strip().rstrip("/")
    if not base or not digest:
        return None
    token = asset_access_token(work_item_id, digest, secret_key)
    filename = Path(str(attachment.get("filename") or "file")).name or "file"
    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "-", filename).strip("-._") or "file"
    return (
        f"{base}/api/v1/work-assets/{work_item_id}/{digest}/{safe_name}"
        f"?token={token}"
    )


def assets_root() -> Path:
    return Path(get_settings().session_dir).expanduser().resolve().parent / "work-assets"


def _ext_for(attachment: dict[str, Any]) -> str:
    name = str(attachment.get("filename") or "")
    suffix = Path(name).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".pdf", ".zip"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    mime = str(attachment.get("mime_type") or "").strip().lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    if suffix:
        return suffix
    return _MIME_EXT.get(mime, Path(name).suffix or ".bin")


def _safe_stem(name: str) -> str:
    stem = Path(name).stem
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", stem).strip("-._")
    return (cleaned or "file")[:40]


def _decode(attachment: dict[str, Any]) -> bytes | None:
    path = str(attachment.get("path") or "").strip()
    if path:
        file = Path(path)
        if file.is_file():
            try:
                return file.read_bytes()
            except OSError as exc:
                logger.warning("failed to read customer image %s: %s", file, exc)
    data_b64 = attachment.get("data_b64")
    if not data_b64:
        return None
    try:
        return base64.b64decode(data_b64)
    except Exception as exc:
        logger.warning("invalid customer image payload: %s", exc)
        return None


def _public_ref(stored: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": stored.get("kind") or "image",
        "filename": stored.get("filename"),
        "mime_type": stored.get("mime_type"),
        "path": stored.get("path"),
        "digest": stored.get("digest"),
    }


def persist_customer_images(
    item: Any,
    attachments: list[dict[str, Any]] | None,
    *,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    """Save customer images to disk and remember them on the work item."""
    if item is None or item.id is None:
        return []
    incoming = [
        item_att
        for item_att in (attachments or [])
        if isinstance(item_att, dict) and str(item_att.get("kind") or "") in CUSTOMER_FILE_KINDS
    ]
    if not incoming:
        return []
    base = (root or assets_root()) / str(item.id)
    base.mkdir(parents=True, exist_ok=True)
    meta = dict(item.metadata_json or {})
    stored = [
        dict(entry)
        for entry in list(meta.get("customer_images") or [])
        if isinstance(entry, dict)
    ]
    known = {str(entry.get("digest") or "") for entry in stored}
    added: list[dict[str, Any]] = []
    for attachment in incoming:
        if len(stored) >= MAX_CUSTOMER_FILES:
            break
        raw = _decode(attachment)
        if not raw:
            continue
        digest = hashlib.sha1(raw).hexdigest()[:16]
        if digest in known:
            continue
        filename = f"{digest}_{_safe_stem(str(attachment.get('filename') or 'image'))}{_ext_for(attachment)}"
        path = base / filename
        path.write_bytes(raw)
        record = {
            "kind": "image",
            "filename": str(attachment.get("filename") or filename),
            "mime_type": str(attachment.get("mime_type") or "image/jpeg"),
            "path": str(path),
            "digest": digest,
            "size": len(raw),
        }
        stored.append(record)
        known.add(digest)
        added.append(record)
    if added:
        meta["customer_images"] = stored[-MAX_CUSTOMER_FILES:]
        item.metadata_json = meta
    return [_public_ref(entry) for entry in added]


def load_customer_images(item: Any | None) -> list[dict[str, Any]]:
    if item is None:
        return []
    loaded: list[dict[str, Any]] = []
    for entry in list((item.metadata_json or {}).get("customer_images") or []):
        if not isinstance(entry, dict):
            continue
        path = Path(str(entry.get("path") or ""))
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        loaded.append(
            {
                "kind": "image",
                "filename": entry.get("filename") or path.name,
                "mime_type": entry.get("mime_type") or "image/jpeg",
                "path": str(path),
                "digest": entry.get("digest"),
                "data_b64": base64.b64encode(raw).decode("ascii"),
            }
        )
    return loaded[:MAX_CUSTOMER_FILES]


def collect_files_for_cursor(
    context: dict[str, Any] | None,
    item: Any | None,
) -> list[dict[str, Any]]:
    """Collect customer files (images and documents) with bytes for Cursor transfer."""
    seen: set[str] = set()
    files: list[dict[str, Any]] = []
    sources = list((context or {}).get("_attachments") or [])
    sources.extend(load_customer_images(item))
    for attachment in sources:
        if not isinstance(attachment, dict):
            continue
        if str(attachment.get("kind") or "") not in CUSTOMER_FILE_KINDS:
            continue
        raw = _decode(attachment)
        if not raw:
            continue
        digest = str(attachment.get("digest") or hashlib.sha1(raw).hexdigest()[:16])
        if digest in seen:
            continue
        seen.add(digest)
        payload = dict(attachment)
        payload["digest"] = digest
        if not payload.get("data_b64"):
            payload["data_b64"] = base64.b64encode(raw).decode("ascii")
        files.append(payload)
        if len(files) >= MAX_CUSTOMER_FILES:
            break
    return files


def collect_images_for_cursor(
    context: dict[str, Any] | None,
    item: Any | None,
) -> list[dict[str, Any]]:
    return [
        item
        for item in collect_files_for_cursor(context, item)
        if str(item.get("kind") or "") in IMAGE_KINDS
    ]


def _coerce_path(value: Any) -> Path | None:
    text = str(value or "").strip()
    if not text or text in {"/", ".", "~"}:
        return None
    if text.startswith("file:"):
        parsed = urlparse(text)
        text = unquote(parsed.path or "")
        if text.startswith("/") and len(text) > 2 and text[2] == ":":
            text = text[1:]
    path = Path(text)
    if path.is_dir():
        return path
    if path.is_file():
        return path.parent
    return None


def extract_workspace_path(status: Any) -> Path | None:
    data = status if isinstance(status, dict) else None
    if data is None:
        return None
    for key in _WORKSPACE_KEYS:
        found = _coerce_path(data.get(key))
        if found is not None:
            return found
    windows = data.get("windows") or data.get("targets") or []
    if isinstance(windows, list):
        for window in windows:
            if not isinstance(window, dict):
                continue
            for key in (*_WORKSPACE_KEYS, "workspaceUri", "folderUri"):
                found = _coerce_path(window.get(key))
                if found is not None:
                    return found
    return None


def materialize_cursor_images(
    status: Any,
    prompt: str,
    attachments: list[dict[str, Any]] | None,
    *,
    work_item_id: Any = None,
) -> tuple[str, list[str]]:
    """Copy customer images into the Cursor workspace and mention them in the prompt."""
    images = [
        item
        for item in (attachments or [])
        if isinstance(item, dict) and str(item.get("kind") or "") in IMAGE_KINDS
    ]
    if not images:
        return prompt, []
    workspace = extract_workspace_path(status)
    case = str(work_item_id or "inbox").strip() or "inbox"
    dest_dir = (workspace / "from-customer" / f"case-{case}") if workspace is not None else None
    saved: list[str] = []
    for index, attachment in enumerate(images[:MAX_IMAGES], start=1):
        raw = _decode(attachment)
        if not raw:
            continue
        filename = (
            f"{index:02d}_{_safe_stem(str(attachment.get('filename') or 'image'))}"
            f"{_ext_for(attachment)}"
        )
        if dest_dir is not None:
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                target = dest_dir / filename
                target.write_bytes(raw)
                try:
                    display = str(target.relative_to(workspace)) if workspace is not None else str(target)
                except ValueError:
                    display = str(target)
                saved.append(display.replace("\\", "/"))
                continue
            except OSError as exc:
                logger.warning("could not copy image into Cursor workspace: %s", exc)
        fallback = str(attachment.get("path") or "").strip()
        if fallback:
            saved.append(fallback.replace("\\", "/"))
    if not saved:
        return prompt, []
    lines = [
        (prompt or "").rstrip(),
        "",
        "The customer attached image file(s) that MUST be used for this task.",
        "They are already saved in the project. Open each file and use it — do not invent a placeholder.",
    ]
    for path in saved:
        lines.append(f"- {path}")
    return "\n".join(lines).strip(), saved
