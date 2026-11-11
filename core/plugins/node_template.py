"""Pipeline-operation scaffolds using the existing typed module/tool node API."""
import hashlib
import json
import re
from pathlib import Path


def node_module_name(identity):
    label = re.sub(r'[^a-zA-Z0-9_]', '_', identity)[:55]
    return 'nodes_' + label + '_' + hashlib.sha256(identity.encode()).hexdigest()[:8]


def write_node_module(directory, identity, module_name, display_name):
    root = Path(directory); root.mkdir()
    prefix = 'node_' + hashlib.sha256((identity + '\0' + module_name).encode()).hexdigest()[:16]
    code = '''"""Pipeline operations. Not advertised as tools to the model by default."""
from core.registry import BaseModule, BaseTool


class UppercaseNode(BaseTool):
    model_visible = False
    pipeline_title = "Верхний регистр"

    def __init__(self):
        super().__init__(
            name=UPPERCASE_NAME,
            description="Convert text to uppercase.",
            args_schema={"type": "object", "properties": {"text": {"type": "string"}},
                         "required": ["text"], "additionalProperties": False},
        )

    async def execute(self, text):
        return text.upper()


class AddNumbersNode(BaseTool):
    model_visible = False
    pipeline_title = "Сложить числа"

    def __init__(self):
        super().__init__(
            name=ADD_NAME,
            description="Add two numbers.",
            args_schema={"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                         "required": ["a", "b"], "additionalProperties": False},
        )

    async def execute(self, a, b):
        return a + b


class Module(BaseModule):
    def get_tools(self):
        return [UppercaseNode(), AddNumbersNode()]
'''.replace('UPPERCASE_NAME', repr(prefix + '_uppercase')).replace('ADD_NAME', repr(prefix + '_add'))
    (root / '__init__.py').write_text(code, encoding='utf-8')
    (root / 'ui').mkdir()
    (root / 'ui' / 'config.json').write_text(json.dumps({
        'display_name': display_name + ' · Узлы', 'color': '#34b4a1'}, ensure_ascii=False, indent=2), encoding='utf-8')
    (root / 'README.md').write_text('''# Узлы пайплайна

Два примера исполняемых операций: верхний регистр текста и сумма чисел.
Их Python-реализация находится в __init__.py, входы описаны в args_schema.
Ноды появляются в библиотеке редактора в категории System modules после
включения плагина и полного перезапуска приложения, даже без запущенного ядра.
Читаемые названия карточек задаются строкой pipeline_title в классе операции.

Для запуска включите созданный системный модуль в стартапе и разрешите его
папке проекта. Подключите Exec и необходимые Arg-входы. Результат — Arg result.
При заполненных входах примеры выполняются без обращения к модели.
model_visible=False скрывает операции из списка инструментов модели,
но не мешает прямому выполнению нодой графа. Удалите этот флаг, если нужно
также предлагать операцию модели как обычный инструмент.

Для новых узлов добавьте классы BaseTool и верните их в Module.get_tools().
Указывайте name, description и args_schema литералами в super().__init__,
чтобы редактор мог прочитать схемы без импорта кода и без запуска ядра.
Имена операций должны быть уникальны для всего приложения.
''', encoding='utf-8')
