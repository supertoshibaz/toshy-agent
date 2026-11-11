"""Stage module resource bundles in a selected terminal, without executing them."""

import asyncio
from pathlib import Path
import shutil
import tempfile
import os
import re


async def prepare(manager, files, version, context, profile_id="", origin=None):
    if not re.fullmatch(r"[0-9a-f]{64}", version):
        raise ValueError("Некорректная версия пакета ресурсов.")
    profile = manager.effective_profile(
        manager.store.get(profile_id) if profile_id else manager.route(context), context
    )
    if not profile["active"]:
        raise ValueError("Выбранное окружение терминала отключено.")
    if (
        profile["id"] in manager.maintenance
        or profile["id"] in manager.completion_profiles
    ):
        raise ValueError("Окружение терминала обслуживается.")
    # Authorization is scoped to this run, never persisted on a tab or inherited
    # by a different chat/subagent. The owning module chose this profile.
    manager.resource_profiles.setdefault(context.run_id, set()).add(profile["id"])
    manager.reservations.setdefault(profile["id"], set()).add(context.run_id)
    directory = manager.store.root / "resource-bundles" / version
    async with manager.resource_locks.setdefault(version, asyncio.Lock()):

        def local_copy():
            if directory.is_dir() and (directory / ".complete").is_file():
                return
            directory.parent.mkdir(parents=True, exist_ok=True)
            temp = Path(tempfile.mkdtemp(prefix=".bundle-", dir=directory.parent))
            try:
                for name, body in files.items():
                    if (
                        not isinstance(name, str)
                        or name.startswith(("/", "\\"))
                        or any(part in {"", ".", ".."} for part in name.split("/"))
                        or "\\" in name
                        or ":" in name
                    ):
                        raise ValueError("Некорректный путь ресурса.")
                    path = temp / "content" / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(body)
                    if body.startswith(b"#!"):
                        path.chmod(0o755)
                (temp / ".origin.json").write_text(
                    __import__("json").dumps(origin or {}, ensure_ascii=False),
                    encoding="utf-8",
                )
                (temp / ".complete").write_text(version, encoding="ascii")
                if directory.exists():
                    shutil.rmtree(directory)
                temp.rename(directory)
            finally:
                shutil.rmtree(temp, ignore_errors=True)

        await asyncio.to_thread(local_copy)
        tabs = [
            t
            for t in manager.terminal_list(profile["id"], context)
            if t["state"] in {"ready", "stopped"}
        ]
        public = (
            tabs[0] if tabs else manager.terminal_open(profile, context, title="Skills")
        )
        tab = manager.terminal_get(public["id"], context)
        async with manager.terminal_locks.setdefault(tab["id"], asyncio.Lock()):
            job = await manager._terminal_start(tab, context)
            if tab["state"] == "starting" and not await manager._terminal_ready(
                tab, 30
            ):
                raise ValueError(
                    "Оболочка ещё запускается. Повторите подготовку скилла после её запуска."
                )
            if tab["state"] != "ready":
                raise ValueError("Вкладка терминала занята программой.")
            if profile["backend"] == "docker":
                root = "/home/toshy/.toshy-resources"
                destination = root + "/" + version
                container = manager._name(profile["id"])
                exists, _ = await manager.cli(
                    "exec",
                    container,
                    "test",
                    "-f",
                    destination + "/.complete",
                    check=False,
                )
                if exists:
                    await manager.cli("exec", container, "mkdir", "-p", destination)
                    await manager.cli(
                        "cp",
                        str(directory / "content"),
                        container + ":" + destination,
                        timeout=90,
                    )
                    await manager.cli(
                        "cp",
                        str(directory / ".origin.json"),
                        container + ":" + destination + "/.origin.json",
                    )
                    # The marker is written only after every copy succeeds; an
                    # interrupted transfer must be retried instead of reused.
                    await manager.cli(
                        "exec", container, "touch", destination + "/.complete"
                    )
            elif profile["backend"] == "ssh":
                connection = await manager.ssh_connection(profile, job)
                sftp = await connection.start_sftp_client()
                try:
                    home = await sftp.realpath(".")
                    destination = home.rstrip("/") + "/.toshy-resources/" + version
                    if not await sftp.exists(destination + "/.complete"):
                        for name in sorted(files):
                            target = destination + "/content/" + name
                            await sftp.makedirs(target.rsplit("/", 1)[0], exist_ok=True)
                            async with sftp.open(target, "wb") as stream:
                                await stream.write(files[name])
                            if (
                                files[name].startswith(b"#!")
                                and profile["target_os"] != "windows"
                            ):
                                await sftp.chmod(target, 0o755)
                        async with sftp.open(destination + "/.complete", "w") as stream:
                            await stream.write(version)
                    if profile["target_os"] == "windows" and re.match(
                        r"^/[A-Za-z]:/", destination
                    ):
                        destination = destination[1:]
                finally:
                    sftp.exit()
            else:
                destination = str(directory.resolve())
    return dict(
        root=destination.rstrip("/\\")
        + (
            "\\content"
            if profile["backend"] == "local" and os.name == "nt"
            else "/content"
        ),
        terminal_session_id=tab["id"],
        environment=profile["id"],
        backend=profile["backend"],
        os=(
            profile.get("target_os")
            if profile["backend"] == "ssh"
            else ("linux" if profile["backend"] == "docker" else os.name)
        ),
        version=version,
        files=list(files),
        executed=False,
        origin=origin or {},
    )
