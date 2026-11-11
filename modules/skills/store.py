"""Read and author skill packages without importing their executable files."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import threading
import zipfile

import yaml

MAX_FILES = 512
MAX_BYTES = 16 * 1024 * 1024
_lock = threading.RLock()


def relative_path(value):
    value = str(value)
    parts = value.split("/")
    reserved = {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
    if (
        not value
        or len(value) > 500
        or PurePosixPath(value).is_absolute()
        or re.search(r'[\\<>:"|?*\x00-\x1f]', value)
        or any(
            p in {"", ".", ".."}
            or p.endswith((" ", "."))
            or p.split(".")[0].lower() in reserved
            for p in parts
        )
    ):
        raise ValueError("Недопустимый относительный путь ресурса: " + value)
    return value


def frontmatter(text):
    match = re.match(r"\A\ufeff?---\s*\r?\n(.*?)\r?\n---(?:\s*\r?\n|\s*\Z)", text, re.S)
    if not match:
        raise ValueError("SKILL.md должен начинаться с YAML-блока name и description.")
    data = yaml.safe_load(match[1])
    if not isinstance(data, dict):
        raise ValueError("Метаданные скилла должны быть YAML-объектом.")
    name, description = data.get("name"), data.get("description")
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
        or len(name) > 64
    ):
        raise ValueError("Имя скилла: до 64 строчных латинских букв, цифры и дефисы.")
    if (
        not isinstance(description, str)
        or not description.strip()
        or len(description) > 1024
    ):
        raise ValueError("Нужно непустое описание скилла длиной до 1024 символов.")
    return data


def file_map(directory):
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError("Папка скилла не должна быть символической ссылкой.")
    files, total, folded = {}, 0, set()
    for current, folders, names in os.walk(directory, followlinks=False):
        folders[:] = [
            x for x in folders if x not in {".git", "__pycache__", ".venv", "venv"}
        ]
        for name in [*folders, *names]:
            if (Path(current) / name).is_symlink():
                raise ValueError("Ссылки внутри скилла не поддерживаются.")
        for name in sorted(names):
            path = Path(current) / name
            if name.endswith((".pyc", ".pyo")):
                continue
            relative = relative_path(path.relative_to(directory).as_posix())
            if relative.casefold() in folded:
                raise ValueError(
                    "Имена файлов различаются только регистром: " + relative
                )
            folded.add(relative.casefold())
            if not path.is_file():
                raise ValueError("Ресурс должен быть обычным файлом.")
            size = path.stat().st_size
            total += size
            if len(files) >= MAX_FILES or total > MAX_BYTES:
                raise ValueError("Скилл превышает лимит 512 файлов / 16 МиБ.")
            content = path.read_bytes()
            if len(content) != size:
                raise ValueError("Скилл изменился во время чтения. Повторите загрузку.")
            files[relative] = content
    frontmatter(files.get("SKILL.md", b"").decode("utf-8-sig"))
    return files


def digest(files):
    value = hashlib.sha256()
    for name, body in sorted(files.items()):
        value.update(name.encode("utf-8") + b"\0" + hashlib.sha256(body).digest())
    return value.hexdigest()


def has_scripts(files):
    return any(
        name.startswith("scripts/")
        or Path(name).suffix.lower()
        in {".py", ".sh", ".ps1", ".bat", ".cmd", ".js", ".mjs"}
        for name in files
    )


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


class SkillStore:
    def __init__(self, packages, owner):
        self.packages, self.owner = packages, owner
        self.root = packages.path(owner) / "skills"
        if self.root.is_symlink():
            raise ValueError("Папка скиллов не должна быть ссылкой.")
        from core.paths import APP_PATHS

        self.environment_path = APP_PATHS.runtime / "skill-environments.json"

    def settings(self):
        try:
            data = json.loads(
                (self.root / ".settings.json").read_text(encoding="utf-8")
            )
            data = data if isinstance(data, dict) else {}
        except FileNotFoundError:
            data = {}
        try:
            environments = json.loads(
                self.environment_path.read_text(encoding="utf-8")
            ).get(self.owner, {})
        except FileNotFoundError:
            environments = {}
        for key, value in environments.items():
            data.setdefault(key, {})["environment"] = value
        return data

    def configure(self, identity, **values):
        with _lock:
            settings = self.settings()
            item = settings.setdefault(identity, {})
            for key in ("enabled", "automatic", "environment"):
                if key in values:
                    item[key] = (
                        str(values[key] or "")
                        if key == "environment"
                        else bool(values[key])
                    )
            if "environment" in values:
                try:
                    environments = json.loads(
                        self.environment_path.read_text(encoding="utf-8")
                    )
                except FileNotFoundError:
                    environments = {}
                environments.setdefault(self.owner, {})[identity] = str(
                    values["environment"] or ""
                )
                atomic_json(self.environment_path, environments)
            portable = {
                key: {k: v for k, v in value.items() if k != "environment"}
                for key, value in settings.items()
            }
            atomic_json(self.root / ".settings.json", portable)

    def directory(self, name):
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", str(name)) or len(name) > 64:
            raise ValueError("Некорректное имя скилла.")
        path = self.root / name
        if self.root.is_symlink() or path.is_symlink():
            raise ValueError("Папка скиллов не должна быть ссылкой.")
        return path

    def catalog(self):
        settings, result = self.settings(), []
        for path in sorted(self.root.glob("*/SKILL.md")):
            if path.parent.name.startswith("."):
                continue
            name = path.parent.name
            entry = dict(
                id=self.owner + "/" + name,
                key=name,
                owner=self.owner,
                name=name,
                source="local",
                description="",
                scripts=False,
            )
            try:
                if self.directory(name) != path.parent or path.is_symlink():
                    raise ValueError("Недопустимый путь скилла.")
                if path.stat().st_size > MAX_BYTES:
                    raise ValueError("SKILL.md слишком большой.")
                meta = frontmatter(path.read_text(encoding="utf-8-sig"))
                if meta["name"] != name:
                    raise ValueError("name в SKILL.md должен совпадать с именем папки.")
                entry.update(
                    name=meta["name"],
                    description=meta["description"],
                    automatic=not meta.get("disable-model-invocation", False),
                )
                entry["scripts"] = any((path.parent / "scripts").rglob("*")) or any(
                    p.suffix.lower()
                    in {".py", ".sh", ".ps1", ".bat", ".cmd", ".js", ".mjs"}
                    for p in path.parent.iterdir()
                )
                platforms = meta.get("platforms")
                entry["platforms"] = platforms if isinstance(platforms, list) else []
            except Exception as error:
                entry["error"] = str(error)
            entry = {"enabled": True, "automatic": True, "environment": "", **entry}
            entry.update(settings.get(name, {}))
            result.append(entry)
        return result

    def read(self, name):
        files = file_map(self.directory(name))
        if frontmatter(files["SKILL.md"].decode("utf-8-sig"))["name"] != name:
            raise ValueError("Имя папки и name в SKILL.md не совпадают.")
        return files

    def install_files(self, files):
        if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_BYTES:
            raise ValueError("Скилл превышает лимит 512 файлов / 16 МиБ.")
        name = frontmatter(files.get("SKILL.md", b"").decode("utf-8-sig"))["name"]
        target = self.directory(name)
        self.root.mkdir(parents=True, exist_ok=True)
        with _lock:
            if target.exists():
                raise ValueError("Скилл с таким именем уже существует: " + name)
            temp = Path(tempfile.mkdtemp(prefix=".import-", dir=self.root))
            try:
                folded = set()
                for relative, content in files.items():
                    relative = relative_path(relative)
                    if relative.casefold() in folded:
                        raise ValueError("Повторяющийся путь ресурса.")
                    folded.add(relative.casefold())
                    path = temp / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                file_map(temp)
                temp.rename(target)
            finally:
                shutil.rmtree(temp, ignore_errors=True)
        return name

    def import_path(self, source):
        source = Path(source)
        if source.is_dir():
            return self.install_files(file_map(source))
        with zipfile.ZipFile(source) as archive:
            entries = [x for x in archive.infolist() if not x.is_dir()]
            if (
                len(entries) > MAX_FILES
                or sum(x.file_size for x in entries) > MAX_BYTES
            ):
                raise ValueError("ZIP превышает лимит скилла.")
            roots = [
                x.filename[: -len("SKILL.md")]
                for x in entries
                if x.filename.endswith("SKILL.md")
            ]
            if len(roots) != 1:
                raise ValueError("Выберите ZIP с одним скиллом и одним SKILL.md.")
            prefix = roots[0]
            files = {}
            for entry in entries:
                relative_path(entry.filename)
                if stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
                    raise ValueError(
                        "Ссылки и зашифрованные файлы в ZIP не поддерживаются."
                    )
                if not entry.filename.startswith(prefix):
                    raise ValueError("Все файлы ZIP должны находиться в папке скилла.")
                name = relative_path(entry.filename[len(prefix) :])
                if name in files:
                    raise ValueError("Повторяющийся файл в ZIP.")
                files[name] = archive.read(entry)
            return self.install_files(files)

    def write_file(self, name, relative, content):
        relative = relative_path(relative)
        with _lock:
            files = self.read(name)
            files[relative] = str(content).encode("utf-8")
            if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_BYTES:
                raise ValueError("Превышен лимит скилла.")
            if frontmatter(files["SKILL.md"].decode("utf-8-sig"))["name"] != name:
                raise ValueError("Техническое имя существующего скилла менять нельзя.")
            target = self.directory(name) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(prefix=".write-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(files[relative])
                os.replace(temp, target)
            finally:
                Path(temp).unlink(missing_ok=True)

    def remove(self, name):
        with _lock:
            shutil.rmtree(self.directory(name))
            settings = self.settings()
            settings.pop(name, None)
            portable = {
                key: {k: v for k, v in value.items() if k != "environment"}
                for key, value in settings.items()
            }
            atomic_json(self.root / ".settings.json", portable)
