"""Pure graph-control contracts shared by the pipeline editor and future runtime.

This module deliberately has no Qt dependency. The visual Graph nodes serialize
settings that follow these contracts; the execution engine can consume the same
values later.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List


GRAPH_START_TYPE = "graph.start"
GRAPH_END_TYPE = "graph.end_subgraph"
GRAPH_SHUTDOWN_TYPE = "graph.shutdown"
GRAPH_LOOP_TYPE = "graph.loop"
GRAPH_ROUTER_TYPE = "graph.router"
GRAPH_HUMAN_IN_LOOP_TYPE = "graph.human_in_loop"
PYTHON_CODE_TYPE = "advanced.python_code"
GRAPH_START_NODE_ID = "graph_start"


PYTHON_CODE_ERROR_ACTIONS = (
    {
        "value": "skip_node",
        "label": "Пропустить ноду",
        "description": "Не повторять скрипт и продолжить Exec-путь после Python Code.",
    },
    {
        "value": "retry_script",
        "label": "Повторить скрипт",
        "description": "Запустить тот же Python Code ещё раз с теми же входными Arg.",
    },
    {
        "value": "finish_graph",
        "label": "Завершить граф",
        "description": "Штатно завершить текущий pipeline/подграф.",
    },
    {
        "value": "shutdown_generation",
        "label": "Остановить генерацию целиком",
        "description": "Принудительно завершить весь текущий run/генерацию.",
    },
)

ARGUMENT_SCHEMA_TYPES = ("string", "boolean", "integer", "float", "array", "object")


def normalize_argument_definitions(raw: Any, *, with_description: bool = False) -> List[Dict[str, Any]]:
    """Normalize serialized dynamic Arg definitions without importing Qt.

    ``id`` is the stable graph-port identity; ``name`` is the Python/user-facing
    variable name and may therefore change without silently reusing a different
    connection.
    """
    result: List[Dict[str, Any]] = []
    seen_ids = set()
    for index, raw_item in enumerate(list(raw or []), start=1):
        if not isinstance(raw_item, dict):
            continue
        item_id = str(raw_item.get("id") or f"arg_{index}").strip()
        if not item_id or item_id in seen_ids:
            continue
        name = str(raw_item.get("name") or f"arg_{index}").strip() or f"arg_{index}"
        schema_type = str(raw_item.get("type") or "string").strip().casefold()
        if schema_type not in ARGUMENT_SCHEMA_TYPES:
            schema_type = "string"
        item: Dict[str, Any] = {
            "id": item_id,
            "name": name,
            "type": schema_type,
        }
        if with_description:
            item["description"] = str(raw_item.get("description") or "")
        result.append(item)
        seen_ids.add(item_id)
    return result


def human_in_loop_missing_arguments(
    definitions: Any,
    supplied_values: Dict[str, Any] | None,
) -> List[str]:
    """Return output argument names left blank by the user.

    Future runtime uses this set to ask the model to fill only the missing values;
    explicit user values remain authoritative and are never overwritten.
    """
    supplied_values = dict(supplied_values or {})
    missing: List[str] = []
    for item in normalize_argument_definitions(definitions, with_description=True):
        name = str(item["name"])
        value = supplied_values.get(name, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(name)
    return missing


LOOP_CONDITION_DEFINITIONS = (
    {
        "type": "error",
        "title": "По ошибке",
        "description": "Повторить, если предыдущий исполняемый шаг завершился ошибкой.",
    },
    {
        "type": "argument",
        "title": "По аргументу",
        "description": "Повторить, если входной Boolean Arg равен True.",
    },
    {
        "type": "counter",
        "title": "По счётчику",
        "description": "Повторять, пока общее число уже выполненных витков меньше указанного.",
    },
    {
        "type": "subagent",
        "title": "По решению субагента",
        "description": "Передать результат субагенту; его строгое Boolean-решение определяет необходимость витка.",
    },
    {
        "type": "agent",
        "title": "По решению самого агента",
        "description": "Главная модель перепроверяет результат по указанному промпту и возвращает Boolean-решение.",
    },
    {
        "type": "python",
        "title": "По результату Python-скрипта",
        "description": "Выполнить отдельное условие на Python. True — виток, False — продолжение; другое значение требует решения пользователя.",
    },
    {
        "type": "user",
        "title": "По решению пользователя",
        "description": "Приостановить pipeline и попросить пользователя выбрать: повторить виток или продолжить.",
    },
)


ROUTER_MECHANISM_DEFINITIONS = (
    {
        "type": "error",
        "title": "По ошибке",
        "description": "Если предыдущий исполняемый шаг завершился ошибкой, перейти в заранее выбранный Exec-поток.",
    },
    {
        "type": "argument",
        "title": "По аргументу",
        "description": "Номер Exec-потока приходит через Integer Arg. Нумерация начинается с нуля.",
    },
    {
        "type": "always",
        "title": "Всегда выводить в X",
        "description": "Безусловно выбрать указанный Exec-поток.",
    },
    {
        "type": "subagent",
        "title": "По решению субагента",
        "description": "Субагент должен вернуть номер Exec-потока целым числом, начиная с нуля.",
    },
    {
        "type": "agent",
        "title": "По решению самого агента",
        "description": "Главная модель должна вернуть номер Exec-потока целым числом, начиная с нуля.",
    },
    {
        "type": "python",
        "title": "По результату Python-скрипта",
        "description": "Python-скрипт должен вернуть номер Exec-потока целым числом, начиная с нуля.",
    },
    {
        "type": "user",
        "title": "По решению пользователя",
        "description": "Приостановить pipeline и предложить пользователю выбрать один из Exec-потоков.",
    },
)


def default_loop_conditions() -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for definition in LOOP_CONDITION_DEFINITIONS:
        item: Dict[str, Any] = {
            "type": definition["type"],
            "enabled": False,
        }
        if definition["type"] == "counter":
            item["repeats"] = 1
        elif definition["type"] in {"subagent", "agent"}:
            item["prompt"] = ""
        elif definition["type"] == "python":
            item["code"] = ""
        elif definition["type"] == "user":
            item["message"] = ""
        result.append(item)
    return result


def normalize_loop_conditions(raw: Any) -> List[Dict[str, Any]]:
    """Return every known Loop mechanism once while preserving saved priority."""
    known = {str(item["type"]): item for item in LOOP_CONDITION_DEFINITIONS}
    result: List[Dict[str, Any]] = []
    seen = set()
    for raw_item in list(raw or []):
        if not isinstance(raw_item, dict):
            continue
        condition_type = str(raw_item.get("type") or "")
        if condition_type not in known or condition_type in seen:
            continue
        item = dict(raw_item)
        item["type"] = condition_type
        item["enabled"] = bool(item.get("enabled"))
        if condition_type == "counter":
            try:
                item["repeats"] = max(1, int(item.get("repeats", 1)))
            except (TypeError, ValueError):
                item["repeats"] = 1
        elif condition_type in {"subagent", "agent"}:
            item["prompt"] = str(item.get("prompt") or "")
        elif condition_type == "python":
            item["code"] = str(item.get("code") or "")
        elif condition_type == "user":
            item["message"] = str(item.get("message") or "")
        result.append(item)
        seen.add(condition_type)

    defaults = {item["type"]: item for item in default_loop_conditions()}
    for definition in LOOP_CONDITION_DEFINITIONS:
        condition_type = str(definition["type"])
        if condition_type not in seen:
            result.append(dict(defaults[condition_type]))
    return result


def default_router_mechanisms() -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for definition in ROUTER_MECHANISM_DEFINITIONS:
        mechanism_type = str(definition["type"])
        item: Dict[str, Any] = {
            "type": mechanism_type,
            "enabled": False,
        }
        if mechanism_type in {"error", "always"}:
            item["route"] = 0
        elif mechanism_type in {"subagent", "agent"}:
            item["prompt"] = ""
        elif mechanism_type == "python":
            item["code"] = ""
        elif mechanism_type == "user":
            item["message"] = ""
        result.append(item)
    return result


def normalize_router_mechanisms(raw: Any) -> List[Dict[str, Any]]:
    """Return every known Router mechanism once while preserving saved priority."""
    known = {str(item["type"]): item for item in ROUTER_MECHANISM_DEFINITIONS}
    result: List[Dict[str, Any]] = []
    seen = set()
    for raw_item in list(raw or []):
        if not isinstance(raw_item, dict):
            continue
        mechanism_type = str(raw_item.get("type") or "")
        if mechanism_type not in known or mechanism_type in seen:
            continue
        item = dict(raw_item)
        item["type"] = mechanism_type
        item["enabled"] = bool(item.get("enabled"))
        if mechanism_type in {"error", "always"}:
            try:
                item["route"] = max(0, int(item.get("route", 0)))
            except (TypeError, ValueError):
                item["route"] = 0
        elif mechanism_type in {"subagent", "agent"}:
            item["prompt"] = str(item.get("prompt") or "")
        elif mechanism_type == "python":
            item["code"] = str(item.get("code") or "")
        elif mechanism_type == "user":
            item["message"] = str(item.get("message") or "")
        result.append(item)
        seen.add(mechanism_type)

    defaults = {item["type"]: item for item in default_router_mechanisms()}
    for definition in ROUTER_MECHANISM_DEFINITIONS:
        mechanism_type = str(definition["type"])
        if mechanism_type not in seen:
            result.append(dict(defaults[mechanism_type]))
    return result


class PythonLoopDecision(str, Enum):
    REPEAT = "repeat"
    CONTINUE = "continue"
    ASK_USER = "ask_user"


class RouterDecisionKind(str, Enum):
    ROUTE = "route"
    NO_DECISION = "no_decision"
    ASK_USER = "ask_user"


def classify_python_loop_result(value: Any = None, *, error: BaseException | None = None) -> PythonLoopDecision:
    """Strict Python condition contract: only real bool values are automatic."""
    if error is not None:
        return PythonLoopDecision.ASK_USER
    if value is True:
        return PythonLoopDecision.REPEAT
    if value is False:
        return PythonLoopDecision.CONTINUE
    return PythonLoopDecision.ASK_USER


def normalize_router_route(value: Any, route_count: int) -> int | None:
    """Return a valid zero-based route number or None.

    bool is intentionally rejected even though Python considers it an int.
    """
    if isinstance(value, bool):
        return None
    if not isinstance(value, int):
        return None
    route_count = max(0, int(route_count))
    if 0 <= value < route_count:
        return int(value)
    return None


def classify_router_result(
    value: Any = None,
    *,
    route_count: int,
    error: BaseException | None = None,
) -> tuple[RouterDecisionKind, int | None]:
    """Strict Router contract for Arg/Python/agent decisions.

    A valid integer selects one zero-based route. Any exception or unsupported /
    out-of-range value is escalated to future human-in-the-loop handling.
    """
    if error is not None:
        return RouterDecisionKind.ASK_USER, None
    route = normalize_router_route(value, route_count)
    if route is None:
        return RouterDecisionKind.ASK_USER, None
    return RouterDecisionKind.ROUTE, route


def maximum_allows_repeat(repeat_count: int, max_repeats: int) -> bool:
    """Absolute Loop safety cap checked before all configurable mechanisms."""
    return max(0, int(repeat_count)) < max(1, int(max_repeats))


def counter_requests_repeat(repeat_count: int, target_repeats: int) -> bool:
    """All loop passes share one repeat counter, regardless of what caused them."""
    return max(0, int(repeat_count)) < max(1, int(target_repeats))
