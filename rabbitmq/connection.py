import socket

import rabbitmq.spec as spec
import rabbitmq.codec as codec
import rabbitmq.channel as channel
import rabbitmq.simplebuffer as simplebuffer
from rabbitmq.exceptions import *

class PlainCredentials:
    def __init__(self, username, password):
        self.username = username
        self.password = password

    def response_for(self, start):
        if 'PLAIN' not in start.mechanisms.split():
            return None
        return ('PLAIN', '\0%s\0%s' % (self.username, self.password))

class ConnectionParameters:
    def __init__(self, channel_max = 0, frame_max = 131072, heartbeat = 0):
        self.channel_max = channel_max
        self.frame_max = frame_max
        self.heartbeat = heartbeat

class Connection:
    def __init__(self,
                 host,
                 port = None,
                 virtual_host = "/",
                 credentials = None,
                 parameters = None,
                 wait_for_open = True):
        self.state = codec.ConnectionState()
        self.credentials = credentials or PlainCredentials('guest', 'guest')
        self.virtual_host = virtual_host
        self.parameters = parameters or ConnectionParameters()
        self.outbound_buffer = simplebuffer.SimpleBuffer()
        self.frame_handler = self._login1
        self.connection_open = False
        self.connection_close = None
        self.channels = {}
        self.next_channel = 0

        self.amqp_connect(host, port or spec.PORT)
        self.send_frame(self._local_protocol_header())

        if wait_for_open:
            self.wait_for_open()

    def amqp_connect(self, host, port):
        raise NotImplementedError('Subclass Responsibility')

    def _local_protocol_header(self):
        return codec.FrameProtocolHeader(1,
                                         1,
                                         spec.PROTOCOL_VERSION[0],
                                         spec.PROTOCOL_VERSION[1])

    def handle_connect(self):
        ## TODO: split out into asyncore-specific subclass
        pass

    def _set_connection_close(self, c):
        if not self.connection_close:
            self.connection_close = c
            for chan in self.channels.values():
                chan._set_channel_close(c)

    def close(self):
        if self.connection_open:
            self.connection_open = False
            c = spec.Connection.Close(reply_code = 200,
                                      reply_text = 'Normal shutdown',
                                      class_id = 0,
                                      method_id = 0)
            self._rpc(0, c, [spec.Connection.CloseOk])
            self._set_connection_close(c)
        self.shutdown_event_loop()

    def shutdown_event_loop(self):
        """Subclasses should override this as required to implement
        event-dispatcher shutdown logic."""
        pass

    def handle_close(self):
        ## TODO: split out into asyncore-specific subclass
        self._set_connection_close(spec.Connection.Close(reply_code = 0,
                                                         reply_text = 'Socket closed',
                                                         class_id = 0,
                                                         method_id = 0))
        self.close() ## asyncore

    def handle_read(self):
        ## TODO: split out into asyncore-specific subclass
        b = self.state.channel_max
        if not b: b = 131072

        try:
            buf = self.recv(b) ## asyncore
        except socket.error:
            self.handle_close()
            raise

        if not buf:
            self.close() ## asyncore
            return

        while buf:
            (consumed_count, frame) = self.state.handle_input(buf)
            buf = buf[consumed_count:]
            if frame:
                self.frame_handler(frame)

    def writable(self):
        return True if len(self.outbound_buffer) else False

    def handle_write(self):
        ## TODO: split out into asyncore-specific subclass
        r = self.send(self.outbound_buffer.read())
        self.outbound_buffer.consume(r)

    def _next_channel_number(self):
        tries = 0
        limit = self.state.channel_max or 32767
        while self.next_channel in self.channels:
            self.next_channel = (self.next_channel + 1) % limit
            tries = tries + 1
            if self.next_channel == 0:
                self.next_channel = 1
            if tries > limit:
                raise NoFreeChannels()
        return self.next_channel

    def _set_channel(self, channel_number, channel):
        self.channels[channel_number] = channel

    def _ensure_channel(self, channel_number):
        if self.connection_close:
            raise ConnectionClosed(self.connection_close)
        return self.channels[channel_number]._ensure()

    def reset_channel(self, channel_number):
        if channel_number in self.channels:
            del self.channels[channel_number]

    def send_frame(self, frame):
        self.outbound_buffer.write( frame.marshal() )
        #print 'Wrote %r' % (frame, )

    def send_method(self, channel_number, method, content = None):
        self.send_frame(codec.FrameMethod(channel_number, method))
        props = None
        body = None
        if isinstance(content, tuple):
            props = content[0]
            body = content[1]
        else:
            body = content
        if props:
            length = 0
            if body: length = len(body)
            self.send_frame(codec.FrameHeader(channel_number, length, props))
        if body:
            maxpiece = (self.state.frame_max - \
                        codec.ConnectionState.HEADER_SIZE - \
                        codec.ConnectionState.FOOTER_SIZE)
            body_buf = simplebuffer.SimpleBuffer( body )
            while body_buf:
                piecelen = min(len(body_buf), maxpiece)
                piece = body_buf.read_and_consume( piecelen )
                self.send_frame(codec.FrameBody(channel_number, piece))

    def _rpc(self, channel_number, method, acceptable_replies):
        channel = self._ensure_channel(channel_number)
        self.send_method(channel_number, method)
        return channel.wait_for_reply(acceptable_replies)

    def _login1(self, frame):
        if isinstance(frame, codec.FrameProtocolHeader):
            raise ProtocolVersionMismatch(self._local_protocol_header,
                                          frame)

        response = self.credentials.response_for(frame.method)
        if not response:
            raise LoginError("No acceptable SASL mechanism for the given credentials",
                             credentials)
        self.send_method(0, spec.Connection.StartOk(client_properties = \
                                                      {"product": "RabbitMQ Python"},
                                                    mechanism = response[0],
                                                    response = response[1]))
        self._erase_credentials()
        self.frame_handler = self._login2

    def _erase_credentials(self):
        self.credentials = None

    def _login2(self, frame):
        channel_max = combine_tuning(self.parameters.channel_max, frame.method.channel_max)
        frame_max = combine_tuning(self.parameters.frame_max, frame.method.frame_max)
        self.state.tune(channel_max, frame_max)
        self.send_method(0, spec.Connection.TuneOk(
            channel_max = channel_max,
            frame_max = frame_max,
            heartbeat = combine_tuning(self.parameters.heartbeat, frame.method.heartbeat)))
        self.frame_handler = self._generic_frame_handler
        self._install_channel0()
        self.known_hosts = \
                         self._rpc(0, spec.Connection.Open(virtual_host = self.virtual_host,
                                                           insist = True),
                                   [spec.Connection.OpenOk]).known_hosts
        self.connection_open = True
        self.handle_connection_open()

    def _install_channel0(self):
        c = channel.ChannelHandler(self, 0)
        c.async_map[spec.Connection.Close] = self._async_connection_close

    def channel(self):
        return channel.Channel(channel.ChannelHandler(self))

    def wait_for_open(self):
        while not self.connection_open and not self.connection_close:
            self.drain_events()

    def drain_events(self):
        """Subclasses should override as required to wait for a few
        events -- perhaps running the dispatch loop once, or a small
        number of times -- and dispatch them, and then to return
        control to this method's caller, which will be waiting for
        something to have been set by one of the event handlers."""
        raise NotImplementedError('Subclass Responsibility')

    def handle_connection_open(self):
        pass

    def handle_connection_close(self):
        pass

    def _async_connection_close(self, method_frame, header_frame, body):
        self._set_connection_close(method_frame.method)
        self.connection_open = False
        self.send_method(0, spec.Connection.CloseOk())
        self.handle_connection_close()

    def _generic_frame_handler(self, frame):
        #print "GENERIC_FRAME_HANDLER", frame
        if isinstance(frame, codec.FrameHeartbeat):
            self.send_frame(frame) # echo the heartbeat
        else:
            self.channels[frame.channel_number].frame_handler(frame)

def combine_tuning(a, b):
    if a == 0: return b
    if b == 0: return a
    return min(a, b)
