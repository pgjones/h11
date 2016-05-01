# We model the joint state of the client and server as a pair of finite state
# automata: one for the client and one for the server. Transitions in each
# machine can be triggered by either local or remote events. (For example, the
# client sending a Request triggers both the client to move to SENDING-BODY
# and the server to move to SENDING-RESPONSE.)

# Import all event types
from .events import *
# Import all state sentinels
from .state import *
# Import the internal things we need
from .util import ProtocolError
from .state import ConnectionState
from .headers import (
    get_comma_header, set_comma_header, get_framing_headers,
    has_expect_100_continue,
)
from .receivebuffer import ReceiveBuffer
from .readers import READERS
from .writers import WRITERS

# Everything in __all__ gets re-exported as part of the h11 public API.
__all__ = ["Connection"]

# If we ever have this much buffered without it making a complete parseable
# event, we error out.
# Value copied from node.js's http_parser.c's HTTP_MAX_HEADER_SIZE (Headers
# are the only time we really buffer, so the effect is the same.)
HTTP_MAX_BUFFER_SIZE = 80 * 1024

# RFC 7230's rules for connection lifecycles:
# - If either side says they want to close the connection, then the connection
#   must close.
# - HTTP/1.1 defaults to keep-alive unless someone says Connection: close
# - HTTP/1.0 defaults to close unless both sides say Connection: keep-alive
#   (and even this is a mess -- e.g. if you're implementing a proxy then
#   sending Connection: keep-alive is forbidden).
#
# We simplify life by simply not supporting keep-alive with HTTP/1.0 peers. So
# our rule is:
# - If someone says Connection: close, we will close
# - If someone uses HTTP/1.0, we will close.
def _keep_alive(event):
    connection = get_comma_header(event.headers, "Connection")
    if b"close" in connection:
        return False
    if getattr(event, "http_version", b"1.1") < b"1.1":
        return False
    return True


# See https://tools.ietf.org/html/rfc7230#section-3.3.3
def _response_allows_body(request_method, response):
    if (response.status_code < 200
        or response.status_code in (204, 304)
        or request_method == b"HEAD"
        or (request_method == b"CONNECT"
            and 200 <= response.status_code < 300)):
        return False
    else:
        return True


def _server_switched_protocol(request_method, event):
    if (type(event) is Response
        and request_method == b"CONNECT"
        and 200 <= event.status_code < 300):
        # successful CONNECT response
        return True
    if (type(event) is InformationalResponse
        and event.status_code == 101):
        # successful Upgrade: response (101 Switching Protocol)
        return True
    return False


def _client_requests_protocol_switch(event):
    assert type(event) is Request
    if event.method == b"CONNECT":
        return True
    upgrade = get_comma_header(event.headers, "Upgrade")
    if upgrade:
        return True
    return False

################################################################
#
# The main Connection class
#
################################################################

class Connection:
    def __init__(self, our_role):
        # State and role tracking
        if our_role not in (CLIENT, SERVER):
            raise ValueError(
                "expected CLIENT or SERVER, not {!r}".format(our_role))
        self.our_role = our_role
        if our_role is CLIENT:
            self.their_role = SERVER
        else:
            self.their_role = CLIENT
        self._cstate = ConnectionState()

        # Callables for converting data->events or vice-versa given the
        # current state
        self._writer = self._get_io_object(self.our_role, None, WRITERS)
        self._reader = self._get_io_object(self.their_role, None, READERS)

        # Holds any unprocessed received data
        self._receive_buffer = ReceiveBuffer()
        # If this is true, then it indicates that the incoming connection was
        # closed *after* the end of whatever's in self._receive_buffer:
        self._receive_buffer_closed = False

        # Extra bits of state
        self._request_method = None
        self.their_http_version = None
        self.client_is_waiting_for_100_continue = False

    def state_of(self, role):
        return self._cstate.state(role)

    @property
    def client_state(self):
        return self._cstate.state(CLIENT)

    @property
    def server_state(self):
        return self._cstate.state(SERVER)

    @property
    def our_state(self):
        return self._cstate.state(self.our_role)

    @property
    def their_state(self):
        return self._cstate.state(self.their_role)

    @property
    def they_are_waiting_for_100_continue(self):
        return (self.their_role is CLIENT
                and self.client_is_waiting_for_100_continue)

    @property
    def can_reuse(self):
        return self._cstate.can_reuse

    def prepare_to_reuse(self):
        self._cstate.prepare_to_reuse()
        self._request_method = None
        # self.their_http_version gets left alone, since it presumably lasts
        # beyond a single request/response cycle
        assert not self.client_is_waiting_for_100_continue
        assert self._cstate.keep_alive
        assert not self._cstate.client_requested_protocol_switch

    def _get_io_object(self, role, event, io_dict):
        state = self._cstate.state(role)
        if state is SEND_BODY:
            # Special case: the io_dict has a dict of reader/writer factories
            # that depend on the request/response framing.
            return self._get_send_body_object(role, event, io_dict[SEND_BODY])
        else:
            # General case: the io_dict just has the appropriate reader/writer
            # for this state
            return io_dict.get((role, state))

    def _get_send_body_object(self, role, event, send_body_dict):
        if (type(event) is Response
            and not _response_allows_body(self._request_method, event)):
            # Body is empty, no matter what the headers say (e.g. HEAD)
            return send_body_dict["content-length"](0)
        # Otherwise, trust the headers
        (transfer_encoding, content_length) = get_framing_headers(event.headers)
        if transfer_encoding:
            assert transfer_encoding == b"chunked"
            return send_body_dict["chunked"]()
        if content_length:
            return send_body_dict["content-length"](content_length)
        if type(event) is Response:
            return send_body_dict["http/1.0"]()
        else:
            return send_body_dict["content-length"](0)

    # All events and state machine updates go through here.
    def _process_event(self, role, event):
        # First, pass the event through the state machine to make sure it
        # succeeds.
        switched_protocol =_server_switched_protocol(self._request_method,
                                                     event)
        changed = self._cstate.process_event(role,
                                             type(event),
                                             switched_protocol)

        # Then perform the updates triggered by it.

        # self._request_method
        if type(event) is Request:
            self._request_method = event.method

        # self.their_http_version
        if type(event) in (Request, Response, InformationalResponse):
            self.their_http_version = event.http_version

        # Keep alive handling
        #
        # RFC 7230 doesn't really say what one should do if Connection: close
        # shows up on a 1xx InformationalResponse. I think the idea is that
        # this is not supposed to happen. In any case, if it does happen, we
        # ignore it.
        if type(event) in (Request, Response) and not _keep_alive(event):
            self._cstate.keep_alive = False

        # client side of Upgrade/CONNECT
        if type(event) is Request and _client_requests_protocol_switch(event):
            self._cstate.client_requested_protocol_switch = True
        # server side of Upgrade/CONNECT is handled above

        # 100-continue
        if type(event) is Request and has_expect_100_continue(event):
            self.client_is_waiting_for_100_continue = True
        if type(event) in (InformationalResponse, Response):
            self.client_is_waiting_for_100_continue = False

        # Update reader/writer
        if self.our_role in changed:
            self._writer = self._get_io_object(self.our_role, event, WRITERS)
        if self.their_role in changed:
            print("their state changed to", self.their_state)
            self._reader = self._get_io_object(self.their_role, event, READERS)
            print("new reader is ", self._reader)

    @property
    def trailing_data(self):
        return bytes(self._receive_buffer)

    # Argument interpretation:
    # - b""  -> connection closed
    # - None -> no new data, just check for whether any events have become
    #           available (useful iff we were in Paused state)
    # - data -> bytes-like of data received
    def receive_data(self, data):
        print("Received {} new bytes".format(len(data)))
        if data is not None:
            if data:
                self._receive_buffer += data
            else:
                self._receive_buffer_closed = True

        events = []
        while True:
            state = self.their_state
            print("Looping in state", state)
            # We don't pause immediately when they enter DONE, because even in
            # DONE state we can still process a ConnectionClosed() event. But
            # if we have data in our buffer, then we definitely aren't getting
            # a ConnectionClosed() immediately and we need to pause.
            if state is DONE and self._receive_buffer:
                events.append(Paused(reason="need-reset"))
                break
            if state is MIGHT_SWITCH_PROTOCOL:
                events.append(Paused(reason="might-switch-protocol"))
                break
            if state is SWITCHED_PROTOCOL:
                events.append(Paused(reason="switched-protocol"))
                break
            if not self._receive_buffer and self._receive_buffer_closed:
                print("processing close")
                if hasattr(self._reader, "read_eof"):
                    print("{!r} has read_eof".format(self._reader))
                    event = self._reader.read_eof()
                    print("read_eof() returned", event)
                elif state is CLOSED:
                    break
                else:
                    print("{!r} does NOT have read_eof".format(self._reader))
                    event = ConnectionClosed()
            else:
                if self._reader is None:
                    if self._receive_buffer:
                        raise ProtocolError(
                            "unexpectedly received data in state {}"
                            .format(state))
                    else:
                        # Terminal state like MUST_CLOSE with no data... no
                        # problem, nothing to do, perhaps they'll close it in
                        # a moment.
                        break
                print("calling reader", self._reader)
                event = self._reader(self._receive_buffer)
                print("it returned:", event)
            if event is None:
                if len(self._receive_buffer) > HTTP_MAX_BUFFER_SIZE:
                    # 414 is "Request-URI Too Long" which is not quite
                    # accurate because we'll also issue this if someone tries
                    # to send e.g. a megabyte of headers, but whatever.
                    raise ProtocolError("Receive buffer too long",
                                        error_status_hint=414)
                break
            self._process_event(self.their_role, event)
            events.append(event)
            if type(event) is ConnectionClosed:
                break
        self._receive_buffer.compress()
        print("Returning {} new events".format(len(events)))
        return events

    def send(self, event, *, combine=True):
        if type(event) is Response:
            self._clean_up_response_headers_for_sending(event)
        # We want to process the event locally before actually sending it, so
        # that if processing it throws an error then nothing happens. But
        # processing it may change self._writer. So we save self._writer now
        # and then call it after. Special case: sending ConnectionClosed()
        # skips the writer.
        writer = self._writer
        self._process_event(self.our_role, event)
        if type(event) is ConnectionClosed:
            return None
        else:
            data_list = []
            writer(event, data_list.append)
            if combine:
                return b"".join(data_list)
            else:
                return data_list

    # When sending a Response, we take responsibility for a few things:
    #
    # - Sometimes you MUST set Connection: close. We take care of those
    #   times. (You can also set it yourself if you want, and if you do then
    #   we'll respect that and close the connection at the right time. But you
    #   don't have to worry about that unless you want to.)
    #
    # - The user has to set Content-Length if they want it. Otherwise, for
    #   responses that have bodies (e.g. not HEAD), then we will automatically
    #   select the right mechanism for streaming a body of unknown length,
    #   which depends on depending on the peer's HTTP version.
    #
    # This function's *only* responsibility is making sure headers are set up
    # right -- everything downstream just looks at the headers. There are no
    # side channels. It mutates the response event in-place (but not the
    # response.headers list object).
    def _clean_up_response_headers_for_sending(self, response):
        assert type(response) is Response

        headers = list(response.headers)
        need_close = False

        _, effective_content_length = get_framing_headers(headers)
        if (response_allows_body(self._request_method, response)
            and effective_content_length is None):
            # This response has a body of unknown length.
            # If our peer is HTTP/1.1, we use Transfer-Encoding: chunked
            # If our peer is HTTP/1.0, we use no framing headers, and close the
            # connection afterwards.
            #
            # Make sure to clear Content-Length (in principle user could have
            # set both and then we ignored Content-Length b/c
            # Transfer-Encoding overwrote it -- this would be naughty of them,
            # but the HTTP spec says that if our peer does this then we have
            # to fix it instead of erroring out, so we'll accord the user the
            # same respect).
            set_comma_header(headers, "Content-Length", [])
            if self.their_http_version < b"1.1":
                set_comma_header(headers, "Transfer-Encoding", [])
                # This is actually redundant ATM, since currently we
                # unconditionally disable keep-alive when talking to HTTP/1.0
                # peers. But let's be defensive just in case we add
                # Connection: keep-alive support later:
                need_close = True
            else:
                set_comma_header(headers, "Transfer-Encoding", ["chunked"])

        if not self._cstate.keep_alive or need_close:
            # Make sure Connection: close is set
            connection = set(get_comma_header(headers, "Connection"))
            if b"close" not in connection:
                connection.discard(b"keep-alive")
                connection.add(b"close")
                set_comma_header(headers, "Connection", sorted(connection))

        response.headers = headers

    # XX maybe take this out? I guess real servers will have to have some more
    # serious thing, like wanting to handle stuff like 'raise Error(404)' as
    # part of their API? and specifying the body? and providing Date: and
    # Server: headers? and ...
    def maybe_send_error_response(self, exception, headers):
        if self.our_role is not SERVER:
            return b""
        if self.our_state not in (IDLE, SEND_RESPONSE):
            return b""

        if isinstance(exception, ProtocolError):
            status_code = exception.error_status_hint
        elif isinstance(exception, TimeoutError):
            # 408 Request Timeout -- maybe not 100% accurate but hopefully
            # close enough.
            status_code = 408
        else:
            # 500 Internal Server Error
            status_code = 500

        set_comma_header(headers, "Content-Length", [b"0"])
        set_comma_header(headers, "Connection", [b"close"])
        data = self.send(Response(status_code=status_code, headers=headers))
        data += self.send(EndOfMessage())
        return data
