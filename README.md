# Comelit ViP Intercom for Home Assistant

Local control of a Comelit ViP or SimpleBus video door entry system. It speaks
the native ViP protocol on your own network, TCP port 64100. No cloud, no
Comelit account, no polling.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/jfmlima/comelit-vip/ci.yml?label=CI)](https://github.com/jfmlima/comelit-vip/actions/workflows/ci.yml)
[![HACS: custom](https://img.shields.io/badge/HACS-custom-orange.svg)](https://hacs.xyz)

## Entities

| Entity | What it does |
|---|---|
| `button.*` | One per entry in the panel's door and actuator address books. Fires the relay. |
| `event.*_doorbell` | `ring` when somebody is at an entrance panel, `internal_call` for a call raised anywhere else, `call_ended` when either finishes. Also on the bus as `comelit_vip_event`. |
| `camera.*_entrance` | One per entrance panel. Shows the last still, not a live picture. |
| `button.*_update_snapshot` | One per entrance panel. Takes a fresh still. |

It can also record a clip on each ring. The panel's own stored recordings
cannot be read over the protocol.

### Which calls count as somebody at the door

`ring` is a visitor at an entrance panel. The bell outside your apartment door
and a call from another apartment fire `internal_call`.

On a two wire system bridged by the monitor, a floor call and a visitor arrive
with the same addresses, both naming the entrance panel as their origin. They
differ by a two byte tag in the call setup: `PP` for an entrance panel, `FF`
for the floor call (*fuoriporta*). The tag is on every event as `kind`, and an
entrance call also carries the panel's name in `entrance`.

## Not included

- **Audio.** The protocol carries G.711 in both directions; none of it is
  implemented.
- **The panel's stored video messages.** No known way to read them.
- **Answering a call.** A ring is reported; answering is left to the monitor
  and the Comelit app.

## Hardware

Written against a **Comelit 6741W** Mini hands free Wi-Fi monitor, model `MSVF`
on firmware 2.1.3, on a SimpleBus system with one entrance panel. The 6701W,
6742W, 6842W and the 1456 gateways speak the same protocol; the door and
configuration parts are shared with other projects running on them. Ring
events and video are only confirmed on the Mini and 6701W. A 1456B building
gateway is reported to open doors but not to stream.

## Give Home Assistant its own user

The panel keeps a list of app users, one per device, each with its own 32
character token. **Home Assistant needs a slot of its own.** If it shares the
token your phone uses, the panel delivers each call to only one of them, and
the phone may get no notification and be unable to answer.

Add a user from the panel's web interface at `http://<panel>:8080/`:

1. Log in with the installer password, `comelit` from the factory.
2. Open the **Users** page. Slot 1 is usually your phone, marked `Apps` and
   `Activated`. The rest are `Not Activated`. A 6741W has fifteen.
3. Press the activation code button on an empty slot. A six character code
   appears.
4. Redeem it with the Comelit app on a spare phone or tablet, then point this
   integration at that slot.

To list the users, setup downloads the panel's configuration backup with the
installer password; that archive holds every slot's token. Only the token for
the slot you pick is stored. Nothing is written back to the panel: this
integration adds, deletes and disables nothing.

**Undoing it.** The Users page deletes a pending activation code, and the
panel's Setup menu has *User management* to delete or disable a registered
device. Two actions there cannot be undone and this integration never performs
them: *reset user*, which removes a device's activation, and *unlink device* on
the Device Info page, which cuts the panel off from your Comelit cloud account.

### Why the FACT shortcut is not used

The protocol's `FACT` channel has an undocumented, unauthenticated command that
issues a token for an email address. What it does to an occupied slot is not
known, and in the one public implementation it originally ran right after a
command that deletes every registered user.

## Install

1. In HACS, add this repository as a custom repository of type **Integration**:
   `https://github.com/jfmlima/comelit-vip`
2. Install **Comelit ViP Intercom** and restart Home Assistant.
3. **Settings > Devices & services > Add integration > Comelit ViP Intercom**.

You need the panel's address and its installer password. Setup lists the app
users it finds and asks which one to use, even when it finds only one (on a
fresh panel that one is your phone). If you already hold a token for a spare
slot, paste it and leave the password empty.

If the intercom rejects the token three times in a row, Home Assistant asks
for a new one. The first refusals are logged with the panel's response code,
because a refusal can also mean another client holds the same slot. The
reauthentication prompt changes only the token; the address stays as it is.

## Options

| Option | Default | Meaning |
|---|---|---|
| RTSP port | 8554 | Port the built in relay listens on. |
| Let other devices reach the stream | off | Bind every interface instead of loopback. |
| Address for other devices | detected | Host to advertise in `camera.attributes.stream_url`. |
| Take a picture when somebody rings | off | Capture a still so the camera card shows the last visitor. |
| Record on ring | off | Save a clip when somebody rings. |
| Clip length | 15 s | How long that clip runs. |
| Folder | `comelit_vip` inside Home Assistant's media folder | Where clips are written. Must be a media folder or an allowed path. On Home Assistant OS and in Docker that is `/media/comelit_vip`; in a venv it is `media/comelit_vip` under the configuration directory. |

## Video

The panel sends video only inside a call. A call starts when something opens
the stream and ends a couple of seconds after the last viewer leaves. Home
Assistant's stream component counts as a viewer and holds the stream for
thirty seconds after you close the picture.

Measured on a 6741W: one video request buys about 35 seconds of video,
repeating it buys about 60, and then the panel goes quiet without ending the
call. The request is repeated every 15 seconds, and a call that has been
silent for two seconds is ended and started again while anyone is still
watching, so a live view runs in cycles of about a minute with a gap of a few
seconds between them.

A call held by this integration does not stop the doorbell: the wall monitor
still rings, can answer, and can open the door. The panel refuses a new call
from a second client while one is up, and refuses any outbound call while a
ring is in progress.

Two options start a call on their own: taking a still on a ring and recording
a clip on a ring. Both are off by default. Because the panel refuses an
outbound call during a ring, both wait for the ring to end and then dial the
entrance that rang, so the picture is of whoever is still at the door
afterwards. Only a visitor's ring counts; a floor call takes no picture. If a
call to another entrance is already up, the picture is skipped and both
addresses are logged.

Nothing else starts a call. The camera entity serves the last still rather than
a live picture because Home Assistant requests a picture whenever a camera is
on screen, and each request would hold a call open.

Expect the first frame two to three seconds after the stream opens, at 320x240
and roughly 12 fps.

### Frigate and other consumers

The relay binds loopback, so out of the box only Home Assistant can open the
stream. Turn on **Let other devices reach the stream** and it binds every
interface instead; the URL is then in each camera's `stream_url` attribute. The
first entrance is served at `/comelit` and every other one at
`/comelit/<its ViP address>`. Nothing else is served: the relay cannot be made
to dial an address the installer never named, and while a call to one entrance
is up a request for another is answered `503`.

The relay has no password, and a `DESCRIBE` starts a call, so any device that
can reach the port can keep the panel in a call for as long as it stays
connected. That does not block the doorbell, but it does block any other
client's calls and the ring captures above.

## Layout

```
custom_components/comelit_vip/        the Home Assistant integration
custom_components/comelit_vip/viper/  the protocol library, no Home Assistant imports
```

## Development

```bash
uv venv --python 3.14
uv pip install --group dev
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check .
```

Python 3.14.2 is the minimum, as required by Home Assistant 2026.3.1, the
oldest release named in `hacs.json`. CI runs the tests on 3.14 and once more
against Home Assistant 2026.3.1.

pytest and pytest-asyncio are pinned by the Home Assistant test harness and
are not listed separately.

## Credit

Comelit does not document the ViP protocol. This work builds on public reverse
engineering by others:

- [madchicken/comelit-client](https://github.com/madchicken/comelit-client) for channel framing, door open, and where the token lives
- [grdw/viper-client](https://github.com/grdw/viper-client) and [the write-up](https://grdw.nl/2023/01/28/my-intercom-part-1.html) for channel names and UDP discovery
- [ttmx/comelit-vip](https://github.com/ttmx/comelit-vip) for the CTP transport model and the video call
- [antoiba86/hass-comelit-intercom-local](https://github.com/antoiba86/hass-comelit-intercom-local) and Michael Nestrud's protocol reference for registration, renewals and ring events

## Licence

MIT, see [LICENSE](LICENSE).
