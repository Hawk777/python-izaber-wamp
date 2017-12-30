#!/usr/bin/python

import json
import time
import threading
import six
from six.moves import queue

import websocket
from .messages import *

class WAMPClient(threading.Thread):
    ws = None
    url = None
    uri_base = None
    realm = None
    agent = None
    username = None
    password = None
    authmethods = None
    timeout = None

    _subscriptions = None
    _registered_calls = None
    _requests_pending = None
    _request_stop = False
    _running = True
    _loop_timeout = 0.1


    def generate_request_id(self):
        """ We cheat, we just use the millisecond timestamp for the request
        """
        return int(round(time.time() * 1000))

    def connect(self):
        """ This just creates the websocket connection
        """
        self.ws = websocket.create_connection(
                        self.url,
                        subprotocols=['wamp.2.json'],
                        timeout=self._loop_timeout,
                    )
        self._subscriptions    = {}
        self._registered_calls = {}
        self._requests_pending = {};

    def __init__(
                self,
                url='ws://localhost:8080',
                realm='realm1',
                agent='wamp-2.0',
                uri_base='',
                authmethods=[u'ticket'],
                username=None,
                password=None,
                timeout=None,
                ):

        super(WAMPClient,self).__init__()
        self.daemon = True

        self.configure(
            url = url,
            uri_base = uri_base,
            realm = realm,
            agent = agent,
            timeout = timeout,
            authmethods = authmethods,
            username = username,
            password = password,
        )

    def configure(self, **kwargs):
        for k in ('url','uri_base','realm',
                  'agent','timeout','authmethods',
                  'username','password'):
            if k in kwargs:
                setattr(self,k,kwargs[k])


    def handle_challenge(self,data):
        """ Executed when the server requests additional
            authentication
        """
        # Send challenge response
        self.send_message(AUTHENTICATE(
            signature = self.password,
            extra = {}
        ))

    def login(self):
        """ Say hello to the server and wait for the welcome 
            message before proceeding
        """
        self.connect()

        self._welcome_queue = queue.Queue()

        details = {
            'agent': 'izaber-wamp',
            'authid': self.username,
            'authmethods': self.authmethods,
            'roles': {
                'subscriber': {},
                'publisher': {},
                'caller': {},
                'callee': {},
            },
        }
        self.send_message(HELLO(
                                realm = self.realm,
                                details = details
                            ))

        # Wait till we get a welcome message
        try:
            message = self._welcome_queue.get(block=True,timeout=self.timeout)
        except Exception as ex:
            raise Exception("Timed out waiting for login authorization")
        self.session_id = message.session_id

    def call(self, uri, *args, **kwargs ):
        """ Sends a RPC request to the WAMP server
        """
        options = {
            'disclose_me': True
        }
        uri = self.uri_base + '.' + uri
        message = self.send_and_await_response(CALL(
                      options=options,
                      procedure=uri,
                      args=args,
                      kwargs=kwargs
                    ))

        if message == WAMP_RESULT:
            return message.args[0]

        if message == WAMP_ERROR:
            raise Exception(message.args[0])

        return message

    def send_message(self,message):
        """ Send awamp message to the server. We don't wait
            for a response here. Just fire out a message
        """
        message = message.as_str()
        self.ws.send(message)

    def send_and_await_response(self,request):
        """ Used by most things. Sends out a request then awaits a response
            keyed by the request_id
        """
        wait_queue = queue.Queue()
        request_id = request.request_id
        self._requests_pending[request_id] = wait_queue;
        self.send_message(request)
        try:
            return wait_queue.get(block=True,timeout=self.timeout)
        except Exception as ex:
            raise Exception("Did not receive a response!")

    def dispatch_to_awaiting(self,result):
        request_id = result.request_id
        if request_id in self._requests_pending:
            self._requests_pending[request_id].put(result)
            del self._requests_pending[request_id]

    def handle_welcome(self, welcome):
        self._welcome_queue.put(welcome)

    def handle_result(self, result):
        """ Dispatch the result back to the appropriate awaiter
        """
        self.dispatch_to_awaiting(result)

    def handle_result(self, result):
        """ Dispatch the result back to the appropriate awaiter
        """
        self.dispatch_to_awaiting(result)

    def handle_subscribed(self, result):
        """ Handle the successful subscription
        """
        self.dispatch_to_awaiting(result)

    def handle_registered(self, result):
        """ Handle the request registration
        """
        self.dispatch_to_awaiting(result)

    def handle_error(self, error):
        """ OOops! An error occurred
        """
        self.dispatch_to_awaiting(error)

    def handle_invocation(self, message):
        req_id = message.request_id
        reg_id = message.registration_id
        if reg_id in self._registered_calls:
            result = self._registered_calls[reg_id](
                message,
                *(message.args)
                **(message.kwargs)
            )
            self.send_message(YIELD(
                request_id = req_id,
                registration_id = reg_id,
                options={},
                args=[result]
            ))
        else:
            error_uri = self.uri_base + '.error.unknown.uri'
            self.send_message(ERROR(
                WAMP_INVOCATION,
                req_id,
                {},
                error_uri
            ))

    def handle_event(self, event):
        """ Send the event to the subclass or simply reject
        """
        subscription_id = event.subscription_id
        if subscription_id in self.subscriptions:
            self.subscriptions[subscription_id][SUBSCRIPTION_CALLBACK](event)

    def handle_unknown(self, message):
        """ We don't know what to do with this. So we'll send it
            into the queue just in case someone wants to do something
            with it but we'll just blackhole it.
        """
        self.dispatch_to_awaiting(result)

    def subscribe(self,topic,callback=None,options=None):
        """ Subscribe to a uri for events from a publisher
        """
        id = self.generate_request_id()
        topic = self.uri_base + '.' + topic
        result = self.send_and_await_response(SUBSCRIBE(
                                    options={},
                                    topic=topic
                                ))
        if result == WAMP_SUBSCRIBED:
            if not callback:
                callback = lambda a: None
            self.subscriptions[result.subscription_id] = [topic,callback]

    def publish(self,topic,options=None,args=None,kwargs=None):
        """ Publishes a messages to the server
        """
        id = self.generate_request_id()
        topic = self.uri_base + '.' + topic
        result = self.send_and_await_response(PUBLISH(
                    options=options or {},
                    topic=topic,
                    args=args,
                    kwargs=kwargs
                  ))
        return result

    def close(self):
        """ Close the WAMP connection """
        if self._running:
            self.send_message(GOODBYE(
                  details={},
                  reason="wamp.error.system_shutdown"
                ))
        self.ws.close()
        self._running = False

    def stop(self):
        """ Request the system to stop the main loop and shutdown the system
        """
        self._request_stop = True
        for i in range(100):
            if not self._running:
                break
            sleep(0.1)

    def start(self):
        """ Initialize websockets, login and start listening for events
        """
        self.connect()
        super(WAMPClient,self).start()
        self.login()

    def register(self,uri,callback,options=None):
        uri = self.uri_base + '.' + uri
        result = self.send_and_await_response(REGISTER(
                      options=options or {},
                      procedure=uri
                  ))
        if result == WAMP_REGISTERED:
            self._registered_calls[result.registration_id] = callback
        return result

    def run(self):
        """ Waits and receives messages from the server. This
            function somewhat needs to block so is executed in its
            own thread until self._request_stop is called.
        """
        while not self._request_stop:
            try:
                data = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not data: continue

            try:
                message = WampMessage.loads(data)
                try:
                    code_name = message.code_name.lower()
                    handler_name = "handle_"+code_name
                    handler_function = getattr(self,handler_name)
                    handler_function(message)
                except AttributeError as ex:
                    self.handle_unknown(message)
            except Exception as ex:
                # FIXME: Needs more granular exception handling
                raise

        self.close()
