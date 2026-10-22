"""Bounded per-connection IRC byte framing, with no I/O or payload logging."""


class IRCLineBuffer:
    def __init__(self, maximum: object = 65536) -> None:
        if type(maximum) is not int or maximum < 1:
            raise ValueError('Invalid incoming line limit')
        self.maximum = maximum
        self.pending = b''
        self.discarding = False

    def feed(self, data: object) -> tuple[list[str], int, int]:
        """Return complete UTF-8 lines and counts of discarded invalid/large frames.

        Invalid UTF-8 discards its entire frame. Oversize input is discarded until
        CRLF, retaining at most a trailing CR for delimiter synchronization. A
        partial final frame is never decoded or dispatched, including at EOF.
        """
        if not isinstance(data, bytes):
            raise TypeError('IRC input must be bytes')
        parts = (self.pending + data).split(b'\r\n')
        lines: list[str] = []
        invalid = oversized = 0
        for frame in parts[:-1]:
            if self.discarding:
                self.discarding = False
                continue
            if len(frame) > self.maximum:
                oversized += 1
                continue
            try:
                lines.append(frame.decode('utf-8'))
            except UnicodeDecodeError:
                invalid += 1
        tail = parts[-1]
        if not self.discarding and len(tail) > self.maximum + int(tail.endswith(b'\r')):
            self.discarding = True
            oversized += 1
        self.pending = (b'\r' if tail.endswith(b'\r') else b'') if self.discarding else tail
        return lines, invalid, oversized
