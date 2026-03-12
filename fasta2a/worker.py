from __future__ import annotations as _annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic

import anyio
from opentelemetry.trace import get_tracer, use_span
from typing_extensions import assert_never

from .schema import StreamResponse, TaskArtifactUpdateEvent, TaskStatusUpdateEvent
from .storage import ContextT, Storage

if TYPE_CHECKING:
    from .broker import Broker, TaskOperation
    from .schema import Artifact, Message, TaskIdParams, TaskSendParams, TaskState

tracer = get_tracer(__name__)


@dataclass
class Worker(ABC, Generic[ContextT]):
    """A worker is responsible for executing tasks."""

    broker: Broker
    storage: Storage[ContextT]

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Run the worker.

        It connects to the broker, and it makes itself available to receive commands.
        """
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._loop)
            yield
            tg.cancel_scope.cancel()

    async def _loop(self) -> None:
        async for task_operation in self.broker.receive_task_operations():
            await self._handle_task_operation(task_operation)

    async def _handle_task_operation(self, task_operation: TaskOperation) -> None:
        try:
            with use_span(task_operation['_current_span']):
                with tracer.start_as_current_span(
                    f'{task_operation["operation"]} task', attributes={'logfire.tags': ['fasta2a']}
                ):
                    if task_operation['operation'] == 'run':
                        await self.run_task(task_operation['params'])
                    elif task_operation['operation'] == 'cancel':
                        await self.cancel_task(task_operation['params'])
                    else:
                        assert_never(task_operation)
        except Exception:
            task_id = task_operation['params']['id']
            task = await self.storage.update_task(task_id, state='failed')
            from .schema import TaskStatus

            await self.broker.event_bus.emit(
                task_id,
                StreamResponse(
                    status_update=TaskStatusUpdateEvent(
                        task_id=task_id,
                        context_id=task['context_id'],
                        status=TaskStatus(state='failed'),
                    )
                ),
            )
            await self.broker.event_bus.close(task_id)

    async def update_task(
        self,
        task_id: str,
        state: TaskState,
        new_artifacts: list[Artifact] | None = None,
        new_messages: list[Message] | None = None,
    ) -> None:
        """Update a task's state in storage and publish streaming events to the broker.

        This is the primary method workers should use to update task state. It handles
        both persisting the update and notifying any stream subscribers.
        """
        task = await self.storage.update_task(task_id, state, new_artifacts, new_messages)

        final = state in ('completed', 'failed', 'canceled')

        # For non-final updates, publish status first
        if not final:
            await self.broker.event_bus.emit(
                task_id,
                StreamResponse(
                    status_update=TaskStatusUpdateEvent(
                        task_id=task_id,
                        context_id=task['context_id'],
                        status=task['status'],
                    ),
                ),
            )

        # Publish message events before final status so subscribers receive them
        if new_messages:
            for message in new_messages:
                await self.broker.event_bus.emit(task_id, StreamResponse(message=message))

        # Publish artifact events
        if new_artifacts:
            for artifact in new_artifacts:
                await self.broker.event_bus.emit(
                    task_id,
                    StreamResponse(
                        artifact_update=TaskArtifactUpdateEvent(
                            task_id=task_id,
                            context_id=task['context_id'],
                            artifact=artifact,
                        ),
                    ),
                )

        # For final updates, publish status last (after messages and artifacts)
        if final:
            await self.broker.event_bus.emit(
                task_id,
                StreamResponse(
                    status_update=TaskStatusUpdateEvent(
                        task_id=task_id,
                        context_id=task['context_id'],
                        status=task['status'],
                    ),
                ),
            )
            await self.broker.event_bus.close(task_id)

    @abstractmethod
    async def run_task(self, params: TaskSendParams) -> None: ...

    @abstractmethod
    async def cancel_task(self, params: TaskIdParams) -> None: ...

    @abstractmethod
    def build_message_history(self, history: list[Message]) -> list[Any]: ...

    @abstractmethod
    def build_artifacts(self, result: Any) -> list[Artifact]: ...
