"""Day-one data loading and validation for the trip project."""

import os

if os.environ.get("TRIP_OFFLINE") == "1":
    from .offline import deny_network
    deny_network()
