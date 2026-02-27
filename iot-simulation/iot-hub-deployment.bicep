// ============================================================================
// IoT Hub deployment for Drone Network Monitoring Demo
// Deploys an IoT Hub with a consumer group for reading drone telemetry.
// Usage:
//   az deployment group create -g <rg> --template-file iot-hub-deployment.bicep \
//       --parameters prefix=pdx location=southcentralus droneCount=5
// ============================================================================

@description('Resource naming prefix (lowercase, no spaces).')
param prefix string

@description('Azure region for the IoT Hub.')
param location string = resourceGroup().location

@description('IoT Hub SKU name.')
@allowed(['F1', 'S1', 'S2', 'S3', 'B1', 'B2', 'B3'])
param skuName string = 'S1'

@description('IoT Hub SKU capacity (number of units).')
param skuCapacity int = 1

@description('Number of simulated drones (used for tagging only; devices created via CLI).')
param droneCount int = 5

@description('Retention period in days for device-to-cloud messages.')
param d2cRetentionDays int = 1

@description('Number of device-to-cloud partitions.')
param d2cPartitionCount int = 4

var iotHubName = '${prefix}-iothub'
var consumerGroupName = 'drone-telemetry'

resource iotHub 'Microsoft.Devices/IotHubs@2023-06-30' = {
  name: iotHubName
  location: location
  tags: {
    project: 'adaptivecloudlab-mwc26-demo'
    component: 'iot-hub'
    droneCount: string(droneCount)
  }
  sku: {
    name: skuName
    capacity: skuCapacity
  }
  properties: {
    eventHubEndpoints: {
      events: {
        retentionTimeInDays: d2cRetentionDays
        partitionCount: d2cPartitionCount
      }
    }
    routing: {
      fallbackRoute: {
        name: '$fallback'
        source: 'DeviceMessages'
        endpointNames: ['events']
        isEnabled: true
        condition: 'true'
      }
    }
  }
}

// Consumer group for the telemetry aggregation pipeline
resource consumerGroup 'Microsoft.Devices/IotHubs/eventHubEndpoints/ConsumerGroups@2023-06-30' = {
  name: '${iotHub.name}/events/${consumerGroupName}'
  properties: {
    name: consumerGroupName
  }
}

// ── Outputs ─────────────────────────────────────────────────────────────────
@description('The deployed IoT Hub name.')
output iotHubName string = iotHub.name

@description('The IoT Hub resource ID.')
output iotHubId string = iotHub.id

@description('The IoT Hub hostname (for device connection strings).')
output iotHubHostName string = iotHub.properties.hostName

@description('Built-in Event Hub-compatible endpoint.')
output eventHubEndpoint string = iotHub.properties.eventHubEndpoints.events.endpoint

@description('Built-in Event Hub-compatible path.')
output eventHubPath string = iotHub.properties.eventHubEndpoints.events.path

@description('Consumer group for drone telemetry.')
output consumerGroupName string = consumerGroupName