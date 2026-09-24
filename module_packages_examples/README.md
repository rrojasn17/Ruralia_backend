# Paquetes de ejemplo para el Marketplace interno

Estos ZIP contienen `module.json` en la raíz y pueden importarse desde **Administración > Módulos**.

- `mod_caficultura-1.0.0.zip`
- `mod_gestionfv-0.1.0.zip`

El ZIP importado **no ejecuta código**. La activación solo es posible cuando el runtime revisado correspondiente (`api/modules/mod_xxx`) ya forma parte del backend desplegado. Esto separa el catálogo/marketplace del mecanismo de ejecución y evita instalar código arbitrario desde el CMS.
