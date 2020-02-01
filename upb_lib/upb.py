"""Master class that combines all UPB pieces together."""

import asyncio
from functools import partial
import logging
from importlib import import_module
import serial_asyncio

from .message import get_control_word, encode_message, MessageDecode
from .parse_upstart import process_upstart_file
from .proto import Connection
from .util import parse_url
from .lights import Lights
from .links import Links

LOG = logging.getLogger(__name__)


class UpbPim:
    """Represents all the components on an UPB PIM."""

    def __init__(self, host=None, port=2101, serial=None, reconnect_callback=None, loop=None):
        """Initialize a new PIM instance."""
        self.loop = loop if loop else asyncio.get_event_loop()
        self._host = host
        self._port = port
        self._serial = serial
        self._conn = None
        self._transport = None
        self.connection_lost_callbk = None
        self.reconnect_callback = reconnect_callback
        self._connection_retry_timer = 1
        self._message_decode = MessageDecode()
        self._sync_handlers = []
        self._heartbeat = None
        self.lights = Lights(self)
        self.links = Links(self)

    def _create_element(self, element):
        module = import_module("upb_lib." + element)
        class_ = getattr(module, element.capitalize())
        setattr(self, element, class_(self))

    async def _connect(self, connection_lost_callbk=None):
        """Asyncio connection to UPB."""
        self.connection_lost_callbk = connection_lost_callbk
        conn = partial(
            Connection,
            self.loop,
            self._connected,
            self._disconnected,
            self._got_data,
            self._timeout,
        )
        try:
            if self._serial:
                LOG.info("Connecting to UPB PIM at %s", self._serial)
                await serial_asyncio.create_serial_connection(
                    self.loop, conn, self._serial, baudrate=param
                )
            elif self._host:
                LOG.info("Connecting to UPB PIM at host %s port %d", self._host, self._port)
                await asyncio.wait_for(
                    self.loop.create_connection(conn, host=self._host, port=self._port),
                    timeout=30,
                )
            else:
                raise Exception('Please set the host or serial PIM connection.')
        except (ValueError, OSError, asyncio.TimeoutError) as err:
            LOG.warning(
                "Could not connect to UPB PIM (%s). Retrying in %d seconds",
                err,
                self._connection_retry_timer,
            )
            self.loop.call_later(self._connection_retry_timer, self.connect)
            self._connection_retry_timer = (
                2 * self._connection_retry_timer
                if self._connection_retry_timer < 32
                else 60
            )

    def _connected(self, transport, conn):
        """Login and sync the UPB PIM panel to memory."""
        LOG.info("Connected to UPB PIM")
        self._conn = conn
        self._transport = transport
        self._connection_retry_timer = 1
        self.reconnect_callback(self)

        # The intention of this is to clear anything in the PIM receive buffer.
        # A number of times on startup error(s) (PE) are returned. This too will
        # return an error, but hopefully resets the PIM
        #self.send("")

        self.call_sync_handlers()
        # control = get_control_word(link=False)
        # self.send(encode_message(control, 194, 8, 255, 0x10, bytearray([0,16])))
        # self.send(encode_message(control, 194, 8, 255, 0x10, bytearray([16,16])))
        # self.send(encode_message(control, 194, 8, 255, 0x10, bytearray([32,16])))
        # self.send(encode_message(control, 194, 8, 255, 0x10, bytearray([48,16])))
        # self.send(encode_message(control, 194, 8, 255, 0x10, bytearray([64,16])))
        # self.send(encode_message(control, 194, 8, 255, 0x10, bytearray([80,16])))
        # self.send(encode_message(control, 194, 9, 255, 0x30))

    def _reset_connection(self):
        LOG.warning("PIM connection heartbeat timed out, disconnecting")
        self._transport.close()
        self._heartbeat = None

    def _disconnected(self):
        self._conn = None
        self.loop.call_later(self._connection_retry_timer, self.connect)
        if self._heartbeat:
            self._heartbeat.cancel()
            self._heartbeat = None

    def add_handler(self, msg_type, handler):
        self._message_decode.add_handler(msg_type, handler)

    def _got_data(self, data):  # pylint: disable=no-self-use
        try:
            self._message_decode.decode(data)
        except (ValueError, AttributeError) as err:
            LOG.debug(err)

    def _timeout(self, msg_code):
        self._message_decode.timeout_handler(msg_code)

    def add_sync_handler(self, sync_handler):
        """Register a fn that synchronizes part of the panel."""
        self._sync_handlers.append(sync_handler)

    def call_sync_handlers(self):
        """Invoke the synchronization handlers."""
        LOG.debug("Synchronizing panel...")
        for sync_handler in self._sync_handlers:
            sync_handler()

    def is_connected(self):
        """Status of connection to PIM."""
        return self._conn is not None

    def connect(self):
        """Connect to the panel"""
        asyncio.ensure_future(self._connect())

    def run(self):
        """Enter the asyncio loop."""
        self.loop.run_forever()

    def send(self, msg):
        """Send a message to UPB PIM."""
        if self._conn:
            self._conn.write_data(msg)

    def pause(self):
        """Pause the connection from sending/receiving."""
        if self._conn:
            self._conn.pause()

    def resume(self):
        """Restart the connection from sending/receiving."""
        if self._conn:
            self._conn.resume()
