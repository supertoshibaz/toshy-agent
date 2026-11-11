from ..specs import PipelineNodeSpec, exec_in, exec_out, prompt_in


def register_nodes(registry):
    registry.register(PipelineNodeSpec(
        type_id='advanced.agent_decision', title="Autonomous agent decision",
        kind='tool', category_path=('Advanced',),
        description="The agent selects at most one tool from project modules, including MCP, then continues Exec after it finishes.",
        inputs=(exec_in(), prompt_in()), outputs=(exec_out(),), implemented=True))
