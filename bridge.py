#!/usr/bin/env python3
"""
Heatmiser → MQTT Bridge — Home Assistant Auto-Discovery
Safe RS485 access with command queue + immediate state updates
"""

import json
import logging
import threading
import time
import queue
import itertools
import paho.mqtt.client as mqtt
from heatmiserv3 import heatmiser, connection

LOG = logging.getLogger("heatmiser_mqtt_bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


class HeatmiserMqttBridge:
    def __init__(self, mqtt_cfg, hm_cfg, zones, hotwater=None, discovery_prefix="homeassistant"):
        self.mqtt_cfg = mqtt_cfg
        self.hm_cfg = hm_cfg
        self.zones = {z['name']: z for z in zones}
        self.discovery_prefix = discovery_prefix
        self.hotwater = hotwater
        self.poll_interval = hm_cfg.get("poll_interval", 120)

        # Single UH1 connection + lock
        self.hm_lock = threading.Lock()
        self.hm_conn = connection.HeatmiserUH1(
            device=hm_cfg['device'],
            ipaddress=hm_cfg['ip'], 
            port=hm_cfg['port'],
            url=hm_cfg['url'])

        # Cache Thermostat objects
        self.thermostats = {}
        self.hotwaterThermostat = None
        for name, zone in self.zones.items():
            self.thermostats[name] = heatmiser.HeatmiserThermostat(zone['id'], zone['type'], self.hm_conn)
            if self.hotwater and zone['id'] == self.hotwater['zone_id']:
                LOG.info("Hot water control enabled on zone '%s'", name)
                self.hotwaterThermostat = self.thermostats[name]


        # Command queue with priority (0=high=command,1=low=poll)
        self.task_queue = queue.PriorityQueue()
        self._counter = itertools.count()
        self._poll_pending = False
        self._poll_pending_lock = threading.Lock()

        # Start worker thread
        threading.Thread(target=self.worker_thread, daemon=True).start()

        # MQTT setup
        self.mqtt = mqtt.Client()
        self.mqtt.username_pw_set(mqtt_cfg['username'], mqtt_cfg['password'])
        self.mqtt.on_connect = self.on_connect
        self.mqtt.on_message = self.on_message
        self.mqtt.connect(mqtt_cfg['broker'], mqtt_cfg['port'])
        self.mqtt.loop_start()

        # Publish discovery
        for name, zone in self.zones.items():
            self.publish_discovery(name, zone)

        # Start polling thread
        threading.Thread(target=self.state_loop, daemon=True).start()

    # ------------------------------------------------------------------
    # MQTT handlers
    # ------------------------------------------------------------------
    def on_connect(self, client, userdata, flags, rc):
        LOG.info("MQTT connected (rc=%s). Subscribing...", rc)
        for name in self.zones:
            self.mqtt.subscribe(f"home/heatmiser/{name}/set/#")
        if self.hotwater:
            self.mqtt.subscribe("home/heatmiser/hotwater/set/#")

    def on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode().strip()
        LOG.info("MQTT RX: %s => %s", topic, payload)

        # --- Hot water ---
        if topic.startswith("home/heatmiser/hotwater/set/") and self.hotwater:
            thermo = self.hotwaterThermostat
            if topic.endswith("hw_state"):
                cmd = payload.upper()
                if cmd == "ON":
                    self.enqueue_task(0, thermo.set_hotwater_state, args=(thermo.HotWaterWriteState.ON,),
                                      desc="HotWater ON", callback=lambda _: self._publish_hotwater_state(cmd))
                elif cmd == "OFF":
                    self.enqueue_task(0, thermo.set_hotwater_state, args=(thermo.HotWaterWriteState.OFF,),
                                      desc="HotWater OFF", callback=lambda _: self._publish_hotwater_state(cmd))
                else:
                    LOG.warning("Invalid hotwater payload: %s", payload)                
            return

        # --- Thermostats ---
        for name, zone in self.zones.items():
            if topic.startswith(f"home/heatmiser/{name}/set/"):
                thermo = self.thermostats[name]

                if topic.endswith("target"):
                    try:
                        val = round(float(payload))
                        expected = {"target": val}
                        expected_copy = expected.copy()
                        self.enqueue_task(
                            0, thermo.set_target_temp, args=(val,),
                            desc=f"{name} set target {val}°C",
                            callback=lambda _: self._publish_single_state(name, expected_copy)
                        )                        
                    except ValueError:
                        LOG.warning("Invalid target temp for %s: %s", name, payload)

                elif topic.endswith("mode"):
                    frost = payload.upper() == "OFF"
                    expected = {"mode": payload.lower()}
                    expected_copy = expected.copy()
                    self.enqueue_task(
                        0, thermo.set_frost_protect_mode, args=(frost,),
                        desc=f"{name} set mode",
                        callback=lambda _: self._publish_single_state(name, expected_copy)
                    )
                return

    # ------------------------------------------------------------------
    # Command queue
    # ------------------------------------------------------------------
    def enqueue_task(self, priority, func, args=(), desc="", is_poll=False, callback=None):
        """Put a task into the queue with priority (0=command,1=poll)."""
        count = next(self._counter)
        self.task_queue.put((priority, count, (func, args, desc, is_poll, callback)))

    def worker_thread(self):
        """Execute commands sequentially, handle callbacks."""
        while True:
            priority, _, (func, args, desc, is_poll, callback) = self.task_queue.get()
            try:
                LOG.debug("Executing task: %s [%s]", desc, "poll" if is_poll else "command")
                result = self.with_lock(lambda: func(*args))
                if callback:
                    try:
                        callback(result)
                    except Exception as e:
                        LOG.warning("Callback for %s failed: %s", desc, e)
                if is_poll and isinstance(result, dict):
                    self._publish_poll_results(result)
            except Exception as e:
                LOG.error("Task '%s' failed: %s", desc, e)
            finally:
                if is_poll:
                    with self._poll_pending_lock:
                        self._poll_pending = False
                self.task_queue.task_done()
                time.sleep(0.25 if priority == 1 else 0.5)

    def with_lock(self, func, retries=2, delay=0.3):
        """Run UH1 command safely with retries."""
        for attempt in range(retries + 1):
            with self.hm_lock:
                try:
                    return func()
                except (OSError, TimeoutError) as e:
                    LOG.warning("UH1 command failed (%s), attempt %d/%d", e, attempt + 1, retries)
                    if attempt == retries:
                        self.reconnect()
                time.sleep(delay)
        raise Exception("Failed to communicate with UH1 after retries")

    def reconnect(self):
        with self.hm_lock:
            try:
                self.hm_conn.close()
            except Exception:
                pass
            time.sleep(1)
            self.hm_conn = connection.HeatmiserUH1(device=self.hm_cfg['device'],
                ipaddress=self.hm_cfg['ip'], 
                port=self.hm_cfg['port'],
                url=self.hm_cfg['url'])
            LOG.info("Reconnected to UH1")

    # ------------------------------------------------------------------
    # State publishing
    # ------------------------------------------------------------------
    def _publish_single_state(self, name, overrides):
        """Publish immediate state update for a single thermostat."""
        thermo = self.thermostats[name]
        state = {
            "temperature": thermo.get_air_temp(),
            "target": thermo.get_target_temp(),
            "mode": "off" if thermo.get_run_mode() == "frost" else "heat",
            "action": "heating" if thermo.get_current_state() else "idle",
        }
        state.update(overrides)
        for key, value in state.items():
            self.mqtt.publish(f"home/heatmiser/{name}/state/{key}", value, retain=True)
        LOG.debug(
            "Immediate MQTT update for %s → temp=%.1f target=%.1f mode=%s action=%s",
            name, state["temperature"], state["target"], state["mode"], state["action"]
        )
    def _publish_hotwater_state(self, hw_state=None):
        """Publish hot water state."""
        if not self.hotwater:
            return
        if hw_state is None:
            time.sleep(0.5) # slight delay to ensure state is updated    
            self.hotwaterThermostat.read_dcb()  # refresh data from UH1        
            hw_state = self.hotwaterThermostat.get_hotwater_state().upper()
            LOG.debug("Hot Water state not provided real state obtained: %s", hw_state)
        try:
            self.mqtt.publish("home/heatmiser/hotwater/state/hw_state", hw_state, retain=True)
            LOG.debug("Hot water state published: %s", hw_state)
        except Exception as e:
            LOG.warning("Failed to publish hot water state: %s", e)

    def publish_states(self):
        """Poll all thermostats and hot water (low-priority)."""
        def poll_all():
            results = {}
            for name, thermo in self.thermostats.items():
                thermo.read_dcb()  # refresh data from UH1
                zone = self.zones[name]
                temp = thermo.get_air_temp() if zone.get("sensor_type") == "air" else thermo.get_floor_temp()
                target = thermo.get_target_temp()
                mode = "off" if thermo.get_run_mode() == "frost" else "heat"
                action = "heating" if thermo.get_current_state() else "idle"
                hw_state = None                
                results[name] = dict(
                    temperature=temp, target=target, mode=mode, action=action, hw_state=hw_state
                )
                time.sleep(0.05)
            if self.hotwater:
                hw_state = self.hotwaterThermostat.get_hotwater_state().upper()
                results["hotwater"] = {"hw_state": hw_state} 
            return results

        with self._poll_pending_lock:
            if self._poll_pending:
                return
            self._poll_pending = True
        self.enqueue_task(1, poll_all, desc="Poll all thermostats", is_poll=True)

    def _publish_poll_results(self, results):
        """Publish results of poll to MQTT (outside lock)."""
        for name, res in results.items():
            if name == "hotwater" and self.hotwater:
                if res.get("hw_state") is not None:
                    self.mqtt.publish("home/heatmiser/hotwater/state/hw_state", res['hw_state'], retain=True)
                    LOG.debug("Published polled hot water state")
                continue
            self.mqtt.publish(f"home/heatmiser/{name}/state/temperature", res['temperature'], retain=True)
            self.mqtt.publish(f"home/heatmiser/{name}/state/target", res['target'], retain=True)
            self.mqtt.publish(f"home/heatmiser/{name}/state/mode", res['mode'], retain=True)
            self.mqtt.publish(f"home/heatmiser/{name}/state/action", res['action'], retain=True)            
            LOG.debug("Published polled state for %s", name)

    # ------------------------------------------------------------------
    # Home Assistant discovery
    # ------------------------------------------------------------------
    def publish_discovery(self, name, zone):
        tid = zone['id']
        climate_topic = f"{self.discovery_prefix}/climate/heatmiser_{name}/config"
        climate_payload = {
            "name": f"{name.capitalize()} Thermostat",
            "unique_id": f"heatmiser_{tid}_climate",
            "current_temperature_topic": f"home/heatmiser/{name}/state/temperature",
            "temperature_state_topic": f"home/heatmiser/{name}/state/target",
            "temperature_command_topic": f"home/heatmiser/{name}/set/target",
            "min_temp": 5,
            "max_temp": 30,
            "modes": ["heat", "off"],
            "mode_state_topic": f"home/heatmiser/{name}/state/mode",
            "mode_command_topic": f"home/heatmiser/{name}/set/mode",
            "action_topic": f"home/heatmiser/{name}/state/action",
        }
        self.mqtt.publish(climate_topic, json.dumps(climate_payload), retain=True)
        LOG.info("Published discovery for zone %s", name)

        if zone.get("type", "").lower() == "prthw" and self.hotwater:
            switch_topic = f"{self.discovery_prefix}/switch/heatmiser_hotwater/config"
            switch_payload = {
                "name": self.hotwater.get("name", "Hot Water"),
                "unique_id": f"heatmiser_{tid}_hotwater",
                "command_topic": "home/heatmiser/hotwater/set/hw_state",
                "state_topic": "home/heatmiser/hotwater/state/hw_state",
                "payload_on": "ON",
                "payload_off": "OFF",
                "state_on": "ON",
                "state_off": "OFF",
            }
            self.mqtt.publish(switch_topic, json.dumps(switch_payload), retain=True)
            LOG.info("Published hot water discovery for %s", name)

    # ------------------------------------------------------------------
    # Background polling loop
    # ------------------------------------------------------------------
    def state_loop(self):
        while True:
            try:
                self.publish_states()
            except Exception as e:
                LOG.error("Polling loop failed: %s", e)

            # occasional queue health log
            if int(time.time()) % 600 < 2:
                LOG.debug("Queue size: %d", self.task_queue.qsize())

            time.sleep(self.poll_interval)
