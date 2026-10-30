# 🚧 WIP — DO NOT USE

This directory is a **non-installing proof-of-concept** for an Unraid plugin. No .plg installer is published because Incus runtime, security, network and GPU integration are not yet validated.

Run the read-only preflight on an Unraid host: `sh unraid/scripts/preflight.sh`.

The script does not install packages, change files, or restart services. It emits observations, not a certification of compatibility. Never use upstream `polyseat-prepare` on Unraid.
