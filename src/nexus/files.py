"""File copies between registered machines, relayed through the Nexus host."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import re
import shlex
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field

from .errors import NexusError
from .ssh import SSH_OPTIONS, ssh_target


class TextFileEdit(BaseModel):
    path: str
    content: str
    original_content: str = Field(alias="originalContent")


class FileRename(BaseModel):
    path: str
    name: str = Field(min_length=1)


class FileCreate(BaseModel):
    directory: str
    name: str = Field(min_length=1)
    is_directory: bool = Field(alias="isDirectory")


def absolute_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not path.is_absolute() or "\0" in value:
        raise NexusError("请提供机器上的绝对路径")
    return path


def entry_name(name: str) -> str:
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise NexusError("请输入文件名")
    return name


def file_info(path: Path, destination: str | None = None) -> dict:
    directory = path.is_dir()
    return {
        "name": path.name,
        "path": destination or str(path),
        "isDirectory": directory,
        "size": None if directory else path.stat().st_size,
        "mimeType": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    }


class FileTransfers:
    def __init__(self, machine, rpc):
        self.machine = machine
        self.rpc = rpc

    async def create(self, machine_id: str, request: FileCreate) -> dict:
        machine = self.machine(machine_id)
        target = absolute_path(request.directory) / entry_name(request.name)
        try:
            if machine.get("transport") == "ssh":
                command = 'mkdir -- "$1"' if request.is_directory else '(set -C; : > "$1")'
                collision = (
                    'if test -e "$1" || test -L "$1"; then '
                    'printf "%s\\n" "名称已存在：$1" >&2; exit 17; fi'
                )
                script = f"{collision}; {command} || {{ {collision}; exit 1; }}"
                await self._shell(
                    machine, shlex.join(["sh", "-c", script, "nexus-create", str(target)])
                )
            elif request.is_directory:
                await asyncio.to_thread(Path(target).mkdir)
            else:
                await asyncio.to_thread(Path(target).touch, exist_ok=False)
        except FileExistsError as exc:
            raise NexusError(f"名称已存在：{target}", status_code=409) from exc
        except FileNotFoundError as exc:
            raise NexusError(str(exc), status_code=404) from exc
        except PermissionError as exc:
            raise NexusError(str(exc), status_code=403) from exc
        except OSError as exc:
            raise NexusError(str(exc)) from exc
        return {"machineId": machine_id, "path": str(target)}

    async def rename(self, machine_id: str, request: FileRename) -> dict:
        machine = self.machine(machine_id)
        source = absolute_path(request.path)
        if not source.name:
            raise NexusError("请输入文件名")
        target = source.with_name(entry_name(request.name))
        if source == target:
            return {"machineId": machine_id, "path": str(target)}
        try:
            if machine.get("transport") == "ssh":
                script = (
                    'if test -e "$2" || test -L "$2"; then '
                    'printf "%s\\n" "名称已存在：$2" >&2; exit 17; fi; '
                    'mv -n -- "$1" "$2" || exit; '
                    'if test -e "$1" || test -L "$1"; then '
                    'printf "%s\\n" "名称已存在：$2" >&2; exit 17; fi'
                )
                await self._shell(
                    machine,
                    shlex.join(["sh", "-c", script, "nexus-rename", str(source), str(target)]),
                )
            else:

                def move():
                    destination = Path(target)
                    if destination.exists() or destination.is_symlink():
                        raise NexusError(f"名称已存在：{target}", status_code=409)
                    Path(source).rename(destination)

                await asyncio.to_thread(move)
        except FileNotFoundError as exc:
            raise NexusError(str(exc), status_code=404) from exc
        except PermissionError as exc:
            raise NexusError(str(exc), status_code=403) from exc
        except OSError as exc:
            raise NexusError(str(exc)) from exc
        return {"machineId": machine_id, "path": str(target)}

    async def _run(self, command: list[str] | tuple) -> bytes:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise NexusError(str(exc), status_code=502) from exc
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise NexusError(
                stderr.decode(errors="replace").strip() or "文件传输失败",
                status_code=409 if process.returncode == 17 else 400,
            )
        return stdout

    async def _shell(self, machine: dict, script: str) -> bytes:
        return await self._run(
            [
                "ssh",
                *SSH_OPTIONS,
                "-p",
                str(machine.get("port") or 22),
                "--",
                ssh_target(machine),
                script,
            ]
        )

    async def resolve_path(self, machine_id: str, value: str) -> PurePosixPath:
        if value == "~" or value.startswith("~/"):
            machine = self.machine(machine_id)
            home = (
                (await self._shell(machine, "printf '%s' \"$HOME\"")).decode().strip()
                if machine.get("transport") == "ssh"
                else str(Path.home())
            )
            value = str(PurePosixPath(home) / value[2:])
        return absolute_path(value)

    async def require_directory(self, machine: dict, value: str) -> None:
        path = absolute_path(value)
        not_directory = NexusError(f"项目路径不是文件夹：{value}", code="not_directory")
        if machine.get("transport") == "ssh":
            script = 'if test -d "$1"; then printf 1; elif test -e "$1" || test -L "$1"; then printf file; fi'
            exists = await self._shell(
                machine, shlex.join(["sh", "-c", script, "nexus-project", str(path)])
            )
            if exists == b"file":
                raise not_directory
        else:
            try:
                metadata = await asyncio.to_thread(Path(path).stat)
            except FileNotFoundError:
                exists = False
            except NotADirectoryError as exc:
                raise not_directory from exc
            except PermissionError as exc:
                raise NexusError(str(exc), status_code=403) from exc
            else:
                if not stat.S_ISDIR(metadata.st_mode):
                    raise not_directory
                exists = True
        if not exists:
            raise NexusError(
                f"项目目录不存在：{value}", code="directory_not_found", status_code=404
            )

    async def create_directory(self, machine: dict, value: str) -> None:
        path = absolute_path(value)
        try:
            if machine.get("transport") == "ssh":
                await self._shell(machine, shlex.join(["mkdir", "-p", "--", str(path)]))
            else:
                await asyncio.to_thread(Path(path).mkdir, parents=True, exist_ok=True)
        except PermissionError as exc:
            raise NexusError(str(exc), status_code=403) from exc
        except OSError as exc:
            raise NexusError(str(exc)) from exc

    def _remote(self, machine: dict, path: str, *, source: bool = False) -> str:
        host = machine["host"]
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        target = f"{machine['user']}@{host}" if machine.get("user") else host
        # SFTP expands source globs; these paths identify literal files.
        if source:
            path = re.sub(r"([\\*?\[\]])", r"\\\1", path)
        return f"{target}:{path}"

    async def _scp(self, machine: dict, source: str, target: str, recursive: bool = False) -> None:
        await self._run(
            [
                "scp",
                "-B",
                "-q",
                *SSH_OPTIONS,
                "-P",
                str(machine.get("port") or 22),
                *(["-r"] if recursive else []),
                "--",
                source,
                target,
            ]
        )

    async def _content_matches(self, machine: dict, paths: list[str], term: str) -> set[str]:
        if not paths:
            return set()
        script = (
            'query=$1; shift; for file in "$@"; do '
            'if grep -F -i -I -q -- "$query" "$file" 2>/dev/null; then '
            "printf '%s\\0' \"$file\"; fi; done"
        )
        command = ["sh", "-c", script, "nexus-file-search", term, *paths]
        output = (
            await self._shell(machine, shlex.join(command))
            if machine.get("transport") == "ssh"
            else await self._run(command)
        )
        return set(output.decode().split("\0"))

    async def list(
        self, machine_id: str, path: str = "~", search_term: str = "", mode: str = "title"
    ) -> dict:
        machine = self.machine(machine_id)
        directory = await self.resolve_path(machine_id, path)
        try:
            if machine.get("transport") == "ssh":
                result = await self.rpc(machine_id, "fs/readDirectory", {"path": str(directory)})
                entries = [
                    {
                        "name": entry["fileName"],
                        "path": str(directory / entry["fileName"]),
                        "isDirectory": entry["isDirectory"],
                    }
                    for entry in result["entries"]
                    if entry["isFile"] or entry["isDirectory"]
                ]
            else:

                def read():
                    try:
                        children = list(Path(directory).iterdir())
                    except (FileNotFoundError, NotADirectoryError) as exc:
                        raise NexusError(
                            str(exc), code="directory_not_found", status_code=404
                        ) from exc
                    return [
                        file_info(entry) for entry in children if entry.is_file() or entry.is_dir()
                    ]

                entries = await asyncio.to_thread(read)
        except NexusError as exc:
            if exc.code == -32603 and re.search(r"\(os error (?:2|20)\)$", exc.message):
                raise NexusError(str(exc), code="directory_not_found", status_code=404) from exc
            raise
        except PermissionError as exc:
            raise NexusError(str(exc), status_code=403) from exc
        except OSError as exc:
            raise NexusError(str(exc), status_code=502) from exc
        if term := search_term.strip():
            titles = {
                entry["path"] for entry in entries if term.casefold() in entry["name"].casefold()
            }
            matches = (
                titles
                if mode == "title"
                else await self._content_matches(
                    machine,
                    [
                        entry["path"]
                        for entry in entries
                        if not entry["isDirectory"]
                        and (mode != "both" or entry["path"] not in titles)
                    ],
                    term,
                )
            )
            if mode == "both":
                matches |= titles
            entries = [entry for entry in entries if entry["path"] in matches]
        entries.sort(key=lambda entry: (not entry["isDirectory"], entry["name"].casefold()))
        return {
            "machineId": machine_id,
            "path": str(directory),
            "parent": str(directory.parent),
            "entries": entries,
        }

    async def _prepare(
        self, machine: dict, directory: PurePosixPath, names: list[str], overwrite: bool
    ) -> None:
        if machine.get("transport") == "ssh":
            script = [shlex.join(["mkdir", "-p", "--", str(directory)])]
            if not overwrite:
                for name in names:
                    target = shlex.quote(str(directory / name))
                    message = shlex.quote(f"文件已存在：{directory / name}")
                    script.append(
                        f"if test -e {target} || test -L {target}; then "
                        f"printf '%s\\n' {message} >&2; exit 17; fi"
                    )
            await self._shell(machine, " && ".join(script))
        else:
            folder = Path(directory)
            await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
            if not overwrite:
                for name in names:
                    target = folder / name
                    if target.exists() or target.is_symlink():
                        raise NexusError(f"文件已存在：{target}", status_code=409)

    async def _put(self, source: Path, machine: dict, destination: str, recursive: bool) -> None:
        if machine.get("transport") == "ssh":
            # Copy into the parent directory so SCP preserves the source filename.
            await self._scp(
                machine,
                str(source),
                self._remote(machine, str(PurePosixPath(destination).parent)),
                recursive,
            )
        elif source.is_dir():
            await asyncio.to_thread(shutil.copytree, source, destination, dirs_exist_ok=True)
        else:
            await asyncio.to_thread(shutil.copy2, source, destination)

    async def transfer(
        self,
        source_machine_id: str,
        source_paths: list[str],
        target_machine_id: str,
        target_directory: str,
        recursive: bool = False,
        overwrite: bool = False,
    ) -> dict:
        source_machine = self.machine(source_machine_id)
        target_machine = self.machine(target_machine_id)
        sources = [absolute_path(path) for path in source_paths]
        directory = absolute_path(target_directory)
        names = [path.name for path in sources]
        if not names or any(not name for name in names) or len(set(names)) != len(names):
            raise NexusError("请选择文件，且同次传输的文件名不能重复")
        try:
            with tempfile.TemporaryDirectory(prefix="nexus-transfer-") as staging:
                files = []
                for source in sources:
                    if source_machine.get("transport") == "ssh":
                        local = Path(staging) / source.name
                        await self._scp(
                            source_machine,
                            self._remote(source_machine, str(source), source=True),
                            str(local),
                            recursive,
                        )
                    else:
                        local = Path(source)
                    if not local.is_file() and not (recursive and local.is_dir()):
                        raise NexusError(f"无法读取文件：{source}；目录传输需启用 recursive")
                    files.append(local)
                await self._prepare(target_machine, directory, names, overwrite)
                result = []
                for local in files:
                    destination = str(directory / local.name)
                    await self._put(local, target_machine, destination, recursive)
                    result.append(file_info(local, destination))
        except OSError as exc:
            raise NexusError(str(exc)) from exc
        return {
            "sourceMachineId": source_machine_id,
            "targetMachineId": target_machine_id,
            "directory": str(directory),
            "files": result,
        }

    async def upload(
        self, machine_id: str, directory: str, uploads, overwrite: bool = False
    ) -> dict:
        machine = self.machine(machine_id)
        folder = absolute_path(directory)
        names = [upload.filename or "" for upload in uploads]
        if not names or any(
            name in {"", ".", ".."} or "/" in name or "\\" in name or "\0" in name for name in names
        ):
            raise NexusError("文件名无效")
        if len(set(names)) != len(names):
            raise NexusError("同次上传的文件名不能重复")
        try:
            with tempfile.TemporaryDirectory(prefix="nexus-upload-") as staging:
                await self._prepare(machine, folder, names, overwrite)
                result = []
                for upload, name in zip(uploads, names):
                    local = Path(staging) / name

                    def save():
                        with local.open("wb") as stream:
                            shutil.copyfileobj(upload.file, stream, length=1024 * 1024)

                    await asyncio.to_thread(save)
                    destination = str(folder / name)
                    await self._put(local, machine, destination, False)
                    result.append(file_info(local, destination))
        except OSError as exc:
            raise NexusError(str(exc)) from exc
        return {"machineId": machine_id, "directory": str(folder), "files": result}

    async def download(self, machine_id: str, path: str):
        machine = self.machine(machine_id)
        source = absolute_path(path)
        staging = None
        try:
            if machine.get("transport") == "ssh":
                staging = tempfile.TemporaryDirectory(prefix="nexus-download-")
                local = Path(staging.name) / source.name
                await self._scp(
                    machine, self._remote(machine, str(source), source=True), str(local)
                )
            else:
                local = Path(source)
            if not local.is_file():
                raise NexusError("请选择可下载的文件", status_code=404)
            return local, staging
        except BaseException as exc:
            if staging:
                staging.cleanup()
            if isinstance(exc, OSError):
                raise NexusError(str(exc), status_code=404) from exc
            raise

    async def edit_text(self, machine_id: str, edit: TextFileEdit) -> dict:
        machine = self.machine(machine_id)
        path = absolute_path(edit.path)
        if "\0" in edit.content or "\0" in edit.original_content:
            raise NexusError("仅支持编辑 UTF-8 文本文件")
        content = edit.content.encode("utf-8")
        original = edit.original_content.encode("utf-8")

        def check(current: bytes):
            if current != original:
                raise NexusError(
                    "文件已被修改，请保留当前编辑并重新打开文件",
                    code="file_changed",
                    status_code=409,
                )

        def save():
            with Path(path).open("r+b") as stream:
                check(stream.read())
                stream.seek(0)
                stream.write(content)
                stream.truncate()

        try:
            if machine.get("transport") == "ssh":
                current = await self.rpc(machine_id, "fs/readFile", {"path": str(path)})
                check(base64.b64decode(current["dataBase64"]))
                await self.rpc(
                    machine_id,
                    "fs/writeFile",
                    {
                        "path": str(path),
                        "dataBase64": base64.b64encode(content).decode("ascii"),
                    },
                )
            else:
                await asyncio.to_thread(save)
        except FileNotFoundError as exc:
            raise NexusError(str(exc), status_code=404) from exc
        except PermissionError as exc:
            raise NexusError(str(exc), status_code=403) from exc
        except OSError as exc:
            raise NexusError(str(exc)) from exc
        return {"machineId": machine_id, "path": str(path)}
