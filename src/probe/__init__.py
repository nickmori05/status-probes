"""HTTP probes that record a status code and a latency.

They store each observation, read a quorum, publish it, and notify when
an endpoint moves between up and down.
"""

from probe.checker import ProbeResult, check_url

__all__ = ["ProbeResult", "check_url"]
