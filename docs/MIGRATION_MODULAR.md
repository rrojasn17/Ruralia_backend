# Migración a arquitectura modular

## Instalación existente (Navia/Caficultura)

1. Respaldar PostgreSQL y uploads.
2. Aplicar primero el ZIP diferencial de backend y luego el de frontend.
3. Eliminar solo los archivos indicados en `DELETE_FILES.txt`.
4. Reconstruir imágenes/dependencias normalmente.
5. Ejecutar `python -m schema_upgrade` o el servicio `schema-init` de Docker Compose.
6. Iniciar API.
7. Entrar como Root/SuperAdmin y revisar **Administración > Módulos**.
8. La detección legacy debe mostrar `Caficultura` como `active`.
9. Validar recibos, lotes, clientes, fincas, IA e IoT antes de habilitar cambios de módulo en producción.

## Instalación nueva

1. Desplegar backend/frontend.
2. Ejecutar schema core.
3. Iniciar sesión con Root/SuperAdmin.
4. El dashboard debe aparecer sin funciones productivas.
5. Desde **Módulos**, importar uno de los paquetes de `module_packages_examples/`.
6. Instalar.
7. Activar.

## Prueba recomendada de exclusividad

1. Importar/instalar Caficultura.
2. Importar/instalar GestiónFV.
3. Activar GestiónFV y comprobar que Caficultura queda `disabled`.
4. Activar Caficultura y comprobar que GestiónFV queda `disabled`.
5. Verificar que los datos/tablas de ambos módulos permanecen intactos.

## Rollback

La desinstalación actual es lógica/no destructiva. No borra tablas. Para rollback de código, restaurar la versión anterior de frontend/backend y la copia de base de datos si se ejecutaron migraciones incompatibles posteriores.
