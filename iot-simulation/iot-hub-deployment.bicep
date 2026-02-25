// Bicep template to deploy IoT Hub.
resource iotHub 'Microsoft.Devices/IotHubs@2022-04-01' = {
  name: 'your-iot-hub-name'
  location: resourceGroup().location
  sku: {
    name: 'S1'
    tier: 'Standard'
    capacity: 1
  }
  properties: {
    // Add properties here
  }
}