# Portland Cluster — Flux Migration (Pending)
#
# The Portland cluster Flux configuration will be added here once the mobile
# cluster deployment has been validated.
#
# When ready, mirror the structure from flux/clusters/mobile/ and create a
# Portland-specific infrastructure overlay at flux/infrastructure/portland/
# with the appropriate patches:
#   - DCGM GPU node pool nodeSelector:
#       msft.microsoft/nodepool-name: pdx-aks-ead6c2bf-pdxgpu
#   - MetalLB IP range for the Portland subnet
#
# Bootstrap command (when ready):
#   flux bootstrap github \
#     --owner=mgodfre3 \
#     --repository=adaptivecloudlab-mwc26-demo \
#     --branch=main \
#     --path=flux/clusters/portland \
#     --personal
