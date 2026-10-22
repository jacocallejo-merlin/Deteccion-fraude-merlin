"""
Las dos webs bajo una sola dirección: la de casos en / y la de modelos colgando de /modelos/.

    uvicorn unida:app --port 7860      # dentro del Space (ver arrancar.sh)

En local cada web sigue arrancando por separado (puertos 8000 y 8001). Los enlaces entre ellas salen de
WEB_MODELOS_URL y WEB_CASOS_URL, que el Dockerfile del Space fija a /modelos/ y /.
"""
from fastapi.responses import RedirectResponse

from web.app import app
from web_modelos.app import app as app_modelos


@app.get('/modelos', include_in_schema=False)
def modelos_con_barra():
    # sin la barra final, las rutas relativas de la web de modelos (static/, api/) apuntarían a la raíz
    return RedirectResponse('/modelos/')


app.mount('/modelos', app_modelos)
