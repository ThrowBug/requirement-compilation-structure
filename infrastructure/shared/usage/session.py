"""Stage observation works both in the compiler and standalone stage entrypoints."""

from contextlib import contextmanager, ExitStack

from infrastructure.shared.artifacts.dependency_graph import current_graph, DependencyGraph
from infrastructure.shared.usage.recorder import current_recorder, UsageRecorder, usage_scope


@contextmanager
def observed_stage(output, stage):
    with ExitStack() as stack:
        recorder = current_recorder()
        owned = recorder is None
        if owned:
            recorder = stack.enter_context(UsageRecorder(output).activate())
        graph = current_graph()
        if graph is None:
            graph = stack.enter_context(DependencyGraph(output).activate())
        stack.enter_context(graph.stage(stage))
        stack.enter_context(usage_scope(stage=stage))
        status = "GREEN"
        try:
            yield
        except BaseException as error:
            status = "INTERRUPTED" if isinstance(error, KeyboardInterrupt) else "FAILED"
            raise
        finally:
            recorder.stage_result(stage, status)
            if owned:
                recorder.finish(status)
