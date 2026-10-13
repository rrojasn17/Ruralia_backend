# Copias de Caficultura

Desde Configuración → Respaldos, **Exportar respaldo** descarga un ZIP con un snapshot `navia_backup_v2`: usuarios, configuración guardada en la base, todas las tablas persistentes de Caficultura (incluidos IoT, IA y notificaciones) y los archivos de uploads públicos, IA y comprobantes. El snapshot PostgreSQL utiliza una lectura consistente. El manifiesto de archivos contiene huellas SHA-256.

**Restaurar copia** reemplaza los datos de Caficultura del destino dentro de una transacción. El destino deja de conservar sus clientes, fincas, catálogos, lecturas y demás registros operativos anteriores. Conserva el superadmin local con sus credenciales y sesión para mantener el acceso; las referencias al usuario del respaldo se ajustan cuando su ID difiere. Los archivos del ZIP reemplazan los uploads del destino y se recuperan los originales si falla la restauración. Se reparan las secuencias y se evita que el arranque recree catálogos o datos de demostración sobre la copia.

El usuario confirma el reemplazo en la pantalla; requiere el superadmin de bootstrap. **Combinar con los datos existentes** sigue disponible como modo alternativo y conserva los registros adicionales del destino.

## Respaldo anterior adjunto

`navia-respaldo-2026-10-13-002835.json` utiliza `navia_backup_v1`, contiene 38 tablas, 5 sensores, 1009 lecturas y 2 diseños de panel. Incluye las variables CO₂, PM10 y PM2.5 del nodo Dragino. El diseño guardado de la finca 1 solo incluye widgets del sensor anterior; el panel ahora incorpora las variables nuevas.

Los JSON v1 se aceptan en ambos modos. La restauración avisa que no contienen los archivos ni todas las tablas de IA y notificaciones; esos datos no se pueden recuperar de ese archivo. Para obtener una copia que los incluya, actualizar la instalación de origen y volver a exportar un ZIP. Los JSON sin archivos conservan los archivos existentes del destino y presentan el aviso correspondiente.

## Alcance

El ZIP copia el estado persistente de Caficultura y sus archivos, para restaurarlo sobre una instalación compatible. No contiene el código, `.env`, claves de servicios configuradas en variables de entorno, base de PostgreSQL completa, módulos externos ni sesiones/tokens temporales de autenticación. La configuración de la instalación y el módulo instalado se mantienen en el destino. El superadmin local es la excepción explícita al reemplazo de usuarios.

`GET /respaldos/exportar?format=zip` genera el archivo completo. `format=json` devuelve el snapshot de tablas para compatibilidad e indica que no lleva archivos. `POST /respaldos/importar?mode=replace` restaura una copia y `mode=merge` combina datos. Los ZIP y JSON nuevos se validan antes de modificar el destino; tablas o columnas de una versión más nueva requieren actualizar la instalación.
