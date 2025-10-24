# heatmiser-mqtt-bridge
Bridge between Heatmiser UH1 Thermostats to MQTT for home automation control

This project makes extensive use of Neil Trimboy and laterly Andy Lockran https://github.com/andylockran/heatmiserV3/tree/main heatmiser code which has been modified slightly to make it more relevant for this use case. 

The software is connects to Heatmiser V3 thermostats via a UH1 and a serial connection. Polls the thermostats at regular intervals and publishes their current state to MQTT that can be readily accessed in Home Assistant. The state and temperature of the thermostats can similarly be updated via Home Assisstant and MQTT. I imagine the code could be adapted to work with other Home Automation systems such as Open Hab but I have not tried as yet.

##Installation
This application has been developed using Python 3 you will need to install pyserial, importlib-resources, yaml, serial-asyncio and paho-mqtt. In a debian based linux you could:
```
sudo apt install pyserial
sudo apt install python3-importlib-resources
sudo apt install python3-yaml
sudo apt install python3-serial-asyncio
sudo apt-get install python3-paho-mqtt
```
Update options.json.example to match your local environment and rename to options.json

### annotated options.json with comments explaining the fields (comments not supported in json)
```
{ 
  "heatmiser": {
    "device": null, # Serial device path, e.g. "/dev/ttyUSB0" or null for IP
    "ip": "192.168.1.xx",
    "port": "1024",
    "url": null # URL path, e.g. "/api/" or null
  },   
  "mqtt": {
    "broker": "192.168.1.xx", # MQTT broker IP address adress of the Home Assistant server if that is where MQTT is hosted
    "port": 1883,
    "username": "mqttuser",
    "password": "yourpassword"
  },
  "zones": [ 
    { "id": 1, "name": "Office", "type": "prt", "sensor_type": "air" },
    { "id": 2, "name": "Toilet", "type": "prt", "sensor_type": "air" },    
    { "id": 4, "name": "Kitchen", "type": "prt", "sensor_type": "floor" },#floor sensor type for underloor heating
    { "id": 5, "name": "Diningroom", "type": "prt", "sensor_type": "air" },
    { "id": 6, "name": "Hallway", "type": "prthw", "sensor_type": "air" },#prthw sensor type allows control of boiler
    { "id": 7, "name": "Livingroom", "type": "prt", "sensor_type":"air" },
    { "id": 8, "name": "Bed1", "type": "prt", "sensor_type": "air" },
    { "id": 9, "name": "Masterbed", "type": "prt", "sensor_type": "air" },
    { "id": 10, "name": "Bathroom", "type": "prt", "sensor_type": "air" },
    { "id": 11, "name": "Bed2", "type": "prt", "sensor_type": "air" },
    { "id": 12, "name": "Bed3", "type": "prt", "sensor_type": "air" }
  ],
  "hotwater": {
    "zone_id": 6, # Zone ID for hot water control via a prthw thermostat
    "name": "Boiler"
  },
  "poll_interval": 120, # Poll interval in seconds how regularly to poll the Heatmiser system
  "log_level": "DEBUG"
}
```


Then run:
```
python3 main.py
```
These heatmiser thermostats and the UHI controller are sensitive to mid air collisions, so the strategy in this application is to have a queue. A request to change a state of a thermostat or boiler control, or to poll the current state of the thermostats gets added to a queue. State changes are prioritised over polling, but there is a delay in actions requested as each has to be tackled one at a time.

In my case I have a lot of thermostats, so polling takes a while. The code could be improved to add individual thermostat polls to the queue so that polling could be interupted. Once a state change is made MQTT is updated directly rather than waiting for a poll so that Home Assistant controls feel responsive.

There are delays built into the code at the moment to reduce the chance of midair collision, these could be reduced through experimentation.

The code needs refinement but is operating effectively for me without CRC error. 

Once the application is running you should see the topics and events in MQTT. In Home Assistant you should be able to add your thermostats from the MQTT device section to your dashboard. Clicking the fire symbol takes your thermostat out of frost mode which is the same as clicking the phyical power button on the thermostat, clicking the power button on th HA tile will turn frost mode back on. The thermostat is idle when it is not calling for heat. It will call for heat when the target temperature is above the actual room temperature.

<img width="475" height="453" alt="hallwayThermostat" src="https://github.com/user-attachments/assets/799c6ea3-757e-4a42-b317-42ea98d0b34a" />

I have currently taken a very simple appoach for the boiler and just set it up as a simple switch which can be scheduled through automations in Home Assisstant. The heatmiser protocol does support scheduling but I have not implemented it.

<img width="471" height="130" alt="image" src="https://github.com/user-attachments/assets/c4c4b2b2-6a61-43be-a1f2-4aef43d8864b" />



