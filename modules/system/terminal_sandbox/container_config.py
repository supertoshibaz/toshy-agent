"""Compare Docker's effective settings without exposing environment secrets."""

from pathlib import Path
from .sandbox_security import container_user


def environment(values):
    result = {}
    for value in values or []:
        name, separator, text = str(value).partition("=")
        if separator:
            result[name] = text
    return result


def configuration_differences(info, profile, image_environment=None):
    config, host = info.get("Config") or {}, info.get("HostConfig") or {}
    differences = []
    if config.get("Image") != profile["image"]:
        differences.append("образ")
    memory = int(profile["memory_mb"]) * 1024 * 1024
    if host.get("Memory") != memory or host.get("MemorySwap") != memory:
        differences.append("лимит памяти")
    cpus = int(round(float(profile["cpus"]) * 1_000_000_000))
    actual_cpus = host.get("NanoCpus") or 0
    if not actual_cpus and (host.get("CpuPeriod") or 0) > 0:
        actual_cpus = round(host.get("CpuQuota", 0) * 1_000_000_000 / host["CpuPeriod"])
    if actual_cpus != cpus:
        differences.append("лимит CPU")
    if host.get("PidsLimit") != int(profile["pids"]):
        differences.append("лимит процессов")
    if host.get("NetworkMode") not in (
        {"bridge", "default"} if profile["network"] else {"none"}
    ):
        differences.append("доступ к сети")
    if config.get("User") != container_user(profile):
        differences.append("пользователь / Root внутри контейнера")
    if bool(host.get("ReadonlyRootfs")) != bool(profile.get("restrict_writes")):
        differences.append("режим записи системных каталогов")
    desired = {
        (str(Path(m["host"]).resolve()), m["container"], not m["readonly"])
        for m in profile["mounts"]
    }
    actual = {
        (str(Path(m["Source"]).resolve()), m["Destination"], bool(m.get("RW")))
        for m in info.get("Mounts") or []
        if m.get("Type") == "bind"
    }
    if actual != desired:
        differences.append("подключённые папки / Project Folder")
    if image_environment is not None:
        expected = environment(image_environment)
        expected.update(profile["environment"])
        expected.update(
            HOME="/home/toshy",
            TMPDIR="/home/toshy/.cache/tmp",
            PIP_CACHE_DIR="/home/toshy/.cache/pip",
            npm_config_cache="/home/toshy/.cache/npm",
        )
        if environment(config.get("Env")) != expected:
            differences.append("переменные окружения")
    return differences
