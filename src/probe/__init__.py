"""HTTP probes that record a status code and a latency, store it, read a quorum, and publish it."""

from probe.checker import ProbeResult, check_url

__all__ = ["ProbeResult", "check_url"]
