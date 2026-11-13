from app.command_packages.common import run_step_for_op, validate_step_for_op
OP = 'text_box_create'
def validate_step(**kwargs):
    return validate_step_for_op(OP, **kwargs)
def run_step(**kwargs):
    return run_step_for_op(OP, **kwargs)
