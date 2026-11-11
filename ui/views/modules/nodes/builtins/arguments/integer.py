from __future__ import annotations

from ...specs import PipelineNodeSpec, content_arg_in, content_arg_out


def register_nodes(registry) -> None:
    registry.register(
        PipelineNodeSpec(
            type_id="content.integer",
            title="Integer",
            kind="content",
            category_path=("Arguments",),
            description="Integer value. The node passes its value through the Arg stream.",
            inputs=(content_arg_in("integer"),),
            outputs=(content_arg_out("integer"),),
            implemented=True,
            metadata={
                "value_type": "integer",
                "default_value": None,
                "settings_key": "value",
            },
        )
    )
