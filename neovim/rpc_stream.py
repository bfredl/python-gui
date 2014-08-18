from collections import deque
import logging

logger = logging.getLogger(__name__)
debug, info, warn = (logger.debug, logger.info, logger.warn,)

def decode_obj(obj, encoding=None, encoding_errors='strict'):
    """
    Recursively decode instances of 'bytes' into Unicode
    """
    if not encoding:
        return obj

    if isinstance(obj, bytes):
        return obj.decode(encoding, errors=encoding_errors)
    elif isinstance(obj, list) or isinstance(obj, tuple):
        return [decode_obj(o, encoding, encoding_errors) for o in obj]
    elif isinstance(obj, dict):
        d = {}
        for k,v in obj.items():
            k = decode_obj(k, encoding, encoding_errors)
            v = decode_obj(v, encoding, encoding_errors)
            d[k] = v
        return d
    return obj

class RPCStream(object):
    def __init__(self, stream, decode_str=None):
        self.stream = stream
        self.pending_requests = {}
        self.next_request_id = 1
        self.interrupted = False
        self.stopped = False
        self.running = False
        self._decode_str=decode_str
        self.encoding = None
        self.posted_notifications = deque()

    def decode_obj(self, obj, decode_str=None):
        """
        If decode_str is True, decode binary strings inside the object
        and return new object. Otherwise return obj.
        """
        if not self.encoding:
            return obj
        if decode_str == None:
            decode_str = self._decode_str
        if decode_str:
            return decode_obj(obj, self.encoding)
        return obj

    def configure(self, vim):
        self.stream.configure(vim)


    def post(self, name, args):
        self.posted_notifications.append((name, args,))
        self.stream.interrupt()


    def send(self, method, args, response_cb):
        request_id = self.next_request_id
        # Update request id
        self.next_request_id = request_id + 1
        # Send the request
        self.stream.send([0, request_id, method, args])
        # set the callback
        self.pending_requests[request_id] = response_cb


    def loop_start(self, request_cb, notification_cb, error_cb):
        def msg_cb(msg):
            msg_type = msg[0]
            if msg_type == 0:
                # request
                #   - msg[1]: id
                #   - msg[2]: method name
                #   - msg[3]: arguments
                debug('received request: %s, %s', msg[2], msg[3])
                request_cb(self.decode_obj(msg[2]), self.decode_obj(msg[3]), reply_fn(self.stream, msg[1]))
            elif msg_type == 1:
                # response to a previous request:
                #   - msg[1]: the id
                #   - msg[2]: error(if any)
                #   - msg[3]: result(if not errored)
                debug('received response: %s, %s', msg[2], msg[3])
                self.pending_requests.pop(msg[1])(self.decode_obj(msg[2]), self.decode_obj(msg[3]))
            elif msg_type == 2:
                # notification/event
                #   - msg[1]: event name
                #   - msg[2]: arguments
                debug('received notification: %s, %s', msg[1], msg[2])
                notification_cb(self.decode_obj(msg[1]), self.decode_obj(msg[2]))
            else:
                error = 'Received invalid message %s' % msg
                warn(error)
                raise Exception(error)

        self._run(msg_cb, notification_cb, error_cb)
        debug('exiting rpc stream loop')



    def loop_stop(self):
        self.stopped = True
        if self.running:
            self.stream.loop_stop()


    def _run(self, msg_cb, notification_cb, error_cb):
        self.stopped = False
        while not self.stopped:
            if self.posted_notifications:
                notification_cb(*self.posted_notifications.popleft())
                continue
            self.running = True
            self.stream.loop_start(msg_cb, error_cb)
            self.running = False


def reply_fn(stream, request_id):
    def reply(value, error=False):
        if error:
            resp = [1, request_id, value, None]
        else:
            resp = [1, request_id, None, value]
        stream.send(resp)

    return reply
