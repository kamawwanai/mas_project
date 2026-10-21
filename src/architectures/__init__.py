from .workflow import Workflow
from .single_agent import SingleAgent
from .mas import MAS

PLANNERS = {"workflow": Workflow, "single_agent": SingleAgent, "mas": MAS}
