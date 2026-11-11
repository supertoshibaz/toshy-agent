from __future__ import annotations

from ...specs import PipelineNodeSpec, content_arg_in, content_arg_out


def register_nodes(registry) -> None:
    registry.register(
        PipelineNodeSpec(
            type_id="content.float",
            title="Float",
            kind="content",
            category_path=("Arguments",),
            description="Floating-point value. The node passes its value through the Arg stream.",
            inputs=(content_arg_in("float"),),
            outputs=(content_arg_out("float"),),
            implemented=True,
            metadata={
                "value_type": "float",
                "default_value": None,
                "settings_key": "value",
            },
        )
    )
