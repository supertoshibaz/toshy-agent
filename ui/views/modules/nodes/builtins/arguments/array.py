from __future__ import annotations

from ...specs import PipelineNodeSpec, array_arg_in, array_arg_out


def register_nodes(registry) -> None:
    registry.register(
        PipelineNodeSpec(
            type_id="content.array",
            title="Array",
            kind="content",
            category_path=("Arguments",),
            description=(
                "Array value. Select the item type, enter literal items separated "
                "by commas, or feed values through the automatically expanding Arg inputs."
            ),
            inputs=(array_arg_in(1, "string"),),
            outputs=(array_arg_out("string"),),
            implemented=True,
            metadata={
                "value_type": "array",
                "default_value": [],
                "default_item_type": "string",
                "item_types": ("string", "binary", "integer", "float"),
                "dynamic_inputs": "array",
                "settings_key": "value",
            },
        )
    )
