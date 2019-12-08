import asyncio

from lightbus.client.docks.base import BaseDock
from lightbus.client.utilities import queue_exception_checker
from lightbus.client import commands
from lightbus.utilities.async_tools import cancel
from lightbus.utilities.singledispatch import singledispatchmethod


class EventDock(BaseDock):
    """ Takes internal Lightbus commands and performs interactions with the Event transport
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.listener_tasks = set()

    @singledispatchmethod
    async def handle(self, command):
        raise NotImplementedError(f"Did not recognise command {command.__name__}")

    @handle.register
    async def handle_consume_events(self, command: commands.ConsumeEventsCommand):
        event_transport_pools = self.transport_registry.get_event_transport_pools(
            api_names=[api_name for api_name, _ in command.events]
        )

        async def listener(event_transport_pool_, events_):
            async with event_transport_pool_ as event_transport:
                consumer = event_transport.consume(
                    listen_for=events_,
                    listener_name=command.listener_name,
                    error_queue=self.error_queue,
                    **command.options,
                )
                async for event_messages in consumer:
                    for event_message in event_messages:
                        await command.destination_queue.put(event_message)

        tasks = []
        for event_transport_pool, api_names in event_transport_pools:
            # Create a listener task for each event transport,
            # passing each a list of events for which it should listen
            events = [
                (api_name, event_name)
                for api_name, event_name in command.events
                if api_name in api_names
            ]

            task = asyncio.ensure_future(listener(event_transport_pool, events))
            task.is_listener = True  # Used by close()
            tasks.append(task)

        listener_task = asyncio.gather(*tasks)

        exception_checker = queue_exception_checker(queue=self.error_queue)
        listener_task.add_done_callback(exception_checker)

        # Setting is_listener lets Client.close() know that it should mop up this
        # task automatically on shutdown
        listener_task.is_listener = True

        self.listener_tasks.add(listener_task)

    @handle.register
    async def handle_consume_events(self, command: commands.CloseCommand):
        await cancel(*self.listener_tasks)

        for event_transport in self.transport_registry.get_event_transport_pools():
            await event_transport.close()
