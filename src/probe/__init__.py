"""HTTP probes that record a status code and a latency, and store the observation."""

from probe.checker import ProbeResult, check_url

__all__ = ["ProbeResult", "check_url"]
