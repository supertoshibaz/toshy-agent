"""Resolve package scripts before submitting them to the normal terminal policy."""

import ntpath
import posixpath
import shlex
import sys

from .store import relative_path


async def run(runtime, entry, context, path, args=None, interpreter=None, timeout=None):
    path = relative_path(path)
    args = [] if args is None else args
    interpreter = [] if interpreter is None else interpreter
    for name, values in (("args", args), ("interpreter", interpreter)):
        if not isinstance(values, list) or any(
            not isinstance(value, str) or "\x00" in value for value in values
        ):
            raise ValueError(name + " должен быть списком строк без нулевых символов.")
    if interpreter and (len(interpreter) > 32 or any(not item for item in interpreter)):
        raise ValueError("Некорректный список interpreter.")
    if not interpreter and not path.lower().endswith(".py"):
        raise ValueError(
            "Для этого типа файла укажите interpreter списком, например [bash]."
        )
    files = await runtime.bundle(entry, context, all_files=True)
    if path not in files:
        raise ValueError("Файл отсутствует в скилле: " + path)
    prepared = await runtime.prepare(entry, context, files)
    runtime.remember(
        context, entry, files["SKILL.md"].decode("utf-8-sig"), files, prepared
    )
    terminal = runtime.module.registry.loaded_modules["terminal_sandbox"].manager
    profile = terminal.store.get(prepared["environment"])
    from modules.system.terminal_sandbox.host_process import shell, ps_quote

    windows = shell(profile)[1]
    paths = ntpath if windows else posixpath
    absolute = paths.normpath(paths.join(prepared["root"], *path.split("/")))
    if not interpreter:
        interpreter = (
            [sys.executable]
            if profile["backend"] == "local"
            else ["python" if windows else "python3"]
        )
    command_args = [*interpreter, absolute, *args]
    command = (
        "& " + " ".join(ps_quote(item) for item in command_args)
        if windows
        else shlex.join(command_args)
    )
    result = await terminal.terminal_execute(
        command, context, identity=prepared["terminal_session_id"], timeout=timeout
    )
    execution = dict(prepared, executed=bool(result.get('command_sent', False)))
    execution['status'] = result.get('status', 'unknown')
    return dict(
        skill=entry["id"],
        path=path,
        resolved_path=absolute,
        execution=execution,
        command=command,
        terminal=result,
    )
