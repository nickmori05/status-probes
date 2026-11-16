"""HTTP probes that record a status code and a latency, store it, and read a quorum."""

from probe.checker import ProbeResult, check_url

__all__ = ["ProbeResult", "check_url"]
