# ticket-monitoring · seguimiento de tarifas aéreas

[简体中文](README.md) · [English](README_EN.md) · Español · [日本語](README.ja.md)

Este fork de [dengmeiluan/ticket-monitoring](https://github.com/dengmeiluan/ticket-monitoring) permite seguir precios de vuelos nacionales e internacionales desde tu equipo. Configura rutas y fechas, consulta el calendario y las tendencias, y recibe avisos por DingTalk al alcanzar tu precio objetivo. La [demo del proyecto original](https://dengmeiluan.github.io/ticket-monitoring/site/) utiliza datos de ejemplo. Para instalar sin Python, consulta los [paquetes del proyecto original](https://github.com/dengmeiluan/ticket-monitoring/releases).

## Instalación desde este fork

Necesitas Python 3.11 o posterior. Los scripts preparan un entorno virtual e instalan las dependencias y Chromium para Playwright.

**Windows (PowerShell)**

```powershell
git clone https://github.com/Hokkaidoooo/ticket-monitoring.git
cd ticket-monitoring
powershell -ExecutionPolicy Bypass -File .\setup.ps1
.\.venv\Scripts\python.exe -m core.setup_check
.\.venv\Scripts\python.exe main.py
```

**macOS o Linux**

```bash
git clone https://github.com/Hokkaidoooo/ticket-monitoring.git
cd ticket-monitoring
bash setup.sh
source .venv/bin/activate
python -m core.setup_check
python main.py
```

## Configura el primer seguimiento

1. Abre `http://127.0.0.1:8765` o la dirección que muestre la terminal. En la configuración (`配置`), añade una ruta con códigos IATA, fechas de salida y un precio objetivo; habilítala y guarda los cambios.
2. Si quieres avisos, configura tu webhook y, cuando corresponda, el secreto de firma de DingTalk. Usa el botón de prueba (`测试推送`) antes de esperar un aviso real. Consulta la [guía de DingTalk (en inglés)](README_EN.md#dingtalk-robot-setup).
3. Revisa las cotizaciones y su historial en el panel. Confirma el precio y la disponibilidad con el proveedor antes de comprar; algunos canales requieren que inicies sesión.

Los canales web funcionan en Windows, macOS y Linux; los canales opcionales de apps de Android requieren Windows y MuMu. No expongas el panel a Internet: escucha en la interfaz local por defecto y no tiene autenticación propia. Las notificaciones se envían a los servicios que configures; consulta la [política de seguridad (en chino)](SECURITY.md) antes de usar un alojamiento de imágenes para informes. Respeta las condiciones de cada proveedor.

[Licencia Apache 2.0](LICENSE) · [Créditos](CONTRIBUTORS.md)
