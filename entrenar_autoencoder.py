import os

import joblib
import numpy as np
import pandas as pd
import torch
from torch import nn

CARPETA_DATOS = 'datos_ml'
CARPETA_MODELOS = 'modelos'

# --- arquitectura ---
CAPAS = [24, 16, 8]   
CUELLO = 8           

# --- entrenamiento ---
RUIDO = 0            
                     
EPOCAS = 2000         
BATCH = 256
LR = 1e-3
PACIENCIA = 20       
VALIDACION = 0.15    
SEMILLA = 42


def cargar():
    X_train = pd.read_parquet(os.path.join(CARPETA_DATOS, 'X_train.parquet'))
    X_test = pd.read_parquet(os.path.join(CARPETA_DATOS, 'X_test.parquet'))
    info_train = pd.read_parquet(os.path.join(CARPETA_DATOS, 'info_train.parquet'))
    print(f'Train: {X_train.shape}   Test: {X_test.shape}')

    corte = int(len(X_train) * (1 - VALIDACION))
    es_fraude = info_train['es_fraude'].astype(int).to_numpy()
    legitimas = es_fraude == 0

    X_fit = X_train.iloc[:corte][legitimas[:corte]]
    X_val = X_train.iloc[corte:][legitimas[corte:]]
    X_val_completo = X_train.iloc[corte:]

    print(f'Entrenamiento semi-supervisado: solo transacciones legítimas '
          f'({es_fraude.sum()} fraudes apartados)')
    print(f'  ajuste: {len(X_fit):,} filas   validación: {len(X_val):,} filas (las más recientes)')
    print(f'  validación completa para evaluar: {len(X_val_completo):,} filas '
          f'({int(es_fraude[corte:].sum())} fraudes)')
    return X_train, X_test, X_fit, X_val, X_val_completo


class Autoencoder(nn.Module):
    def __init__(self, n_entrada, capas, cuello):
        super().__init__()
        tamanos = [n_entrada] + list(capas) + [cuello]

        codificador = []
        for a, b in zip(tamanos[:-1], tamanos[1:]):
            codificador += [nn.Linear(a, b), nn.Tanh()]
        self.codificador = nn.Sequential(*codificador)

        invertido = tamanos[::-1]
        decodificador = []
        for i, (a, b) in enumerate(zip(invertido[:-1], invertido[1:])):
            decodificador.append(nn.Linear(a, b))
            if i < len(invertido) - 2:
                decodificador.append(nn.Tanh())  
        self.decodificador = nn.Sequential(*decodificador)  

    def forward(self, x):
        return self.decodificador(self.codificador(x))


def entrenar(X_fit, X_val):
    torch.manual_seed(SEMILLA)
    np.random.seed(SEMILLA)

    datos = torch.tensor(X_fit.to_numpy(), dtype=torch.float32)
    validacion = torch.tensor(X_val.to_numpy(), dtype=torch.float32)

    modelo = Autoencoder(datos.shape[1], CAPAS, CUELLO)
    parametros = sum(p.numel() for p in modelo.parameters())
    arquitectura = ' -> '.join(str(t) for t in
                               [datos.shape[1], *CAPAS, CUELLO, *CAPAS[::-1], datos.shape[1]])
    print(f'\nArquitectura: {arquitectura}   ({parametros:,} parámetros)')
    print(f'Ruido gaussiano en la entrada: sigma = {RUIDO}')

    optimizador = torch.optim.Adam(modelo.parameters(), lr=LR)
    criterio = nn.MSELoss()
    generador = torch.Generator().manual_seed(SEMILLA)

    mejor, mejor_estado, sin_mejorar = float('inf'), None, 0
    print(f'\n{"época":>6}{"pérdida ajuste":>17}{"pérdida validación":>21}')

    for epoca in range(1, EPOCAS + 1):
        modelo.train()
        orden = torch.randperm(len(datos), generator=generador)
        acumulado = 0.0
        for i in range(0, len(datos), BATCH):
            lote = datos[orden[i:i + BATCH]]
            ruidoso = lote + torch.randn(lote.shape, generator=generador) * RUIDO
            perdida = criterio(modelo(ruidoso), lote)
            optimizador.zero_grad()
            perdida.backward()
            optimizador.step()
            acumulado += perdida.item() * len(lote)
        perdida_ajuste = acumulado / len(datos)

        modelo.eval()
        with torch.no_grad():
            ruido_val = torch.randn(validacion.shape, generator=generador) * RUIDO
            perdida_val = criterio(modelo(validacion + ruido_val), validacion).item()

        if epoca % 10 == 0 or epoca == 1:
            print(f'{epoca:>6}{perdida_ajuste:>17.5f}{perdida_val:>21.5f}')

        if perdida_val < mejor - 1e-5:
            mejor, sin_mejorar = perdida_val, 0
            mejor_estado = {k: v.clone() for k, v in modelo.state_dict().items()}
        else:
            sin_mejorar += 1
            if sin_mejorar >= PACIENCIA:
                print(f'{epoca:>6}   parada temprana (sin mejorar en {PACIENCIA} épocas)')
                break

    modelo.load_state_dict(mejor_estado)
    print(f'Mejor pérdida de validación: {mejor:.5f}')
    return modelo


def error_reconstruccion(modelo, X):
    modelo.eval()
    with torch.no_grad():
        datos = torch.tensor(X.to_numpy(), dtype=torch.float32)
        return ((modelo(datos) - datos) ** 2).mean(dim=1).numpy()


def error_por_variable(modelo, X):
    modelo.eval()
    with torch.no_grad():
        datos = torch.tensor(X.to_numpy(), dtype=torch.float32)
        return pd.DataFrame(((modelo(datos) - datos) ** 2).numpy(),
                            columns=X.columns, index=X.index)


def normalizar(errores_train, errores):
    referencia = np.sort(errores_train)
    return np.searchsorted(referencia, errores, side='right') / len(referencia)


def explicar(modelo, X_test, err_test, n=5, variables=3):
    detalle = error_por_variable(modelo, X_test)
    print(f'\nLas {n} transacciones más anómalas del test y por qué:')
    for pos in np.argsort(-err_test)[:n]:
        fila = detalle.iloc[pos].sort_values(ascending=False)
        total = fila.sum()
        culpables = ', '.join(f'{nombre} ({valor / total * 100:.0f} %)'
                              for nombre, valor in fila.head(variables).items())
        print(f'  fila {X_test.index[pos]}  error {err_test[pos]:.2f}  ->  {culpables}')


def guardar(modelo, conjuntos, errores, scores, n_entrada, columnas):
    os.makedirs(CARPETA_MODELOS, exist_ok=True)
    torch.save({'estado': modelo.state_dict(), 'capas': CAPAS, 'cuello': CUELLO,
                'ruido': RUIDO, 'n_entrada': n_entrada, 'columnas': columnas},
               os.path.join(CARPETA_MODELOS, 'autoencoder.pt'))
    joblib.dump(np.sort(errores['train']),
                os.path.join(CARPETA_MODELOS, 'autoencoder_referencia.joblib'))

    for parte in ('train', 'val', 'test'):
        pd.DataFrame({'error': errores[parte], 'score': scores[parte]},
                     index=conjuntos[parte].index).to_parquet(
            os.path.join(CARPETA_MODELOS, f'autoencoder_scores_{parte}.parquet'))

    print(f'\nGuardado en "{CARPETA_MODELOS}/": autoencoder.pt, autoencoder_referencia.joblib '
          'y los scores de train, val y test')



def main():
    X_train, X_test, X_fit, X_val, X_val_completo = cargar()
    modelo = entrenar(X_fit, X_val)

    conjuntos = {'train': X_train, 'val': X_val_completo, 'test': X_test}
    errores = {k: error_reconstruccion(modelo, X) for k, X in conjuntos.items()}
    scores = {k: normalizar(errores['train'], e) for k, e in errores.items()}

    print('\nError de reconstrucción (train completo):')
    print(pd.Series(errores['train']).describe().round(3).to_string())
    print(f'\nTest por encima del percentil 99 del train: '
          f'{(scores["test"] >= 0.99).sum():,} de {len(scores["test"]):,} '
          f'({(scores["test"] >= 0.99).mean() * 100:.2f} %)')

    explicar(modelo, X_test, errores['test'])
    guardar(modelo, conjuntos, errores, scores, X_train.shape[1], list(X_train.columns))


if __name__ == '__main__':
    main()
