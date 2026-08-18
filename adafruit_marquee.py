# SPDX-FileCopyrightText: 2017 Scott Shawcroft, written for Adafruit Industries
# SPDX-FileCopyrightText: Copyright (c) 2026 Brent Rubell for Adafruit Industries
#
# SPDX-License-Identifier: MIT
"""
`adafruit_marquee`
================================================================================

CircuitPython library to support Adafruit.IO's Marquee feature


* Author(s): brentru

Implementation Notes
--------------------

**Hardware:**

**Software and Dependencies:**

* Adafruit CircuitPython firmware for the supported boards:
  https://circuitpython.org/downloads

"""

import binascii
import json
import struct
import time
from io import BytesIO

import adafruit_imageload
import adafruit_minimqtt.adafruit_minimqtt as MQTT
import alarm
import board
import busio
import displayio
from fourwire import FourWire

__version__ = "0.0.0+auto.0"
__repo__ = "https://github.com/adafruit/Adafruit_CircuitPython_Marquee.git"

# Marquee sleep-memory record, Schema v1
_MAGIC = b"MRQ\x01"
# Long-lived keep-alive to avoid disconnection during EPD refreshes
_KEEP_ALIVE = 60
# Default Adafruit IO feed name
_DEFAULT_FEED = "marquee"
# Default Marquee configuration file
_DEFAULT_CONFIG_FILE = "cfg-marquee.json"

_ALARM_TYPES = ("timer", "pin", "timer+pin")
_SLEEP_MODES = ("light", "deep")


def get_pin_from_cfg(pin_cfg, name):
    """Resolve a board.* pin name from a config section."""
    pin_name = pin_cfg.get(name)
    return getattr(board, pin_name.replace("board.", "")) if pin_name else None


class Marquee:
    """Adafruit IO Marquee client.

    :param socket_pool: Socket pool, e.g. from
        ``adafruit_connection_manager.get_radio_socketpool()``.
    :param ssl_context: SSL context, e.g. from
        ``adafruit_connection_manager.get_radio_ssl_context()``.
    :param str aio_username: Desired Adafruit IO username.
    :param str aio_key: Desired Adafruit IO key.
    :param str aio_feed: Desired Adafruit IO feed name. Defaults to ``"marquee"``.
    :param str config_file: Path to the display configuration file. Defaults to
        ``"cfg-marquee.json"``.
    :param bool debug: When ``True``, print progress to the serial console.
    """

    def __init__(  # noqa: PLR0913 - keyword-only configuration surface
        self,
        *,
        socket_pool=None,
        ssl_context=None,
        aio_username=None,
        aio_key=None,
        aio_feed=_DEFAULT_FEED,
        config_file=_DEFAULT_CONFIG_FILE,
        debug=False,
    ):
        self._debug = debug
        self._wake_reason = self.get_wake_reason()

        for name, value in (
            ("socket_pool", socket_pool),
            ("ssl_context", ssl_context),
            ("aio_username", aio_username),
            ("aio_key", aio_key),
        ):
            if value is None:
                raise ValueError(f"Marquee requires a {name}")
        if not aio_feed:
            raise ValueError("Marquee requires an aio_feed")

        config = self._load_config(config_file)

        # Parse panel, interface configuration, build the display
        self._panel = config["display"]
        self._iface = config["interface"]

        # To avoid blocking, MQTT payloads are saved by the callbacks and executed in
        # the main loop
        self._pending_image = None
        self._pending_sleep = None
        # If the MQTT connection fails during display refresh, it's flagged and the
        # main loop handles the reconnection
        self._needs_reconnect = False

        # Construct the Adafruit IO feed paths
        self._feed = f"{aio_username}/feeds/{aio_feed}"
        self._feed_sleep = f"{aio_username}/feeds/{aio_feed}-sleep"
        self._feed_status = f"{aio_username}/feeds/{aio_feed}-status"

        # Attempt to configure display and rotation
        self._display = None
        self._init_display()

        # Configure the MQTT client
        self._client = MQTT.MQTT(
            broker="io.adafruit.us",
            username=aio_username,
            password=aio_key,
            socket_pool=socket_pool,
            ssl_context=ssl_context,
            keep_alive=_KEEP_ALIVE,
        )

        # Assign callbacks for MQTT feeds
        self._client.on_connect = self._on_mqtt_connect
        self._client.on_disconnect = self._on_mqtt_disconnect
        self._client.add_topic_callback(self._feed, self._on_image)
        self._client.add_topic_callback(self._feed_sleep, self._on_sleep)

    def _log(self, message):
        """Print ``message`` when the library was constructed with ``debug=True``."""
        if self._debug:
            print(message)

    def _load_config(self, config_file):
        """Read and validate the Marquee configuration file, ``cfg-marquee.json``."""
        # CircuitPython raises a bare OSError for a missing file on some ports, so
        # catch the base class -- FileNotFoundError is a subclass and covered here.
        try:
            with open(config_file) as f:
                config = json.load(f)
        except OSError:
            raise OSError(f"Required file '{config_file}' not found!") from None

        if "display" not in config or "interface" not in config:
            raise ValueError("Invalid configuration file: missing 'display' or 'interface' keys.")
        return config

    def _init_display(self):
        """Attach to the display described by the configuration file."""
        if self._panel.get("driver") == "SSD1680":
            # Initialize SSD1680 display driver
            import adafruit_ssd1680

            displayio.release_displays()
            # Create the display bus
            spi_config = self._iface.get("spi", {})
            if "sck" in spi_config or "mosi" in spi_config:
                spi = busio.SPI(
                    clock=get_pin_from_cfg(spi_config, "sck"),
                    MOSI=get_pin_from_cfg(spi_config, "mosi"),
                )
            else:
                spi = board.SPI()
            pin_config = self._iface.get("pins", {})
            epd_cs = get_pin_from_cfg(pin_config, "cs")
            epd_dc = get_pin_from_cfg(pin_config, "dc")
            epd_reset = get_pin_from_cfg(pin_config, "reset")
            self._epd_busy_pin = get_pin_from_cfg(pin_config, "busy")
            display_bus = FourWire(
                spi,
                command=epd_dc,
                chip_select=epd_cs,
                reset=epd_reset,
                baudrate=spi_config.get("baudrate", 1_000_000),
            )
            time.sleep(1)
            # Create the SSD1680 display driver
            self._display = adafruit_ssd1680.SSD1680(
                display_bus,
                width=250,
                height=122,
                busy_pin=self._epd_busy_pin,
                highlight_color=0xFF0000,
                rotation=270,
                colstart=8,  # Comment out for older displays
            )
            g = displayio.Group()
            self._display.root_group = g

        """
        if self._panel.get("panel") == "adafruit-magtag":
            self._display = board.DISPLAY
        else:
            raise NotImplementedError("Hardware other than magtag not configured yet")
        """
        self._set_display_rotation()

    def _set_display_rotation(self):
        """Transforms rotation in cfg-marquee.json from a quadrant index (0-3) to degrees."""
        self._panel_rotation = self._panel.get("rotation", 0)
        if self._panel_rotation in (1, 2, 3):
            self._panel_rotation *= 90
        if self._panel_rotation not in (0, 90, 180, 270):
            raise ValueError(f"Invalid display rotation: {self._panel.get('rotation')}")
        self._display.rotation = self._panel_rotation

    @property
    def connected(self):
        """Whether the MQTT client is connected"""
        return self._client.is_connected()

    @property
    def busy(self):
        """Whether the e-paper display is refreshing.

        This reads ``interface.pins.busy`` through the SSD1680 driver when that
        optional pin is configured.
        """
        return self._display.busy

    def connect(self):
        """Attempts to connect to Adafruit IO's MQTT Broker."""
        self._log("Connecting to Adafruit IO...")
        self._client.connect()

    def disconnect(self):
        """Disconnect from Adafruit IO."""
        self._client.disconnect()

    def loop(self, timeout=1):
        """Attempts to display new messages from Adafruit IO.
        :param float timeout: Seconds to poll for. Must be at least MiniMQTT's socket
            timeout (1s by default).
        """
        # Perform a reconnect if the last ping failed during a display refresh
        if self._needs_reconnect:
            self._log("Reconnecting to Adafruit IO...")
            try:
              # Reconnect and re-subscribe to the feeds
              self._client.reconnect()
              self._needs_reconnect = False
            except (MQTT.MMQTTException, OSError, AssertionError) as err:
              # Back off and try again next loop() to avoid dropping into the REPL
              self._log(f"Reconnect failed, retrying next loop(): {err}")
              time.sleep(timeout)
              return

        # Poll the message queue
        try:
          self._client.loop(timeout=timeout)
        except (MQTT.MMQTTException, OSError, AssertionError) as err:
          self._log(f"MQTT loop failed: {err}")
          self._needs_reconnect = True

        # Execute any pending commands from the callbacks
        if self._pending_image is not None:
            msg, self._pending_image = self._pending_image, None
            self._show_image(msg)
        if self._pending_sleep is not None:
            msg, self._pending_sleep = self._pending_sleep, None
            self._handle_sleep(msg)

    def get_wake_reason(self):
        """Sets the reason the board woke from sleep, ``None`` if this is a cold boot."""
        reason = alarm.wake_alarm
        if reason is alarm.pin.PinAlarm:
            self._log("Woke from sleep on a pin alarm")
            self._wake_reason = "pin"
        elif reason is alarm.time.TimeAlarm:
            self._log("Woke from sleep on a time alarm")
            self._wake_reason = "timer"
        elif reason is None:
            self._log("Woke from cold boot or REPL reset")
            self._wake_reason = None
        return self._wake_reason

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        """Called by MiniMQTT once the broker accepts the connection."""
        self._log("Connected to Adafruit IO!")

        # Determine the wake reason and publish it to the status feed
        self._wake_reason = self.get_wake_reason()
        try:
            client.publish(
                self._feed_status,
                json.dumps({"state": "awake", "wake_reason": self._wake_reason}),
            )
        except (MQTT.MMQTTException, OSError) as err:
            self._log(f"Could not publish awake status ({err}), continuing anyway")

        # Subscribe to all changes on the marquee feeds
        client.subscribe(self._feed)
        client.subscribe(self._feed_sleep)
        self._log("Subscribed!")
        # Kick out the last message on the feed, if it exists, so we can display it
        # immediately
        client.publish(f"{self._feed}/get", "get")
        # If we were previously sleeping, we need to fetch the sleep feed to see if we
        # should sleep again
        if self._wake_reason is not None:
            self._log("Woke from sleep, fetching sleep feed to see if we should sleep again")
            client.publish(f"{self._feed_sleep}/get", "get")

    def _on_mqtt_disconnect(self, client, userdata, rc):
        """Called by MiniMQTT after the client disconnects."""
        self._log("Disconnected from Adafruit IO!")

    def _on_image(self, client, topic, message):
        """Stash an image payload for the main loop.

        Deliberately does no work: MiniMQTT dispatches this from the middle of reading
        a packet, so drawing or publishing here can desync the MQTT stream.
        """
        self._pending_image = message

    def _on_sleep(self, client, topic, message):
        """Saves messages across the sleep feed, processed later by _handle_sleep()"""
        self._pending_sleep = message

    def _show_image(self, message):
        """Decode a base64 BMP payload and draw it on the display."""
        payload = message if isinstance(message, bytes) else message.encode()
        crc = binascii.crc32(memoryview(payload)) & 0xFFFFFFFF
        if crc == self._load_prv_bmp_crc():
            self._log("BMP unchanged, skipping redraw")
            return

        bytes_img = self._convert_message_to_image(message)
        try:
            image, palette = adafruit_imageload.load(bytes_img)
        except AttributeError as err:
            self._log(f"Error loading image: {err}")
            return
        self._log(f"Loaded {image.width}x{image.height} image")
        if image.width != self._display.width or image.height != self._display.height:
            # The image's resolution not matching is cosmetic, so display and log the mismatch.
            self._log(
                "WARNING: Image resolution does not match display resolution: "
                f"({self._display.width}x{self._display.height})"
            )

        # Draw the image to the display
        tile_grid = displayio.TileGrid(image, pixel_shader=palette)
        group = displayio.Group()
        group.append(tile_grid)
        self._display.root_group = group

        # Display the image and wait for the panel to refresh
        self._refresh_display()

        # Store the BMP CRC for next wake
        self._store_crc(crc)
        print("Image displayed!")

    def _wait_for_refresh(self, refresh_time):
        """Sleep out the panel cooldown without dropping the MQTT connection."""
        ping_interval = _KEEP_ALIVE / 2
        # Computed once -- re-evaluating the deadline each pass would never expire
        deadline = time.monotonic() + refresh_time
        prv_ping = time.monotonic()

        while time.monotonic() < deadline:
            if time.monotonic() - prv_ping >= ping_interval:
                prv_ping = time.monotonic()
                if not self._needs_reconnect:
                    try:
                        self._client.ping()
                    except (MQTT.MMQTTException, OSError) as err:
                        self._log(f"Ping failed ({err}), will reconnect after the refresh")
                        self._needs_reconnect = True
            # Sleep every pass, not just when a ping was due, or this busy-waits
            time.sleep(0.5)

    def _refresh_display(self):
        """Refreshes the display and waits for it to finish."""
        wait_to_refresh = self._display.time_to_refresh
        if wait_to_refresh > 0:
            self._log(f"Display cooling down, waiting {wait_to_refresh:.1f}s...")
            self._wait_for_refresh(wait_to_refresh)

        # NOTE: This call is async
        self._display.refresh()

        self._log("Waiting for display to finish drawing...")
        while self.busy:
            time.sleep(0.1)

    def _handle_sleep(self, message):
        """Parse a ``{feed}-sleep`` payload, announce the sleep, then sleep.

        For a ``"deep"`` request this does not return -- the board resets and re-runs
        ``code.py`` on wake. Returns immediately when the payload is unusable or
        describes no armable alarm.
        """
        parsed = self._parse_sleep(message)
        if parsed is None:
            return
        alarm_type, sleep_mode, sleep_time, time_alarm, pin_alarm = parsed

        # Publish out to the status feed
        try:
            self._client.publish(
                self._feed_status,
                json.dumps(
                    {
                        "state": "sleeping",
                        "sleep_time": sleep_time,
                        "alarm_type": alarm_type,
                    }
                ),
                qos=1
            )
        except (MQTT.MMQTTException, OSError) as err:
            self._log(f"Could not publish sleep status ({err}), continuing to sleep anyway")

        alarms = tuple(a for a in (time_alarm, pin_alarm) if a is not None)
        if time_alarm is not None and pin_alarm is not None:
            detail = f"for {sleep_time}s or until the button is pressed"
        elif pin_alarm is not None:
            detail = "until the button is pressed"
        else:
            detail = f"for {sleep_time}s"

        if sleep_mode == "deep":
            try:
                self._client.disconnect()
            except Exception as err:  # noqa: BLE001 - never block the sleep
                self._log(f"Could not disconnect cleanly: {err}")
            self._log(f"Deep sleeping {detail}...")
            # Does not return -- the board resets on wake
            alarm.exit_and_deep_sleep_until_alarms(*alarms)
        else:
            self._log(f"Light sleeping {detail}...")
            alarm.light_sleep_until_alarms(*alarms)
            # Resume execution after light sleep
            self._log(f"Woke from light sleep on {alarm.wake_alarm}")
            # Force a reconnection in case the broker closed the port
            self._needs_reconnect = True

    def _parse_sleep(self, message):
        """Validate a sleep payload and prepare the alarms it describes.

        :return: ``(alarm_type, sleep_mode, sleep_time, time_alarm, pin_alarm)``, or
            ``None`` when the payload is unusable or describes no armable alarm.
        """
        # A payload we cannot read is a normal state (first boot, a feed that was
        # never created), not an error -- stay awake rather than guessing.
        try:
            sleep_msg = json.loads(message)
            alarm_type = sleep_msg["alarm_type"]
            sleep_mode = sleep_msg["sleep_mode"]
            sleep_time = int(sleep_msg["sleep_time"])
        except (ValueError, KeyError, TypeError) as err:
            self._log(f"ERR: Unusable sleep payload ({err}), staying awake")
            return None

        # Validate the payload values
        if alarm_type not in _ALARM_TYPES:
            self._log(f"ERR: Unknown alarm_type: {alarm_type}, staying awake")
            return None
        if sleep_mode not in _SLEEP_MODES:
            self._log(f"ERR: Unknown sleep_mode: {sleep_mode}, staying awake")
            return None

        time_alarm = None
        pin_alarm = None

        # Configure a time-based alarm
        if alarm_type in ("timer", "timer+pin") and sleep_time > 0:
            time_alarm = alarm.time.TimeAlarm(monotonic_time=time.monotonic() + sleep_time)

        # TODO: Add a way to kick the device OUT of sleep in this message structure!

        # TODO: Configure a pin alarm
        # TODO NOTE: Pin alarms require a value and a pull value too, we need this in
        # the schema
        if alarm_type in ("pin", "timer+pin"):
            # TODO: See the value and pull values here?
            # pin_alarm = alarm.pin.PinAlarm(board.BUTTON_A, value=False, pull=True)
            self._log("Pin alarms are not yet implemented, ignoring pin alarm request")

        # If alarms are not configured, don't sleep
        if time_alarm is None and pin_alarm is None:
            self._log("No alarms configured, staying awake")
            return None

        return alarm_type, sleep_mode, sleep_time, time_alarm, pin_alarm

    def _convert_message_to_image(self, message):
        """Converts a base64-encoded BMP to a binary-encoded BMP."""
        try:
            msg_decoded = binascii.a2b_base64(message)
        except ValueError as err:
            self._log(f"Error decoding base64 message: {err}")
            return None

        try:
            bytes_img = BytesIO(msg_decoded)
        except Exception:  # noqa: BLE001 - matches the proof-of-concept's bare except
            self._log("Error converting message to BytesIO")
            return None
        return bytes_img

    def _load_prv_bmp_crc(self):
        """Load the previous BMP's CRC from sleep memory."""
        # If this is a cold boot, fetch image from IO instead
        if alarm.wake_alarm is None:
            return None
        try:
            rec = bytes(alarm.sleep_memory[0:8])
        except (NotImplementedError, AttributeError):
            # Port doesn't support sleep memory, always fetch and redraw the image on
            # cold boot
            return None
        if rec[0:4] != _MAGIC:
            return None
        return struct.unpack("<I", rec[4:8])[0]

    def _store_crc(self, crc):
        """Stores the BMP's CRC in sleep memory."""
        try:
            alarm.sleep_memory[0:8] = _MAGIC + struct.pack("<I", crc)
        except (NotImplementedError, AttributeError):
            pass
