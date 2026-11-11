"""What the run service's work records when it can't go on."""

# Why work is failed when its process stopped, and kept stopping, part way.
STOPPED = "The service stopped mid-run."


class CantStart(Exception):
    """The run couldn't start; the message is recorded as the outcome."""
