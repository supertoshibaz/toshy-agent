from __future__ import annotations

from ...specs import PipelineNodeSpec, content_arg_in, content_arg_out


def register_nodes(registry) -> None:
    registry.register(
        PipelineNodeSpec(
            type_id="content.binary",
            title="Binary",
            kind="content",
            category_path=("Arguments",),
            description="Binary value. True/False are local values; None opens the Arg input.",
            inputs=(content_arg_in("binary"),),
            outputs=(content_arg_out("binary"),),
            implemented=True,
            metadata={
                "value_type": "binary",
                "default_value": True,
                "settings_key": "value",
            },
        )
    )
