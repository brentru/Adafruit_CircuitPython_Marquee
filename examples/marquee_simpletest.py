# SPDX-FileCopyrightText: 2017 Scott Shawcroft, written for Adafruit Industries
# SPDX-FileCopyrightText: Copyright (c) 2026 brentru for Adafruit Industries
#
# SPDX-License-Identifier: Unlicense
# POC
# Adafruit IO Marquee for CircuitPython
from os import getenv
import os
from io import BytesIO
import alarm
import binascii
import board
import busio
import digitalio
import displayio
import time
import json
import adafruit_connection_manager
import wifi
import adafruit_imageload
import adafruit_minimqtt.adafruit_minimqtt as MQTT
from micropython import const
import struct

# Marquee, Schema v1
_MAGIC = b"MRQ\x01"

# Adafruit IO configuration
AIO_USER = getenv("ADAFRUIT_AIO_USERNAME")
AIO_KEY = getenv("ADAFRUIT_AIO_KEY")
AIO_HOST = getenv("ADAFRUIT_IO_HOST")  # Used for debugging ONLY, not production server
FEED_NAME = getenv("ADAFRUIT_IO_FEED", "marquee")  # Fallback to default marquee feed if not specified
FEED = f"{AIO_USER}/feeds/{FEED_NAME}"  # Construct the full feed path
FEED_SLEEP = f"{AIO_USER}/feeds/{FEED_NAME}-sleep"  # Construct the full feed path for sleep messages
FEED_STATUS = f"{AIO_USER}/feeds/{FEED_NAME}-status"  # Construct the full feed path for status messages

# Begin Marquee #
# Open the configuration file
try:
    with open("cfg-marquee.json", "r") as f:
        CONFIG = json.load(f)
except FileNotFoundError:
    raise FileNotFoundError("Required file 'cfg-marquee.json' not found!")

# Validate the configuration file
if "display" not in CONFIG or "interface" not in CONFIG:
    raise ValueError("Invalid configuration file: missing 'display' or 'interface' keys.")

# Parse panel and interface configuration
PANEL = CONFIG["display"]
PINS = CONFIG["interface"]["pins"]
SPI_CFG = CONFIG["interface"]["spi"]

# cfg-marquee.json stores rotation as a quadrant index (0-3), but displayio
# expects degrees..accept either format so a config written in degrees also works.
ROTATION = PANEL.get("rotation", 0)
if ROTATION in (1, 2, 3):
    ROTATION *= 90
if ROTATION not in (0, 90, 180, 270):
    raise ValueError(f"Invalid display rotation: {PANEL.get('rotation')}")


# To avoid blocking, MQTT payloads are saved by the callbacks and executed in the main loop
pending_image = None
pending_sleep = None
# If the MQTT connection fails during display refresh, it's flagged and the main loop handles the reconnection
needs_reconnect = False


# Define callback methods which are called when events occur
def connected(client, userdata, flags, rc):
    # This function will be called when the client is connected
    # successfully to the broker.
    print(f"Connected to Adafruit IO!")

    # Publish status
    try:
        mqtt_client.publish(FEED_STATUS, json.dumps({
            "state": "awake",
            "wake_reason": "timer"
        }))
    except (MQTT.MMQTTException, OSError) as err:
        print(f"Could not publish awake status ({err}), continuing anyway")

    # Subscribe to all changes on the marquee feed
    client.subscribe(FEED)
    client.subscribe(FEED_SLEEP)
    print("Subscribed!")
    # Kick out the last message on the feed, if it exists, so we can display it immediately
    client.publish(f"{FEED}/get", "get")
    # If we were previously sleeping, we need to fetch the sleep feed to see if we should sleep again
    if (alarm.wake_alarm is not None):
        print("Woke from sleep, fetching sleep feed to see if we should sleep again")
        client.publish(f"{FEED_SLEEP}/get", "get")

def disconnected(client, userdata, rc):
    # This method is called when the client is disconnected
    print("Disconnected from Adafruit IO!")


def cb_marquee(client, topic, message):
    global pending_image
    pending_image = message


def cb_marquee_sleep(client, topic, message):
    global pending_sleep
    print(f"New message on feed {topic}: {message}")
    pending_sleep = message


def show_image(message):
    global needs_reconnect

    payload = message if isinstance(message, bytes) else message.encode()
    crc = binascii.crc32(memoryview(payload)) & 0xFFFFFFFF
    if crc == _load_prv_bmp_crc():
        print("BMP unchanged, skipping redraw")
        return

    bytes_img = convert_message_to_image(message)
    try:
        image, palette = adafruit_imageload.load(bytes_img)
    except AttributeError as err:
        print(f"Error loading image: {err}")
        return
    print(f"Loaded {image.width}x{image.height} image")
    if image.width != display.width or image.height != display.height:
        # A mismatch here silently clips the image instead of raising, so say so
        print(f"WARNING: image does not match display ({display.width}x{display.height})")

    tile_grid = displayio.TileGrid(image, pixel_shader=palette)
    group = displayio.Group()
    group.append(tile_grid)
    display.root_group = group

    # Wait out the panel's cooldown before refreshing
    time.sleep(display.time_to_refresh)
    display.refresh()
    # Ping the broker to keep the connection alive while waiting for the display to finish refreshing
    while display.busy:
        if not needs_reconnect:
            try:
                mqtt_client.ping()
            except (MQTT.MMQTTException, OSError) as err:
                print(f"Ping failed ({err}), will reconnect after the refresh")
                needs_reconnect = True
        time.sleep(0.5)
    # Store the BMP CRC for next wake
    _store_crc(crc)


def enter_sleep(message):
    """Arm the alarms described by a {feed}-sleep payload, then sleep."""
    # A payload we cannot read is a normal state (first boot, a feed that was
    # never created), not an error -- fall back to a plain timer rather than
    # skipping the sleep entirely.
    try:
        sleep_msg = json.loads(message)
        alarm_type = sleep_msg["alarm_type"]
        sleep_mode = sleep_msg["sleep_mode"]
        sleep_time = int(sleep_msg["sleep_time"])
    except (ValueError, KeyError, TypeError) as err:
        print(f"ERR: Unusable sleep payload ({err}), staying awake")
        return

    # Validate the payload values
    if alarm_type not in ("timer", "pin", "timer+pin"):
        print(f"ERR: Unknown alarm_type: {alarm_type}, staying awake")
        return
    if sleep_mode not in ("light", "deep"):
        print(f"ERR: Unknown sleep_mode: {sleep_mode}, staying awake")
        return

    time_alarm = None
    pin_alarm = None

    # Configure a time-based alarm
    if alarm_type in ("timer", "timer+pin") and sleep_time > 0:
        time_alarm = alarm.time.TimeAlarm(monotonic_time=time.monotonic() + sleep_time)

    # TODO: Add a way to kick the device OUT of sleep in this message structure!

    # TODO: Configure a pin alarm
    # TODO NOTE: Pin alarms require a value and a pull value too, we need this in the schema
    if alarm_type in ("pin", "timer+pin"):
        # TODO: See the value and pull values here?
        # pin_alarm = alarm.pin.PinAlarm(board.BUTTON_A, value=False, pull=True)
        print("Pin alarms are not yet implemented, ignoring pin alarm request")

    # If alarms are not configured, don't sleep
    if time_alarm is None and pin_alarm is None:
        print("No alarms configured, staying awake")
        return

    # Publish out to FEED_STATUS
    try:
        mqtt_client.publish(FEED_STATUS, json.dumps({
            "state": "sleeping",
            "sleep_time": sleep_time,
            "alarm_type": alarm_type
        }))
    except (MQTT.MMQTTException, OSError) as err:
        print(f"Could not publish sleep status ({err}), continuing to sleep anyway")

    # Exit into sleep
    if sleep_mode == "deep":
        try:
            mqtt_client.disconnect()
        except Exception as err:
            print(f"Could not disconnect cleanly: {err}")
        if time_alarm and pin_alarm:
            # TODO: Replace this with a real pin alarm when implemented
            print(f"Deep sleeping for {sleep_time}s or until the button is pressed...")
            alarm.exit_and_deep_sleep_until_alarms(time_alarm, pin_alarm)
        elif pin_alarm:
            # TODO: Replace this with a real pin alarm when implemented
            print("Deep sleeping until the button is pressed...")
            alarm.exit_and_deep_sleep_until_alarms(pin_alarm)
        else:
            print(f"Deep sleeping for {sleep_time}s...")
            alarm.exit_and_deep_sleep_until_alarms(time_alarm)
    else:
        # Light sleep
        if time_alarm and pin_alarm:
            # TODO: Replace this with a real pin alarm when implemented
            print(f"Light sleeping for {sleep_time}s or until the button is pressed...")
            alarm.light_sleep_until_alarms(time_alarm, pin_alarm)
        elif pin_alarm:
            # TODO: Replace this with a real pin alarm when implemented
            print("Light sleeping until the button is pressed...")
            alarm.light_sleep_until_alarms(pin_alarm)
        else:
            print(f"Light sleeping for {sleep_time}s...")
            alarm.light_sleep_until_alarms(time_alarm)

        # Resume execution after light sleep
        print(f"Woke from light sleep on {alarm.wake_alarm}")


def convert_message_to_image(message):
    """Converts a base64-encoded BMP to a binary-encoded BMP."""
    try:
        msg_decoded = binascii.a2b_base64(message)
    except ValueError as err:
        print(f"Error decoding base64 message: {err}")
        return None

    try:
        bytes_img = BytesIO(msg_decoded)
    except:
        print("Error converting message to BytesIO")
        return None
    return bytes_img


def _load_prv_bmp_crc():
    """Load the previous BMP's CRC from sleep memory."""
    # If this is a cold boot, fetch image from IO instead
    if alarm.wake_alarm is None:
        return None
    try:
        rec = bytes(alarm.sleep_memory[0:8])
    except (NotImplementedError, AttributeError):
        # Port doesn't support sleep memory, always fetch and redraw the image on cold boot
        return None
    if rec[0:4] != _MAGIC:
        return None
    return struct.unpack("<I", rec[4:8])[0]

def _store_crc(crc):
    """Stores the BMP's CRC in sleep memory."""
    try:
        alarm.sleep_memory[0:8] = _MAGIC + struct.pack("<I", crc)
    except (NotImplementedError, AttributeError):
        pass


# Display configuration and setup
display = board.DISPLAY
display.rotation = ROTATION
print(f"Display: {display.width}x{display.height} rotation={display.rotation}")

# Connect to WiFi
print(f"Connecting to {os.getenv('CIRCUITPY_WIFI_SSID')}")
wifi.radio.connect(os.getenv("CIRCUITPY_WIFI_SSID"), os.getenv("CIRCUITPY_WIFI_PASSWORD"))
print(f"Connected to {os.getenv('CIRCUITPY_WIFI_SSID')}")

# Create a socket pool and ssl_context
pool = adafruit_connection_manager.get_radio_socketpool(wifi.radio)
ssl_context = adafruit_connection_manager.get_radio_ssl_context(wifi.radio)

# Set up a MiniMQTT Client
mqtt_client = MQTT.MQTT(
    broker=AIO_HOST,
    username=AIO_USER,
    password=AIO_KEY,
    socket_pool=pool,
    ssl_context=ssl_context,
    keep_alive=60 # Long-lived Keep-alive to avoid disconnection during EPD refreshes
)


# Setup the callback methods above
mqtt_client.on_connect = connected
mqtt_client.on_disconnect = disconnected
mqtt_client.add_topic_callback(FEED_SLEEP, cb_marquee_sleep)
mqtt_client.add_topic_callback(FEED, cb_marquee)

# Connect the client to the MQTT broker.
print("Connecting to Adafruit IO...")
mqtt_client.connect()

while True:
    # Perform a reconnect if the last ping failed during a display refresh
    if needs_reconnect:
        needs_reconnect = False
        print("Reconnecting to Adafruit IO...")
        mqtt_client.reconnect()

    # Poll the message queue
    mqtt_client.loop(timeout=1)

    # Execute any pending commands from the callbacks
    if pending_image is not None:
        msg, pending_image = pending_image, None
        show_image(msg)
    if pending_sleep is not None:
        msg, pending_sleep = pending_sleep, None
        enter_sleep(msg)