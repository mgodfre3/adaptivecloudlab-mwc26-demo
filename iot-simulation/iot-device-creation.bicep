@description('The IoT Hub name for the device registration.')
param iotHubName string

@description('The unique device ID.')
param deviceId string = 'drone-simulator'

resource device 'Microsoft.Devices/IotHubs/Devices@2021-04-12' = {
  parent: resourceId('Microsoft.Devices/IotHubs', iotHubName)
  name: deviceId
  properties: {
    status: 'enabled'
  }
}.
