import os
import imaplib
import email
from email.header import decode_header
import zipfile
import glob
import psycopg2
import uuid
import re
import difflib
import sys
import tempfile
from decimal import Decimal
from defusedxml import ElementTree as ET
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import track

# ==========================================
# 1. CONFIGURACIÓN CLOUD NATIVE Y CONSTANTES
# ==========================================
load_dotenv()
console = Console()

DB_CONFIG = {
    'dbname': os.getenv('DB_NAME'),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASS'),
    'host': os.getenv('DB_HOST'),
    'port': os.getenv('DB_PORT')
}

EMAIL_USER = os.getenv('EMAIL_USER')
EMAIL_PASS = os.getenv('EMAIL_PASS')

# Parámetros de auditoría y proveedor DIAN
NIT_PROVEEDOR = os.getenv('NIT_PROVEEDOR', '814003956').strip()
ASUNTO_ESTACION = os.getenv('ASUNTO_ESTACION', 'EL PLACER LTDA').strip()
VENTANA_DIAS_CONCILIACION = int(os.getenv('VENTANA_DIAS_CONCILIACION', '15'))
TOLERANCIA_GALONES = Decimal('0.001')

# Lista blanca de correos oficiales autorizados (directos y rebotes internos)
REMITENTES_OFICIALES = (
    'factura@transaccionesco.siigo.net',
    'secretariadegobierno@funes-narino.gov.co',
    'alcaldia@funes-narino.gov.co',
    os.getenv('REMITENTE_ESTACION', 'facturacion@elplacer.com').strip().lower()
)

# Expresión regular oficial DIAN: NIT;RAZON_SOCIAL;PREFIJO_CONSECUTIVO(PIEF/PLAC/otra);COD_DOC
REGEX_ASUNTO_DIAN = re.compile(
    r'814003956\s*;\s*EL PLACER LTDA\s*;\s*([A-Z0-9\-]+)\s*;\s*(\d{2})',
    re.IGNORECASE
)

# Catálogo global de alias (con PODA CESPED separado de GUADAÑA y correcciones reales de isleros)
MAQUINARIA_ESPECIAL = {
    "EXCAVADORA LLANTAS": ["CX130", "CX 130", "LLANTAS", "CX-130", "EXCAVADORA DE LLANTAS CX130B", "RETRO DE LLANTAS CX130B"],
    "EXCAVADORA ORUGAS": ["ORUGAS", "ORUGA", "URUGAS", "RETROURUGAS", "RETROEXCAVADORA", "RETRO DE ORUGAS", "EXCAVADORA DE ORUGAS"],
    "TRACTOR": ["TRACTOR", "AGROLUX", "FARH", "FAHR", "AGROLUZ"],
    "PODA CESPED": ["PODA CESPED", "PODA", "CESPED"],
    "GUADAÑA": ["GUADAÑA", "GUAADAÑA", "GUADANA", "ROCERIA", "VIVERO"],
    "MOTONIVELADORA": ["MOTONIVELADORA", "MOTO NIVELADORA", "NIVELADORA"],
    "VIBROCOMPACTADOR": ["VIBROCOMPACTADOR", "VIBRO COMPACTADOR", "VIBRO", "VIVROCOMPACTADOR", "VIVRO"],
    "NC6574": ["NC6574", "NCG574", "NCG 574"]
}


def decodificar_encabezado_mime(valor_crudo):
    if not valor_crudo:
        return ""
    partes = decode_header(valor_crudo)
    texto_limpio = []
    for fragmento, codificacion in partes:
        if isinstance(fragmento, bytes):
            texto_limpio.append(fragmento.decode(codificacion or 'utf-8', errors='ignore'))
        else:
            texto_limpio.append(str(fragmento))
    return "".join(texto_limpio).strip()


def conectar_gmail():
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(EMAIL_USER, EMAIL_PASS)
    mail.select("inbox")

    # Búsqueda ultra segura: únicamente mensajes UNSEEN para evitar SEARCH command error: BAD
    _, mensajes = mail.search(None, 'UNSEEN')

    ids = mensajes[0].split() if mensajes[0] else []
    return mail, ids


def sanitizar_ruta(ruta_base, nombre_archivo):
    base_abs = os.path.abspath(ruta_base)
    ruta_absoluta = os.path.abspath(os.path.join(base_abs, nombre_archivo))
    if os.path.commonpath([base_abs, ruta_absoluta]) != base_abs:
        raise ValueError(f"Intento de extracción maliciosa (Zip Slip) detectado: {nombre_archivo}")
    return ruta_absoluta


def extraer_placa_y_resto(texto, placas_autorizadas):
    texto_limpio = texto.upper()

    for nombre_oficial, variantes in MAQUINARIA_ESPECIAL.items():
        for v in variantes:
            if v in texto_limpio:
                if nombre_oficial in placas_autorizadas:
                    resto = texto_limpio.replace(v, "").strip()
                    resto = re.sub(r'^[\s\-:]+|[\s\-:]+$', '', resto)
                    return nombre_oficial, resto if resto else "SIN_OBSERVACION"

    match = re.search(r'([A-Z]{3}[\s-]?\d{2}[A-Z0-9])', texto_limpio)
    if match:
        placa_extraida = match.group(1).replace("-", "").replace(" ", "")
        # Cutoff 0.80 permite tolerar 1 error de tipeo en placas de 6 caracteres (5/6 = 0.833)
        coincidencias = difflib.get_close_matches(placa_extraida, placas_autorizadas, n=1, cutoff=0.80)

        if coincidencias:
            placa_oficial = coincidencias[0]
            resto = texto_limpio.replace(match.group(1), "").strip()
            resto = re.sub(r'^[\s\-:]+|[\s\-:]+$', '', resto)
            return placa_oficial, resto if resto else "SIN_OBSERVACION"

    return None, texto_limpio.strip() if texto_limpio.strip() else "SIN_OBSERVACION"


def resolver_contrato_por_ventana(cursor, placa, fecha):
    """
    Imputa automáticamente el contrato según la bolsa del vehículo y la ventana
    temporal [fecha_inicio, fecha_fin_real]. Si no cae en ventana exacta, busca
    el contrato abierto activo de la bolsa como respaldo.
    """
    if not placa or placa == "SIN_PLACA" or not fecha:
        return None, False

    cursor.execute("""
        SELECT c.id_contrato
        FROM public.dim_contratos c
        JOIN public.dim_vehiculos v ON UPPER(TRIM(v.bolsa)) = UPPER(TRIM(c.bolsa))
        WHERE UPPER(TRIM(v.placa)) = %s
          AND %s::date >= c.fecha_inicio
          AND (c.fecha_fin_real IS NULL OR %s::date <= c.fecha_fin_real)
        ORDER BY c.fecha_inicio DESC
        LIMIT 1;
    """, (placa, fecha, fecha))
    fila = cursor.fetchone()
    if fila:
        return fila[0], True

    # Respaldo: contrato abierto activo de la bolsa (marca alerta fuera de ventana)
    cursor.execute("""
        SELECT c.id_contrato
        FROM public.dim_contratos c
        JOIN public.dim_vehiculos v ON UPPER(TRIM(v.bolsa)) = UPPER(TRIM(c.bolsa))
        WHERE UPPER(TRIM(v.placa)) = %s
          AND c.fecha_fin_real IS NULL
          AND UPPER(TRIM(c.estado)) = 'ACTIVO'
        ORDER BY c.fecha_inicio DESC
        LIMIT 1;
    """, (placa,))
    fila_respaldo = cursor.fetchone()
    if fila_respaldo:
        return fila_respaldo[0], False

    return None, False


def procesar_xml_blindado(ruta_xml, message_id, cursor, conn, placas_autorizadas):
    ns = {'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
          'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2'}

    requiere_revision = False
    num_factura = f"SIN_NUM_{uuid.uuid4().hex[:5]}"
    fecha = None
    date_key = None
    cufe = None
    placa = "SIN_PLACA"
    observacion_adicional = "SIN_OBSERVACION"
    gals = Decimal('0.00')
    total = Decimal('0.00')
    v_unit = Decimal('0.00')
    tipo_combustible = "NO_ESPECIFICADO"
    id_vale = None
    id_contrato = None

    try:
        tree = ET.parse(ruta_xml)
        root = tree.getroot()

        xml_interno = root.find('.//cac:Attachment/cac:ExternalReference/cbc:Description', ns)
        if xml_interno is None or not xml_interno.text:
            raise ValueError("El XML no contiene un documento UBL adjunto válido.")

        texto_xml = xml_interno.text.upper()
        f_root = ET.fromstring(xml_interno.text)

        nodo_id = f_root.find('./cbc:ID', ns)
        if nodo_id is not None and nodo_id.text:
            num_factura = nodo_id.text.strip()
        else:
            requiere_revision = True

        nodo_cufe = f_root.find('.//cbc:UUID', ns)
        if nodo_cufe is not None and nodo_cufe.text:
            cufe = nodo_cufe.text.strip()
        else:
            requiere_revision = True

        # Prevenir duplicados lógicos (Idempotencia)
        cursor.execute(
            "SELECT 1 FROM fact_facturas WHERE cufe = %s OR num_factura_estacion = %s",
            (cufe, num_factura)
        )
        if cursor.fetchone():
            return ("SKIP", num_factura, "Duplicado", "")

        tipo_doc = "Nota de Credito" if "CreditNote" in f_root.tag else "Factura"
        tipo_combustible = (
            "GASOLINA" if "GASOLINA" in texto_xml
            else "DIESEL" if "DIESEL" in texto_xml
            else "NO_ESPECIFICADO"
        )

        # Extracción de placa a través del motor difuso
        for nota in f_root.findall('.//cbc:Note', ns):
            if nota.text:
                res_placa, res_resto = extraer_placa_y_resto(nota.text, placas_autorizadas)
                if res_placa:
                    placa = res_placa
                    observacion_adicional = res_resto
                    break
                else:
                    observacion_adicional = res_resto

        if placa == "SIN_PLACA":
            for desc in f_root.findall('.//cac:Item/cbc:Description', ns):
                if desc.text:
                    res_placa, res_resto = extraer_placa_y_resto(desc.text, placas_autorizadas)
                    if res_placa:
                        placa = res_placa
                        observacion_adicional = res_resto
                        break
                    elif observacion_adicional == "SIN_OBSERVACION":
                        observacion_adicional = res_resto

        # === 1. DATA WRANGLING: Normalización de llaves foráneas ===
        placa = placa.strip().upper()
        if placa == "SIN_PLACA":
            requiere_revision = True

        nodo_fecha = f_root.find('.//cbc:IssueDate', ns)
        if nodo_fecha is not None and nodo_fecha.text:
            fecha = nodo_fecha.text.strip()[:10]
            # === 2. DATA WRANGLING: Llave Temporal (date_key NOT NULL en Supabase) ===
            date_key = int(fecha.replace("-", ""))
        else:
            raise ValueError(f"El XML {num_factura} no contiene cbc:IssueDate válido (date_key NOT NULL).")

        try:
            total_nodo = f_root.find('.//cac:LegalMonetaryTotal/cbc:PayableAmount', ns)
            total = Decimal(total_nodo.text.strip()) if (total_nodo is not None and total_nodo.text) else Decimal('0.00')
        except Exception:
            requiere_revision = True

        try:
            v_unit_nodo = f_root.find('.//cac:Price/cbc:PriceAmount', ns)
            v_unit = Decimal(v_unit_nodo.text.strip()) if (v_unit_nodo is not None and v_unit_nodo.text) else Decimal('0.00')
        except Exception:
            requiere_revision = True

        try:
            gals_nodo = f_root.find('.//cbc:InvoicedQuantity', ns)
            if gals_nodo is None:
                gals_nodo = f_root.find('.//cbc:CreditedQuantity', ns)
            gals = Decimal(gals_nodo.text.strip()) if (gals_nodo is not None and gals_nodo.text) else Decimal('0.00')
        except Exception:
            requiere_revision = True

        if tipo_doc == "Nota de Credito":
            if total > 0:
                total *= Decimal('-1')
            if gals > 0:
                gals *= Decimal('-1')

        # === 3. CONCILIACIÓN 1:1 (Ventana <= 15 días + Prioridad por galones exactos) ===
        if placa != "SIN_PLACA" and fecha and tipo_doc == "Factura":
            query_matching = """
                SELECT v.id_vale, v.id_contrato, v.num_vale_fisico, v.galones_autorizados
                FROM fact_vales v
                LEFT JOIN fact_facturas f ON v.id_vale = f.id_vale
                WHERE UPPER(TRIM(v.placa)) = %s
                  AND f.id_vale IS NULL
                  AND ABS(v.fecha_emision - %s::date) <= %s
                ORDER BY
                    ABS(COALESCE(v.galones_autorizados, 0) - %s) ASC,
                    ABS(v.fecha_emision - %s::date) ASC,
                    v.id_vale ASC
                LIMIT 1;
            """
            cursor.execute(query_matching, (placa, fecha, VENTANA_DIAS_CONCILIACION, gals, fecha))
            match = cursor.fetchone()

            if match:
                id_vale, id_contrato, num_vale_fisico, gals_aut = match
                etiqueta_vale = num_vale_fisico if num_vale_fisico else "S/N"
                gals_aut_dec = Decimal(str(gals_aut)) if gals_aut is not None else Decimal('0.00')

                if gals_aut is not None and (gals - gals_aut_dec) > TOLERANCIA_GALONES:
                    requiere_revision = True
                    observacion_adicional = (
                        f"ALERTA SOBRECOSTO: Cobrados {gals} gal vs Autorizados {gals_aut_dec} gal. "
                        f"CONCILIADO CON VALE #{etiqueta_vale}. {observacion_adicional}"
                    )
                else:
                    observacion_adicional = f"CONCILIADO CON VALE #{etiqueta_vale}. {observacion_adicional}"
            else:
                requiere_revision = True
                observacion_adicional = (
                    f"ALERTA: Vale físico no encontrado (ventana <= {VENTANA_DIAS_CONCILIACION} días). "
                    f"{observacion_adicional}"
                )

        # === 4. IMPUTACIÓN PRESUPUESTAL MULTICONTRATO (Prioridad 2: Ventana Temporal) ===
        if id_contrato is None and placa != "SIN_PLACA":
            id_contrato, en_ventana = resolver_contrato_por_ventana(cursor, placa, fecha)
            if id_contrato and not en_ventana:
                requiere_revision = True
                observacion_adicional = f"ALERTA: Contrato asignado por respaldo fuera de ventana. {observacion_adicional}"

        id_f = str(uuid.uuid4())

        # === 5. INSERCIÓN CON CUMPLIMIENTO ESTRICTO DE ESQUEMA ===
        cursor.execute("""
            INSERT INTO fact_facturas
            (id_factura, cufe, message_id, num_factura_estacion, fecha_factura, date_key,
             placa, galones_cobrados, total_cobrado, tipo_documento, valor_unitario,
             tipo_combustible, requiere_revision, obs_adc, id_vale, id_contrato)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            id_f, cufe[:250] if cufe else None, message_id, num_factura, fecha, date_key,
            placa, gals, total, tipo_doc, v_unit, tipo_combustible,
            requiere_revision, observacion_adicional[:500], id_vale, id_contrato
        ))

        conn.commit()
        if tipo_doc == "Nota de Credito":
            estado = "🧾 Nota de Crédito"
        elif requiere_revision:
            estado = "⚠️ Revisión (Sin Vale/Sobrecosto)"
        else:
            estado = "✅ Conciliado"
        return ("OK", num_factura, estado, f"${total:,.2f}")

    except Exception as e:
        conn.rollback()
        # === 6. DEAD-LETTER QUEUE: El correo queda UNSEEN para diagnóstico ===
        return ("ERROR", num_factura, "Fallo Estructural XML o Integridad", str(e))


def procesar_facturas():
    console.print(Panel.fit(
        "[bold blue]🤖 ROBOT FINANCIERO CLOUD[/bold blue]",
        subtitle=f"Conciliación 1:1 (<= {VENTANA_DIAS_CONCILIACION} días) + Imputación Multicontrato"
    ))

    if not all([os.getenv('DB_HOST'), os.getenv('EMAIL_PASS')]):
        console.print("[bold red]❌ Error: Faltan credenciales en la configuración[/bold red]")
        sys.exit(1)

    try:
        with psycopg2.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT placa FROM dim_vehiculos WHERE placa IS NOT NULL")
                placas_autorizadas = [row[0].strip().upper() for row in cursor.fetchall()]

                mail, ids = conectar_gmail()

                if not ids:
                    console.print("[yellow]📭 No hay correos nuevos para procesar en la bandeja.[/yellow]")
                else:
                    table = Table(title="Procesando documentos en la nube")
                    table.add_column("Factura", style="cyan")
                    table.add_column("Auditoría", style="bold")
                    table.add_column("Total")

                    for num in track(ids, description="Extrayendo y blindando transacciones..."):
                        _, data = mail.fetch(num, "(BODY.PEEK[])")
                        msg = email.message_from_bytes(data[0][1])

                        # FILTRO DEFENSIVO EN PYTHON: Decodificación MIME + Remitentes Oficiales + Estructura DIAN
                        remitente_correo = decodificar_encabezado_mime(msg.get('From', '')).lower()
                        asunto_correo = decodificar_encabezado_mime(msg.get('Subject', '')).upper()

                        es_remitente_oficial = any(rem in remitente_correo for rem in REMITENTES_OFICIALES if rem)
                        match_dian = REGEX_ASUNTO_DIAN.search(asunto_correo)
                        es_asunto_valido = (
                            bool(match_dian)
                            or (NIT_PROVEEDOR in asunto_correo)
                            or (ASUNTO_ESTACION.upper() in asunto_correo)
                        )

                        if not (es_remitente_oficial and es_asunto_valido):
                            continue

                        message_id = msg.get('Message-ID', 'SIN_ID_CORREO')
                        procesado_con_exito = False
                        hubo_error_xml = False

                        try:
                            with tempfile.TemporaryDirectory() as DIR_TEMP:
                                contador_zip = 0
                                for part in msg.walk():
                                    nombre_archivo = part.get_filename()
                                    if nombre_archivo:
                                        nombre_archivo = decodificar_encabezado_mime(nombre_archivo)
                                    if nombre_archivo and nombre_archivo.lower().endswith('.zip'):
                                        contador_zip += 1
                                        sub_dir = os.path.join(DIR_TEMP, f"zip_{contador_zip}")
                                        os.makedirs(sub_dir, exist_ok=True)

                                        nombre_seguro = os.path.basename(nombre_archivo)
                                        ruta_zip = sanitizar_ruta(sub_dir, nombre_seguro)
                                        with open(ruta_zip, 'wb') as f:
                                            f.write(part.get_payload(decode=True))

                                        with zipfile.ZipFile(ruta_zip, 'r') as z:
                                            limite_seguridad = 0
                                            for miembro in z.infolist():
                                                limite_seguridad += miembro.file_size
                                                if limite_seguridad > 20 * 1024 * 1024:
                                                    raise ValueError("Posible ZIP Bomb: Tamaño excede límite seguro.")

                                                sanitizar_ruta(sub_dir, miembro.filename)
                                                z.extract(miembro, sub_dir)

                                        xmls = glob.glob(os.path.join(sub_dir, '*.xml'))

                                        for xml_doc in xmls:
                                            res = procesar_xml_blindado(
                                                xml_doc, message_id, cursor, conn, placas_autorizadas
                                            )
                                            if res[0] in ["OK", "ERROR"]:
                                                color = (
                                                    "[yellow]" if "Revisión" in res[2]
                                                    else "[red]" if "Fallo" in res[2]
                                                    else "[magenta]" if "Nota" in res[2]
                                                    else "[green]"
                                                )
                                                table.add_row(res[1], f"{color}{res[2]}[/]", res[3])
                                            elif res[0] == "SKIP":
                                                table.add_row(res[1], "[bold cyan]⏭️ Omitido[/bold cyan]", "N/A")

                                            if res[0] == "ERROR":
                                                hubo_error_xml = True
                                            else:
                                                procesado_con_exito = True

                        except Exception as e_correo:
                            console.print(f"[bold red]⚠️ Error procesando el correo {message_id}: {e_correo}[/bold red]")
                            continue

                        # Solo marcamos como leído si todos los XML del correo se procesaron sin error estructural
                        if procesado_con_exito and not hubo_error_xml:
                            mail.store(num, '+FLAGS', '\\Seen')

                    console.print(table)
                    console.print("\n[bold green]💾 Operación finalizada. Matriz financiera asegurada en PostgreSQL.[/bold green]")

            mail.logout()

    except psycopg2.Error as e_db:
        console.print(f"[bold red]❌ Error fatal de base de datos: {e_db}[/bold red]")
        sys.exit(1)
    except Exception as e_gral:
        console.print(f"[bold red]❌ Error Crítico del Sistema: {e_gral}[/bold red]")
        sys.exit(1)


if __name__ == "__main__":
    procesar_facturas()