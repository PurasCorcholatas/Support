# Especificación Técnica de Base de Datos - Serviunix Soporte IA

## Introducción
Este documento detalla la arquitectura de persistencia de datos del sistema de soporte basado en IA. El sistema utiliza **PostgreSQL 18.2** como motor de base de datos relacional para la gestión de usuarios, estados de conversación y persistencia del grafo de estados.

## Arquitectura de Datos (ERD)

```mermaid
erDiagram
    USERS ||--o{ CONVERSATION : has
    USERS {
        int id PK
        bigint phone_number
        string name
        string company
        string email
        string sede
        string greeting_step
        boolean bot_active
    }
    CONVERSATION ||--o{ MESSAGES : contains}
    CONVERSATION ||--o{ TICKETS : results_in}
    CONVERSATION {
        int id PK
        int users FK
        status state
        timestamp created_at
    }
    MESSAGES {
        int id PK
        int conversation_id FK
        string sender
        text message_text
        timestamp created_at
    }
    TICKETS {
        int id PK
        int conversation_id FK
        int zammad_ticket_id
        string subject
        string status
        text description
    }
    NOTIFIED_TICKETS {
        int id PK
        string ticket_id
        timestamp notified_at
    }
    BOT_MESSAGES {
        int id PK
        string content_preview
        timestamp created_at
    }
```

## Diccionario de Datos

### Gestión de Usuarios y Sesiones
- **`users`**: Entidad principal que almacena el perfil del cliente. El campo `bot_active` controla la lógica de interrupción durante la intervención humana. El campo `greeting_step` persiste el progreso del onboarding.
- **`conversation`**: Agrupador lógico de mensajes. Utiliza el tipo ENUM `status` ('open', 'closed').
- **`messages`**: Histórico crudo de intercambios entre el usuario y la IA.

### Integración de Soporte (Zammad)
- **`tickets`**: Referencia cruzada entre las conversaciones del bot y los IDs internos de Zammad.
- **`notified_tickets`**: Registro de control para el servicio de polling, garantizando que cada notificación de cierre se envíe una sola vez.

### Persistencia del Motor de IA (LangGraph)
Las siguientes tablas son gestionadas automáticamente por el `PostgresSaver` de LangGraph para permitir la recuperación de estados y el manejo de hilos (threads):
- **`checkpoints`**: Almacena el estado serializado del grafo en puntos específicos.
- **`checkpoint_blobs`**: Datos binarios de los estados.
- **`checkpoint_writes`**: Registro de escrituras intermedias durante la ejecución de nodos.

## Procedimientos de Operación y Mantenimiento

### Copia de Seguridad (Backup)
Para realizar un respaldo completo de la base de datos en producción:
```bash
pg_dump -U postgres -h localhost -d support_ia > backup_support_ia_$(date +%Y%m%d).sql
```

### Optimización y Limpieza
Se recomienda ejecutar tareas de limpieza periódicas en la tabla `bot_messages` para mantener el rendimiento del sistema de detección de bucles:
- El servicio `cleaner_bot_messages_loop` se encarga de purgar registros de más de 24 horas automáticamente.

### Consideraciones Técnicas
- **Tipos de Datos**: El campo `phone_number` en `users` es `bigint` para optimizar el indexamiento de números internacionales.
- **Integridad**: Se implementan Check Constraints en el campo `email` para validar el formato estándar de correo electrónico a nivel de motor.

---
**Documentación de Infraestructura - Serviunix**
