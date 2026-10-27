---
title: Centinela
emoji: 🛡️
colorFrom: green
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: Detección de fraude en pagos con aprendizaje no supervisado
---

# Centinela

Herramienta antifraude para una entidad de pagos y sus comercios.

- **Casos y alertas** (`/`): los pagos sospechosos de cada día, con la acción recomendada y la probabilidad de fraude.
- **Modelos** (`/modelos/`): resumen visual de los modelos entrenados, cuál se usa y por qué.

Los datos son simulados. Esta carpeta la genera `despliegue/publicar.py` del repositorio del proyecto: un solo
contenedor con ClickHouse y las dos webs, que al arrancar carga una copia fija de la base de datos. Las revisiones
que se hagan en la web se pierden cuando el servicio se reinicia.

La cabecera de arriba es la que pide Hugging Face Spaces; Render usa `render.yaml`.
