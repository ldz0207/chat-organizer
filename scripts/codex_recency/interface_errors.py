"""Only transport failures are eligible for automatic reconnect."""


class InterfaceUnavailable(RuntimeError):
    def __init__(self, source, operation, reason):
        self.source, self.operation = source, operation
        super().__init__("{} {}: {}".format(source, operation, reason))


class RequestTimedOut(InterfaceUnavailable):
    def __init__(self, source, operation, seconds):
        self.seconds = seconds
        super().__init__(source, operation, "no response within {:g}s".format(seconds))
