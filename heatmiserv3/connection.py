"""This module is effectively a singleton for serial comms"""
import serial
import logging
from . import constants
from heatmiserv3 import heatmiser

logging.basicConfig(level=logging.INFO)


class HeatmiserUH1(object):
    """
    UH1 interface that can open either:
      - a socket URL (e.g. ipaddress + port -> socket://IP:PORT) or
      - a direct serial device (e.g. '/dev/ttyUSB0' or 'COM3').

    Parameters:
      device: Optional[str] - path to serial device (preferred for USB-to-serial)
      ipaddress: Optional[str] - IP address or host for socket mode
      port: Optional[int|str] - port for socket mode
      url: Optional[str] - full serial_for_url string (overrides ipaddress/port)
      mode: 'auto'|'socket'|'device' - choose mode explicitly or let class decide (default 'auto')
      baudrate: Optional[int] - overrides constants.COM_BAUD
    """

    def __init__(self, device=None, ipaddress=None, port=None, url=None,
                 mode="auto", baudrate=None):
        self.thermostats = {}
        self.status = False
        self._serport = None
        self.mode = mode
        self._baudrate = baudrate if baudrate is not None else constants.COM_BAUD

        # Decide which mode to use
        if mode == "device":
            chosen = "device"
        elif mode == "socket":
            chosen = "socket"
        else:  # auto
            if device:
                chosen = "device"
            elif url or (ipaddress and port):
                chosen = "socket"
            else:
                raise ValueError("Must provide either device or (ipaddress and port) or url")

        self._mode = chosen
        logging.info("HeatmiserUH1 selected mode: %s", self._mode)

        try:
            if self._mode == "socket":
                # Build url if not supplied
                if not url:
                    url = f"socket://{ipaddress}:{port}"
                # serial_for_url supports do_not_open argument for lazy-open
                # Some pyserial versions accept do_not_open=True
                try:
                    self._serport = serial.serial_for_url(url, do_not_open=True)
                except TypeError:
                    # Older pyserial may not support do_not_open; create and close immediately
                    self._serport = serial.serial_for_url(url)
                    if self._serport.is_open:
                        self._serport.close()
                # configure properties
                self._serport.baudrate = self._baudrate
                self._serport.bytesize = constants.COM_SIZE
                self._serport.parity = constants.COM_PARITY
                self._serport.stopbits = constants.COM_STOP
                self._serport.timeout = constants.COM_TIMEOUT

            else:  # device
                if not device:
                    raise ValueError("Device path required for device mode")
                # Create Serial object in closed state and set properties before open
                self._serport = serial.Serial()
                self._serport.port = device
                self._serport.baudrate = self._baudrate
                self._serport.bytesize = constants.COM_SIZE
                self._serport.parity = constants.COM_PARITY
                self._serport.stopbits = constants.COM_STOP
                self._serport.timeout = constants.COM_TIMEOUT

            # Ensure port not left open from previous process (best-effort)
            try:
                if self._serport.is_open:
                    logging.info("Port was left open; closing before proceed.")
                    self._serport.close()
            except Exception:
                # some serial_for_url objects might not implement is_open before open; ignore
                pass

            # Try to open now
            self._open()
        except Exception as e:
            logging.exception("Failed to initialize serial connection: %s", e)
            # Leave status False; user can call reopen() or handle the failure

    def _open(self):
        if not self._serport:
            logging.error("No serial port object available to open.")
            return False

        if not self.status:
            try:
                logging.info("Opening serial port (%s).", getattr(self._serport, "port", "URL"))
                self._serport.open()
                self.status = True
                logging.info("Serial port opened successfully.")
                return True
            except serial.SerialException as e:
                logging.error("SerialException while opening port: %s", e)
                self.status = False
                return False
            except Exception as e:
                logging.exception("Unexpected error opening serial port: %s", e)
                self.status = False
                return False
        else:
            logging.info("Attempting to access already open port")
            return False

    def reopen(self):
        """
        Attempt to open the port if closed. Returns True if opened, False otherwise.
        """
        if not self.status:
            return self._open()
        else:
            logging.error("Cannot open serial port: already open")
            return False

    def close(self):
        """
        Close the serial port cleanly. Returns True on success or if already closed.
        """
        if self._serport:
            try:
                if getattr(self._serport, "is_open", False):
                    logging.info("Closing serial port.")
                    self._serport.close()
                    self.status = False
                else:
                    self.status = False
                return True
            except serial.SerialException as e:
                logging.error("Error while closing serial port: %s", e)
                return False
            except Exception as e:
                logging.exception("Unexpected error while closing serial port: %s", e)
                return False
        else:
            self.status = False
            return True

    def __del__(self):
        # Best-effort cleanup (not guaranteed to run)
        try:
            logging.info("HeatmiserUH1 __del__ closing port.")
            self.close()
        except Exception:
            pass

    def registerThermostat(self, thermostat):
        """Registers a thermostat with the UH1"""
        try:
            type(thermostat) == heatmiser.HeatmiserThermostat
            if thermostat.address in self.thermostats.keys():
                raise ValueError("Key already present")
            else:
                self.thermostats[thermostat.address] = thermostat
        except ValueError:
            pass
        except Exception as e:
            logging.info("You're not adding a HeatmiiserThermostat Object")
            logging.info(e.message)
        return self._serport

    def listThermostats(self):
        if self.thermostats:
            return self.thermostats
        else:
            return None

