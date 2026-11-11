from ..specs import PipelineNodeSpec, exec_in, exec_out, arg_result

def register_nodes(registry):
    registry.register(PipelineNodeSpec(type_id='pipeline.call', title='Nested Pipeline', kind='graph_control', category_path=('Graph',),
        description="Calls an internal pipeline in the same Custom Module.", inputs=(exec_in(),), outputs=(exec_out(), arg_result()),
        implemented=True, metadata={'inspector_type': 'pipeline_call', 'default_settings': {'pipeline': '', 'inputs': [], 'outputs': []}}))
