"""Tests for the SSE streaming feature in fasta2a.

This module tests:
- Worker.update_task() streaming event publishing
- InMemoryBroker pub/sub for streaming
- TaskManager stream_message method
- FastA2A message/stream endpoint
"""

from __future__ import annotations as _annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from fasta2a import FastA2A, Worker
from fasta2a.broker import InMemoryBroker
from fasta2a.schema import (
    Artifact,
    Message,
    MessageSendParams,
    StreamEvent,
    StreamMessageRequest,
    TaskIdParams,
    TaskSendParams,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)
from fasta2a.storage import InMemoryStorage

pytestmark = pytest.mark.anyio


# Test fixtures and helpers


@asynccontextmanager
async def create_test_client(app: FastA2A):
    """Create a test client for the FastA2A app."""
    async with LifespanManager(app=app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url='http://testclient') as client:
            yield client


Context = list[Message]


@dataclass
class EchoWorker(Worker[Context]):
    """A simple worker for testing that echoes messages."""

    response_text: str = 'Hello from test worker!'
    delay: float = 0.1

    async def run_task(self, params: TaskSendParams) -> None:
        task = await self.storage.load_task(params['id'])
        assert task is not None

        # Update to working state
        await self.update_task(task['id'], state='working')

        # Simulate some work
        await asyncio.sleep(self.delay)

        # Create response message
        message = Message(
            role='agent',
            parts=[TextPart(text=self.response_text, kind='text')],
            kind='message',
            message_id=str(uuid.uuid4()),
        )

        # Create artifact
        artifact = Artifact(
            artifact_id=str(uuid.uuid4()),
            name='result',
            parts=[TextPart(text=self.response_text, kind='text')],
        )

        # Complete the task with message and artifact
        await self.update_task(
            task['id'],
            state='completed',
            new_messages=[message],
            new_artifacts=[artifact],
        )

    async def cancel_task(self, params: TaskIdParams) -> None:
        await self.update_task(params['id'], state='canceled')

    def build_message_history(self, history: list[Message]) -> list[Any]:
        return history

    def build_artifacts(self, result: Any) -> list[Artifact]:
        return []


@asynccontextmanager
async def create_streaming_app(response_text: str = 'Hello!'):
    """Create a FastA2A app with streaming enabled."""
    broker = InMemoryBroker()
    storage = InMemoryStorage()

    worker = EchoWorker(
        broker=broker,
        storage=storage,
        response_text=response_text,
    )

    @asynccontextmanager
    async def lifespan(app: FastA2A):
        async with app.task_manager:
            async with worker.run():
                yield

    app = FastA2A(
        storage=storage,
        broker=broker,
        streaming=True,
        lifespan=lifespan,
    )

    yield app


# Tests for InMemoryBroker streaming


class TestInMemoryBrokerStreaming:
    """Tests for InMemoryBroker pub/sub streaming."""

    async def test_subscribe_and_receive_events(self):
        """Test that subscribers receive events sent by send_stream_event."""
        broker = InMemoryBroker()

        async with broker:
            task_id = 'test-task-123'
            received_events: list[StreamEvent] = []

            # Start subscriber in background
            async def subscriber():
                async for event in broker.subscribe_to_stream(task_id):
                    received_events.append(event)
                    # Check if event has a 'final' field (for status events)
                    if isinstance(event, dict) and event.get('final', False):
                        break

            subscriber_task = asyncio.create_task(subscriber())

            # Give subscriber time to register
            await asyncio.sleep(0.01)

            # Send events
            status_event_1 = TaskStatusUpdateEvent(
                task_id=task_id,
                context_id='test-context',
                kind='status-update',
                status=TaskStatus(state='working'),
                final=False,
            )
            message_event = Message(
                message_id='test-msg-1',
                role='agent',
                parts=[TextPart(kind='text', text='Hello')],
                kind='message',
            )
            status_event_2 = TaskStatusUpdateEvent(
                task_id=task_id,
                context_id='test-context',
                kind='status-update',
                status=TaskStatus(state='completed'),
                final=True,
            )
            await broker.send_stream_event(task_id, status_event_1)
            await broker.send_stream_event(task_id, message_event)
            await broker.send_stream_event(task_id, status_event_2)

            # Wait for subscriber to finish
            await asyncio.wait_for(subscriber_task, timeout=1.0)

            assert len(received_events) == 3
            assert received_events[0]['kind'] == 'status-update'
            assert received_events[1]['kind'] == 'message'  # Message has both kind and role
            # Only check 'final' on status update events
            assert received_events[2]['kind'] == 'status-update' and received_events[2]['final'] is True

    async def test_multiple_subscribers(self):
        """Test that multiple subscribers receive the same events."""
        broker = InMemoryBroker()

        async with broker:
            task_id = 'test-task-456'
            received_1: list[StreamEvent] = []
            received_2: list[StreamEvent] = []

            async def subscriber1():
                async for event in broker.subscribe_to_stream(task_id):
                    received_1.append(event)
                    if isinstance(event, dict) and event.get('final', False):
                        break

            async def subscriber2():
                async for event in broker.subscribe_to_stream(task_id):
                    received_2.append(event)
                    if isinstance(event, dict) and event.get('final', False):
                        break

            task1 = asyncio.create_task(subscriber1())
            task2 = asyncio.create_task(subscriber2())

            await asyncio.sleep(0.01)

            await broker.send_stream_event(
                task_id,
                TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id='test-context',
                    kind='status-update',
                    status=TaskStatus(state='working'),
                    final=False,
                ),
            )
            await broker.send_stream_event(
                task_id,
                TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id='test-context',
                    kind='status-update',
                    status=TaskStatus(state='completed'),
                    final=True,
                ),
            )

            await asyncio.wait_for(asyncio.gather(task1, task2), timeout=1.0)

            assert len(received_1) == 2
            assert len(received_2) == 2

    async def test_no_subscribers_doesnt_error(self):
        """Test that sending events with no subscribers doesn't raise errors."""
        broker = InMemoryBroker()

        async with broker:
            # Should not raise
            await broker.send_stream_event(
                'nonexistent-task',
                TaskStatusUpdateEvent(
                    task_id='nonexistent-task',
                    context_id='test-context',
                    kind='status-update',
                    status=TaskStatus(state='working'),
                    final=False,
                ),
            )


# Tests for Worker.update_task() streaming events


class TestWorkerUpdateTask:
    """Tests for Worker.update_task() event publishing."""

    async def test_publishes_status_update_on_working(self):
        """Test that updating to 'working' publishes a status-update event."""
        broker = InMemoryBroker()
        storage = InMemoryStorage()
        worker = EchoWorker(broker=broker, storage=storage)

        async with broker:
            # Create a task
            message = Message(
                role='user',
                parts=[TextPart(text='Hello', kind='text')],
                kind='message',
                message_id='msg-1',
            )
            task = await storage.submit_task('ctx-1', message)
            task_id = task['id']

            received_events: list[StreamEvent] = []

            async def subscriber():
                async for event in broker.subscribe_to_stream(task_id):
                    received_events.append(event)
                    # Stop after first event for this test
                    break

            sub_task = asyncio.create_task(subscriber())
            await asyncio.sleep(0.01)

            # Update to working via worker
            await worker.update_task(task_id, state='working')

            await asyncio.wait_for(sub_task, timeout=1.0)

            assert len(received_events) == 1
            assert received_events[0]['kind'] == 'status-update'
            assert received_events[0]['status']['state'] == 'working'
            assert received_events[0]['final'] is False

    async def test_publishes_message_before_final_status(self):
        """Test that messages are published before the final status update."""
        broker = InMemoryBroker()
        storage = InMemoryStorage()
        worker = EchoWorker(broker=broker, storage=storage)

        async with broker:
            message = Message(
                role='user',
                parts=[TextPart(text='Hello', kind='text')],
                kind='message',
                message_id='msg-1',
            )
            task = await storage.submit_task('ctx-1', message)
            task_id = task['id']

            received_events: list[StreamEvent] = []

            async def subscriber():
                async for event in broker.subscribe_to_stream(task_id):
                    received_events.append(event)
                    if isinstance(event, dict) and event.get('final', False):
                        break

            sub_task = asyncio.create_task(subscriber())
            await asyncio.sleep(0.01)

            # Complete with a new message via worker
            agent_message = Message(
                role='agent',
                parts=[TextPart(text='Hi there!', kind='text')],
                kind='message',
                message_id='msg-2',
            )
            await worker.update_task(task_id, state='completed', new_messages=[agent_message])

            await asyncio.wait_for(sub_task, timeout=1.0)

            # Should have: message, then final status
            assert len(received_events) == 2
            assert received_events[0]['kind'] == 'message'
            assert received_events[0]['role'] == 'agent'
            assert received_events[1]['kind'] == 'status-update'
            assert received_events[1]['final'] is True

    async def test_publishes_artifacts(self):
        """Test that artifacts are published."""
        broker = InMemoryBroker()
        storage = InMemoryStorage()
        worker = EchoWorker(broker=broker, storage=storage)

        async with broker:
            message = Message(
                role='user',
                parts=[TextPart(text='Hello', kind='text')],
                kind='message',
                message_id='msg-1',
            )
            task = await storage.submit_task('ctx-1', message)
            task_id = task['id']

            received_events: list[StreamEvent] = []

            async def subscriber():
                async for event in broker.subscribe_to_stream(task_id):
                    received_events.append(event)
                    if isinstance(event, dict) and event.get('final', False):
                        break

            sub_task = asyncio.create_task(subscriber())
            await asyncio.sleep(0.01)

            artifact = Artifact(
                artifact_id='art-1',
                name='result',
                parts=[TextPart(text='Result data', kind='text')],
            )
            await worker.update_task(task_id, state='completed', new_artifacts=[artifact])

            await asyncio.wait_for(sub_task, timeout=1.0)

            # Should have: artifact, then final status
            assert len(received_events) == 2
            assert received_events[0]['kind'] == 'artifact-update'
            artifact = received_events[0]['artifact']
            assert artifact.get('name') == 'result'  # Use .get() for NotRequired fields
            assert received_events[1]['kind'] == 'status-update'
            assert received_events[1]['final'] is True


# Tests for FastA2A streaming endpoint


class TestFastA2AStreaming:
    """Tests for the FastA2A message/stream SSE endpoint."""

    async def test_agent_card_shows_streaming_enabled(self):
        """Test that agent card reflects streaming capability."""
        async with create_streaming_app() as app:
            async with create_test_client(app) as client:
                response = await client.get('/.well-known/agent-card.json')
                assert response.status_code == 200
                data = response.json()
                assert data['capabilities']['streaming'] is True

    async def test_message_stream_returns_sse(self):
        """Test that message/stream returns SSE response."""
        async with create_streaming_app(response_text='Test response') as app:
            async with create_test_client(app) as client:
                payload = {
                    'jsonrpc': '2.0',
                    'id': 'test-request-1',
                    'method': 'message/stream',
                    'params': {
                        'message': {
                            'role': 'user',
                            'parts': [{'kind': 'text', 'text': 'Hello'}],
                            'kind': 'message',
                            'messageId': 'user-msg-1',
                        }
                    },
                }

                # Use streaming request
                async with client.stream('POST', '/', json=payload) as response:
                    assert response.status_code == 200
                    assert 'text/event-stream' in response.headers.get('content-type', '')

                    events: list[Any] = []
                    async for line in response.aiter_lines():
                        if line.startswith('data: '):
                            data = json.loads(line[6:])
                            events.append(data)

                    # Should have multiple events
                    assert len(events) >= 3  # task, working status, completed status

                    # First event should be the task
                    assert events[0]['result']['kind'] == 'task'
                    assert events[0]['result']['status']['state'] == 'submitted'

                    # Should have a working status
                    working_events = [
                        e
                        for e in events
                        if e['result'].get('kind') == 'status-update'
                        and e['result'].get('status', {}).get('state') == 'working'
                    ]
                    assert len(working_events) >= 1

                    # Should have a message with the response
                    message_events = [
                        e for e in events if e['result'].get('kind') == 'message' and e['result'].get('role') == 'agent'
                    ]
                    assert len(message_events) >= 1
                    assert 'Test response' in message_events[0]['result']['parts'][0]['text']

                    # Last event should be final status
                    final_events = [e for e in events if e['result'].get('final') is True]
                    assert len(final_events) >= 1

    async def test_message_stream_includes_context_id(self):
        """Test that streamed events include context_id."""
        async with create_streaming_app() as app:
            async with create_test_client(app) as client:
                context_id = str(uuid.uuid4())
                payload = {
                    'jsonrpc': '2.0',
                    'id': 'test-2',
                    'method': 'message/stream',
                    'params': {
                        'message': {
                            'role': 'user',
                            'parts': [{'kind': 'text', 'text': 'Hi'}],
                            'kind': 'message',
                            'messageId': 'msg-1',
                            'contextId': context_id,
                        }
                    },
                }

                async with client.stream('POST', '/', json=payload) as response:
                    events: list[Any] = []
                    async for line in response.aiter_lines():
                        if line.startswith('data: '):
                            events.append(json.loads(line[6:]))

                    # All events should have the same context_id
                    for event in events:
                        result = event['result']
                        event_context = result.get('contextId') or result.get('context_id')
                        assert event_context == context_id

    async def test_message_send_still_works(self):
        """Test that non-streaming message/send still works."""
        async with create_streaming_app() as app:
            async with create_test_client(app) as client:
                payload = {
                    'jsonrpc': '2.0',
                    'id': 'test-3',
                    'method': 'message/send',
                    'params': {
                        'message': {
                            'role': 'user',
                            'parts': [{'kind': 'text', 'text': 'Hello'}],
                            'kind': 'message',
                            'messageId': 'msg-1',
                        }
                    },
                }

                response = await client.post('/', json=payload)
                assert response.status_code == 200
                data = response.json()
                assert data['result']['kind'] == 'task'
                assert data['result']['status']['state'] == 'submitted'


# Tests for TaskManager stream_message


class TestTaskManagerStreamMessage:
    """Tests for TaskManager.stream_message method."""

    async def test_stream_message_yields_events_in_order(self):
        """Test that stream_message yields events: task, status updates, messages, final status."""
        broker = InMemoryBroker()
        storage = InMemoryStorage()

        # Use a longer delay to ensure we capture the initial task before it completes
        worker = EchoWorker(broker=broker, storage=storage, delay=0.2)

        async with broker:
            async with worker.run():
                from fasta2a.task_manager import TaskManager

                task_manager = TaskManager(broker=broker, storage=storage)
                async with task_manager:
                    request: StreamMessageRequest = {
                        'jsonrpc': '2.0',
                        'id': 'req-1',
                        'method': 'message/stream',
                        'params': MessageSendParams(
                            message=Message(
                                role='user',
                                parts=[TextPart(kind='text', text='Test')],
                                kind='message',
                                message_id='msg-1',
                            )
                        ),
                    }

                    events: list[StreamEvent] = []
                    async for event in task_manager.stream_message(request):
                        events.append(event)
                        # Stop when we get the final status
                        if isinstance(event, dict) and event.get('kind') == 'status-update' and event.get('final'):
                            break

                    # Should have at least: task, working status, message, final status
                    assert len(events) >= 4

                    # First event should be the task (submitted state)
                    assert events[0]['kind'] == 'task'

                    # Should have a working status update
                    working_events = [
                        e
                        for e in events
                        if e.get('kind') == 'status-update' and e.get('status', {}).get('state') == 'working'
                    ]
                    assert len(working_events) >= 1

                    # Should have an agent message
                    message_events = [e for e in events if e.get('kind') == 'message' and e.get('role') == 'agent']
                    assert len(message_events) >= 1

                    # Last event should be final status
                    assert events[-1]['kind'] == 'status-update'
                    assert events[-1]['final'] is True
                    assert events[-1]['status']['state'] == 'completed'
