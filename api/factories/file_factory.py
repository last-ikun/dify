import mimetypes
import os
import urllib.parse
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from constants import AUDIO_EXTENSIONS, DOCUMENT_EXTENSIONS, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from core.file import File, FileBelongsTo, FileTransferMethod, FileType, FileUploadConfig, helpers
from core.helper import ssrf_proxy
from extensions.ext_database import db
from models import MessageFile, ToolFile, UploadFile


def build_from_message_files(
    *,
    message_files: Sequence["MessageFile"],
    tenant_id: str,
    config: FileUploadConfig,
) -> Sequence[File]:
    results = [
        build_from_message_file(message_file=file, tenant_id=tenant_id, config=config)
        for file in message_files
        if file.belongs_to != FileBelongsTo.ASSISTANT
    ]
    return results


def build_from_message_file(
    *,
    message_file: "MessageFile",
    tenant_id: str,
    config: FileUploadConfig,
):
    mapping = {
        "transfer_method": message_file.transfer_method,
        "url": message_file.url,
        "id": message_file.id,
        "type": message_file.type,
        "upload_file_id": message_file.upload_file_id,
    }
    return build_from_mapping(
        mapping=mapping,
        tenant_id=tenant_id,
        config=config,
    )


def build_from_mapping(
    *,
    mapping: Mapping[str, Any],
    tenant_id: str,
    config: FileUploadConfig | None = None,
    strict_type_validation: bool = False,
) -> File:
    transfer_method = FileTransferMethod.value_of(mapping.get("transfer_method"))

    build_functions: dict[FileTransferMethod, Callable] = {
        FileTransferMethod.LOCAL_FILE: _build_from_local_file,
        FileTransferMethod.REMOTE_URL: _build_from_remote_url,
        FileTransferMethod.TOOL_FILE: _build_from_tool_file,
    }

    build_func = build_functions.get(transfer_method)
    if not build_func:
        raise ValueError(f"Invalid file transfer method: {transfer_method}")

    file: File = build_func(
        mapping=mapping,
        tenant_id=tenant_id,
        transfer_method=transfer_method,
        strict_type_validation=strict_type_validation,
    )

    if config and not _is_file_valid_with_config(
        input_file_type=mapping.get("type", FileType.CUSTOM),
        file_extension=file.extension or "",
        file_transfer_method=file.transfer_method,
        config=config,
    ):
        raise ValueError(f"File validation failed for file: {file.filename}")

    return file


def build_from_mappings(
    *,
    mappings: Sequence[Mapping[str, Any]],
    config: FileUploadConfig | None = None,
    tenant_id: str,
    strict_type_validation: bool = False,
) -> Sequence[File]:
    # TODO(QuantumGhost): Performance concern - each mapping triggers a separate database query.
    # Implement batch processing to reduce database load when handling multiple files.
    files = [
        build_from_mapping(
            mapping=mapping,
            tenant_id=tenant_id,
            config=config,
            strict_type_validation=strict_type_validation,
        )
        for mapping in mappings
    ]

    if (
        config
        # If image config is set.
        and config.image_config
        # And the number of image files exceeds the maximum limit
        and sum(1 for _ in (filter(lambda x: x.type == FileType.IMAGE, files))) > config.image_config.number_limits
    ):
        raise ValueError(f"Number of image files exceeds the maximum limit {config.image_config.number_limits}")
    if config and config.number_limits and len(files) > config.number_limits:
        raise ValueError(f"Number of files exceeds the maximum limit {config.number_limits}")

    return files


def _build_from_local_file(
    *,
    mapping: Mapping[str, Any],
    tenant_id: str,
    transfer_method: FileTransferMethod,
    strict_type_validation: bool = False,
) -> File:
    upload_file_id = mapping.get("upload_file_id")
    if not upload_file_id:
        raise ValueError("Invalid upload file id")
    # check if upload_file_id is a valid uuid
    try:
        uuid.UUID(upload_file_id)
    except ValueError:
        raise ValueError("Invalid upload file id format")
    stmt = select(UploadFile).where(
        UploadFile.id == upload_file_id,
        UploadFile.tenant_id == tenant_id,
    )

    row = db.session.scalar(stmt)
    if row is None:
        raise ValueError("Invalid upload file")

    detected_file_type = _standardize_file_type(extension="." + row.extension, mime_type=row.mime_type)
    specified_type = mapping.get("type", "custom")

    if strict_type_validation and detected_file_type.value != specified_type:
        raise ValueError("Detected file type does not match the specified type. Please verify the file.")

    file_type = FileType(specified_type) if specified_type and specified_type != FileType.CUSTOM else detected_file_type

    return File(
        id=mapping.get("id"),
        filename=row.name,
        extension="." + row.extension,
        mime_type=row.mime_type,
        tenant_id=tenant_id,
        type=file_type,
        transfer_method=transfer_method,
        remote_url=row.source_url,
        related_id=mapping.get("upload_file_id"),
        size=row.size,
        storage_key=row.key,
    )


def _build_from_remote_url(
    *,
    mapping: Mapping[str, Any],
    tenant_id: str,
    transfer_method: FileTransferMethod,
    strict_type_validation: bool = False,
) -> File:
    upload_file_id = mapping.get("upload_file_id")
    if upload_file_id:
        try:
            uuid.UUID(upload_file_id)
        except ValueError:
            raise ValueError("Invalid upload file id format")
        stmt = select(UploadFile).where(
            UploadFile.id == upload_file_id,
            UploadFile.tenant_id == tenant_id,
        )

        upload_file = db.session.scalar(stmt)
        if upload_file is None:
            raise ValueError("Invalid upload file")

        detected_file_type = _standardize_file_type(
            extension="." + upload_file.extension, mime_type=upload_file.mime_type
        )

        specified_type = mapping.get("type")

        if strict_type_validation and specified_type and detected_file_type.value != specified_type:
            raise ValueError("Detected file type does not match the specified type. Please verify the file.")

        file_type = (
            FileType(specified_type) if specified_type and specified_type != FileType.CUSTOM else detected_file_type
        )

        return File(
            id=mapping.get("id"),
            filename=upload_file.name,
            extension="." + upload_file.extension,
            mime_type=upload_file.mime_type,
            tenant_id=tenant_id,
            type=file_type,
            transfer_method=transfer_method,
            remote_url=helpers.get_signed_file_url(upload_file_id=str(upload_file_id)),
            related_id=mapping.get("upload_file_id"),
            size=upload_file.size,
            storage_key=upload_file.key,
        )
    url = mapping.get("url") or mapping.get("remote_url")
    if not url:
        raise ValueError("Invalid file url")

    mime_type, filename, file_size = _get_remote_file_info(url)
    extension = mimetypes.guess_extension(mime_type) or ("." + filename.split(".")[-1] if "." in filename else ".bin")

    file_type = _standardize_file_type(extension=extension, mime_type=mime_type)
    if file_type.value != mapping.get("type", "custom"):
        raise ValueError("Detected file type does not match the specified type. Please verify the file.")

    return File(
        id=mapping.get("id"),
        filename=filename,
        tenant_id=tenant_id,
        type=file_type,
        transfer_method=transfer_method,
        remote_url=url,
        mime_type=mime_type,
        extension=extension,
        size=file_size,
        storage_key="",
    )


def _get_remote_file_info(url: str):
    file_size = -1
    parsed_url = urllib.parse.urlparse(url)
    url_path = parsed_url.path
    filename = os.path.basename(url_path)

    # Initialize mime_type from filename as fallback
    mime_type, _ = mimetypes.guess_type(filename)

    resp = ssrf_proxy.head(url, follow_redirects=True)
    resp = cast(httpx.Response, resp)
    if resp.status_code == httpx.codes.OK:
        if content_disposition := resp.headers.get("Content-Disposition"):
            filename = str(content_disposition.split("filename=")[-1].strip('"'))
            # Re-guess mime_type from updated filename
            mime_type, _ = mimetypes.guess_type(filename)
        file_size = int(resp.headers.get("Content-Length", file_size))

    return mime_type, filename, file_size


def _build_from_tool_file(
    *,
    mapping: Mapping[str, Any],
    tenant_id: str,
    transfer_method: FileTransferMethod,
    strict_type_validation: bool = False,
) -> File:
    tool_file = db.session.scalar(
        select(ToolFile).where(
            ToolFile.id == mapping.get("tool_file_id"),
            ToolFile.tenant_id == tenant_id,
        )
    )

    if tool_file is None:
        raise ValueError(f"ToolFile {mapping.get('tool_file_id')} not found")

    extension = "." + tool_file.file_key.split(".")[-1] if "." in tool_file.file_key else ".bin"

    detected_file_type = _standardize_file_type(extension=extension, mime_type=tool_file.mimetype)

    specified_type = mapping.get("type")

    if strict_type_validation and specified_type and detected_file_type.value != specified_type:
        raise ValueError("Detected file type does not match the specified type. Please verify the file.")

    file_type = FileType(specified_type) if specified_type and specified_type != FileType.CUSTOM else detected_file_type

    return File(
        id=mapping.get("id"),
        tenant_id=tenant_id,
        filename=tool_file.name,
        type=file_type,
        transfer_method=transfer_method,
        remote_url=tool_file.original_url,
        related_id=tool_file.id,
        extension=extension,
        mime_type=tool_file.mimetype,
        size=tool_file.size,
        storage_key=tool_file.file_key,
    )


def _is_file_valid_with_config(
    *,
    input_file_type: str,
    file_extension: str,
    file_transfer_method: FileTransferMethod,
    config: FileUploadConfig,
) -> bool:
    if (
        config.allowed_file_types
        and input_file_type not in config.allowed_file_types
        and input_file_type != FileType.CUSTOM
    ):
        return False

    if (
        input_file_type == FileType.CUSTOM
        and config.allowed_file_extensions is not None
        and file_extension not in config.allowed_file_extensions
    ):
        return False

    if input_file_type == FileType.IMAGE:
        if (
            config.image_config
            and config.image_config.transfer_methods
            and file_transfer_method not in config.image_config.transfer_methods
        ):
            return False
    elif config.allowed_file_upload_methods and file_transfer_method not in config.allowed_file_upload_methods:
        return False

    return True


def _standardize_file_type(*, extension: str = "", mime_type: str = "") -> FileType:
    """
    Infer the possible actual type of the file based on the extension and mime_type
    """
    guessed_type = None
    if extension:
        guessed_type = _get_file_type_by_extension(extension)
    if guessed_type is None and mime_type:
        guessed_type = _get_file_type_by_mimetype(mime_type)
    return guessed_type or FileType.CUSTOM


def _get_file_type_by_extension(extension: str) -> FileType | None:
    extension = extension.lstrip(".")
    if extension in IMAGE_EXTENSIONS:
        return FileType.IMAGE
    elif extension in VIDEO_EXTENSIONS:
        return FileType.VIDEO
    elif extension in AUDIO_EXTENSIONS:
        return FileType.AUDIO
    elif extension in DOCUMENT_EXTENSIONS:
        return FileType.DOCUMENT
    return None


def _get_file_type_by_mimetype(mime_type: str) -> FileType | None:
    if "image" in mime_type:
        file_type = FileType.IMAGE
    elif "video" in mime_type:
        file_type = FileType.VIDEO
    elif "audio" in mime_type:
        file_type = FileType.AUDIO
    elif "text" in mime_type or "pdf" in mime_type:
        file_type = FileType.DOCUMENT
    else:
        file_type = FileType.CUSTOM
    return file_type


def get_file_type_by_mime_type(mime_type: str) -> FileType:
    return _get_file_type_by_mimetype(mime_type) or FileType.CUSTOM


class StorageKeyLoader:
    """FileKeyLoader load the storage key from database for a list of files.
    This loader is batched, the database query count is constant regardless of the input size.
    """

    def __init__(self, session: Session, tenant_id: str) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def _load_upload_files(self, upload_file_ids: Sequence[uuid.UUID]) -> Mapping[uuid.UUID, UploadFile]:
        stmt = select(UploadFile).where(
            UploadFile.id.in_(upload_file_ids),
            UploadFile.tenant_id == self._tenant_id,
        )

        return {uuid.UUID(i.id): i for i in self._session.scalars(stmt)}

    def _load_tool_files(self, tool_file_ids: Sequence[uuid.UUID]) -> Mapping[uuid.UUID, ToolFile]:
        stmt = select(ToolFile).where(
            ToolFile.id.in_(tool_file_ids),
            ToolFile.tenant_id == self._tenant_id,
        )
        return {uuid.UUID(i.id): i for i in self._session.scalars(stmt)}

    def load_storage_keys(self, files: Sequence[File]):
        """Loads storage keys for a sequence of files by retrieving the corresponding
        `UploadFile` or `ToolFile` records from the database based on their transfer method.

        This method doesn't modify the input sequence structure but updates the `_storage_key`
        property of each file object by extracting the relevant key from its database record.

        Performance note: This is a batched operation where database query count remains constant
        regardless of input size. However, for optimal performance, input sequences should contain
        fewer than 1000 files. For larger collections, split into smaller batches and process each
        batch separately.
        """

        upload_file_ids: list[uuid.UUID] = []
        tool_file_ids: list[uuid.UUID] = []
        for file in files:
            related_model_id = file.related_id
            if file.related_id is None:
                raise ValueError("file id should not be None.")
            if file.tenant_id != self._tenant_id:
                err_msg = (
                    f"invalid file, expected tenant_id={self._tenant_id}, "
                    f"got tenant_id={file.tenant_id}, file_id={file.id}, related_model_id={related_model_id}"
                )
                raise ValueError(err_msg)
            model_id = uuid.UUID(related_model_id)

            if file.transfer_method in (FileTransferMethod.LOCAL_FILE, FileTransferMethod.REMOTE_URL):
                upload_file_ids.append(model_id)
            elif file.transfer_method == FileTransferMethod.TOOL_FILE:
                tool_file_ids.append(model_id)

        tool_files = self._load_tool_files(tool_file_ids)
        upload_files = self._load_upload_files(upload_file_ids)
        for file in files:
            model_id = uuid.UUID(file.related_id)
            if file.transfer_method in (FileTransferMethod.LOCAL_FILE, FileTransferMethod.REMOTE_URL):
                upload_file_row = upload_files.get(model_id)
                if upload_file_row is None:
                    raise ValueError(f"Upload file not found for id: {model_id}")
                file._storage_key = upload_file_row.key
            elif file.transfer_method == FileTransferMethod.TOOL_FILE:
                tool_file_row = tool_files.get(model_id)
                if tool_file_row is None:
                    raise ValueError(f"Tool file not found for id: {model_id}")
                file._storage_key = tool_file_row.file_key
