"""The worker registers exactly the activities the workflows schedule."""

import ast
import inspect

from pipeline.temporal import document_workflows
from pipeline.temporal.registry import ACTIVITIES


def test_registered_activities_are_the_scheduled_ones():
    tree = ast.parse(inspect.getsource(document_workflows))
    scheduled = {
        call.args[0].id
        for call in ast.walk(tree)
        if isinstance(call, ast.Call) and getattr(call.func, "attr", "") == "execute_activity"
    }
    assert scheduled == {activity.__name__ for activity in ACTIVITIES}
