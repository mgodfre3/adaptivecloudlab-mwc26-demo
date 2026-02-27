// ============================================================================
// IoT Hub Device Registration — NOT POSSIBLE via Bicep/ARM
// ============================================================================
// IoT Hub devices (device identities) are a data-plane concept and do NOT have
// an ARM resource type.  Device registration must be done via:
//   - az iot hub device-identity create  (CLI)
//   - IoT Hub Service SDK (Python / C# / Node)
//   - IoT Hub REST API
//
// This file is kept as documentation.  Actual device creation is handled by
// the deployment script:  scripts/03-deploy-iot-simulation.ps1
//
// Example CLI:
//   az iot hub device-identity create --hub-name pdx-iothub --device-id drone-1
//   az iot hub device-identity connection-string show --hub-name pdx-iothub --device-id drone-1
// ============================================================================

// To keep the Bicep tooling happy, export a descriptive output.
@description('Reminder: IoT devices must be created via CLI/SDK, not Bicep.')
output notice string = 'Use scripts/03-deploy-iot-simulation.ps1 to register drone devices.'
