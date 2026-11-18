# Copias de Caficultura

Configuración → Respaldos de Caficultura exporta un ZIP `ruralia_module_backup_v1` con las tablas y adjuntos propios de Café: clientes, fincas, catálogos, recibos, lotes, ventas, IoT, IA y notificaciones. Cada archivo incluido tiene una huella SHA-256 y debe estar referenciado por un registro de Café.

La copia no contiene las tablas globales de cuentas y configuración, contraseñas, sesiones, permisos globales, marca, catálogo de módulos ni datos de Flores Volcán. Los autores se identifican por correo; las cuentas existentes conservan todos sus atributos. Los autores que no existen localmente se crean como referencias históricas inactivas, sin permisos ni contraseñas importadas. El resultado informa esas identidades para que el administrador gestione su acceso por separado.

**Restaurar copia** reemplaza solo los registros y los adjuntos referenciados de Caficultura, dentro de una transacción. Requiere el superadmin de bootstrap desde la pantalla de Café. No cambia la industria activa ni borra cuentas. Los archivos y datos del núcleo u otros módulos permanecen intactos. Si la restauración falla, los datos y archivos anteriores se recuperan.

**Combinar con los datos existentes** agrega o actualiza los registros de Café. El importador reconcilia códigos, claves únicas, referencias y widgets IoT entre instalaciones. Los roles restaurados pertenecen únicamente a Café y requieren Root; las asignaciones de otras industrias se conservan.

Los JSON conservan los archivos existentes y avisan que no incluyen adjuntos. Los respaldos anteriores `navia_backup_v1` y `navia_backup_v2` siguen siendo aceptados, pero los datos globales que incluían se omiten: contraseñas, permisos, configuración y archivos sin relación con los registros de Café. El resultado indica esas omisiones. Las tablas ausentes de formatos anteriores no se pueden recuperar de ese archivo.

El motor de archivo, integridad y restauración vive en el núcleo. Cada industria declara su proveedor `BACKUP_ENTRYPOINT`, sus modelos y sus límites de archivos. Véase [RESPALDOS_MODULARES.md](RESPALDOS_MODULARES.md) para agregar módulos futuros y usar la API independiente de la industria activa.

Las rutas de compatibilidad siguen disponibles:

- `GET /respaldos/exportar?format=zip|json`
- `POST /respaldos/importar?mode=replace|merge`

El ZIP no contiene código, `.env` ni una copia integral de PostgreSQL. Es una copia de los datos de Café, que se restaura sobre una versión compatible del módulo ya instalado.
