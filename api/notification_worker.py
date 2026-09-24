"""Compatibilidad temporal con despliegues que invocan ``notification_worker``.

El worker real ya pertenece al motor modular. Este wrapper puede eliminarse cuando
todos los despliegues hayan migrado a ``python -m module_worker``.
"""

from module_worker import main


if __name__ == "__main__":
    raise SystemExit(main())
