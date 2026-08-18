"""Transfer customer files from ice.agent server to the Cursor PC workspace."""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import Any

from .cursor_assets import (
    CUSTOMER_FILE_KINDS,
    _decode,
    asset_access_token,
    asset_download_url,
    customer_file_relative_path,
)
from .cursorremote_drive import mcp_call

logger = logging.getLogger(__name__)

WRITE_TOOL_CANDIDATES = (
    "write_workspace_file",
    "write_file",
    "upload_workspace_file",
    "save_workspace_file",
)
MAX_SEND_PROMPT_ATTACHMENT_BYTES = 1_500_000


async def _discover_write_tool(session: Any) -> str | None:
    try:
        result = await session.list_tools()
        names = {str(item.name) for item in list(result.tools or [])}
    except Exception as exc:
        logger.debug("cursor file transfer: list_tools failed: %s", exc)
        return None
    for candidate in WRITE_TOOL_CANDIDATES:
        if candidate in names:
            return candidate
    return None


async def _write_via_mcp(
    session: Any,
    tool_name: str,
    *,
    relative_path: str,
    raw: bytes,
    mime_type: str,
) -> bool:
    payload = {
        "path": relative_path,
        "relativePath": relative_path,
        "relative_path": relative_path,
        "content_base64": base64.b64encode(raw).decode("ascii"),
        "encoding": "base64",
        "mime_type": mime_type,
        "mimeType": mime_type,
    }
    try:
        await mcp_call(session, tool_name, payload)
        return True
    except Exception as exc:
        logger.warning("cursor file transfer: %s failed for %s: %s", tool_name, relative_path, exc)
        return False


def _send_prompt_attachment(attachment: dict[str, Any], raw: bytes) -> dict[str, Any] | None:
    if len(raw) > MAX_SEND_PROMPT_ATTACHMENT_BYTES:
        return None
    mime = str(attachment.get("mime_type") or "application/octet-stream")
    return {
        "filename": str(attachment.get("filename") or "file"),
        "mime_type": mime,
        "mimeType": mime,
        "content_base64": base64.b64encode(raw).decode("ascii"),
        "encoding": "base64",
    }


def build_customer_files_prompt(
    prompt: str,
    *,
    workspace_paths: list[str],
    download_steps: list[str] | None = None,
    inline_note: str = "",
) -> str:
    lines = [(prompt or "").rstrip()]
    if workspace_paths or download_steps:
        lines.extend(["", "Customer file(s) for this task — use them, do not substitute placeholders."])
    if workspace_paths:
        lines.append("Files in the project workspace:")
        for path in workspace_paths:
            lines.append(f"- {path}")
    if download_steps:
        lines.append("")
        lines.append("If files are missing locally, download them first:")
        lines.extend(download_steps)
    if inline_note:
        lines.extend(["", inline_note])
    return "\n".join(line for line in lines if line is not None).strip()


async def deliver_customer_files_to_cursor(
    session: Any,
    attachments: list[dict[str, Any]] | None,
    *,
    work_item_id: Any = None,
    public_base_url: str = "",
    secret_key: str = "",
) -> dict[str, Any]:
    """Upload customer files onto the Cursor PC, or provide fetch instructions."""
    files: list[tuple[dict[str, Any], bytes, str]] = []
    for attachment in attachments or []:
        if not isinstance(attachment, dict):
            continue
        if str(attachment.get("kind") or "") not in CUSTOMER_FILE_KINDS:
            continue
        raw = _decode(attachment)
        if not raw:
            continue
        digest = str(attachment.get("digest") or hashlib.sha1(raw).hexdigest()[:16])
        rel = customer_file_relative_path(
            work_item_id,
            attachment,
            index=len(files) + 1,
            digest=digest,
        )
        files.append((attachment, raw, rel))

    if not files:
        return {
            "paths": [],
            "method": "none",
            "download_steps": [],
            "send_prompt_attachments": [],
            "prompt": (attachments and "") or "",
        }

    write_tool = await _discover_write_tool(session)
    workspace_paths: list[str] = []
    pending: list[tuple[dict[str, Any], bytes, str]] = []

    if write_tool:
        for attachment, raw, rel in files:
            if await _write_via_mcp(session, write_tool, relative_path=rel, raw=raw, mime_type=str(attachment.get("mime_type") or "")):
                workspace_paths.append(rel)
            else:
                pending.append((attachment, raw, rel))
    else:
        pending = list(files)

    download_steps: list[str] = []
    send_prompt_attachments: list[dict[str, Any]] = []

    for attachment, raw, rel in pending:
        digest = str(attachment.get("digest") or hashlib.sha1(raw).hexdigest()[:16])
        url = asset_download_url(
            work_item_id,
            digest,
            attachment,
            public_base_url=public_base_url,
            secret_key=secret_key,
        )
        if url:
            filename = rel.rsplit("/", 1)[-1]
            download_steps.append(
                f'curl -fsSL "{url}" -o "{rel}"'
            )
            download_steps.append(
                f'# or: Invoke-WebRequest -Uri "{url}" -OutFile "{rel}"'
            )
            workspace_paths.append(rel)
        inline = _send_prompt_attachment(attachment, raw)
        if inline is not None and str(attachment.get("kind") or "") == "image":
            send_prompt_attachments.append(inline)

    method = "mcp_write" if write_tool and not pending else "mixed"
    if not write_tool and download_steps:
        method = "download_urls"
    elif send_prompt_attachments and not workspace_paths:
        method = "send_prompt_attachments"

    inline_note = ""
    if send_prompt_attachments and method != "mcp_write":
        inline_note = (
            "Image bytes are also attached to this prompt when supported. "
            "Still save/copy them under from-customer/ in the workspace if needed."
        )

    return {
        "paths": workspace_paths,
        "method": method,
        "download_steps": download_steps,
        "send_prompt_attachments": send_prompt_attachments,
        "inline_note": inline_note,
    }
