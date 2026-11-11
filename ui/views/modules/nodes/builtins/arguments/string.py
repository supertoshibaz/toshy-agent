from __future__ import annotations

from ...specs import PipelineNodeSpec, content_arg_in, content_arg_out


def register_nodes(registry) -> None:
    registry.register(
        PipelineNodeSpec(
            type_id="content.string",
            title="String",
            kind="content",
            category_path=("Arguments",),
            description="String value. The node passes its value through the Arg stream.",
            inputs=(content_arg_in("string"),),
            outputs=(content_arg_out("string"),),
            implemented=True,
            metadata={
                "value_type": "string",
                "default_value": "",
                "settings_key": "value",
            },
        )
    )
