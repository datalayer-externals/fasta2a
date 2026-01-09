"""This module defines the Storage class, which is responsible for storing and retrieving tasks."""

from __future__ import annotations as _annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING, Any, Generic

from typing_extensions import TypeVar

from .schema import (
    Artifact,
    Message,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)

if TYPE_CHECKING:
    from .broker import Broker

ContextT = TypeVar('ContextT', default=Any)


class Storage(ABC, Generic[ContextT]):
    """A storage to retrieve and save tasks, as well as retrieve and save context.

    The storage serves two purposes:
    1. Task storage: Stores tasks in A2A protocol format with their status, artifacts, and message history
    2. Context storage: Stores conversation context in a format optimized for the specific agent implementation
    """

    @abstractmethod
    async def load_task(self, task_id: str, history_length: int | None = None) -> Task | None:
        """Load a task from storage.

        If the task is not found, return None.
        """

    @abstractmethod
    async def submit_task(self, context_id: str, message: Message) -> Task:
        """Submit a task to storage."""

    @abstractmethod
    async def update_task(
        self,
        task_id: str,
        state: TaskState,
        new_artifacts: list[Artifact] | None = None,
        new_messages: list[Message] | None = None,
    ) -> Task:
        """Update the state of a task. Appends artifacts and messages, if specified."""

    @abstractmethod
    async def load_context(self, context_id: str) -> ContextT | None:
        """Retrieve the stored context given the `context_id`."""

    @abstractmethod
    async def update_context(self, context_id: str, context: ContextT) -> None:
        """Updates the context for a `context_id`.

        Implementing agent can decide what to store in context.
        """


class InMemoryStorage(Storage[ContextT]):
    """A storage to retrieve and save tasks in memory."""

    def __init__(self):
        self.tasks: dict[str, Task] = {}
        self.contexts: dict[str, ContextT] = {}

    async def load_task(self, task_id: str, history_length: int | None = None) -> Task | None:
        """Load a task from memory.

        Args:
            task_id: The id of the task to load.
            history_length: The number of messages to return in the history.

        Returns:
            The task.
        """
        if task_id not in self.tasks:
            return None

        task = self.tasks[task_id]
        if history_length and 'history' in task:
            task['history'] = task['history'][-history_length:]
        return task

    async def submit_task(self, context_id: str, message: Message) -> Task:
        """Submit a task to storage."""
        # Generate a unique task ID
        task_id = str(uuid.uuid4())

        # Add IDs to the message for A2A protocol
        message['task_id'] = task_id
        message['context_id'] = context_id

        task_status = TaskStatus(state='submitted', timestamp=datetime.now().isoformat())
        task = Task(id=task_id, context_id=context_id, kind='task', status=task_status, history=[message])
        self.tasks[task_id] = task

        return task

    async def update_task(
        self,
        task_id: str,
        state: TaskState,
        new_artifacts: list[Artifact] | None = None,
        new_messages: list[Message] | None = None,
    ) -> Task:
        """Update the state of a task."""
        task = self.tasks[task_id]
        task['status'] = TaskStatus(state=state, timestamp=datetime.now().isoformat())

        if new_artifacts:
            if 'artifacts' not in task:
                task['artifacts'] = []
            task['artifacts'].extend(new_artifacts)

        if new_messages:
            if 'history' not in task:
                task['history'] = []
            # Add IDs to messages for consistency
            for message in new_messages:
                message['task_id'] = task_id
                message['context_id'] = task['context_id']
                task['history'].append(message)

        return task

    async def update_context(self, context_id: str, context: ContextT) -> None:
        """Updates the context given the `context_id`."""
        self.contexts[context_id] = context

    async def load_context(self, context_id: str) -> ContextT | None:
        """Retrieve the stored context given the `context_id`."""
        return self.contexts.get(context_id)


class StreamingStorageWrapper(Storage[ContextT]):
    """A storage wrapper that publishes streaming events when tasks are updated.

    This wrapper intercepts update_task calls and publishes TaskStatusUpdateEvent
    and TaskArtifactUpdateEvent to the broker, enabling SSE streaming without
    modifying the underlying storage or worker implementations.
    """

    def __init__(self, storage: Storage[ContextT], broker: Broker):
        self._storage = storage
        self._broker = broker

    async def load_task(self, task_id: str, history_length: int | None = None) -> Task | None:
        return await self._storage.load_task(task_id, history_length)

    async def submit_task(self, context_id: str, message: Message) -> Task:
        return await self._storage.submit_task(context_id, message)

    async def update_task(
        self,
        task_id: str,
        state: TaskState,
        new_artifacts: list[Artifact] | None = None,
        new_messages: list[Message] | None = None,
    ) -> Task:
        """Update task and publish streaming events."""
        # Update the underlying storage first
        task = await self._storage.update_task(task_id, state, new_artifacts, new_messages)

        # Determine if this is a final state
        final = state in ('completed', 'failed', 'canceled')

        # For non-final updates, publish status first
        if not final:
            status_event = TaskStatusUpdateEvent(
                kind='status-update',
                task_id=task_id,
                context_id=task['context_id'],
                status=task['status'],
                final=False,
            )
            await self._broker.send_stream_event(task_id, status_event)

        # Publish message events BEFORE final status (so subscriber receives them)
        if new_messages:
            for message in new_messages:
                await self._broker.send_stream_event(task_id, message)

        # Publish artifact events
        if new_artifacts:
            for artifact in new_artifacts:
                artifact_event = TaskArtifactUpdateEvent(
                    kind='artifact-update',
                    task_id=task_id,
                    context_id=task['context_id'],
                    artifact=artifact,
                )
                await self._broker.send_stream_event(task_id, artifact_event)

        # For final updates, publish status LAST (after messages and artifacts)
        if final:
            status_event = TaskStatusUpdateEvent(
                kind='status-update',
                task_id=task_id,
                context_id=task['context_id'],
                status=task['status'],
                final=True,
            )
            await self._broker.send_stream_event(task_id, status_event)

        return task

    async def load_context(self, context_id: str) -> ContextT | None:
        return await self._storage.load_context(context_id)

    async def update_context(self, context_id: str, context: ContextT) -> None:
        await self._storage.update_context(context_id, context)
