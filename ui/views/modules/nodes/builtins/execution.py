from ..specs import PipelineNodeSpec, PipelinePortSpec, exec_in, exec_out, arg_result
from modules.pipelines.execution import NOTIFICATION, PARALLEL, TRANSFORM


def register_nodes(registry):
    arg = PipelinePortSpec('arg:in', 'Arg', 'arg')
    registry.register(PipelineNodeSpec(NOTIFICATION, "Notification", 'graph_control', (),
        "Shows a colored system message only to the user. {arg} inserts an input value. Does not call an LLM.",
        (exec_in(), arg), (exec_out(),), True,
        {'inspector_type':'graph_message', 'graph_role':'notification', 'message_label':"Notification text",
         'message_placeholder':"Message; insert input with {arg}",
         'default_settings': {'message': '', 'color': '#5b8def'}}))
    registry.register(PipelineNodeSpec(PARALLEL, "Parallel tools", 'execution', (),
        "One plan for the entire batch. Concurrent or sequential execution, with a shared Exec after all jobs finish.",
        (exec_in(),), (exec_out(), PipelinePortSpec('arg:result', 'Arg · result', 'arg', schema_type='object')), True))
    registry.register(PipelineNodeSpec(TRANSFORM, "Data transformation", 'execution', (),
        "Sequential operations on JSON, text, arrays and dates without an LLM. Path: a.0.name or /a/0/name.",
        (exec_in(), arg), (exec_out(), arg_result()), True,
        {'default_settings': {'operations': [{'op': 'get', 'path': ''}], 'parse_json_input': True}}))
