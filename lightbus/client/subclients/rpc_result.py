import asyncio
import logging
import time
from asyncio import iscoroutinefunction, iscoroutine
from typing import List

from lightbus.api import Api
from lightbus.client import commands
from lightbus.client.commands import ConsumeRpcsCommand
from lightbus.client.subclients.base import BaseSubClient
from lightbus.client.utilities import validate_event_or_rpc_name, Error
from lightbus.client.validator import validate_outgoing, validate_incoming
from lightbus.exceptions import (
    NoApisToListenOn,
    SuddenDeathException,
    LightbusServerError,
    LightbusTimeout,
)
from lightbus.log import L, Bold
from lightbus.message import ResultMessage, RpcMessage
from lightbus.utilities.async_tools import run_user_provided_callable, cancel
from lightbus.utilities.casting import cast_to_signature
from lightbus.utilities.deforming import deform_to_bus
from lightbus.utilities.frozendict import frozendict
from lightbus.utilities.human import human_time
from lightbus.utilities.singledispatch import singledispatchmethod

logger = logging.getLogger(__name__)


async def bail_on_error(error_queue: asyncio.Queue, co):

    assert iscoroutine(co), "@bail_on_error only operates on coroutines"
    # TODO: May need to pass a fresh error queue through to ensure
    #       we don't compete with the bus client's event monitor
    fn_task = asyncio.ensure_future(co)
    monitor_task = asyncio.ensure_future(error_queue.get())

    done, pending = await asyncio.wait({fn_task, monitor_task}, return_when=asyncio.FIRST_COMPLETED)

    if fn_task in done:
        # All ok
        await cancel(monitor_task)
        return fn_task.result()
    else:
        # An error appeared in the queue
        await cancel(fn_task)
        error: Error = monitor_task.result()
        raise error.value


class RpcResultClient(BaseSubClient):
    """Functionality for both RPCs and results"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def consume_rpcs(self, apis: List[Api] = None):
        """Start a background task to consume RPCs

        This will consumer RPCs on APIs which have been registered with this
        bus client.
        """
        if apis is None:
            apis = self.api_registry.all()

        if not apis:
            raise NoApisToListenOn(
                "No APIs to consume on in consume_rpcs(). Either this method was called with apis=[], "
                "or the API registry is empty."
            )

        api_names = [api.meta.name for api in apis]

        await self.producer.send(ConsumeRpcsCommand(api_names=api_names)).wait()

    async def call_rpc_remote(
        self, api_name: str, name: str, kwargs: dict = frozendict(), options: dict = frozendict()
    ):
        """ Perform an RPC call

        Call an RPC and return the result.
        """
        # TODO: InternalProducer command
        kwargs = deform_to_bus(kwargs)
        rpc_message = RpcMessage(api_name=api_name, procedure_name=name, kwargs=kwargs)
        validate_event_or_rpc_name(api_name, "rpc", name)

        logger.info("📞  Calling remote RPC {}.{}".format(Bold(api_name), Bold(name)))

        start_time = time.time()
        # TODO: It is possible that the RPC will be called before we start waiting for the
        #       response. This is bad.

        validate_outgoing(self.config, self.schema, rpc_message)

        await self._execute_hook("before_rpc_call", rpc_message=rpc_message)

        result_queue = asyncio.Queue()

        # Send the RPC
        await self.producer.send(
            commands.CallRpcCommand(message=rpc_message, options=options)
        ).wait()

        # Start a listener which will wait for results
        await self.producer.send(
            commands.ReceiveResultCommand(
                message=rpc_message, destination_queue=result_queue, options=options
            )
        ).wait()

        # Wait for the result from the listener we started.
        # The RpcResultDock will handle timeouts
        result = await bail_on_error(self.error_queue, result_queue.get())

        call_time = time.time() - start_time

        try:
            if isinstance(result, Exception):
                raise result
        except asyncio.TimeoutError:
            # TODO: Remove RPC from queue. Perhaps add a RpcBackend.cancel() method. Optional,
            #       as not all backends will support it. No point processing calls which have timed out.
            raise LightbusTimeout(
                f"Timeout when calling RPC {rpc_message.canonical_name} after waiting for {human_time(call_time)}. "
                f"It is possible no Lightbus process is serving this API, or perhaps it is taking "
                f"too long to process the request. In which case consider raising the 'rpc_timeout' "
                f"config option."
            ) from None
        else:
            assert isinstance(result, ResultMessage)
            result_message = result

        await self._execute_hook(
            "after_rpc_call", rpc_message=rpc_message, result_message=result_message
        )

        if not result_message.error:
            logger.info(
                L(
                    "🏁  Remote call of {} completed in {}",
                    Bold(rpc_message.canonical_name),
                    human_time(call_time),
                )
            )
        else:
            logger.warning(
                L(
                    "⚡ Server error during remote call of {}. Took {}: {}",
                    Bold(rpc_message.canonical_name),
                    human_time(call_time),
                    result_message.result,
                )
            )
            raise LightbusServerError(
                "Error while calling {}: {}\nRemote stack trace:\n{}".format(
                    rpc_message.canonical_name, result_message.result, result_message.trace
                )
            )

        validate_incoming(self.config, self.schema, result_message)

        return result_message.result

    async def _call_rpc_local(self, api_name: str, name: str, kwargs: dict = frozendict()):
        # TODO: InternalProducer command
        api = self.api_registry.get(api_name)
        validate_event_or_rpc_name(api_name, "rpc", name)

        start_time = time.time()
        try:
            method = getattr(api, name)
            if self.config.api(api_name).cast_values:
                kwargs = cast_to_signature(kwargs, method)
            result = await run_user_provided_callable(method, args=[], kwargs=kwargs)
        except (asyncio.CancelledError, SuddenDeathException):
            raise
        except Exception as e:
            logging.exception(e)
            logger.warning(
                L(
                    "⚡  Error while executing {}.{}. Took {}",
                    Bold(api_name),
                    Bold(name),
                    human_time(time.time() - start_time),
                )
            )
            raise
        else:
            logger.info(
                L(
                    "⚡  Executed {}.{} in {}",
                    Bold(api_name),
                    Bold(name),
                    human_time(time.time() - start_time),
                )
            )
            return result

    async def close(self):
        await self.producer.send(commands.CloseCommand()).wait()

        await self.consumer.close()
        await self.producer.close()

    @singledispatchmethod
    async def handle(self, command):
        raise NotImplementedError(f"Did not recognise command {command.__class__.__name__}")

    @handle.register
    async def handle_execute_rpc(self, command: commands.ExecuteRpcCommand):
        validate_incoming(self.config, self.schema, command.message)

        await self._execute_hook("before_rpc_execution", rpc_message=command.message)
        try:
            result = await self._call_rpc_local(
                api_name=command.message.api_name,
                name=command.message.procedure_name,
                kwargs=command.message.kwargs,
            )
        except SuddenDeathException:
            # Used to simulate message failure for testing
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            result = e
        else:
            result = deform_to_bus(result)

        result_message = ResultMessage(
            result=result,
            rpc_message_id=command.message.id,
            api_name=command.message.api_name,
            procedure_name=command.message.procedure_name,
        )
        await self._execute_hook(
            "after_rpc_execution", rpc_message=command.message, result_message=result_message
        )

        if not result_message.error:
            validate_outgoing(self.config, self.schema, result_message)

        await self.producer.send(
            commands.SendResultCommand(message=result_message, rpc_message=command.message)
        ).wait()
