from __future__ import annotations

from modules.pipelines.control import PYTHON_CODE_ERROR_ACTIONS, PYTHON_CODE_TYPE

from ..specs import PipelineNodeSpec, exec_in, exec_out


def register_nodes(registry) -> None:
    registry.register(
        PipelineNodeSpec(
            type_id=PYTHON_CODE_TYPE,
            title="Python Code",
            kind="advanced",
            category_path=("Advanced",),
            description=(
                "Deterministic Python pipeline step. Models pause during execution: input Args become Python variables and output Args are taken from variables with matching names after the script finishes."
            ),
            inputs=(exec_in(),),
            outputs=(exec_out(),),
            implemented=True,
            metadata={
                "inspector_type": "python_code",
                "default_settings": {
                    "code": "",
                    "inputs": [],
                    "outputs": [],
                },
                "runtime_contract": {
                    "model_paused": True,
                    "input_binding": "python_variables_by_name",
                    "output_binding": "python_variables_by_name",
                    "on_exception": "human_in_loop",
                    "error_actions": [dict(item) for item in PYTHON_CODE_ERROR_ACTIONS],
                },
            },
        )
    )
