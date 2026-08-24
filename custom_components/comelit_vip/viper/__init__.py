"""Asyncio client for the Comelit ViP LAN protocol, TCP 64100.

Protocol details come from these public reverse-engineering efforts:
- madchicken/comelit-client (channel framing, door open, token location)
- grdw/viper-client and grdw.nl "My intercom" (channel names, UDP discovery)
- ttmx/comelit-vip (CTP transport model, video call, H.264/G.711 media)
- antoiba86/hass-comelit-intercom-local + mnestrud protocol reference
  (registration handshake, renewal acknowledgements, ring events)
"""

from .call import VideoCall
from .ctp import CtpPacket, build_ctp_packet, decode_logaddr, encode_logaddr, parse_ctp_packet
from .discovery import DeviceInfo, async_discover
from .models import Actuator, Door, PanelConfig, RingEvent
from .session import ViperError, ViperSession
from .web import PanelUser, PanelWebClient, PanelWebError

__all__ = [
    "Actuator",
    "CtpPacket",
    "DeviceInfo",
    "Door",
    "PanelConfig",
    "PanelUser",
    "PanelWebClient",
    "PanelWebError",
    "RingEvent",
    "VideoCall",
    "ViperError",
    "ViperSession",
    "async_discover",
    "build_ctp_packet",
    "decode_logaddr",
    "encode_logaddr",
    "parse_ctp_packet",
]
