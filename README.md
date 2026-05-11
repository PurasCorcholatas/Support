# Sistema de Soporte Técnico Basado en Inteligencia Artificial - Serviunix

## Descripción General
Este repositorio contiene el núcleo lógico del sistema de soporte automatizado de Serviunix. La plataforma integra capacidades de procesamiento de lenguaje natural para la gestión autónoma del ciclo de vida de tickets de soporte, desde la identificación inicial del usuario hasta la resolución técnica o escalación a personal humano.

## Arquitectura Técnica
El sistema se fundamenta en una arquitectura de microservicios y flujos de estados dirigidos, utilizando las siguientes tecnologías:

- **LangGraph**: Orquestador de la lógica conversacional mediante grafos de estados cíclicos y no cíclicos.
- **FastAPI**: Interfaz de aplicación asíncrona para la gestión de webhooks y servicios REST.
- **Zammad API**: Integración con el sistema de Helpdesk para la gestión de incidencias.
- **Chatwoot API**: Interfaz de comunicación omnicanal para el despliegue en WhatsApp.
- **SQLAlchemy**: Capa de abstracción de base de datos para la persistencia en PostgreSQL.
- **LLM Engine**: Integración con modelos GPT-4 y Claude 3.5 para diagnóstico y generación de respuestas.

## Componentes Funcionales

### Gestión de Identidad y Registro
El sistema implementa un flujo de registro obligatorio que valida la identidad del usuario (Nombre, Compañía, Sede) antes de permitir interacciones técnicas. El estado de la conversación se preserva íntegramente durante este proceso para evitar la pérdida de contexto del problema reportado originalmente.

### Diagnóstico Técnico Automatizado
Mediante la implementación de cadenas de pensamiento (Chain of Thought), el agente realiza un diagnóstico exhaustivo solicitando parámetros técnicos, versiones de software y capturas de pantalla, las cuales son procesadas y adjuntadas automáticamente al ticket correspondiente.

### Sincronización Proactiva con Zammad
El servicio incluye un sistema de monitoreo (polling) que detecta cambios de estado en Zammad. Al cerrarse un ticket, el sistema extrae la nota técnica de resolución, la procesa mediante IA para asegurar claridad, y notifica proactivamente al usuario a través del canal de comunicación activo.

### Protocolo de Escalación
Ante la detección de una solicitud explícita de intervención humana o la incapacidad de resolver el incidente mediante flujos automatizados, el sistema ejecuta un protocolo de escalación que notifica al equipo de soporte vía correo electrónico y transfiere el control de la sesión en Chatwoot.

## Especificaciones de Instalación

### Requisitos del Sistema
- Python 3.11 o superior.
- Instancia de PostgreSQL configurada.
- Credenciales de API para los servicios integrados.

### Procedimiento de Despliegue
1. Configuración del entorno virtual:
   ```bash
   python -m venv venv
   source venv/bin/activate  # En Linux/macOS
   .\venv\Scripts\activate   # En Windows
   ```
2. Instalación de dependencias:
   ```bash
   pip install -r requirements.txt
   ```
3. Configuración de variables de entorno:
   Definir parámetros en el archivo `.env` siguiendo el esquema de configuración técnica del sistema.

## Estructura de Directorios
- `/graph`: Lógica de nodos y definición del flujo de estados.
- `/services`: Servicios de integración externa y procesos en segundo plano.
- `/models`: Esquemas de base de datos y modelos relacionales.
- `/router`: Controladores de rutas y gestión de webhooks de entrada.
- `/config`: Parámetros de configuración del sistema y logging.

---
**Documentación técnica oficial - Serviunix Soporte IA**
