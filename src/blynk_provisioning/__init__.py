"""
blynk_provisioning - get this device's Blynk credentials via BLE pairing
with the Blynk app, so a plain Python/paho script can talk to Blynk without
needing the full blynk-edge-agent stack. See the project README for scope
and the one-off-publish / subscribe-and-print examples.
"""

from .credentials import Credentials, CredentialsRejectedError, ProvisioningError, get_credentials

__all__ = ["get_credentials", "Credentials", "ProvisioningError", "CredentialsRejectedError"]

__version__ = "1.0.0"
