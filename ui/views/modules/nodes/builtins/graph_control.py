from __future__ import annotations

from ..specs import PipelineNodeSpec, arg_in, exec_in, exec_out, exec_port


from modules.pipelines.control import (
    GRAPH_END_TYPE,
    GRAPH_LOOP_TYPE,
    GRAPH_ROUTER_TYPE,
    GRAPH_HUMAN_IN_LOOP_TYPE,
    GRAPH_SHUTDOWN_TYPE,
    GRAPH_START_NODE_ID,
    GRAPH_START_TYPE,
    LOOP_CONDITION_DEFINITIONS,
    ROUTER_MECHANISM_DEFINITIONS,
    default_loop_conditions,
    default_router_mechanisms,
)




def register_nodes(registry) -> None:
    # Graph Start must exist in the registry so saved pipelines can load it, but
    # it is deliberately hidden from the creation menu. The persistence layer
    # creates exactly one automatically.
    registry.register(
        PipelineNodeSpec(
            type_id=GRAPH_START_TYPE,
            title="Graph Start",
            kind="graph_control",
            category_path=("Graph",),
            description="The only normal pipeline entry point. Created automatically and cannot be deleted.",
            inputs=(),
            outputs=(exec_out(),),
            implemented=True,
            metadata={
                "library_hidden": True,
                "singleton": True,
                "protected": True,
                "graph_role": "start",
            },
        )
    )

    registry.register(
        PipelineNodeSpec(
            type_id=GRAPH_END_TYPE,
            title="Finish subgraph",
            kind="graph_control",
            category_path=("Graph",),
            description=(
                "Completes the current pipeline/subgraph normally, returning the current path result and an optional message to the model. Model generation may then continue."
            ),
            inputs=(exec_in(),),
            outputs=(),
            implemented=True,
            metadata={
                "inspector_type": "graph_message",
                "message_label": "Message to model on completion",
                "message_placeholder": "Optional message…",
                "default_settings": {"message": ""},
                "graph_role": "end_subgraph",
            },
        )
    )

    registry.register(
        PipelineNodeSpec(
            type_id=GRAPH_SHUTDOWN_TYPE,
            title="Shut down graph",
            kind="graph_control",
            category_path=("Graph",),
            description=(
                "Immediately stops the entire current run and shows the user the message set in the node."
            ),
            inputs=(exec_in(),),
            outputs=(),
            implemented=True,
            metadata={
                "inspector_type": "graph_message",
                "message_label": "Final message to user",
                "message_placeholder": "Message on forced termination…",
                "default_settings": {"message": ""},
                "graph_role": "shutdown",
            },
        )
    )

    registry.register(
        PipelineNodeSpec(
            type_id=GRAPH_LOOP_TYPE,
            title="Loop",
            kind="graph_control",
            category_path=("Graph",),
            description=(
                "Controls repeated execution. Conditions are checked from top to bottom; the first requiring another iteration activates Repeat."
            ),
            inputs=(exec_in(),),
            outputs=(
                exec_port("continue", "Exec · Continue"),
                exec_port("repeat", "Exec · Repeat"),
            ),
            implemented=True,
            metadata={
                "inspector_type": "graph_loop",
                "graph_role": "loop",
                "loop_condition_definitions": LOOP_CONDITION_DEFINITIONS,
                "default_settings": {
                    "max_repeats": 10,
                    "conditions": default_loop_conditions(),
                },
            },
        )
    )
    registry.register(
        PipelineNodeSpec(
            type_id=GRAPH_ROUTER_TYPE,
            title="Router",
            kind="graph_control",
            category_path=("Graph",),
            description=(
                "Selects exactly one Exec output. Use it to branch between pipelines, choose paths or skip downstream nodes."
            ),
            inputs=(exec_in(),),
            outputs=(exec_port("route:0", "Exec · 0"),),
            implemented=True,
            metadata={
                "inspector_type": "graph_router",
                "graph_role": "router",
                "router_mechanism_definitions": ROUTER_MECHANISM_DEFINITIONS,
                "default_settings": {
                    "route_count": 1,
                    "route_descriptions": {"0": ""},
                    "mechanisms": default_router_mechanisms(),
                },
            },
        )
    )

    registry.register(
        PipelineNodeSpec(
            type_id=GRAPH_HUMAN_IN_LOOP_TYPE,
            title="Human In Loop",
            kind="graph_control",
            category_path=("Graph",),
            description=(
                "Pauses the graph and shows a message. Waits for confirmation by default. The user fills optional Arg outputs; the model determines remaining empty values after confirmation."
            ),
            inputs=(exec_in(),),
            outputs=(exec_out(),),
            implemented=True,
            metadata={
                "inspector_type": "human_in_loop",
                "graph_role": "human_in_loop",
                "default_settings": {
                    "message": "",
                    "arguments_enabled": False,
                    "outputs": [],
                },
                "runtime_contract": {
                    "suspend_graph": True,
                    "require_user_continue": True,
                    "model_fill_missing_outputs_only": True,
                },
            },
        )
    )
