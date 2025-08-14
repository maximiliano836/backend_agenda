import openai
import json
from datetime import datetime, timedelta, timezone
import pytz
from sqlalchemy.orm import Session
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from api.app.models import Tenant, Servicio, Empleado, Reserva, BlockedNumber
from api.utils.generador_fake_id import generar_fake_id
from google.oauth2 import service_account
from googleapiclient.discovery import build
import redis
import httpx
import re

class AIConversationManager:
    
    # Eliminar lógica de búsqueda, reserva y cancelación de turnos. Solo delega a calendar_utils.
    async def _execute_ai_function(self, function_call, telefono, business_context, tenant, db):
        """Delegar funciones de turnos a calendar_utils"""
        from api.utils import calendar_utils
        name = function_call["name"]
        args = function_call["args"]

        if name == "buscar_horarios_servicio":
            # Buscar horarios disponibles usando calendar_utils
            servicio_id = args["servicio_id"]
            preferencia_fecha = args.get("preferencia_fecha", "cualquiera")

            servicio = db.query(Servicio).filter(Servicio.id == servicio_id).first()
            if not servicio and not business_context.get("modo_directo"):
                return "❌ Servicio no encontrado."

            # Si el usuario especificó un día específico, ajustar límites
            max_turnos = 50 if preferencia_fecha != "cualquiera" else 10
            max_days = 7
            # Si es un día de la semana, aumentar para asegurar llegar a ese día (ej. sábado)
            if preferencia_fecha in ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]:
                max_turnos = 50
            if preferencia_fecha and "/" in preferencia_fecha:
                try:
                    dia_str, mes_str = preferencia_fecha.split("/")
                    dia_num = int(dia_str)
                    mes_num = int(mes_str)
                    now = datetime.now(self.tz)
                    año_actual = now.year
                    año_objetivo = año_actual + 1 if (mes_num < now.month or (mes_num == now.month and dia_num < now.day)) else año_actual
                    fecha_objetivo = datetime(año_objetivo, mes_num, dia_num).date()
                    dias_hasta_fecha = (fecha_objetivo - now.date()).days
                    if dias_hasta_fecha > 7:
                        max_days = min(dias_hasta_fecha + 1, 30)
                        max_turnos = 50
                except Exception:
                    pass

            if servicio and servicio.calendar_id:
                slots = calendar_utils.get_available_slots_for_service(
                    servicio,
                    intervalo_entre_turnos=getattr(tenant, "intervalo_entre_turnos", 15),
                    max_days=max_days,
                    max_turnos=max_turnos,
                    credentials_json=self.google_credentials
                )
            else:
                # Fallback directo por Tenant
                slots = calendar_utils.get_available_slots(
                    calendar_id=business_context.get("calendar_id_directo") or business_context.get("calendar_id_general"),
                    credentials_json=self.google_credentials,
                    working_hours_json=business_context.get("working_hours_directo") or business_context.get("working_hours_general"),
                    service_duration=business_context.get("duracion_turno_directo") or 60,
                    intervalo_entre_turnos=getattr(tenant, "intervalo_entre_turnos", 15),
                    max_days=max_days,
                    max_turnos=max_turnos,
                    cantidad=1,
                    solo_horas_exactas=bool(business_context.get("solo_horas_exactas_directo"))
                )

            # Filtrar slots por día específico
            if preferencia_fecha and preferencia_fecha != "cualquiera":
                hoy = datetime.now(self.tz).date()
                fecha_objetivo = None
                if preferencia_fecha == "hoy":
                    fecha_objetivo = hoy
                elif preferencia_fecha == "mañana":
                    fecha_objetivo = hoy + timedelta(days=1)
                elif "/" in preferencia_fecha:
                    try:
                        dia_str, mes_str = preferencia_fecha.split("/")
                        dia_num = int(dia_str)
                        mes_num = int(mes_str)
                        now = datetime.now(self.tz)
                        año_actual = now.year
                        año_objetivo = año_actual + 1 if (mes_num < now.month or (mes_num == now.month and dia_num < now.day)) else año_actual
                        fecha_objetivo = datetime(año_objetivo, mes_num, dia_num).date()
                    except Exception:
                        fecha_objetivo = None
                elif preferencia_fecha in ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]:
                    dias_semana = {"lunes": 0, "martes": 1, "miércoles": 2, "jueves": 3, "viernes": 4, "sábado": 5, "domingo": 6}
                    dia_objetivo = dias_semana[preferencia_fecha]
                    dias_hasta_objetivo = (dia_objetivo - hoy.weekday()) % 7
                    dias_hasta_objetivo = 7 if dias_hasta_objetivo == 0 else dias_hasta_objetivo
                    fecha_objetivo = hoy + timedelta(days=dias_hasta_objetivo)
                if fecha_objetivo:
                    slots = [slot for slot in slots if slot.date() == fecha_objetivo]

            if not slots:
                dia_texto = preferencia_fecha if preferencia_fecha != "cualquiera" else "esta semana"
                nombre_item = servicio.nombre if servicio else (business_context.get("titulo_turno_directo") or "Turno")
                return f"😔 No hay horarios disponibles para *{nombre_item}* {dia_texto}.\n¿Quieres elegir otro día?"

            # Determinar cuántos slots mostrar (más si pidieron un día específico)
            max_to_show = 20 if preferencia_fecha != "cualquiera" else 10
            to_show = slots[:max_to_show]

            # Guardar selección y slots en Redis (mostrar más de 8 slots)
            try:
                srv_id = servicio.id if servicio else 0
                srv_nombre = servicio.nombre if servicio else (business_context.get("titulo_turno_directo") or "Turno")
                self.redis_client.set(f"servicio_seleccionado:{telefono}", json.dumps({"id": srv_id, "nombre": srv_nombre}), ex=1800)
                slots_key = f"slots:{telefono}:{srv_id}"
                slots_data = [{"numero": i, "fecha_hora": slot.isoformat(), "empleado_id": None, "empleado_nombre": "Sistema"} for i, slot in enumerate(to_show, 1)]
                self.redis_client.set(slots_key, json.dumps(slots_data), ex=1800)
                # 🆕 Paginación: guardar todos los slots y controles de página
                all_key = f"slots_all:{telefono}:{srv_id}"
                page_key = f"slots_page:{telefono}:{srv_id}"
                size_key = f"slots_page_size:{telefono}:{srv_id}"
                self.redis_client.set(all_key, json.dumps([s.isoformat() for s in slots]), ex=1800)
                self.redis_client.set(page_key, json.dumps(0), ex=1800)
                self.redis_client.set(size_key, json.dumps(max_to_show), ex=1800)
            except Exception as e:
                print(f"❌ Error guardando slots en Redis: {e}")

            nombre_item = servicio.nombre if servicio else (business_context.get("titulo_turno_directo") or "Turno")
            emoji_srv = self._emoji_for_service(nombre_item)
            respuesta = f"{emoji_srv} Horarios disponibles para *{nombre_item}*"
            if preferencia_fecha and preferencia_fecha != "cualquiera":
                respuesta += f" el {preferencia_fecha}"
            respuesta += ":\n\n"
            for i, slot in enumerate(to_show, 1):
                dia_nombre = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"][slot.weekday()]
                respuesta += f"{i}. {dia_nombre.title()} {slot.strftime('%d/%m %H:%M')}\n"
            # Sugerir 'más' si hay más páginas
            if len(slots) > len(to_show):
                respuesta += "\n📝 Escribe 'más' para ver más horarios."
            respuesta += "\n💬 Escribe el número que prefieres para confirmar."
            return respuesta

        elif name == "crear_reserva":
            servicio_id = args["servicio_id"]
            servicio = db.query(Servicio).filter(Servicio.id == servicio_id).first()
            if not servicio and not business_context.get("modo_directo"):
                return "❌ Servicio no encontrado."
            fecha_hora = args["fecha_hora"]
            nombre_cliente = args["nombre_cliente"]
            slot_dt = datetime.fromisoformat(fecha_hora)
            try:
                if servicio and servicio.calendar_id:
                    event_id = calendar_utils.create_event_for_service(servicio, slot_dt, telefono, self.google_credentials, nombre_cliente)
                    empleado_nombre = "Sistema"
                    empleado_calendar_id = servicio.calendar_id
                    servicio_nombre = servicio.nombre
                else:
                    event_id = calendar_utils.create_event_for_tenant_directo(
                        calendar_id=business_context.get("calendar_id_directo") or business_context.get("calendar_id_general"),
                        duracion_minutos=business_context.get("duracion_turno_directo") or 60,
                        fecha_hora=slot_dt,
                        telefono=telefono,
                        credentials_json=self.google_credentials,
                        nombre_cliente=nombre_cliente,
                        titulo_evento=business_context.get("titulo_turno_directo") or "Turno"
                    )
                    empleado_nombre = "Sistema"
                    empleado_calendar_id = business_context.get("calendar_id_directo") or business_context.get("calendar_id_general")
                    servicio_nombre = business_context.get("titulo_turno_directo") or "Turno"

                nueva_reserva = Reserva(
                    fake_id=generar_fake_id(),
                    event_id=event_id,
                    empresa=tenant.comercio,
                    empleado_id=None,
                    empleado_nombre=empleado_nombre,
                    empleado_calendar_id=empleado_calendar_id,
                    cliente_nombre=nombre_cliente,
                    cliente_telefono=telefono,
                    fecha_reserva=slot_dt,
                    servicio=servicio_nombre,
                    estado="activo",
                    cantidad=args.get("cantidad", 1)
                )
                db.add(nueva_reserva)
                db.commit()
                return f"✅ Reserva confirmada para {servicio_nombre} el {slot_dt.strftime('%d/%m %H:%M')} a nombre de {nombre_cliente}.\n🔖 Código: {nueva_reserva.fake_id}"
            except Exception as e:
                return f"❌ Error al crear la reserva: {e}"

        elif name == "cancelar_reserva":
            codigo_reserva = args["codigo_reserva"]
            calendar_id = business_context.get("calendar_id_directo") or business_context.get("calendar_id_general") or "primary"
            try:
                ok = calendar_utils.cancelar_evento_google(calendar_id, codigo_reserva, self.google_credentials)
                return "✅ Reserva cancelada correctamente." if ok else "❌ No se pudo cancelar la reserva."
            except Exception as e:
                return f"❌ Error al cancelar la reserva: {e}"

        return "Función no implementada."

    def _generar_respuesta_fallback(self, mensaje, user_history, business_context):
        """Respuesta fallback si falla la IA"""
        return self._add_help_footer("Disculpa, tuve un problema procesando tu mensaje. ¿Podrías intentar de nuevo?")
    def __init__(self, api_key, redis_client):
        self.client = openai.OpenAI(api_key=api_key)
        self.redis_client = redis_client
        self.tz = pytz.timezone("America/Montevideo")
        self.webconnect_url = os.getenv("webconnect_url", "http://195.26.250.62:3000")  
        self.google_credentials = os.getenv("GOOGLE_CREDENTIALS_JSON")

    def _get_time_increment(self, tenant):
        """
        Devuelve el incremento de minutos entre turnos según la configuración del Tenant.
        """
        intervalo = getattr(tenant, 'intervalo_entre_turnos', None)
        if intervalo:
            return int(intervalo)
        return 15

    def _traducir_dia(self, dia_en):
        """Traduce el nombre de un día de la semana de inglés a español."""
        dias = {
            'monday': 'lunes',
            'tuesday': 'martes',
            'wednesday': 'miércoles',
            'thursday': 'jueves',
            'friday': 'viernes',
            'saturday': 'sábado',
            'sunday': 'domingo',
            'lunes': 'lunes',
            'martes': 'martes',
            'miércoles': 'miércoles',
            'miercoles': 'miércoles',
            'jueves': 'jueves',
            'viernes': 'viernes',
            'sábado': 'sábado',
            'sabado': 'sábado',
            'domingo': 'domingo'
        }
        return dias.get(dia_en.lower(), dia_en)
    
    def _normalize_datetime(self, dt):
        """Normaliza un datetime para que siempre tenga timezone y se convierta a self.tz."""
        if dt is None:
            return None
        # Si no tiene tz, asumir UTC y convertir a la zona horaria configurada
        if getattr(dt, 'tzinfo', None) is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(self.tz)
    
    def _emoji_for_service(self, nombre_servicio: str) -> str:
        """Devolver un emoji acorde al nombre del servicio (fallback: ✨)."""
        if not nombre_servicio:
            return "✨"
        n = nombre_servicio.lower()
        mapping = [
            ("tre", "🧘"),
            ("somatica", "🧘"),
            ("somática", "🧘"),
            ("masaje", "💆"),
            ("terapia", "🧠"),
            ("grupal", "👥"),
            ("equipos", "🧑‍🤝‍🧑"),
            ("taller", "🧑‍🏫"),
            ("charla", "🗣️"),
            ("jornada", "🧑‍🏫"),
            ("uno a uno", "👤"),
        ]
        for kw, emoji in mapping:
            if kw in n:
                return emoji
        return "✨"
    
    def _wants_more_info(self, mensaje: str) -> bool:
        """Detecta si el usuario pide explícitamente más detalles."""
        msg = (mensaje or "").lower()
        triggers = [
            "más info", "mas info", "ver más", "ver mas", "detalles",
            "info completa", "biografia completa", "biografía completa", "ver todo", "ampliar"
        ]
        return any(t in msg for t in triggers)

    def _first_paragraphs(self, text: str, max_paragraphs: int = 1, max_chars: int = 320) -> str:
        """Resumen inteligente: toma 1-2 frases iniciales y limita por caracteres.
        Fallback: primeros párrafos con límite de caracteres.
        """
        if not text:
            return ""
        raw = text.strip()
        # 1) Intentar por frases (máx 2) y cortar por caracteres
        try:
            import re
            sentences = [s.strip() for s in re.split(r"(?<=[\.!?])\s+", raw) if s.strip()]
            if sentences:
                resumen = " ".join(sentences[:2])
                if len(resumen) > max_chars:
                    return resumen[:max_chars - 1].rstrip() + "…"
                return resumen
        except Exception:
            pass
        # 2) Fallback: primeros párrafos
        paras = [p.strip() for p in raw.split("\n\n") if p.strip()]
        summary_parts = []
        total = 0
        for p in paras:
            if len(summary_parts) >= max_paragraphs:
                break
            if total + len(p) + 2 > max_chars and summary_parts:
                break
            summary_parts.append(p)
            total += len(p) + 2
        summary = "\n\n".join(summary_parts)
        if not summary:
            summary = raw[:max_chars - 1].rstrip() + ("…" if len(raw) > max_chars else "")
        return summary

    def _format_business_info(self, tenant, business_context, full: bool = False) -> str:
        """Construye un texto informativo; por defecto resumido, o completo si full=True."""
        nombre_publico = tenant.comercio or (f"{getattr(tenant, 'nombre', '')} {getattr(tenant, 'apellido', '')}".strip() or "Nuestro espacio")
        info_local = (business_context.get("informacion_local") or "").strip()
        telefono = business_context.get("telefono") or ""
        direccion = business_context.get("direccion") or ""

        partes = []
        partes.append(f"✨ Sobre {nombre_publico}")
        if info_local:
            texto = info_local if full else self._first_paragraphs(info_local, max_paragraphs=1, max_chars=320)
            partes.append(texto)
            if not full:
                partes.append("ℹ️ Si querés, te envío más detalles. Decime 'más info' o 'biografía completa'.")
        if telefono or direccion:
            contacto = []
            if direccion:
                contacto.append(f"📍 Dirección: {direccion}")
            if telefono:
                contacto.append(f"📞 Teléfono: {telefono}")
            partes.append("\n".join(contacto))
        partes.append("\n💬 ¿Te gustaría reservar o ver los servicios disponibles? Escribe 'servicios' o pedime un día (hoy/mañana).")
        return "\n\n".join([p for p in partes if p])

    def _respuesta_info_servicio(self, servicio: dict, business_context: dict, full: bool = False, add_footer: bool = True) -> str:
        """Formatea una respuesta informativa de un servicio, breve o completa según 'full'.
        Si add_footer=False, devuelve solo la tarjeta sin el pie de ayuda.
        """
        nombre = servicio.get("nombre", "Servicio")
        desc = (servicio.get("mensaje_personalizado") or "").strip()
        if not desc:
            # Intentar enriquecer desde informacion_local si existe
            desc = self._extract_service_info_from_tenant_info(nombre, business_context.get("informacion_local") or "") or ""

        # Breve siempre primero; completo sólo si piden más info
        cuerpo = desc if (full and desc) else self._first_paragraphs(desc, max_paragraphs=2, max_chars=420)

        bullets = []
        dur = servicio.get("duracion")
        if dur:
            bullets.append(f"⏱️ Duración: {dur} min")
        precio = servicio.get("precio")
        if isinstance(precio, (int, float)) and precio > 0:
            bullets.append(f"💲 Precio: {precio}")

        partes = [f"✨ *{nombre}*"]
        if cuerpo:
            partes.append(cuerpo)
        if bullets:
            partes.append(" · ".join(bullets))

        # Cerrar con CTA útil
        if full:
            partes.append("\n📅 ¿Queres ver horarios disponibles para este servicio?")
        else:
            partes.append(f"\n👉 Si querés más detalles, decime “más info de {nombre}”, o pedime horarios con “ver turnos de {nombre}”.")

        cuerpo_msg = "\n".join(p for p in partes if p).strip()
        return self._add_help_footer(cuerpo_msg) if add_footer else cuerpo_msg

    def _preguntar_dia_disponible(self, servicio_seleccionado, telefono):
        """Pregunta al usuario por el día que desea para el servicio seleccionado e incluye detalles del servicio si existen."""
        tipo_servicio = self._emoji_for_service(servicio_seleccionado['nombre'])
        respuesta = f"{tipo_servicio} *{servicio_seleccionado['nombre']}*\n"
        # Agregar detalles ricos del servicio si están disponibles
        desc = (servicio_seleccionado.get('mensaje_personalizado') or '').strip()
    # ⚠️ Política: No mencionar precio en la respuesta
        duracion = servicio_seleccionado.get('duracion')
        extras = []
        if desc:
            # No truncamos agresivo para mantenerlo nutritivo; WhatsApp soporta mensajes extensos
            extras.append(desc)
        info_basica = []
        if duracion:
            info_basica.append(f"⏱️ Duración: {duracion} min")
    # ⚠️ Política: No mencionar precio en la respuesta
        if info_basica:
            extras.append(" · ".join(info_basica))
        if extras:
            respuesta += "\n" + "\n".join(extras) + "\n"
        respuesta += "\n📅 ¿Para qué día te gustaría reservar?\n"
        respuesta += "Puedes responder con 'hoy', 'mañana', o el nombre de un día (ejemplo: 'viernes').\n"
        respuesta += "\n💬 Escribe el día que prefieres."
        return self._add_help_footer(respuesta)
    
    def _get_conversation_history(self, telefono: str) -> list:
        """Obtener historial de conversación desde Redis"""
        try:
            history_key = f"conversation:{telefono}"
            messages = self.redis_client.lrange(history_key, 0, -1)
            return [json.loads(msg) for msg in messages]
        except:
            return []
    
    def _save_conversation_message(self, telefono: str, role: str, content: str):
        """Guardar mensaje en historial de conversación"""
        try:
            history_key = f"conversation:{telefono}"
            message = {
                "role": role,
                "content": content,
                "timestamp": datetime.now().isoformat()
            }
            self.redis_client.lpush(history_key, json.dumps(message))
            self.redis_client.expire(history_key, 3600 * 24)  # 24 horas
        except Exception as e:
            print(f"Error guardando conversación: {e}")
    
    def _is_blocked_number(self, telefono: str, cliente_id: int, db: Session) -> bool:
        """Verificar si el número está bloqueado"""
        try:
            blocked = db.query(BlockedNumber).filter(
                BlockedNumber.telefono == telefono,
                BlockedNumber.cliente_id == cliente_id
            ).first()
            return blocked is not None
        except:
            return False
    
    def _is_human_mode(self, telefono: str) -> bool:
        """Verificar si está en modo humano"""
        try:
            human_mode_key = f"human_mode:{telefono}"
            return self.redis_client.get(human_mode_key) == "true"
        except:
            return False
    
    def _activate_human_mode(self, telefono: str) -> bool:
        """Activar modo humano para un número"""
        try:
            human_mode_key = f"human_mode:{telefono}"
            self.redis_client.set(human_mode_key, "true", ex=3600)  # 1 hora
            return True
        except:
            return False
    
    def _deactivate_human_mode(self, telefono: str) -> bool:
        """Desactivar modo humano (restaurar bot)"""
        try:
            human_mode_key = f"human_mode:{telefono}"
            self.redis_client.delete(human_mode_key)
            return True
        except:
            return False
    
    def _add_help_footer(self, mensaje: str) -> str:
        """Agregar pie de mensaje con opción de ayuda personalizada"""
        footer = "\n\n💬 _¿Necesitas ayuda personalizada? Escribe 'ayuda persona' para hablar con nuestro equipo._"
        return mensaje + footer
    
    async def _notify_human_support(self, cliente_id: int, telefono: str, mensaje: str):
        """Notificar a soporte humano"""
        try:
            # Aquí podrías implementar notificación por email, Slack, etc.
            print(f"🚨 MODO HUMANO - Cliente {cliente_id} ({telefono}): {mensaje}")
        except Exception as e:
            print(f"Error notificando soporte humano: {e}")
    
    def _get_user_history(self, telefono: str, db: Session) -> dict:
        """Obtener historial completo del usuario"""
        now_aware = datetime.now(self.tz)
        
        # 🔒 SEGURIDAD: Solo reservas del teléfono específico
        # 📅 FILTRADO: Solo reservas futuras (no pasadas)
        reservas_activas = db.query(Reserva).filter(
            Reserva.cliente_telefono == telefono,  # 🔒 Filtro de seguridad por teléfono
            Reserva.estado == "activo",
            Reserva.fecha_reserva > now_aware  # 📅 Solo futuras
        ).order_by(Reserva.fecha_reserva.asc()).all()
        
        reservas_pasadas = db.query(Reserva).filter(
            Reserva.cliente_telefono == telefono,  # 🔒 Filtro de seguridad por teléfono
            Reserva.estado.in_(["completado", "cancelado"])
        ).order_by(Reserva.fecha_reserva.desc()).limit(5).all()
        
        return {
            "reservas_activas": [
                {
                    "codigo": r.fake_id,
                    "servicio": r.servicio,
                    "empleado": r.empleado_nombre,
                    "fecha": r.fecha_reserva.strftime("%d/%m %H:%M") if r.fecha_reserva else "",
                    "puede_cancelar": self._puede_cancelar_reserva(r.fecha_reserva, now_aware)
                }
                for r in reservas_activas
            ],
            "historial": [
                {
                    "servicio": r.servicio,
                    "fecha": r.fecha_reserva.strftime("%d/%m/%Y") if r.fecha_reserva else "",
                    "estado": r.estado
                }
                for r in reservas_pasadas
            ],
            "es_cliente_recurrente": len(reservas_pasadas) > 0,
            "servicio_favorito": self._get_servicio_favorito(reservas_pasadas)
        }
    
    def _get_servicio_favorito(self, reservas_pasadas):
        """Determinar servicio más utilizado"""
        if not reservas_pasadas:
            return None
        
        servicios = {}
        for reserva in reservas_pasadas:
            servicios[reserva.servicio] = servicios.get(reserva.servicio, 0) + 1
        
        return max(servicios, key=servicios.get) if servicios else None
    
    def _puede_cancelar_reserva(self, fecha_reserva, now_aware):
        """Verificar si se puede cancelar una reserva"""
        if not fecha_reserva:
            return False
        
        fecha_reserva_aware = self._normalize_datetime(fecha_reserva)
        return fecha_reserva_aware > now_aware + timedelta(hours=1)
    

    async def process_message(self, telefono: str, mensaje: str, cliente_id: int, db: Session):
        """Procesar mensaje con IA más natural y contextual"""
        try:
            # --- 1. OBTENER TENANT Y CONTEXTO INICIAL ---
            tenant = db.query(Tenant).filter(Tenant.id == cliente_id).first()
            if not tenant:
                return self._add_help_footer("❌ No encontré información del negocio.")

            mensaje_stripped = mensaje.strip().lower()
            saludos = ["hola", "buenas", "buenos días", "buenas tardes", "buenas noches", "hey", "holi", "holaa", "saludos"]

            # --- 2. FLUJO DE BIENVENIDA PERSONALIZADA (SI ES UN SALUDO) ---
            if any(mensaje_stripped.startswith(s) for s in saludos):
                # Limpiar estado previo de cualquier conversación anterior
                self.redis_client.delete(f"servicio_seleccionado:{telefono}")
                for key in self.redis_client.scan_iter(f"slots:{telefono}:*"):
                    self.redis_client.delete(key)
                self.redis_client.delete(f"slot_seleccionado:{telefono}")

                # Verificar si hay un mensaje de bienvenida personalizado
                if tenant.mensaje_bienvenida_personalizado:
                    # Guardar el mensaje del usuario y la respuesta automática en el historial
                    # para que la IA tenga contexto de la respuesta del usuario.
                    self._save_conversation_message(telefono, "user", mensaje)
                    self._save_conversation_message(telefono, "assistant", tenant.mensaje_bienvenida_personalizado)
                    
                    # Devolver el mensaje personalizado con el pie de página de ayuda
                    return self._add_help_footer(tenant.mensaje_bienvenida_personalizado)

            # Verificar si está bloqueado
            if self._is_blocked_number(telefono, cliente_id, db):
                return "❌ Este número está bloqueado."
            # Verificar modo humano
            if self._is_human_mode(telefono):
                # Comando para SALIR del modo humano (restaurar bot)
                if mensaje_stripped in ['bot', 'chatbot', 'automatico', 'volver bot', 'salir']:
                    if self._deactivate_human_mode(telefono):
                        return self._add_help_footer("🤖 ¡Hola de nuevo! Volví para ayudarte con tus reservas.\n\n¿En qué puedo ayudarte?")
                # Si está en modo humano, solo notificar internamente y NO responder
                await self._notify_human_support(cliente_id, telefono, mensaje)
                return ""  # Respuesta vacía - el bot no responde nada
            
            # Comando para ACTIVAR modo humano  
            if any(keyword in mensaje_stripped for keyword in ['ayuda persona', 'persona real', 'hablar con persona', 'soporte humano', 'operador', 'atencion personalizada']):
                if self._activate_human_mode(telefono):
                    return "👥 Te conecté con nuestro equipo humano. A partir de ahora no recibirás respuestas automáticas hasta que escribas 'bot' para volver al chatbot.\n\n💡 Para restaurar el bot automático, escribe 'bot'"
            
            # Obtener historial del usuario y contexto del negocio
            user_history = self._get_user_history(telefono, db)
            business_context = self._get_business_context(tenant, db)
            conversation_history = self._get_conversation_history(telefono)
            
            # Guardar mensaje del usuario (si no se guardó en el flujo de bienvenida)
            self._save_conversation_message(telefono, "user", mensaje)

            # --- FLUJO DE CANCELACIÓN ---
            if "cancelar" in mensaje_stripped or "anular" in mensaje_stripped:
                codigo_match = re.search(r'\b([A-Z0-9]{6,8})\b', mensaje.upper())  # 🔧 Limitar rango
                if codigo_match:
                    codigo_candidato = codigo_match.group(1)
                    # 🔧 VERIFICAR: Que no sea una palabra común
                    palabras_excluir = [
                        'CANCELAR', 'ANULAR', 'QUIERO', 'HACER', 'RESERVA', 'TURNO'
                    ]
                    if codigo_candidato not in palabras_excluir and re.search(r'\d', codigo_candidato):
                        return await self.cancelar_reserva(codigo_candidato, telefono, db)
                    
                # Si no hay código válido, mostrar reservas
                reservas_activas = user_history.get("reservas_activas", [])
                if not reservas_activas:
                    return self._add_help_footer("😊 No tienes reservas próximas para cancelar.")
                
                respuesta = "🔄 *Tus próximas reservas:*\n\n"
                for r in reservas_activas:
                    if r['puede_cancelar']:
                        respuesta += f"✅ Código: `{r['codigo']}` | {r['servicio']} el {r['fecha']}\n"
                    else:
                        respuesta += f"❌ Código: `{r['codigo']}` | {r['servicio']} el {r['fecha']} _(muy próxima)_\n"
                respuesta += "\n💬 Escribe el código de la reserva que deseas cancelar."
                respuesta += "\n\n_Solo puedes cancelar reservas con más de 1 hora de anticipación._"
                return self._add_help_footer(respuesta)

            # --- DETECTAR CÓDIGOS DE RESERVA (sin palabra "cancelar") ---
            # 🔧 MEJORAR: Solo detectar códigos reales, no palabras largas
            codigo_solo = re.search(r'\b([A-Z0-9]{6,8})\b', mensaje.upper())  # Limitar a 6-8 caracteres
            if codigo_solo:
                codigo_candidato = codigo_solo.group(1)
                # 🔧 VERIFICAR: Que no sea una palabra común en español
                palabras_excluir = [
                    'QUIERO', 'HACER', 'RESERVA', 'TURNO', 'HORARIO', 'CANCELAR',
                    'CODIGO', 'TENGO', 'ACTIVOS', 'DISPONIBLE', 'SERVICIO',
                    'MAÑANA', 'TARDE', 'NOCHE', 'VIERNES', 'SABADO', 'DOMINGO'
                ]
                if codigo_candidato not in palabras_excluir:
                    # Verificar que tenga al menos algunos números (códigos reales tienen números)
                    if re.search(r'\d', codigo_candidato):
                        return await self.cancelar_reserva(codigo_candidato, telefono, db)

            # --- CONSULTAR RESERVAS ACTIVAS ---
            if any(phrase in mensaje_stripped for phrase in [
                'turnos activos', 'reservas activas', 'que turnos tengo', 'cuales tengo',
                'mis reservas', 'mis turnos', 'reservas pendientes'
            ]):
                reservas_activas = user_history.get("reservas_activas", [])
                if not reservas_activas:
                    return self._add_help_footer("😊 No tienes reservas próximas.")
                
                respuesta = "📅 *Tus próximas reservas:*\n\n"
                for r in reservas_activas:
                    estado_icono = "✅" if r['puede_cancelar'] else "❌"
                    respuesta += f"{estado_icono} `{r['codigo']}` | {r['servicio']} el {r['fecha']}\n"
                respuesta += "\n💬 Para cancelar, envía el código (ej: `C2HHOH`) o escribe 'cancelar + código'."
                return self._add_help_footer(respuesta)

            # --- 🔒 SEGURIDAD: Detectar consultas sobre otros números de teléfono ---
            numero_pattern = r'\b(?:09[0-9]{8}|59[0-9]{8})\b'  # Patrones de números uruguayos
            numeros_encontrados = re.findall(numero_pattern, mensaje)
            if numeros_encontrados:
                for numero in numeros_encontrados:
                    if numero != telefono.replace('+', ''):  # Verificar que no sea el propio número
                        return self._add_help_footer(f"🔒 Por seguridad, solo puedo mostrar información de TUS reservas.\n\n💬 Si necesitas ayuda con tus propias reservas, puedo ayudarte. ¿Qué necesitas? 😊")

            # --- FLUJO DE CONSULTA DE SERVICIOS ---
            if mensaje_stripped in ["servicios", "ver servicios", "lista", "menu"]:
                return self._add_help_footer(self.mostrar_servicios(business_context))

            # --- INTENCIÓN: INFO DE UN SERVICIO (prioritario sobre info del negocio) ---
            def _norm(s: str) -> str:
                import unicodedata
                s = (s or "").lower().strip()
                return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')

            nmsg = _norm(mensaje)
            servicio_mencionado = None

            # 1) Si ya hay servicio en sesión (Redis) y el usuario pide "más info" o "detalles"
            if self._wants_more_info(mensaje_stripped):
                servicio_guardado_json = self.redis_client.get(f"servicio_seleccionado:{telefono}")
                if servicio_guardado_json:
                    guardado = json.loads(servicio_guardado_json)
                    servicio_mencionado = next((s for s in business_context["servicios"] if s["id"] == guardado["id"]), None)
                    if servicio_mencionado:
                        return self._respuesta_info_servicio(servicio_mencionado, business_context, full=True)

            # 2) Intentar detectar servicio por nombre/alias dentro del mensaje
            if not servicio_mencionado:
                for s in business_context["servicios"]:
                    nname = _norm(s["nombre"]) if s.get("nombre") else ""
                    # tolera variantes (e.g. "tre", "t.r.e")
                    aliases = {nname, nname.replace("®", "").strip(), nname.replace(" ", ""), nname.replace(".", "")}
                    if any(a and a in nmsg for a in aliases):
                        servicio_mencionado = s
                        break
                    # matching por tokens: al menos 2 tokens del servicio presentes en el mensaje
                    tokens = [t for t in re.split(r"\s+", nname) if len(t) > 2]
                    if tokens:
                        overlap = sum(1 for t in tokens if t in nmsg)
                        if overlap >= min(2, len(tokens)):
                            servicio_mencionado = s
                            break

            # 3) Si pidió "info" y además se detectó servicio -> responder info de servicio
            if servicio_mencionado and any(k in nmsg for k in ["info", "informacion", "información", "detalle", "detalles", "más", "mas"]):
                return self._respuesta_info_servicio(servicio_mencionado, business_context, full=self._wants_more_info(mensaje_stripped))

            # --- FLUJO DE INFORMACIÓN DEL NEGOCIO / BIO / CONTACTO ---
            info_keywords = [
                "quien sos", "quién sos", "quien eres", "quién eres", "quien es diego", "quién es diego",
                "sobre vos", "sobre ti", "sobre diego", "sobre el negocio", "info del local", "informacion del local",
                "información del local", "informacion", "información", "contacto", "teléfono", "telefono",
                "dirección", "direccion", "ubicación", "ubicacion", "horarios", "sobre mi", "sobre mí",
                "bio", "biografia", "biografía"
            ]
            # Si el usuario pide explícitamente más detalle, enviar biografía completa
            if self._wants_more_info(mensaje_stripped):
                return self._add_help_footer(self._format_business_info(tenant, business_context, full=True))
            if any(k in mensaje_stripped for k in info_keywords):
                full = self._wants_more_info(mensaje_stripped)
                return self._add_help_footer(self._format_business_info(tenant, business_context, full=full))

            # --- 🔧 DETECTAR CONFUSIÓN DEL USUARIO ---
            frases_confusion = [
                'no tengo', 'no se', 'no entiendo', 'que hago', 'ayuda',
                'no encuentro', 'perdido', 'confundido'
            ]
            if any(frase in mensaje_stripped for frase in frases_confusion):
                # Si acaba de preguntar por otro número o está en contexto de cancelación, aclarar
                if any(palabra in mensaje_stripped for palabra in ['codigo', 'códigos', 'reserva', 'turno']):
                    return (
                        "🤗 ¡No te preocupes! Te ayudo:\n\n"
                        "📞 Solo puedo ayudarte con TUS propias reservas\n"
                        "📋 Si quieres ver tus reservas: escribe 'mis reservas'\n"
                        "🆕 Si quieres hacer una nueva reserva: escribe 'quiero reservar'\n"
                        "❌ Si quieres cancelar: envía el código de tu reserva\n\n"
                        "💬 ¿Qué necesitas hacer? 😊\n\n💬 _¿Necesitas ayuda personalizada? Escribe 'ayuda persona' para hablar con nuestro equipo._"
                    )

            # --- FLUJO PRINCIPAL CON IA ---
            respuesta = await self._ai_process_conversation_natural(
                mensaje, telefono, conversation_history, user_history, business_context, tenant, db
            )
            self._save_conversation_message(telefono, "assistant", respuesta)
            return self._add_help_footer(respuesta)

        except Exception as e:
            print(f"❌ Error en AI manager: {e}")
            return self._generar_respuesta_fallback(mensaje, None, None)

    def _detectar_hora_mensaje(self, mensaje: str) -> str:
        """🔧 DETECTAR: Hora en diferentes formatos"""
        mensaje = mensaje.lower().strip()
        
        # Patrones de hora más flexibles
        # Formato HH:MM exacto
        time_pattern = r'\b(\d{1,2}):(\d{2})\b'
        time_match = re.search(time_pattern, mensaje)
        if time_match:
            return f"{time_match.group(1).zfill(2)}:{time_match.group(2)}"
        
        # Formato "a las X" o "las X"
        hour_pattern = r'(?:a\s+las\s+|las\s+)(\d{1,2})(?:\s*h|:00|$|\s)'
        hour_match = re.search(hour_pattern, mensaje)
        if hour_match:
            hora = int(hour_match.group(1))
            return f"{hora:02d}:00"
        
        # Formato simple "X de la mañana/tarde"
        simple_pattern = r'\b(\d{1,2})\s*(?:de\s*la\s*)?(?:mañana|tarde|noche)?\b'
        simple_match = re.search(simple_pattern, mensaje)
        if simple_match:
            hora = int(simple_match.group(1))
            # Convertir a formato 24h si es necesario
            if hora <= 12 and ('tarde' in mensaje or 'noche' in mensaje):
                if hora != 12:
                    hora += 12
            return f"{hora:02d}:00"
        
        return None

    def _detectar_cambio_horario(self, mensaje: str) -> bool:
        """🔧 DETECTAR: Si el usuario quiere cambiar de horario"""
        mensaje = mensaje.lower()
        
        # ❌ EXCLUIR frases que NO son cambios de horario
        exclusiones = [
            'mi nombre es', 'me llamo', 'soy ', 'nombre:'
        ]
        if any(exclusion in mensaje for exclusion in exclusiones):
            return False
        
        # ✅ DETECTAR palabras de cambio solo si contienen referencia horaria
        cambio_palabras = [
            'no', 'cambiar', 'otro', 'diferente', 'mejor', 'prefiero',
            'quiero', 'no me gusta', 'no me sirve', 'no puedo'
        ]
        
        # Solo es cambio si menciona horario/tiempo Y tiene palabra de cambio
        tiene_cambio = any(palabra in mensaje for palabra in cambio_palabras)
        tiene_horario = any(palabra in mensaje for palabra in ['hora', 'las ', 'a las', 'de la', 'turno', 'horario'])
        
        return tiene_cambio and tiene_horario

    def _es_nombre_valido(self, mensaje: str) -> bool:
        """🔧 VALIDAR: Si el mensaje contiene un nombre válido"""
        mensaje = mensaje.lower().strip()
        
        # Patrones de nombre válido
        patrones_nombre = [
            'mi nombre es', 'me llamo', 'soy ', 'nombre:'
        ]
        
        # Si contiene patrón de presentación, es nombre válido
        if any(patron in mensaje for patron in patrones_nombre):
            return True
        
        # Si tiene 2+ palabras Y son palabras reales (no solo una palabra)
        palabras = mensaje.split()
        if len(palabras) >= 2:
            # Verificar que no sean referencias de horario/cambio
            referencias_horario = ['hora', 'turno', 'horario', 'las ', 'a las', 'de la']
            if not any(ref in mensaje for ref in referencias_horario):
                # Verificar que cada palabra tenga al menos 2 caracteres
                if all(len(palabra) >= 2 for palabra in palabras):
                    return True
        
        # Si es una sola palabra, debe tener al menos 4 caracteres para ser nombre válido
        if len(palabras) == 1 and len(palabras[0]) >= 4:
            # Verificar que no sea una referencia técnica
            referencias_no_nombre = ['cancelar', 'reservar', 'turno', 'codigo']
            if not any(ref in mensaje for ref in referencias_no_nombre):
                return False  # 🔧 Forzar nombres de 2+ palabras por seguridad
        
        return False

    def _extraer_nombre(self, mensaje: str) -> str:
        """🔧 EXTRAER: El nombre limpio del mensaje"""
        mensaje = mensaje.strip()
        mensaje_lower = mensaje.lower()
        
        # Remover patrones de presentación
        patrones = [
            'mi nombre es ', 'me llamo ', 'soy ', 'nombre: ', 'nombre '
        ]
        
        for patron in patrones:
            if patron in mensaje_lower:
                # Encontrar la posición del patrón y extraer lo que sigue
                idx = mensaje_lower.find(patron)
                return mensaje[idx + len(patron):].strip()
        
        # Si no hay patrón, devolver el mensaje completo
        return mensaje

    def _detectar_dia_mensaje(self, mensaje: str) -> str:
        """🔧 CORREGIDO: Detectar qué día quiere el usuario"""
        mensaje_original = mensaje.lower().strip()
        
        # 🔧 DETECTAR fechas específicas en formato DD/MM
        
        fecha_pattern = r'\b(\d{1,2})/(\d{1,2})\b'
        fecha_match = re.search(fecha_pattern, mensaje_original)
        if fecha_match:
            dia = int(fecha_match.group(1))
            mes = int(fecha_match.group(2))
            # Devolver en formato que pueda ser procesado después
            return f"{dia:02d}/{mes:02d}"
        
        # 🔧 MEJOR LÓGICA: Buscar patrones específicos sin modificar el mensaje globalmente
        if any(word in mensaje_original for word in ['hoy', 'today']):
            return 'hoy'
        elif any(word in mensaje_original for word in ['mañana', 'tomorrow']):
            return 'mañana'
        elif any(word in mensaje_original for word in ['lunes', 'monday']):
            return 'lunes'
        elif any(word in mensaje_original for word in ['martes', 'tuesday']):
            return 'martes'
        elif any(word in mensaje_original for word in ['miércoles', 'miercoles', 'wednesday']):
            return 'miercoles'
        elif any(word in mensaje_original for word in ['jueves', 'thursday']):
            return 'jueves'
        elif any(word in mensaje_original for word in ['viernes', 'vienres', 'friday']):  # 🔧 CORREGIR typo común
            return 'viernes'
        elif any(word in mensaje_original for word in ['sábado', 'sabado', 'saturday']):
            return 'sabado'
        elif any(word in mensaje_original for word in ['domingo', 'sunday']):
            return 'domingo'
        
        return None

    async def _ai_process_conversation_natural(self, mensaje, telefono, conversation_history, user_history, business_context, tenant, db):
        """🔧 CORREGIDO: Procesamiento de IA más natural y contextual"""
        
        mensaje_stripped = mensaje.strip().lower()

        # -------------------
        # 0) ATAJOS DETERMINISTAS ANTES DE LA IA
        # -------------------
        # 0.1) Pedido de VIDEO: responder con link inmediatamente (extraído del contexto del negocio o servicios)
        def _extraer_primer_url(texto: str | None) -> str | None:
            if not texto:
                return None
            try:
                import re
                m = re.search(r"https?://\S+", texto)
                return m.group(0) if m else None
            except Exception:
                return None

        if any(k in mensaje_stripped for k in ["video", "enlace del video", "link del video", "compartime el video", "mandame el video", "ver video"]):
            # Buscar URL primero en informacion_local
            url = _extraer_primer_url(business_context.get("informacion_local") or "")
            # Si no, buscar en servicios informativos
            if not url:
                for s in business_context.get("servicios", []) or []:
                    url = _extraer_primer_url((s.get("mensaje_personalizado") or ""))
                    if url:
                        break
            if url:
                return self._add_help_footer(f"🎬 Aquí tenés el video: {url}")
            else:
                # No se encontró URL concreta: responder claro sin repetir
                return self._add_help_footer("No encuentro el enlace del video en este momento. ¿Querés que te lo envíe por acá cuando esté disponible?")

        # 0.2) "Mostrame/mandame de vuelta/otra vez los horarios" -> repetir lista de horarios (evitar info del negocio)
        if ("horarios" in mensaje_stripped) and any(p in mensaje_stripped for p in ["de vuelta", "devuelta", "otra vez", "de nuevo", "mostrar otra vez", "mostrame otra vez", "mostrame de nuevo", "ver otra vez", "ver de nuevo"]):
            # Intentar reutilizar la última selección y slots de Redis
            try:
                servicio_guardado_str = self.redis_client.get(f"servicio_seleccionado:{telefono}")
                if servicio_guardado_str:
                    servicio_guardado = json.loads(servicio_guardado_str)
                    srv_id = servicio_guardado.get("id", 0)
                    all_key = f"slots_all:{telefono}:{srv_id}"
                    page_key = f"slots_page:{telefono}:{srv_id}"
                    size_key = f"slots_page_size:{telefono}:{srv_id}"
                    all_raw = self.redis_client.get(all_key)
                    if all_raw:
                        from datetime import datetime as _dt
                        all_slots = [_dt.fromisoformat(x) for x in json.loads(all_raw)]
                        size = int(json.loads(self.redis_client.get(size_key) or "10"))
                        # Mostrar primera página nuevamente
                        page_slots = all_slots[:size]
                        # Regrabar página actual como 0
                        self.redis_client.set(page_key, json.dumps(0), ex=600)
                        # Regrabar lista actual visible en slots_key
                        slots_key = f"slots:{telefono}:{srv_id}"
                        slots_data = [{
                            "numero": i + 1,
                            "fecha_hora": s.isoformat(),
                            "empleado_id": None,
                            "empleado_nombre": "Sistema"
                        } for i, s in enumerate(page_slots)]
                        self.redis_client.set(slots_key, json.dumps(slots_data), ex=600)
                        # Responder
                        respuesta = f"{self._emoji_for_service(servicio_guardado.get('nombre') or 'Turno')} Horarios disponibles nuevamente:\n\n"
                        for i, s in enumerate(page_slots, 1):
                            dia_nombre = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"][s.weekday()]
                            respuesta += f"{i}. {dia_nombre.title()} {s.strftime('%d/%m %H:%M')}\n"
                        if len(all_slots) > len(page_slots):
                            respuesta += "\n📝 Escribe 'más' para ver más horarios."
                        respuesta += "\n💬 Escribe el número que prefieres para confirmar."
                        return self._add_help_footer(respuesta)
            except Exception as _:
                pass
            # Si no hay historial en Redis, intentamos detectar día y volver a listar
            dia_detectado_tmp = self._detectar_dia_mensaje(mensaje_stripped) or "cualquiera"
            # Reutilizamos el flujo normal más abajo (buscará y listará)
        
        # 🔧 VERIFICAR PRIMERO SI TIENE SERVICIO SELECCIONADO Y HORARIOS DISPONIBLES
        servicio_key = f"servicio_seleccionado:{telefono}"
        servicio_guardado_str = self.redis_client.get(servicio_key)
        
        if servicio_guardado_str:
            servicio_guardado = json.loads(servicio_guardado_str)
            slots_key = f"slots:{telefono}:{servicio_guardado['id']}"
            slots_data_str = self.redis_client.get(slots_key)
            if slots_data_str:
                slots_data = json.loads(slots_data_str)
                # 🆕 Paginación: comandos 'más', 'siguiente', 'anterior'
                if mensaje_stripped in ["más", "mas", "siguiente", "ver más", "ver mas", "+"]:
                    all_key = f"slots_all:{telefono}:{servicio_guardado['id']}"
                    page_key = f"slots_page:{telefono}:{servicio_guardado['id']}"
                    size_key = f"slots_page_size:{telefono}:{servicio_guardado['id']}"
                    try:
                        all_slots_raw = self.redis_client.get(all_key)
                        if not all_slots_raw:
                            return "❌ No hay más horarios para mostrar."
                        all_slots = [datetime.fromisoformat(x) for x in json.loads(all_slots_raw)]
                        page = int(json.loads(self.redis_client.get(page_key) or "0"))
                        size = int(json.loads(self.redis_client.get(size_key) or "10"))
                        next_page = page + 1
                        start = next_page * size
                        end = start + size
                        if start >= len(all_slots):
                            return "😊 Ya estás viendo los últimos horarios disponibles."
                        page_slots = all_slots[start:end]
                        # reconstruir slots_data de la página
                        slots_data = [{
                            "numero": i + 1,
                            "fecha_hora": s.isoformat(),
                            "empleado_id": None,
                            "empleado_nombre": "Sistema"
                        } for i, s in enumerate(page_slots)]
                        self.redis_client.set(slots_key, json.dumps(slots_data), ex=600)
                        self.redis_client.set(page_key, json.dumps(next_page), ex=600)
                        # Responder con la nueva página
                        respuesta = f"{self._emoji_for_service(servicio_guardado['nombre'])} Horarios disponibles (página {next_page + 1}):\n\n"
                        for i, s in enumerate(page_slots, 1):
                            dia_nombre = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"][s.weekday()]
                            respuesta += f"{i}. {dia_nombre.title()} {s.strftime('%d/%m %H:%M')}\n"
                        if end < len(all_slots):
                            respuesta += "\n📝 Escribe 'más' para ver más horarios."
                        if page > 0:
                            respuesta += "\n↩️ Escribe 'anterior' para ver los anteriores."
                        respuesta += "\n💬 Escribe el número que prefieres para confirmar."
                        return self._add_help_footer(respuesta)
                    except Exception as e:
                        print(f"❌ Error en paginación de slots: {e}")
                        return "❌ No pude cargar más horarios ahora. Intenta nuevamente."
                if mensaje_stripped in ["anterior", "previo", "volver", "<"]:
                    all_key = f"slots_all:{telefono}:{servicio_guardado['id']}"
                    page_key = f"slots_page:{telefono}:{servicio_guardado['id']}"
                    size_key = f"slots_page_size:{telefono}:{servicio_guardado['id']}"
                    try:
                        all_slots_raw = self.redis_client.get(all_key)
                        if not all_slots_raw:
                            return "❌ No hay horarios previos para mostrar."
                        all_slots = [datetime.fromisoformat(x) for x in json.loads(all_slots_raw)]
                        page = int(json.loads(self.redis_client.get(page_key) or "0"))
                        size = int(json.loads(self.redis_client.get(size_key) or "10"))
                        prev_page = max(0, page - 1)
                        if page == 0:
                            return "😊 Ya estás en la primera lista de horarios."
                        start = prev_page * size
                        end = start + size
                        page_slots = all_slots[start:end]
                        slots_data = [{
                            "numero": i + 1,
                            "fecha_hora": s.isoformat(),
                            "empleado_id": None,
                            "empleado_nombre": "Sistema"
                        } for i, s in enumerate(page_slots)]
                        self.redis_client.set(slots_key, json.dumps(slots_data), ex=600)
                        self.redis_client.set(page_key, json.dumps(prev_page), ex=600)
                        respuesta = f"{self._emoji_for_service(servicio_guardado['nombre'])} Horarios disponibles (página {prev_page + 1}):\n\n"
                        for i, s in enumerate(page_slots, 1):
                            dia_nombre = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"][s.weekday()]
                            respuesta += f"{i}. {dia_nombre.title()} {s.strftime('%d/%m %H:%M')}\n"
                        if end < len(all_slots):
                            respuesta += "\n📝 Escribe 'más' para ver más horarios."
                        if prev_page > 0:
                            respuesta += "\n↩️ Escribe 'anterior' para ver los anteriores."
                        respuesta += "\n💬 Escribe el número que prefieres para confirmar."
                        return self._add_help_footer(respuesta)
                    except Exception as e:
                        print(f"❌ Error en paginación de slots (anterior): {e}")
                        return "❌ No pude cargar horarios anteriores ahora. Intenta nuevamente."
                # 1. Selección de horario por número
                if mensaje_stripped.isdigit():
                    try:
                        slot_numero = int(mensaje_stripped)
                        if 1 <= slot_numero <= len(slots_data):
                            slot_seleccionado = slots_data[slot_numero - 1]
                            # Guardar slot seleccionado en Redis para el paso siguiente
                            self.redis_client.set(f"slot_seleccionado:{telefono}", json.dumps(slot_seleccionado), ex=600)
                            return (
                                f"✅ Elegiste:\n\n{self._emoji_for_service(servicio_guardado['nombre'])} *{servicio_guardado['nombre']}*"
                                f"\n📅 {datetime.fromisoformat(slot_seleccionado['fecha_hora']).strftime('%A %d/%m a las %H:%M')}"
                                "\n\n👤 Para confirmar, por favor escribe tu *nombre completo*."
                            )
                        else:
                            return f"❌ Elige un número entre 1 y {len(slots_data)}."
                    except ValueError:
                        return "❌ No entendí el número. Intenta de nuevo."
                # 2. Selección de horario por hora (formatos flexibles)
                hora_detectada = self._detectar_hora_mensaje(mensaje_stripped)
                if hora_detectada:
                    for slot in slots_data:
                        slot_hora = datetime.fromisoformat(slot['fecha_hora']).strftime('%H:%M')
                        if slot_hora == hora_detectada:
                            self.redis_client.set(f"slot_seleccionado:{telefono}", json.dumps(slot), ex=600)
                            return (
                                f"✅ Elegiste:\n\n{self._emoji_for_service(servicio_guardado['nombre'])} *{servicio_guardado['nombre']}*"
                                f"\n📅 {datetime.fromisoformat(slot['fecha_hora']).strftime('%A %d/%m a las %H:%M')}"
                                "\n\n👤 Para confirmar, por favor escribe tu *nombre completo*."
                            )
                    return f"❌ No encontré el horario {hora_detectada}. Elige uno de los horarios numerados."
                # 3. Confirmación de reserva O cambio de horario
                slot_seleccionado_str = self.redis_client.get(f"slot_seleccionado:{telefono}")
                if slot_seleccionado_str:
                    if self._detectar_cambio_horario(mensaje_stripped):
                        # El usuario quiere cambiar, buscar nueva hora
                        hora_nueva = self._detectar_hora_mensaje(mensaje_stripped)
                        if hora_nueva:
                            # Buscar el nuevo horario
                            for slot in slots_data:
                                slot_hora = datetime.fromisoformat(slot['fecha_hora']).strftime('%H:%M')
                                if slot_hora == hora_nueva:
                                    self.redis_client.set(f"slot_seleccionado:{telefono}", json.dumps(slot), ex=600)
                                    return (
                                        f"✅ ¡Perfecto! Cambié tu selección:\n\n{self._emoji_for_service(servicio_guardado['nombre'])} *{servicio_guardado['nombre']}*"
                                        f"\n📅 {datetime.fromisoformat(slot['fecha_hora']).strftime('%A %d/%m a las %H:%M')}"
                                        "\n\n👤 Para confirmar, por favor escribe tu *nombre completo*."
                                    )
                            return f"❌ No encontré el horario {hora_nueva}. Los horarios disponibles son:\n" + "\n".join([f"{i}. {datetime.fromisoformat(s['fecha_hora']).strftime('%H:%M')}" for i, s in enumerate(slots_data, 1)])
                        else:
                            # Quiere cambiar pero no especificó hora nueva
                            self.redis_client.delete(f"slot_seleccionado:{telefono}")
                            return f"🔄 ¡Entendido! Te muestro los horarios disponibles otra vez:\n\n" + "\n".join([f"{i}. {datetime.fromisoformat(s['fecha_hora']).strftime('%H:%M')}" for i, s in enumerate(slots_data, 1)]) + "\n\n💬 Escribe el número o la hora que prefieres."
                    
                    # 🔧 CONFIRMACIÓN: Solo si parece un nombre (más de 2 palabras o no contiene cambios)
                    elif self._es_nombre_valido(mensaje_stripped):
                        slot_seleccionado = json.loads(slot_seleccionado_str)
                        # Extraer el nombre limpio
                        nombre_cliente = self._extraer_nombre(mensaje.strip())
                        # Llamar a la función de calendar_utils para crear la reserva
                        from api.utils import calendar_utils
                        slot_dt = datetime.fromisoformat(slot_seleccionado['fecha_hora'])
                        try:
                            if servicio_guardado['id'] == 0:
                                # Reserva directa por Tenant
                                calendar_id = business_context.get("calendar_id_directo") or business_context.get("calendar_id_general")
                                titulo_evento = business_context.get("titulo_turno_directo") or "Turno"
                                duracion = business_context.get("duracion_turno_directo") or 60
                                event_id = calendar_utils.create_event_for_tenant_directo(
                                    calendar_id=calendar_id,
                                    duracion_minutos=duracion,
                                    fecha_hora=slot_dt,
                                    telefono=telefono,
                                    credentials_json=self.google_credentials,
                                    nombre_cliente=nombre_cliente,
                                    titulo_evento=titulo_evento
                                )
                                servicio_nombre = titulo_evento
                                empleado_calendar_id = calendar_id
                            else:
                                # Reserva por Servicio
                                servicio_modelo = db.query(Servicio).filter(Servicio.id == servicio_guardado['id']).first()
                                if not servicio_modelo:
                                    return "❌ Servicio no disponible. Intenta de nuevo."
                                event_id = calendar_utils.create_event_for_service(
                                    servicio_modelo,
                                    slot_dt,
                                    telefono,
                                    self.google_credentials,
                                    nombre_cliente
                                )
                                servicio_nombre = servicio_modelo.nombre
                                empleado_calendar_id = servicio_modelo.calendar_id

                            # Crear reserva en la base de datos
                            nueva_reserva = Reserva(
                                fake_id=generar_fake_id(),
                                event_id=event_id,
                                empresa=tenant.comercio,
                                empleado_id=None,
                                empleado_nombre="Sistema",
                                empleado_calendar_id=empleado_calendar_id,
                                cliente_nombre=nombre_cliente,
                                cliente_telefono=telefono,
                                fecha_reserva=slot_dt,
                                servicio=servicio_nombre,
                                estado="activo",
                                cantidad=1
                            )
                            db.add(nueva_reserva)
                            db.commit()

                            # Limpiar selección en Redis
                            self.redis_client.delete(f"servicio_seleccionado:{telefono}")
                            self.redis_client.delete(f"slots:{telefono}:{servicio_guardado['id']}")
                            self.redis_client.delete(f"slot_seleccionado:{telefono}")
                            return (
                                f"✅ Reserva confirmada para *{servicio_guardado['nombre']}*"
                                f"\n📅 {slot_dt.strftime('%A %d/%m %H:%M')}"
                                f"\n👤 A nombre de: {nombre_cliente}"
                                f"\n🔖 Código: {nueva_reserva.fake_id}"
                                f"\n\n¡Gracias por reservar! 😊"
                            )
                        except Exception as e:
                            return f"❌ Error al crear la reserva: {e}"
                # Si no se reconoce el mensaje, pedir nombre completo
                if slot_seleccionado_str:
                    return "👤 Por favor, escribe tu *nombre completo* para confirmar la reserva."
            # Si tiene servicio pero no slots, está eligiendo día
            dia_detectado = self._detectar_dia_mensaje(mensaje_stripped)
            if dia_detectado:
                # Buscar horarios disponibles para el día elegido
                from api.utils import calendar_utils
                # Obtener contexto actualizado del negocio (servicios y empleados)
                business_context = self._get_business_context(tenant, db)
                if servicio_guardado["id"] == 0:
                    # Modo directo por Tenant
                    slots = calendar_utils.get_available_slots(
                        calendar_id=business_context.get("calendar_id_directo") or business_context.get("calendar_id_general"),
                        credentials_json=self.google_credentials,
                        working_hours_json=business_context.get("working_hours_directo") or business_context.get("working_hours_general"),
                        service_duration=business_context.get("duracion_turno_directo") or 60,
                        intervalo_entre_turnos=getattr(tenant, "intervalo_entre_turnos", 15),
                        max_days=7,
                        max_turnos=50,
                        cantidad=1,
                        solo_horas_exactas=bool(business_context.get("solo_horas_exactas_directo"))
                    )
                    servicio_guardado_dict = {"id": 0, "nombre": business_context.get("titulo_turno_directo") or "Turno"}
                else:
                    servicio_guardado_dict = next((s for s in business_context["servicios"] if s["id"] == servicio_guardado["id"]), None)
                    if not servicio_guardado_dict:
                        return "❌ Servicio no disponible. Intenta de nuevo."
                    # Buscar el modelo Servicio por ID y pasar el modelo, no un dict
                    servicio_modelo = db.query(Servicio).filter(Servicio.id == servicio_guardado["id"]).first()
                    if not servicio_modelo:
                        return "❌ Servicio no disponible. Intenta de nuevo."
                    slots = calendar_utils.get_available_slots_for_service(
                        servicio_modelo,
                        intervalo_entre_turnos=getattr(tenant, "intervalo_entre_turnos", 15),
                        max_days=7,
                        max_turnos=50,  # Aumentar para asegurar que llegue al día específico
                        credentials_json=self.google_credentials
                    )
                # Filtrar slots por día
                tz = pytz.timezone("America/Montevideo")
                now = datetime.now(tz)
                if dia_detectado == "hoy":
                    dia_objetivo = now.date()
                elif dia_detectado == "mañana":
                    dia_objetivo = (now + timedelta(days=1)).date()
                elif "/" in dia_detectado:  # 🔧 NUEVO: Manejar fechas específicas DD/MM
                    try:
                        dia_str, mes_str = dia_detectado.split("/")
                        dia_num = int(dia_str)
                        mes_num = int(mes_str)
                        
                        # Determinar el año (si el mes es menor al actual, asumir próximo año)
                        año_actual = now.year
                        if mes_num < now.month or (mes_num == now.month and dia_num < now.day):
                            año_objetivo = año_actual + 1
                        else:
                            año_objetivo = año_actual
                        
                        dia_objetivo = datetime(año_objetivo, mes_num, dia_num).date()
                        print(f"🔧 DEBUG: Fecha específica detectada: {dia_detectado} -> {dia_objetivo.strftime('%A %d/%m/%Y')}")
                    except ValueError:
                        print(f"❌ Error: fecha '{dia_detectado}' no válida")
                        return f"❌ No reconozco la fecha '{dia_detectado}'. Usa formato DD/MM o nombres de días."
                else:
                    dias_semana = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
                    # Normalizar nombre del día (quitar acentos)
                    dia_normalizado = dia_detectado.replace("é", "e").replace("á", "a")
                    try:
                        idx = dias_semana.index(dia_normalizado)
                        hoy_idx = now.weekday()
                        dias_hasta = (idx - hoy_idx) % 7
                        if dias_hasta == 0:  # Si es hoy, tomar el próximo de esa semana
                            dias_hasta = 7
                        dia_objetivo = (now + timedelta(days=dias_hasta)).date()
                        print(f"🔧 DEBUG: Día detectado: {dia_detectado}, Normalizado: {dia_normalizado}, Hoy: {now.strftime('%A %d/%m')}, Objetivo: {dia_objetivo.strftime('%A %d/%m')}")
                    except ValueError:
                        print(f"❌ Error: día '{dia_detectado}' no reconocido")
                        return f"❌ No reconozco el día '{dia_detectado}'. Usa: hoy, mañana, lunes, martes, etc."
                slots_dia = [s for s in slots if s.date() == dia_objetivo]
                if not slots_dia:
                    return f"😔 No hay horarios disponibles para *{servicio_guardado_dict['nombre']}* el {dia_detectado}.\n¿Quieres elegir otro día?"
                # Guardar slots en Redis con paginación (20 por página cuando el día es específico)
                srv_id = servicio_guardado_dict['id']
                all_key = f"slots_all:{telefono}:{srv_id}"
                page_key = f"slots_page:{telefono}:{srv_id}"
                size_key = f"slots_page_size:{telefono}:{srv_id}"
                page_size = 20
                self.redis_client.set(all_key, json.dumps([s.isoformat() for s in slots_dia]), ex=1800)
                self.redis_client.set(page_key, json.dumps(0), ex=1800)
                self.redis_client.set(size_key, json.dumps(page_size), ex=1800)
                # Primera página
                first_page = slots_dia[:page_size]
                slots_key = f"slots:{telefono}:{srv_id}"
                slots_data = [{
                    "numero": i,
                    "fecha_hora": slot.isoformat(),
                    "empleado_id": None,
                    "empleado_nombre": "Sistema"
                } for i, slot in enumerate(first_page, 1)]
                self.redis_client.set(slots_key, json.dumps(slots_data), ex=1800)
                respuesta = f"{self._emoji_for_service(servicio_guardado_dict['nombre'])} *Horarios para {servicio_guardado_dict['nombre']}* el {dia_detectado}:\n\n"
                respuesta += "\n".join([f"{i}. {datetime.fromisoformat(s['fecha_hora']).strftime('%H:%M')}" for i, s in enumerate(slots_data, 1)])
                if len(slots_dia) > len(first_page):
                    respuesta += "\n\n📝 Escribe 'más' para ver más horarios."
                respuesta += "\n\n💬 Escribe el número o la hora que prefieres."
                return respuesta

        # MODO DIRECTO SIN SERVICIO SELECCIONADO: detectar día y listar horarios
        if business_context.get("modo_directo") and not servicio_guardado_str:
            dia_detectado = self._detectar_dia_mensaje(mensaje_stripped)
            if dia_detectado:
                from api.utils import calendar_utils
                slots = calendar_utils.get_available_slots(
                    calendar_id=business_context.get("calendar_id_directo") or business_context.get("calendar_id_general"),
                    credentials_json=self.google_credentials,
                    working_hours_json=business_context.get("working_hours_directo") or business_context.get("working_hours_general"),
                    service_duration=business_context.get("duracion_turno_directo") or 60,
                    intervalo_entre_turnos=getattr(tenant, "intervalo_entre_turnos", 15),
                    max_days=7,
                    max_turnos=50,
                    cantidad=1,
                    solo_horas_exactas=bool(business_context.get("solo_horas_exactas_directo"))
                )
                # Filtrar por día
                tz = pytz.timezone("America/Montevideo")
                now = datetime.now(tz)
                if dia_detectado == "hoy":
                    dia_objetivo = now.date()
                elif dia_detectado == "mañana":
                    dia_objetivo = (now + timedelta(days=1)).date()
                elif "/" in dia_detectado:
                    try:
                        dia_str, mes_str = dia_detectado.split("/")
                        dia_num = int(dia_str)
                        mes_num = int(mes_str)
                        año_actual = now.year
                        año_objetivo = año_actual + 1 if (mes_num < now.month or (mes_num == now.month and dia_num < now.day)) else año_actual
                        dia_objetivo = datetime(año_objetivo, mes_num, dia_num).date()
                    except ValueError:
                        return f"❌ No reconozco la fecha '{dia_detectado}'. Usa formato DD/MM o nombres de días."
                else:
                    dias_semana = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
                    dia_normalizado = dia_detectado.replace("é", "e").replace("á", "a")
                    try:
                        idx = dias_semana.index(dia_normalizado)
                        hoy_idx = now.weekday()
                        dias_hasta = (idx - hoy_idx) % 7
                        if dias_hasta == 0:
                            dias_hasta = 7
                        dia_objetivo = (now + timedelta(days=dias_hasta)).date()
                    except ValueError:
                        return f"❌ No reconozco el día '{dia_detectado}'. Usa: hoy, mañana, lunes, martes, etc."

                slots_dia = [s for s in slots if s.date() == dia_objetivo]
                if not slots_dia:
                    return f"😔 No hay horarios disponibles el {dia_detectado}. ¿Querés elegir otro día?"
                # Guardar selección genérica y slots con paginación (20 por página)
                titulo = business_context.get("titulo_turno_directo") or "Turno"
                self.redis_client.set(f"servicio_seleccionado:{telefono}", json.dumps({"id": 0, "nombre": titulo}), ex=1800)
                all_key = f"slots_all:{telefono}:0"
                page_key = f"slots_page:{telefono}:0"
                size_key = f"slots_page_size:{telefono}:0"
                page_size = 20
                self.redis_client.set(all_key, json.dumps([s.isoformat() for s in slots_dia]), ex=1800)
                self.redis_client.set(page_key, json.dumps(0), ex=1800)
                self.redis_client.set(size_key, json.dumps(page_size), ex=1800)
                first_page = slots_dia[:page_size]
                slots_key = f"slots:{telefono}:0"
                slots_data = [{
                    "numero": i,
                    "fecha_hora": slot.isoformat(),
                    "empleado_id": None,
                    "empleado_nombre": "Sistema"
                } for i, slot in enumerate(first_page, 1)]
                self.redis_client.set(slots_key, json.dumps(slots_data), ex=1800)
                respuesta = f"{self._emoji_for_service(titulo)} *Horarios para {titulo}* el {dia_detectado}:\n\n"
                respuesta += "\n".join([f"{i}. {datetime.fromisoformat(s['fecha_hora']).strftime('%H:%M')}" for i, s in enumerate(slots_data, 1)])
                if len(slots_dia) > len(first_page):
                    respuesta += "\n\n📝 Escribe 'más' para ver más horarios."
                respuesta += "\n\n💬 Escribe el número o la hora que prefieres."
                return respuesta
        
        # 🔧 DETECCIÓN DE SELECCIÓN DE SERVICIO (solo si NO tiene servicio guardado)
        servicio_seleccionado = None
        
        print(f"🔧 DEBUG: Mensaje recibido: '{mensaje}' - Servicios disponibles: {[s['nombre'] for s in business_context['servicios']]}")
        
        # Verificar si es un número
        if mensaje_stripped.isdigit():
            try:
                posicion = int(mensaje_stripped)
                if 1 <= posicion <= len(business_context['servicios']):
                    servicio_seleccionado = business_context['servicios'][posicion - 1]
                    print(f"🔧 DEBUG: Servicio seleccionado por número {posicion}: {servicio_seleccionado['nombre']} (ID: {servicio_seleccionado['id']})")
            except:
                pass

        # Si no es número, buscar por nombre de servicio
        if not servicio_seleccionado:
            for servicio in business_context['servicios']:
                nombre_servicio = servicio['nombre'].lower()
                # Buscar coincidencia exacta o parcial
                if (mensaje_stripped == nombre_servicio or 
                    mensaje_stripped in nombre_servicio or 
                    nombre_servicio in mensaje_stripped):
                    servicio_seleccionado = servicio
                    print(f"🔧 DEBUG: Servicio seleccionado por nombre: {servicio_seleccionado['nombre']} (ID: {servicio_seleccionado['id']})")
                    break
                # Coincidencia por tokens (normalizada)
                try:
                    import unicodedata
                    def _norm(s: str) -> str:
                        s = (s or "").lower().strip()
                        return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')
                    nmsg = _norm(mensaje_stripped)
                    nname = _norm(nombre_servicio)
                    tokens = [t for t in re.split(r"\s+", nname) if len(t) > 2]
                    if tokens:
                        overlap = sum(1 for t in tokens if t in nmsg)
                        if overlap >= min(2, len(tokens)):
                            servicio_seleccionado = servicio
                            print(f"🔧 DEBUG: Servicio seleccionado por tokens: {servicio_seleccionado['nombre']} (ID: {servicio_seleccionado['id']})")
                            break
                except Exception:
                    pass

        # Si encontró un servicio
        if servicio_seleccionado:
            # 🔧 VERIFICAR SI ES INFORMATIVO
            es_informativo = servicio_seleccionado.get('es_informativo', False)
            print(f"🔧 DEBUG: Servicio {servicio_seleccionado['nombre']} - Es informativo: {es_informativo}")

            if es_informativo:
                mensaje_personalizado = (servicio_seleccionado.get('mensaje_personalizado') or '').strip()
                if mensaje_personalizado:
                    if self._wants_more_info(mensaje_stripped):
                        return self._add_help_footer(f"ℹ️ *{servicio_seleccionado['nombre']}*\n\n{mensaje_personalizado}\n\n📅 ¿Querés ver horarios?")
                    else:
                        breve = self._first_paragraphs(mensaje_personalizado, max_paragraphs=2, max_chars=420)
                        return self._add_help_footer(f"ℹ️ *{servicio_seleccionado['nombre']}*\n\n{breve}\n\n👉 Decime “más info” si querés el detalle completo, o pedime horarios.")
                else:
                    return self._add_help_footer(f"ℹ️ *{servicio_seleccionado['nombre']}* es un servicio informativo.\n\n💬 ¿En qué más puedo ayudarte?")

            # 🔧 ENRIQUECER DESCRIPCIÓN DESDE informacion_local SI FALTA
            if not (servicio_seleccionado.get('mensaje_personalizado') or '').strip():
                extra = self._extract_service_info_from_tenant_info(servicio_seleccionado['nombre'], business_context.get('informacion_local') or '')
                if extra:
                    servicio_seleccionado['mensaje_personalizado'] = extra

            # 🔧 GUARDAR SERVICIO SELECCIONADO Y PREGUNTAR DÍA
            servicio_key = f"servicio_seleccionado:{telefono}"
            self.redis_client.set(servicio_key, json.dumps(servicio_seleccionado), ex=1800)  # 30 min

            # Tarjeta informativa breve antes de pedir el día
            tarjeta = self._respuesta_info_servicio(servicio_seleccionado, business_context, full=False, add_footer=False)
            pregunta = self._preguntar_dia_disponible(servicio_seleccionado, telefono)
            return f"{tarjeta}\n\n{pregunta}"
        
    # � FILTRO PREVIO: Detectar consultas claramente ajenas al negocio
        palabras_ajenas = [
            'receta', 'cocina', 'comida', 'guiso', 'ingredientes', 'cocinar',
            'amor', 'vida', 'consejo', 'salud', 'medicina', 'doctor',
            'clima', 'tiempo', 'lluvia', 'sol', 'temperatura',
            'deportes', 'futbol', 'partido', 'juego',
            'politica', 'presidente', 'gobierno', 'elecciones',
            'matematicas', 'fisica', 'quimica', 'estudio', 'tarea',
            'musica', 'cancion', 'banda', 'artista',
            'pelicula', 'serie', 'actor', 'actriz'
        ]
        
        if any(palabra in mensaje_stripped for palabra in palabras_ajenas):
            return self._add_help_footer(f"Lo siento, solo puedo ayudarte con reservas y servicios de {tenant.comercio}. ¿Necesitas hacer una reserva o consultar nuestros servicios?")
        
        # �🔧 RESTO DEL PROCESAMIENTO CON IA
        # Construir contexto para la IA
        system_prompt = f"""🤖 Eres la IA asistente de {tenant.comercio} EXCLUSIVAMENTE para reservas, servicios e información del negocio.

⚠️ RESTRICCIÓN CRÍTICA: SOLO responde sobre:
- Reservas de turnos/citas
- Servicios disponibles ({', '.join([s['nombre'] for s in business_context['servicios']])})
- Cancelaciones de reservas
- Consultas sobre horarios disponibles
- Información del negocio, biografía del profesional y datos de contacto/ubicación de {tenant.comercio}

🚫 NO RESPONDAS NUNCA A:
- Recetas de cocina
- Consejos de vida (salvo que estén explícitamente en la información del negocio)
- Preguntas generales no relacionadas con el negocio
- Temas ajenos a reservas, servicios o información del negocio
- Consultas sobre otros temas

Si te preguntan algo no relacionado, responde:
"Lo siento, solo puedo ayudarte con reservas, servicios o información de {tenant.comercio}. ¿Necesitas hacer una reserva o consultar nuestros servicios?"

📊 INFORMACIÓN DEL NEGOCIO:
- 🏢 Nombre: {tenant.comercio}
- ✨ Servicios disponibles: {', '.join([s['nombre'] for s in business_context['servicios']])}
- 👥 Empleados: {', '.join([e['nombre'] for e in business_context['empleados']]) if business_context['empleados'] else 'Sin empleados (servicios directos)'}
- 📍 Dirección: {business_context.get('direccion') or 'N/D'}
- 📞 Teléfono: {business_context.get('telefono') or 'N/D'}
- 📝 Info del local (resumen): {(business_context.get('informacion_local') or '')[:800]}

👤 INFORMACIÓN DEL CLIENTE (📞 {telefono}):
- 🔄 Cliente recurrente: {'🎯 Sí' if user_history['es_cliente_recurrente'] else '🆕 No (cliente nuevo)'}
- ⭐ Servicio favorito: {user_history['servicio_favorito'] or '🤷 Ninguno aún'}
- 📅 Reservas activas: {len(user_history['reservas_activas'])}
- 📊 Historial: {len(user_history['historial'])} reservas anteriores

📋 INSTRUCCIONES IMPORTANTES:
1. 😊 Sé natural, amigable y personalizada. Usa emojis apropiados
2. 🎯 Usa la información del cliente para personalizar respuestas
3. 📋 Cuando te pidan un turno, muestra los servicios numerados (1, 2, 3...)
4. 🔢 Si el usuario dice un número, usa la función buscar_horarios_servicio con el ID REAL
5. 🏆 SERVICIOS CON SUS IDs REALES:
{self._format_servicios_with_real_ids(business_context['servicios'])}
6. 🧠 Recuerda conversaciones anteriores
7. ❓ SOLO responde preguntas sobre el negocio y servicios
8. 📅 Si el usuario menciona un día específico (hoy, mañana, lunes, martes, miércoles, jueves, viernes, sábado, domingo, fecha específica (DD/MM)), usa ese día en preferencia_fecha
9. 🚫 NO busques horarios cuando pregunten por sus reservas actuales o códigos de cancelación
10. 💬 Si preguntan por turnos activos/reservas, indica que pueden cancelar enviando solo el código
11. 🚫 No inventes servicios ni menciones servicios que no estén en la lista disponible.
12. 📝 Si piden información sobre un servicio, usa mensaje_personalizado de ese servicio si existe. Si no, usa nombre, duración y precio.
13. 🏢 Si piden información general (quién es, sobre, contacto, dirección, horarios), responde usando la información del negocio proporcionada.

🛡️ SEGURIDAD CRÍTICA:
- ⚠️ NUNCA muestres información de reservas de otros números de teléfono
- 🚫 Si preguntan por reservas de otro usuario, responde: "Por seguridad, solo puedo mostrar TUS reservas"
- 🔐 Solo ayuda con reservas del número actual: {telefono}

🧠 CONTEXTO INTELIGENTE:
- 🔍 Si el usuario dice "no tengo los códigos" después de preguntar por otro número, NO asumas que quiere hacer una reserva nueva
- 💬 Pregunta qué necesita específicamente: "¿Necesitas ayuda con TUS reservas o quieres hacer una nueva?"
- 🎯 Mantén el contexto de la conversación anterior

🛠️ FUNCIONES DISPONIBLES:
- 🔍 buscar_horarios_servicio: Para mostrar horarios disponibles (usa el ID real del servicio y preferencia_fecha si el usuario especifica un día)
- ❌ cancelar_reserva: Para cancelar reservas existentes

⚠️ IMPORTANTE: NO puedes crear reservas directamente. El flujo de reserva se maneja automáticamente cuando el usuario selecciona horario y proporciona su nombre.

💡 IMPORTANTE: Este negocio {'tiene empleados' if business_context['tiene_empleados'] else 'NO tiene empleados'}.
"""

        # Construir historial de conversación
        messages = [{"role": "system", "content": system_prompt}]
        
        # Agregar historial reciente (últimos 10 mensajes)
        recent_history = conversation_history[-10:] if len(conversation_history) > 10 else conversation_history
        for msg in reversed(recent_history):
            messages.append({
                "role": msg["role"],
                "content": msg["content"]
            })

        # Agregar mensaje actual
        messages.append({"role": "user", "content": mensaje})

    # Definir funciones disponibles - SOLO buscar horarios, NO crear reservas
        functions = [
            {
                "name": "buscar_horarios_servicio",
                "description": "Buscar horarios disponibles para un servicio específico",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "servicio_id": {"type": "integer", "description": "ID REAL del servicio en la base de datos"},
                        "preferencia_horario": {"type": "string", "description": "mañana, tarde, noche o cualquiera"},
                        "preferencia_fecha": {"type": "string", "description": "hoy, mañana, lunes, martes, miércoles, jueves, viernes, sábado, domingo, fecha específica (DD/MM), esta_semana o cualquiera"},
                        "cantidad": {"type": "integer", "description": "Cantidad de personas", "default": 1}
                    },
                    "required": ["servicio_id"]
                }
            },
            {
                "name": "cancelar_reserva",
                "description": "Cancelar una reserva existente",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "codigo_reserva": {"type": "string", "description": "Código de la reserva"}
                    },
                    "required": ["codigo_reserva"]
                }
            }
        ]

        try:
            response = self.client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=messages,
                functions=functions,
                function_call="auto",
                temperature=0.3,
                max_tokens=800
            )

            message = response.choices[0].message

            # Si la IA quiere ejecutar una función
            if message.function_call:
                function_name = message.function_call.name
                function_args = json.loads(message.function_call.arguments)

                # Ejecutar la función
                function_result = await self._execute_ai_function(
                    {"name": function_name, "args": function_args},
                    telefono, business_context, tenant, db
                )

                return function_result

            # Respuesta directa de la IA (sanitizada: no inventar links de video)
            return self._sanitize_ai_response(message.content or "", business_context)

        except Exception as e:
            print(f"❌ Error en OpenAI: {e}")
            return self._generar_respuesta_fallback(mensaje, user_history, business_context)

    def _get_canonical_video_url(self, business_context: dict) -> str | None:
        """Obtiene la URL del video desde informacion_local o servicios (primer URL encontrado)."""
        import re
        def first_url(text: str | None) -> str | None:
            if not text:
                return None
            m = re.search(r"https?://\S+", text)
            return m.group(0) if m else None

        url = first_url(business_context.get("informacion_local") or "")
        if url:
            return url
        for s in business_context.get("servicios", []) or []:
            url = first_url((s.get("mensaje_personalizado") or ""))
            if url:
                return url
        return None

    def _sanitize_ai_response(self, text: str, business_context: dict) -> str:
        """Evita links inventados para 'video': siempre usa el link canónico si existe."""
        try:
            tlow = (text or "").lower()
            # Disparadores relacionados a video
            triggers = ["video", "video explicativo", "enlace del video", "link del video", "ver video"]
            if any(k in tlow for k in triggers):
                canonical = self._get_canonical_video_url(business_context)
                import re
                # Si el modelo inventó un markdown link o una URL, lo removemos/reemplazamos
                # 1) Quitar/normalizar cualquier formato [texto](url)
                text = re.sub(r"\[[^\]]+\]\(https?://[^\)]+\)", "", text)
                # 2) Remover URLs sueltas si vamos a poner la canónica
                if canonical:
                    text = re.sub(r"https?://\S+", "", text)
                    # Agregar la línea correcta con el video canónico
                    suffix = ("\n\n" if not text.endswith("\n") else "") + f"🎬 Aquí tenés el video: {canonical}"
                    text = (text.strip() + suffix).strip()
                else:
                    # No hay canónica: evitar inventos, dejar el texto sin URLs
                    text = re.sub(r"https?://\S+", "", text).strip()
                    if not text:
                        text = "No encuentro el enlace del video en este momento."
            return text
        except Exception:
            return text
    
    def _format_servicios_with_real_ids(self, servicios: list) -> str:
        """
        Devuelve una lista de servicios con sus IDs reales para mostrar al usuario.
        """
        if not servicios:
            return "No hay servicios disponibles."
        lines = []
        for s in servicios:
            lines.append(f"{s['id']}: {s['nombre']}")
        return "\n".join(lines)
    
    def mostrar_servicios(self, business_context: dict) -> str:
        """Devuelve la lista de servicios disponibles para mostrar al cliente (siempre actualizada)."""
        servicios = business_context["servicios"]
        if not servicios:
            if business_context.get("modo_directo"):
                return (
                    "✨ Reservas directas disponibles.\n\n"
                    "Decime ‘hoy’, ‘mañana’ o un día (lunes, martes, …) y te muestro horarios."
                )
            return "No hay servicios disponibles en este momento."
        # Mostrar servicios numerados y por nombre, sin duplicados ni nombres erróneos
        lines = []
        for idx, s in enumerate(servicios, 1):
            lines.append(f"{idx}. {s['nombre']}")
        return f"✨ Servicios disponibles:\n" + "\n".join(lines) + "\n\n💬 Escribe el número o nombre del servicio que te interesa."

    def _get_business_context(self, tenant, db):
        """Obtener contexto del negocio: servicios y empleados desde models.py"""
        from api.app.models import Servicio, Empleado
        servicios_db = db.query(Servicio).filter(Servicio.tenant_id == tenant.id).all()
        empleados_db = db.query(Empleado).filter(Empleado.tenant_id == tenant.id).all()
        
        # Convertir objetos a diccionarios
        servicios = []
        for s in servicios_db:
            servicios.append({
                "id": s.id,
                "nombre": s.nombre,
                "precio": getattr(s, "precio", 0),
                "duracion": getattr(s, "duracion", 60),
                "es_informativo": getattr(s, "es_informativo", False),
                "mensaje_personalizado": getattr(s, "mensaje_personalizado", ""),
                "working_hours": getattr(s, "working_hours", None),
                "calendar_id": getattr(s, "calendar_id", None),
                "turnos_consecutivos": getattr(s, "turnos_consecutivos", False),
            })
        
        empleados = []
        for e in empleados_db:
            empleados.append({
                "id": e.id,
                "nombre": e.nombre,
                "email": getattr(e, "email", ""),
                "telefono": getattr(e, "telefono", "")
            })
        
        # Determinar si no hay empleados ni servicios para activar modo directo
        modo_directo = (len(servicios) == 0 and len(empleados) == 0)

        return {
            "servicios": servicios,
            "empleados": empleados,
            "tiene_empleados": len(empleados) > 0,
            "calendar_id_general": getattr(tenant, "calendar_id_general", None),
            # Información adicional para respuestas ricas
            "informacion_local": getattr(tenant, "informacion_local", None),
            "telefono": getattr(tenant, "telefono", None),
            "direccion": getattr(tenant, "direccion", None),
            "comercio": getattr(tenant, "comercio", None),
            "working_hours_general": getattr(tenant, "working_hours_general", None),
            # Campos de reservas directas
            "modo_directo": modo_directo,
            "calendar_id_directo": getattr(tenant, "calendar_id_directo", None),
            "duracion_turno_directo": getattr(tenant, "duracion_turno_directo", None),
            "precio_turno_directo": getattr(tenant, "precio_turno_directo", None),
            "solo_horas_exactas_directo": getattr(tenant, "solo_horas_exactas_directo", False),
            "turnos_consecutivos_directo": getattr(tenant, "turnos_consecutivos_directo", False),
            "working_hours_directo": getattr(tenant, "working_hours_general", None),
            "titulo_turno_directo": (tenant.comercio or "Turno"),
        }

    async def cancelar_reserva(self, codigo_reserva: str, telefono: str, db: Session):
        """Cancelar una reserva por código"""
        try:
            # 🔒 SEGURIDAD REFORZADA: Buscar la reserva con múltiples filtros de seguridad
            now_aware = datetime.now(self.tz)
            
            reserva = db.query(Reserva).filter(
                Reserva.fake_id == codigo_reserva,  # Código específico
                Reserva.cliente_telefono == telefono,  # 🔒 Solo del teléfono del usuario
                Reserva.estado == "activo",  # Solo activas
                Reserva.fecha_reserva > now_aware  # 📅 Solo futuras
            ).first()
            
            if not reserva:
                return self._add_help_footer(f"❌ No encontré la reserva con código `{codigo_reserva}` o no se puede cancelar.\n\n_Verifica que el código sea correcto y que la reserva sea futura._")
            
            # 🔒 VERIFICACIÓN ADICIONAL: Confirmar que es del mismo teléfono
            if reserva.cliente_telefono != telefono:
                print(f"🚨 INTENTO DE ACCESO NO AUTORIZADO: {telefono} intentó cancelar reserva de {reserva.cliente_telefono}")
                return self._add_help_footer("❌ No tienes autorización para cancelar esta reserva.")
            
            # Verificar si se puede cancelar (debe ser con al menos 1 hora de anticipación)
            if not self._puede_cancelar_reserva(reserva.fecha_reserva, now_aware):
                tiempo_restante = (self._normalize_datetime(reserva.fecha_reserva) - now_aware).total_seconds() / 60
                return self._add_help_footer(f"❌ No puedes cancelar reservas con menos de 1 hora de anticipación.\n\n_Tu reserva es en {int(tiempo_restante)} minutos._")
            
            # Intentar cancelar en Google Calendar si existe
            if reserva.event_id:
                from api.utils import calendar_utils
                # Usar el empleado_calendar_id que ya está guardado en la reserva
                calendar_utils.cancelar_evento_google(
                    reserva.empleado_calendar_id,
                    reserva.event_id,
                    self.google_credentials
                )
            
            # Actualizar estado en la base de datos
            reserva.estado = "cancelado"
            db.commit()
            
            return f"✅ *Reserva cancelada correctamente*\n\n📅 {reserva.servicio} el {reserva.fecha_reserva.strftime('%d/%m %H:%M') if reserva.fecha_reserva else ''}\n🔖 Código: `{codigo_reserva}`\n\n😊 ¡Esperamos verte pronto!\n\n💬 _¿Necesitas ayuda personalizada? Escribe 'ayuda persona' para hablar con nuestro equipo._"
            
        except Exception as e:
            print(f"❌ Error cancelando reserva: {e}")
            return self._add_help_footer(f"❌ Error al cancelar la reserva: {str(e)}")

    def _extract_service_info_from_tenant_info(self, nombre_servicio: str, tenant_info: str) -> str | None:
        """Intenta extraer un fragmento relevante sobre un servicio desde la información general del negocio.
        Busca el nombre del servicio de forma insensible a mayúsculas/acentos y devuelve un párrafo cercano.
        """
        if not tenant_info or not nombre_servicio:
            return None
        import unicodedata, re
        def normalize(s: str) -> str:
            return ''.join(c for c in unicodedata.normalize('NFD', s.lower()) if unicodedata.category(c) != 'Mn')
        info_norm = normalize(tenant_info)
        name_norm = normalize(nombre_servicio)
        idx = info_norm.find(name_norm)
        if idx == -1:
            # probar con alias simples (e.g., tre)
            tokens = [t.strip() for t in re.split(r"[:\-\n]", name_norm) if t.strip()]
            for tok in tokens:
                j = info_norm.find(tok)
                if j != -1:
                    idx = j
                    break
        if idx == -1:
            return None
        # Tomar ventana desde inicio de sección hasta próximo doble salto de línea o 1000 chars
        start = max(0, info_norm.rfind('\n\n', 0, idx))
        end = info_norm.find('\n\n', idx)
        if end == -1:
            end = min(len(info_norm), idx + 1200)
        # Mapear índices normalizados a originales (aproximación: usar mismos índices sobre texto original si longitudes iguales tras normalización de acentos)
        # Como aproximación simple, tomamos misma ventana sobre texto original por posiciones cercanas
        # Para evitar desalineación por remoción de acentos, expandimos un poco los límites
        start_orig = max(0, start - 50)
        end_orig = min(len(tenant_info), end + 50)
        snippet = tenant_info[start_orig:end_orig].strip()
        # Limpiar encabezados redundantes
        snippet = re.sub(r"\n{3,}", "\n\n", snippet)
        return snippet if snippet else None