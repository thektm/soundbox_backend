"""Run Daphne with request duration included in its existing access log line."""
from __future__ import annotations

import datetime

import daphne.cli as daphne_cli
from daphne.access import AccessLogGenerator


class TimedAccessLogGenerator(AccessLogGenerator):
    """Expose Daphne's already-computed HTTP ``time_taken`` without re-timing."""

    def __call__(self, protocol, action, details):
        if protocol == "http" and action == "complete":
            elapsed_ms = max(0.0, float(details.get("time_taken") or 0.0) * 1000.0)
            host = details.get("client") or "-"
            method = details.get("method") or "-"
            path = details.get("path") or "-"
            status = details.get("status") or "-"
            length = details.get("size") or "-"
            date = datetime.datetime.now().strftime("%d/%b/%Y:%H:%M:%S")
            self.stream.write(
                f'[{elapsed_ms:.2f}ms] {host} - - [{date}] "{method} {path}" {status} {length}\n'
            )
            return
        super().__call__(protocol, action, details)


if __name__ == "__main__":
    # CommandLineInterface resolves this module-level symbol when it creates the
    # access logger, so all normal Daphne CLI behavior stays unchanged.
    daphne_cli.AccessLogGenerator = TimedAccessLogGenerator
    daphne_cli.CommandLineInterface.entrypoint()
