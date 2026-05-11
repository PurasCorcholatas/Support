import asyncio
import sys
import uvicorn

# PARCHE DE COMPATIBILIDAD PARA WINDOWS
if sys.platform == "win32":
    # Establecemos la política de Selector (necesaria para psycopg3)
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    # Creamos un nuevo loop BAJO LA POLÍTICA que acabamos de setear
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    print("[WINDOWS] Motor Selector (V2.0) activado correctamente.")

async def serve():
    from main import app
    config = uvicorn.Config(
        app=app, 
        host="0.0.0.0", 
        port=8000, 
        loop="asyncio"
    )
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    if sys.platform == "win32":
        # Usamos el loop que ya creamos arriba o nos aseguramos de obtenerlo correctamente
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        loop.run_until_complete(serve())
    else:
        uvicorn.run("main:app", host="0.0.0.0", port=8000)
