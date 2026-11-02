# Polyseat for Unraid (WIP, not released)

Settings live in `/boot/config/plugins/polyseat-unraid/polyseat-unraid.cfg`
(`SERVICE`, `DATA_DIR`, ...). Seats, game libraries and Incus state live in
`DATA_DIR` and are never removed by the plugin.

    polyseatctl status [--json] | verify | health | preflight | diagnose
    polyseatctl start | stop | restart
    polyseatctl update ARCHIVE MANIFEST SIG --approve | rollback --approve | recover --approve
