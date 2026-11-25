"""Publish and restore whole mockup versions through the Notes MCP service."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time
import uuid

from .errors import NotesError
from .service import IDENTIFIER, NotesClient, html_title

MAX_FILE = 5 * 1024 * 1024
MAX_VERSION = 25 * 1024 * 1024
MAX_FILES = 1000
BATCH_BYTES = 8 * 1024 * 1024
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_INVALID_PATH = re.compile(r"[\x00-\x1f\x7f\\%?#:]")


def check_name(name: str) -> str:
    if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
        raise NotesError("notes_input_invalid",
                         "use 1–120 lowercase letters, digits or hyphens, starting with a letter or digit")
    return name


def _valid_path(path: str) -> bool:
    if not isinstance(path, str) or _INVALID_PATH.search(path):
        return False
    try:
        return len(path.encode()) <= 512 and all(
            part not in ("", ".", "..") and not part.startswith("@") and len(part.encode()) <= 255
            for part in path.split("/")
        )
    except UnicodeError:
        return False


def _read_folder(directory: Path) -> dict[str, bytes]:
    if not directory.is_dir() or directory.is_symlink():
        raise NotesError("notes_input_invalid", f"expected a mockup directory: {directory}")
    files: dict[str, bytes] = {}
    size = 0
    root = directory.resolve()
    for folder, dirs, names, fd in os.fwalk(root, follow_symlinks=False):
        if ".git" in dirs or ".git" in names:
            raise NotesError("notes_input_invalid", "publish a folder of mockup files outside the repository checkout")
        dirs[:] = sorted(name for name in dirs if not name.startswith("."))
        for name in dirs:
            if stat.S_ISLNK(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                raise NotesError("notes_input_invalid", f"symlinked directory cannot be published: {Path(folder) / name}")
        for name in sorted(names):
            if name.startswith("."):
                continue
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise NotesError("notes_input_invalid", f"only regular files can be published: {name}")
            path = (Path(folder) / name).relative_to(root).as_posix()
            if not _valid_path(path):
                raise NotesError("notes_input_invalid", f"invalid mockup path: {path!r}")
            if info.st_size > MAX_FILE or size + info.st_size > MAX_VERSION or len(files) >= MAX_FILES:
                raise NotesError("notes_input_invalid", "mockups allow 5 MiB per file, 25 MiB and 1,000 files per version")
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(file_fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise NotesError("notes_input_invalid", f"only regular files can be published: {path}")
                data = handle.read(min(MAX_FILE, MAX_VERSION - size) + 1)
            size += len(data)
            if len(data) > MAX_FILE or size > MAX_VERSION:
                raise NotesError("notes_input_invalid", "mockup exceeds the file or version size limit")
            files[path] = data
    if not files:
        raise NotesError("notes_input_invalid", "the mockup folder has no files")
    return files


def _batches(items):
    batch = []
    size = 0
    for path, value, raw_size in items:
        # Base64 length plus room for the escaped path and JSON fields.
        cost = 4 * ((raw_size + 2) // 3) + 4096
        if batch and (size + cost > BATCH_BYTES or len(batch) == 25):
            yield batch
            batch, size = [], 0
        batch.append((path, value))
        size += cost
    if batch:
        yield batch


def _manifest(data: dict, version: int | None) -> tuple[int, dict[str, tuple[int, str]]]:
    selected, files = data.get("version"), data.get("files")
    if (type(selected) is not int or selected < 1 or (version is not None and selected != version)
            or not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES):
        raise NotesError("notes_bad_response", "the service returned no published mockup version")
    manifest = {}
    total = 0
    for entry in files:
        path, size, digest = (entry.get(field) if isinstance(entry, dict) else None
                              for field in ("path", "size_bytes", "content_hash"))
        if (not _valid_path(path) or path in manifest or type(size) is not int
                or not 0 <= size <= MAX_FILE or not isinstance(digest, str)
                or not re.fullmatch(r"[a-f0-9]{64}", digest)):
            raise NotesError("notes_bad_response", "the service returned an invalid mockup file manifest")
        total += size
        manifest[path] = size, digest
    if total > MAX_VERSION:
        raise NotesError("notes_bad_response", "the mockup file manifest exceeds the version size limit")
    return selected, manifest


def _merge_file(stage: Path, target_fd: int, path: str, *, check: bool = False) -> None:
    parts = path.split("/")
    parent_fd = os.dup(target_fd)
    try:
        for part in parts[:-1]:
            if not check:
                try:
                    os.mkdir(part, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            try:
                child_fd = os.open(part, _DIR_FLAGS, dir_fd=parent_fd)
            except FileNotFoundError:
                if check:
                    return
                raise
            os.close(parent_fd)
            parent_fd = child_fd
        if check:
            try:
                mode = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False).st_mode
            except FileNotFoundError:
                return
            if not stat.S_ISREG(mode):
                raise NotesError("notes_input_invalid", f"destination is not a regular file: {path}")
        else:
            os.replace(stage / path, parts[-1], dst_dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


class Mockups:
    def __init__(self, client: NotesClient) -> None:
        self.client = client

    def list(self, *, query: str | None = None, prefix: str | None = None) -> list[dict]:
        filters = {k: v for k, v in {"query": query, "prefix": prefix}.items() if v is not None}
        items: list[dict] = []
        while True:
            page = self.client.one("list_mockups", limit=200, offset=len(items), **filters).get("mockups")
            if (not isinstance(page, list) or len(page) > 200
                    or any(not isinstance(m, dict) or not isinstance(m.get("name"), str) for m in page)):
                raise NotesError("notes_bad_response", "list_mockups returned an invalid page")
            items.extend(page)
            if len(page) < 200:
                return items

    def get(self, name: str, *, version: int | None = None) -> dict:
        selector = {} if version is None else {"version": version}
        return self.client.one("get_mockup", name=check_name(name), **selector)

    def _put(self, **arguments) -> None:
        for attempt in range(3):
            try:
                self.client.one("put_mockup_files", **arguments)
                return
            except NotesError as exc:
                if (attempt == 2 or exc.code != "notes_tool_error"
                        or not exc.message.startswith("The asset store is busy.")):
                    raise
                time.sleep(0.25 * (attempt + 1))

    def _upload(self, name: str, files: dict[str, bytes], message: str) -> dict:
        draft = uuid.uuid4().hex
        committing = False
        try:
            for index, batch in enumerate(_batches((path, data, len(data)) for path, data in files.items())):
                self._put(name=name, draft=draft, reset=index == 0,
                          files=[{"path": path, "base64": base64.b64encode(data).decode()}
                                 for path, data in batch])
            committing = True
            return self.client.one("commit_mockup", name=name, draft=draft, message=message)
        except NotesError as exc:
            if committing and exc.code != "notes_tool_error":
                exc.message += f". Check `sb mockups versions {name}` before uploading again"
            try:
                self._put(name=name, draft=draft, reset=True, files=[])
            except NotesError:
                exc.detail["draft"] = draft
                exc.message += f". Draft cleanup could not be confirmed for {draft}"
            raise

    def publish(self, directory: Path, name: str, *, title: str | None = None,
                message: str = "", dry_run: bool = False) -> dict:
        name = check_name(name)
        files = self._prepare(directory, message)
        if dry_run:
            return {"name": name, "files": sorted(files), "dry_run": True}
        title = title or html_title(files.get("index.html", b"").decode("utf-8", errors="replace")) or name
        try:
            self.client.one("create_mockup", name=name, title=title)
        except NotesError as exc:
            if exc.code == "notes_tool_error" and exc.message == "That mockup name is already in use.":
                raise NotesError("mockup_exists",
                                 f"the name '{name}' is in use. If it is yours, use `sb mockups update {name} <dir>`") from None
            raise
        return self._upload(name, files, message)

    def update(self, name: str, directory: Path, *, message: str = "", dry_run: bool = False) -> dict:
        name = check_name(name)
        files = self._prepare(directory, message)
        if dry_run:
            return {"name": name, "files": sorted(files), "dry_run": True}
        return self._upload(name, files, message)

    @staticmethod
    def _prepare(directory: Path, message: str) -> dict[str, bytes]:
        if len(message) > 500:
            raise NotesError("notes_input_invalid", "version messages allow up to 500 characters")
        return _read_folder(directory)

    def delete(self, name: str) -> dict:
        return self.client.one("delete_mockup", name=check_name(name))

    def pull(self, name: str, directory: Path, *, version: int | None = None, force: bool = False) -> dict:
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise NotesError("notes_input_invalid", f"expected a destination directory: {directory}")
        if directory.exists() and any(directory.iterdir()) and not force:
            raise NotesError("notes_input_invalid", f"{directory} is not empty; pass --force to overwrite")
        selected, manifest = _manifest(self.get(name, version=version), version)
        directory = directory.absolute()
        directory = directory.parent.resolve() / directory.name
        directory.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".mockup-", dir=directory.parent))
        try:
            for batch in _batches((path, digest, size) for path, (size, digest) in manifest.items()):
                results = self.client.many([
                    {"tool": "get_mockup", "arguments": {"name": name, "version": selected, "path": path}}
                    for path, _ in batch
                ])
                for (path, digest), result in zip(batch, results):
                    if not result.get("ok"):
                        raise NotesError(result.get("code") or "notes_failed", result.get("error") or "download failed")
                    data = result.get("result")
                    if not isinstance(data, dict):
                        raise NotesError("notes_bad_response", "the service returned an invalid mockup file")
                    size = manifest[path][0]
                    encoded = data.get("base64")
                    try:
                        if not isinstance(encoded, str) or len(encoded) != 4 * ((size + 2) // 3):
                            raise ValueError
                        content = base64.b64decode(encoded, validate=True)
                    except ValueError:
                        raise NotesError("notes_bad_response", "the service returned an invalid mockup file") from None
                    if (data.get("path") != path or data.get("name") != name or data.get("version") != selected
                            or len(content) != size or hashlib.sha256(content).hexdigest() != digest):
                        raise NotesError("notes_bad_response", f"mockup file could not be verified: {path}")
                    file = stage / path
                    file.parent.mkdir(parents=True, exist_ok=True)
                    with file.open("xb") as handle:
                        handle.write(content)
            if force and directory.exists():
                target_fd = os.open(directory, _DIR_FLAGS)
                try:
                    for path in manifest:
                        _merge_file(stage, target_fd, path, check=True)
                    for path in manifest:
                        _merge_file(stage, target_fd, path)
                finally:
                    os.close(target_fd)
            else:
                stage.rename(directory)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        return {"name": name, "version": selected, "directory": str(directory), "files": sorted(manifest)}

    def share(self, name: str, hours: int, *, version: int | None = None) -> dict:
        selector = {} if version is None else {"version": version}
        return self.client.one("create_share_link", name=check_name(name), ttl_hours=hours, **selector)

    def revoke(self, name: str) -> dict:
        name = check_name(name)
        return {"name": name, "revoked": self.client.revoke_target_shares(name=name)}
