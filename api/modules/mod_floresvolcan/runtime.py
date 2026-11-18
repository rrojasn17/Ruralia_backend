ROUTER_ENTRYPOINTS = ("modules.mod_floresvolcan.router:router",)
WORKER_ENTRYPOINT = "modules.mod_floresvolcan.backups:worker_cycle"

BACKUP_ENTRYPOINT = "modules.mod_floresvolcan.backup_provider:backup_spec"
