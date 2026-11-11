"""Explicit installation into the owner's directory, never into global site-packages."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import venv
import zipfile
from pathlib import Path

from .store import McpStore

MAX_DOWNLOAD = 256 * 1024 * 1024
MAX_EXTRACT = 1024 * 1024 * 1024


def download(url, target):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Нужна HTTP(S)-ссылка без пароля в адресе")
    request = urllib.request.Request(url, headers={"User-Agent": "Toshy-MCP/1"})
    size = 0
    with urllib.request.urlopen(request, timeout=30) as response, Path(target).open("wb") as handle:
        if urllib.parse.urlsplit(response.url).scheme not in {"http", "https"}:
            raise ValueError("Недопустимое перенаправление")
        while data := response.read(1024 * 1024):
            size += len(data)
            if size > MAX_DOWNLOAD:
                raise ValueError("Пакет превышает 256 МиБ")
            handle.write(data)


def extract_bundle(archive, destination):
    destination = Path(destination).resolve()
    with zipfile.ZipFile(archive) as bundle:
        total = 0
        for item in bundle.infolist():
            relative = Path(item.filename.replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts or ":" in item.filename:
                raise ValueError("Недопустимый путь в архиве")
            if ((item.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError("Символьные ссылки в пакете не поддерживаются")
            total += item.file_size
            if total > MAX_EXTRACT or len(bundle.infolist()) > 50000:
                raise ValueError("Архив превышает допустимый размер")
            target = (destination / relative).resolve()
            if not target.is_relative_to(destination):
                raise ValueError("Файл выходит за пределы установки")
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(item) as source, target.open("wb") as out:
                    shutil.copyfileobj(source, out)
                if item.external_attr >> 16 & 0o111:
                    target.chmod(target.stat().st_mode | 0o111)


def run(command, cwd, env=None):
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=600)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "Ошибка установки")[-4000:])
    return result.stdout


def environment(directory, *, isolated_python=False):
    env = dict(os.environ)
    if isolated_python:
        for key in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV'):
            env.pop(key, None)
    for key, relative in {"npm_config_cache": "npm-cache", "PIP_CACHE_DIR": "pip-cache",
                          "UV_CACHE_DIR": "uv/cache", "UV_TOOL_DIR": "uv/tools",
                          "UV_PYTHON_INSTALL_DIR": "uv/python", "UV_TOOL_BIN_DIR": "uv/bin",
                          "npm_config_prefix": "npm/prefix", "TMPDIR": "tmp", "TEMP": "tmp", "TMP": "tmp",
                          "HOME": "home", "USERPROFILE": "home", "APPDATA": "home/AppData/Roaming",
                          "LOCALAPPDATA": "home/AppData/Local", "XDG_CACHE_HOME": "cache",
                          "XDG_CONFIG_HOME": "config", "XDG_DATA_HOME": "data", "XDG_STATE_HOME": "state",
                          "PLAYWRIGHT_BROWSERS_PATH": "browsers/playwright", "PUPPETEER_CACHE_DIR": "browsers/puppeteer",
                          "HF_HOME": "models/huggingface", "TORCH_HOME": "models/torch",
                          "MPLCONFIGDIR": "config/matplotlib", "PYTHONPYCACHEPREFIX": "pycache"}.items():
        path = Path(directory) / relative
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    env.pop('NPM_CONFIG_CACHE', None)
    env.pop('NPM_CONFIG_PREFIX', None)
    return env


def python_environment(directory):
    folder = Path(directory) / "venv"
    venv.EnvBuilder(with_pip=False).create(folder)
    interpreter = folder / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    run([str(interpreter), '-I', '-m', 'ensurepip', '--upgrade'], directory,
        environment(Path(directory) / 'runtime', isolated_python=True))
    return interpreter


def npm_install(reference, directory, record):
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("Для этого сервера необходим Node.js с npm")
    if not re.fullmatch(r"(?:@[\w.-]+/)?[\w.-]+(?:@[\w.+-]+)?", reference):
        raise ValueError("Неверное имя npm-пакета")
    run([npm, "install", "--no-audit", "--no-fund", "--prefix", str(directory), reference], directory,
        environment(directory / 'runtime'))
    name = reference.rsplit("@", 1)[0] if "@" in reference[1:] else reference
    package = directory / "node_modules" / name
    manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
    binary = manifest.get("bin", {})
    if isinstance(binary, str):
        binary = {name.split("/")[-1]: binary}
    if len(binary) != 1:
        raise ValueError("У пакета несколько команд или нет команды запуска. Укажите command/args вручную")
    entry = (package / next(iter(binary.values()))).resolve()
    if not entry.is_relative_to(package.resolve()) or not entry.is_file():
        raise ValueError("Некорректная точка входа npm-пакета")
    record.update(command="node", args=["${__dirname}/" + entry.relative_to(directory).as_posix()],
                  installed_version=manifest.get("version", ""))


def pypi_install(reference, directory, record):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:==[A-Za-z0-9_.+-]+)?", reference):
        raise ValueError("Укажите PyPI-пакет либо пакет==версия")
    interpreter = python_environment(directory)
    env = environment(directory / 'runtime', isolated_python=True)
    run([str(interpreter), "-m", "pip", "install", reference], directory, env)
    name = reference.split("==", 1)[0]
    script = ("import importlib.metadata,json,sys; d=importlib.metadata.distribution(sys.argv[1]);"
              "print(json.dumps({'version':d.version,'commands':[x.name for x in d.entry_points "
              "if x.group=='console_scripts']}))")
    info = json.loads(run([str(interpreter), "-c", script, name], directory, env))
    if len(info["commands"]) != 1:
        raise ValueError("У пакета несколько команд или нет команды запуска. Укажите command/args вручную")
    executable = interpreter.parent / (info["commands"][0] + (".exe" if os.name == "nt" else ""))
    record.update(command="${__dirname}/" + executable.relative_to(directory).as_posix(), args=[],
                  installed_version=info["version"])


def configure_bundle(directory, record, *, runtime_directory=None):
    runtime_directory = runtime_directory or directory / 'runtime'
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    server = manifest.get("server") or {}
    config = server.get("mcp_config") or {}
    if not config.get("command"):
        raise ValueError("В manifest.json нет команды запуска")
    platforms = (manifest.get("compatibility") or {}).get("platforms", [])
    platform = "win32" if os.name == "nt" else sys.platform
    if platforms and platform not in platforms:
        raise ValueError("Пакет не поддерживает текущую ОС")
    record.update(name=manifest.get("display_name") or manifest.get("name") or record["name"],
                  installed_version=manifest.get("version", ""), command=config["command"],
                  args=config.get("args", []), env=config.get("env", {}),
                  user_config=manifest.get("user_config", {}), compatibility=manifest.get("compatibility", {}))
    record["values"] = {key: item["default"] for key, item in record["user_config"].items()
                        if "default" in item and not item.get("sensitive")}
    if server.get("type") == "python":
        interpreter = python_environment(directory)
        requirements = directory / "requirements.txt"
        if requirements.is_file():
            run([str(interpreter), "-m", "pip", "install", "-r", str(requirements)], directory,
                environment(runtime_directory, isolated_python=True))
        record["command"] = "${__dirname}/" + interpreter.relative_to(directory).as_posix()
    elif server.get("type") == "node" and (directory / "package.json").is_file() and not (directory / "node_modules").is_dir():
        npm = shutil.which("npm")
        if not npm:
            raise RuntimeError("Для этого сервера необходим Node.js с npm")
        run([npm, "ci" if (directory / "package-lock.json").is_file() else "install", "--no-audit", "--no-fund"],
            directory, environment(runtime_directory))


def registry_descriptor(data, record):
    packages = data.get("packages") or []
    supported = [x for x in packages if x.get("registryType") in {"npm", "pypi"}]
    if supported:
        item = supported[0]
        kind = item["registryType"]
        reference = item["identifier"]
        if item.get("version"):
            reference += ("@" if kind == "npm" else "==") + item["version"]
        record["source"].update(package=reference, package_kind=kind)
        args, fields = [], {}
        for index, argument in enumerate(item.get("packageArguments", [])):
            value = argument.get("value")
            if value is None:
                key = argument.get("valueHint") or argument.get("name", "arg").lstrip("-") or f"arg_{index}"
                fields[key] = {"type": "string", "title": key, "description": argument.get("description", ""),
                               "required": argument.get("isRequired", False), **({"default": argument["default"]} if "default" in argument else {})}
                value = "${user_config." + key + "}"
            if argument.get("type") == "named":
                args.append(argument["name"])
            args.append(value)
        env = {}
        for variable in item.get("environmentVariables", []):
            key = variable["name"]
            fields[key] = {"type": "string", "title": key, "description": variable.get("description", ""),
                           "required": variable.get("isRequired", False), "sensitive": variable.get("isSecret", False),
                           **({"default": variable["default"]} if "default" in variable else {})}
            env[key] = "${user_config." + key + "}"
        record.update(user_config=fields, env=env, args=args)
        record["values"] = {k: v["default"] for k, v in fields.items() if "default" in v and not v.get("sensitive")}
        return kind, reference, args
    remotes = data.get("remotes") or []
    if remotes:
        item = remotes[0]
        record.update(transport=item.get("type", "streamable-http"), url=item["url"], command="")
        for variable in item.get("headers", []):
            key = variable["name"]
            record["user_config"][key] = {"type": "string", "title": key, "description": variable.get("description", ""),
                "required": variable.get("isRequired", False), "sensitive": variable.get("isSecret", False)}
            record["headers"][key] = "${user_config." + key + "}"
        return "remote", record["url"], []
    raise ValueError("Описание не содержит поддерживаемого пакета npm/PyPI или удалённого адреса")


def install(store: McpStore, owner, reference, *, name="", transport="auto", command="", args=None):
    reference = str(reference or "").strip()
    if not reference:
        raise ValueError("Укажите источник сервера")
    record = store.create(owner, name or reference.rsplit("/", 1)[-1],
                          {"reference": reference, "kind": "manual", "version_policy": "latest"})
    directory = store.path(owner, record["id"])
    try:
        kind, package, registry_args = "", reference, None
        parsed = urllib.parse.urlsplit(reference)
        if command:
            record.update(command=command, args=list(args or []), transport="stdio")
        elif reference.startswith("npm:") or reference.startswith("pypi:"):
            kind, package = reference.split(":", 1)
        elif parsed.hostname in {"www.npmjs.com", "npmjs.com"} and parsed.path.startswith("/package/"):
            kind, package = "npm", urllib.parse.unquote(parsed.path[len("/package/"):])
        elif parsed.hostname == "pypi.org" and parsed.path.startswith("/project/"):
            parts = parsed.path.strip("/").split("/")
            kind, package = "pypi", parts[1] + ("==" + parts[2] if len(parts) > 2 else "")
        elif reference.lower().endswith((".mcpb", ".zip")) or Path(reference).is_file() and zipfile.is_zipfile(reference):
            archive = directory / "download.mcpb"
            if parsed.scheme in {"http", "https"}:
                download(reference, archive)
            else:
                shutil.copy2(reference, archive)
            extract_bundle(archive, directory)
            archive.unlink()
            configure_bundle(directory, record)
            record["source"]["kind"] = "mcpb"
        elif parsed.path.endswith(".json") or parsed.hostname == "registry.modelcontextprotocol.io" or Path(reference).is_file():
            target = directory / "source.json"
            if parsed.scheme in {"http", "https"}:
                download(reference, target)
            else:
                shutil.copy2(reference, target)
            descriptor = json.loads(target.read_text(encoding="utf-8"))
            descriptor = descriptor.get("server", descriptor)
            kind, package, registry_args = registry_descriptor(descriptor, record)
            record["source"]["kind"] = "registry"
            record["name"] = descriptor.get("title") or descriptor.get("name") or record["name"]
        elif parsed.hostname == "github.com" and transport != "streamable-http":
            git = shutil.which("git")
            if not git:
                raise RuntimeError("Для скачивания репозитория необходим git")
            target = directory / "source"
            run([git, "clone", "--depth", "1", "--", reference, str(target)], directory, environment(directory / 'runtime'))
            record["source"]["kind"] = "git"
            if (target / "manifest.json").is_file():
                configure_bundle(target, record, runtime_directory=directory / 'runtime')
                record["working_directory"] = "source"
            else:
                record["working_directory"] = "source"
                record["installation"] = "needs_command"
        elif parsed.scheme in {"http", "https"}:
            record.update(transport="sse" if transport == "sse" else "streamable-http", url=reference)
            record["source"]["kind"] = "remote"
        else:
            raise ValueError("Источник: .mcpb, server.json, npm:пакет, pypi:пакет, GitHub или URL MCP")
        if kind == "npm":
            npm_install(package, directory, record)
        elif kind == "pypi":
            pypi_install(package, directory, record)
        if kind in {"npm", "pypi"}:
            if registry_args is not None:
                record["args"].extend(registry_args)
            if record["source"]["kind"] != "registry":
                record["source"].update(kind=kind, package=package)
            pinned = ("@" in package[1:]) if kind == "npm" else ("==" in package)
            if pinned:
                record["source"]["version_policy"] = "fixed"
        if record["installation"] == "pending":
            record["installation"] = "ready"
        record["error"] = ""
    except Exception as error:
        record.update(installation="needs_command" if "Укажите command/args вручную" in str(error) else "failed", error=str(error))
    store.write(owner, record)
    return record
