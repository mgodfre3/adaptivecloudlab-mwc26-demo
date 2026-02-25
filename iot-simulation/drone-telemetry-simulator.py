import random
import time
from azure.iot.device import IoTHubDeviceClient, Message

# IoT Hub connection string
CONNECTION_STRING = "<your-iot-hub-connection-string>"

# Initialize the IoT Hub client
client = IoTHubDeviceClient.create_from_connection_string(CONNECTION_STRING)

# Function to simulate drone telemetry
def simulate_drone_telemetry():
    while True:
        telemetry = {
            "drone_id": "drone-1",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "location": {"lat": random.uniform(-90, 90), "lon": random.uniform(-180, 180)},
            "5g_signal_strength": random.randint(-100, -50),
            "battery": random.randint(10, 100),
            "status": random.choice(["active", "idle", "charging"])
        }
        message = Message(str(telemetry))
        client.send_message(message)
        print(f"Telemetry sent: {telemetry}")
        time.sleep(5)  # Send every 5 seconds

if __name__ == "__main__":
    print("Simulating drone telemetry... Press CTRL+C to exit")
    simulate_drone_telemetry()