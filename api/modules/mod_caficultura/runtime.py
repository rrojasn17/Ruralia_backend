"""Puntos de entrada del runtime Caficultura.

El registro global descubre este archivo por convención; el core no necesita
conocer el nombre del módulo ni sus routers.
"""

ROUTER_ENTRYPOINTS = (
    "modules.mod_caficultura.router:router",
    "modules.mod_caficultura.receipt_portal_router:router",
    "modules.mod_caficultura.notifications_router:router",
    "modules.mod_caficultura.ai_consulting_router:router",
    "modules.mod_caficultura.iot_router:router",
    "modules.mod_caficultura.ai_knowledge_router:router",
    "modules.mod_caficultura.ai_farm_router:router",
)

WORKER_ENTRYPOINT = "modules.mod_caficultura.worker:run_cycle"

LEGACY_DETECTOR_ENTRYPOINT = "modules.mod_caficultura.lifecycle:legacy_schema_exists"
