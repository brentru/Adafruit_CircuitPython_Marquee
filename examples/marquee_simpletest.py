# SPDX-FileCopyrightText: 2021 ladyada for Adafruit Industries
# SPDX-License-Identifier: MIT
from os import getenv
import adafruit_connection_manager
import wifi
from adafruit_marquee import Marquee

# Configure WiFi
ssid = getenv("CIRCUITPY_WIFI_SSID")
print(f"Connecting to {ssid}")
wifi.radio.connect(ssid, getenv("CIRCUITPY_WIFI_PASSWORD"))
print(f"Connected to {ssid}")

# Create a socket pool and ssl_context
pool = adafruit_connection_manager.get_radio_socketpool(wifi.radio)
ssl_context = adafruit_connection_manager.get_radio_ssl_context(wifi.radio)

# Configure EPD Marquee API
marquee = Marquee(
    socket_pool=pool,
    ssl_context=ssl_context,
    aio_username=getenv("ADAFRUIT_AIO_USERNAME"),
    aio_key=getenv("ADAFRUIT_AIO_KEY"),
    aio_feed=getenv("ADAFRUIT_IO_FEED", "marquee"),
    debug=True,
)

marquee.connect()

# NOTE: loop() may sleep the board
while True:
    marquee.loop()
