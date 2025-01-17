import asyncio
import json
import logging
from typing import List, Optional
import uuid
from flet import constants
from flet.connection import Connection
import websockets.client as ws_client
from websockets.client import WebSocketClientProtocol
from flet.protocol import *
from asyncio.queues import Queue


class AsyncConnection(Connection):
    __CONNECT_TIMEOUT = 0.2
    __CONNECT_ATTEMPTS = 50

    def __init__(
        self,
        server_address: str,
        page_name: str,
        auth_token: Optional[str],
        on_event=None,
        on_session_created=None,
    ):
        super().__init__()
        self.__send_queue = Queue(1)
        self.page_name = page_name
        self.__server_address = server_address
        self.__is_reconnecting = False
        self.__host_client_id: Optional[str] = None
        self.__auth_token = auth_token
        self.__ws_callbacks = {}
        self.__on_event = on_event
        self.__on_session_created = on_session_created

    async def connect(self):
        ws_url = self._get_ws_url(self.__server_address)
        logging.debug(f"Connecting via WebSockets to {ws_url}...")

        attempt = self.__CONNECT_ATTEMPTS
        while True:
            try:
                self.__ws: WebSocketClientProtocol = await ws_client.connect(ws_url)
                break
            except Exception as e:
                logging.debug(f"Error connecting to Flet server: {e}")
                if attempt == 0 and not self.__is_reconnecting:
                    raise Exception(
                        f"Failed to connect Flet server in {self.__CONNECT_ATTEMPTS} attempts."
                    )
                attempt -= 1
                await asyncio.sleep(self.__CONNECT_TIMEOUT)
        logging.debug(f"Connected to Flet server {self.__server_address}")
        self.__is_reconnecting = True

        # start send/receive loops
        asyncio.get_event_loop().create_task(self.__start_loops())

        await self.__register_host_client()

    async def __register_host_client(self):
        payload = RegisterHostClientRequestPayload(
            hostClientID=self.__host_client_id,
            pageName=self.page_name,
            isApp=True,
            update=False,
            authToken=self.__auth_token,
            permissions=None,
        )
        response = await self._send_message_with_result(
            Actions.REGISTER_HOST_CLIENT, payload
        )
        register_result = RegisterHostClientResponsePayload(**response)
        self.__host_client_id = register_result.hostClientID
        self.page_name = register_result.pageName
        self.page_url = self.__server_address.rstrip("/")
        if self.page_name != constants.INDEX_PAGE:
            self.page_url += f"/{self.page_name}"

    async def __start_loops(self):
        self.__receive_loop_task = asyncio.create_task(self.__receive_looop())
        self.__send_loop_task = asyncio.create_task(self.__send_loop())
        done, pending = await asyncio.wait(
            [self.__receive_loop_task, self.__send_loop_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        failed = False
        for task in done:
            name = task.get_name()
            exception = task.exception()
            if isinstance(exception, Exception):
                logging.error(f"{name} threw {exception}")
                failed = True
        for task in pending:
            task.cancel()

        # re-connect if one of tasks failed
        if failed:
            logging.debug(f"Re-connecting to Flet server in 1 second")
            await asyncio.sleep(self.__CONNECT_TIMEOUT)
            await self.connect()

    async def __on_ws_message(self, data):
        logging.debug(f"_on_message: {data}")
        msg_dict = json.loads(data)
        msg = Message(**msg_dict)
        if msg.id:
            # callback
            evt = self.__ws_callbacks[msg.id][0]
            self.__ws_callbacks[msg.id] = (None, msg.payload)
            evt.set()
        elif msg.action == Actions.PAGE_EVENT_TO_HOST:
            if self.__on_event is not None:
                asyncio.create_task(self.__on_event(PageEventPayload(**msg.payload)))
        elif msg.action == Actions.SESSION_CREATED:
            if self.__on_session_created is not None:
                asyncio.create_task(
                    self.__on_session_created(PageSessionCreatedPayload(**msg.payload))
                )
        else:
            # it's something else
            print(msg.payload)

    async def __receive_looop(self):
        async for message in self.__ws:
            await self.__on_ws_message(message)

    async def __send_loop(self):
        while True:
            message = await self.__send_queue.get()
            try:
                await self.__ws.send(message)
            except:
                # re-enqueue the message to repeat it when re-connected
                self.__send_queue.put_nowait(message)
                raise

    async def send_command_async(self, session_id: str, command: Command):
        assert self.page_name is not None
        payload = PageCommandRequestPayload(self.page_name, session_id, command)
        response = await self._send_message_with_result(
            Actions.PAGE_COMMAND_FROM_HOST, payload
        )
        result = PageCommandResponsePayload(**response)
        if result.error:
            raise Exception(result.error)
        return result

    async def send_commands_async(self, session_id: str, commands: List[Command]):
        assert self.page_name is not None
        payload = PageCommandsBatchRequestPayload(self.page_name, session_id, commands)
        response = await self._send_message_with_result(
            Actions.PAGE_COMMANDS_BATCH_FROM_HOST, payload
        )
        result = PageCommandsBatchResponsePayload(**response)
        if result.error:
            raise Exception(result.error)
        return result

    async def _send_message_with_result(self, action_name, payload):
        msg_id = uuid.uuid4().hex
        msg = Message(msg_id, action_name, payload)
        j = json.dumps(msg, cls=CommandEncoder, separators=(",", ":"))
        logging.debug(f"_send_message_with_result: {j}")
        evt = asyncio.Event()
        self.__ws_callbacks[msg_id] = (evt, None)
        await self.__send_queue.put(j)
        await evt.wait()
        return self.__ws_callbacks.pop(msg_id)[1]

    async def close(self):
        logging.debug("Closing WebSockets connection...")
        if self.__receive_loop_task:
            self.__receive_loop_task.cancel()
        if self.__send_loop_task:
            self.__send_loop_task.cancel()
        if self.__ws:
            try:
                await self.__ws.close()
            except:
                pass  # do nothing
