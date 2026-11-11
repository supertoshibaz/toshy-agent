from __future__ import annotations

from ..specs import PipelineNodeSpec, prompt_arg_in, prompt_node_out


def register_nodes(registry) -> None:
    registry.register(
        PipelineNodeSpec(
            type_id="content.prompt",
            title="Prompt",
            kind="content",
            category_path=("Content nodes",),
            description=(
                "Additional node-local instruction. Arg inputs append runtime text below "
                "the local prompt; occupying the last Arg slot exposes another one. "
                "The combined text leaves the node as Prompt."
            ),
            inputs=(prompt_arg_in(1),),
            outputs=(prompt_node_out(),),
            implemented=True,
            metadata={
                "value_type": "prompt",
                "default_value": "",
                "settings_key": "text",
            },
        )
    )
