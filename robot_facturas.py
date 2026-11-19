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

# Limpieza estricta de variables de entorno
REMITENTE_ESTACION = os.getenv('REMITENTE_ESTACION', 'factura@transaccionesco.siigo.net').strip()
ASUNTO_ESTACION = os.getenv('ASUNTO_ESTACION', 'EL PLACER LTDA').strip()
VENTANA_DIAS_CONCILIACION = int(os.getenv('VENTANA_DIAS_CONCILIACION', '15'))

# Remitentes autorizados (Estación/Siigo directo o reenvíos oficiales de la Alcaldía)
REMITENTES_AUTORIZADOS = [
    REMITENTE_ESTACION.lower(),
    "siigo.net",
    "funes-narino.gov.co"
]

# Catálogo global de alias calibrado (separa PODA CESPED de GUADAÑA e incluye errores reales de isleros)
MAQUINARIA_ESPECIAL = {
    "EXCAVADORA LLANTAS": [
        "CX130", "CX 130", "LLANTAS", "CX-130",
        "EXCAVADORA DE LLANTAS CX130B", "RETRO DE LLANTAS CX130B"
    ],
    "EXCAVADORA ORUGAS": [
        "ORUGAS", "ORUGA", "URUGAS", "RETROURUGAS",
        "RETROEXCAVADORA", "RETRO DE ORUGAS", "EXCAVADORA DE ORUGAS"
    ],
    "TRACTOR": ["TRACTOR", "AGROLUX", "FARH", "FAHR", "AGROLUZ"],
    "PODA CESPED": ["PODA CESPED", "PODA", "CESPED"],
    "GUADAÑA": ["GUADAÑA", "GUAADAÑA", "GUADANA", "ROCERIA", "VIVERO"],
    "MOTONIVELADORA": ["MOTONIVELADORA", "MOTO NIVELADORA", "NIVELADORA"],
    "VIBROCOMPACTADOR": [
        "VIBROCOMPACTADOR", "VIBRO COMPACTADOR", "VIBRO",
        "VIVROCOMPACTADOR", "VIVRO"
    ],
    "NC6574": ["NC6574", "NCG574", "NCG 574"]
}


def conectar_gmail():
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(EMAIL_USER, EMAIL_PASS)
    mail.select("inbox")
    _, mensajes = mail.search(None, 'UNSEEN')
    ids = mensajes[0].split() if mensajes[0] else []
    return mail, ids


def decodificar_texto_mime(texto_crudo):
    """Decodifica encabezados MIME (asuntos y nombres de adjuntos con UTF-8 o Fwd:)."""
    if not texto_crudo:
        return ""
    partes = decode_header(texto_crudo)
    resultado = []
    for parte, enc in partes:
        if isinstance(parte, bytes):
            resultado.append(parte.decode(enc or 'utf-8', errors='ignore'))
        else:
            resultado.append(str(parte))
    return "".join(resultado)


def sanitizar_ruta(ruta_base, nombre_archivo):
    """Escudo contra vulnerabilidad Zip Slip."""
    base_abs = os.path.abspath(ruta_base)
    ruta_absoluta = os.path.abspath(os.path.join(base_abs, nombre_archivo))
    if os.path.commonpath([base_abs, ruta_absoluta]) != base_abs:
        raise ValueError(f"Intento de extracción maliciosa (Zip Slip) detectado: {nombre_archivo}")
    return ruta_absoluta


def extraer_placa_y_resto(texto, placas_autorizadas):
    """Identifica maquinaria pesada o placas estándar/moto con Fuzzy Matching (cutoff=0.80)."""
    texto_limpio = texto.upper()

    # 1. Buscar maquinaria especial
    for nombre_oficial, variantes in MAQUINARIA_ESPECIAL.items():
        for v in variantes:
            if v in texto_limpio:
                if nombre_oficial in placas_autorizadas:
                    resto = texto_limpio.replace(v, "").strip()
                    resto = re.sub(r'^[\s\-:]+|[\s\-:]+$', '', resto)
                    return nombre_oficial, resto if resto else "SIN_OBSERVACION"

    # 2. Buscar placa estándar (ABC123) o motocicleta (ABC12D)
    match = re.search(r'([A-Z]{3}[\s-]?\d{2}[A-Z0-9])', texto_limpio)
    if match:
        placa_extraida = match.group(1).replace("-", "").replace(" ", "")
        coincidencias = difflib.get_close_matches(placa_extraida, placas_autorizadas, n=1, cutoff=0.80)
        if coincidencias:
            placa_oficial = coincidencias[0]
            resto = texto_limpio.replace(match.group(1), "").strip()
            resto = re.sub(r'^[\s\-:]+|[\s\-:]+$', '', resto)
            return placa_oficial, resto if resto else "SIN_OBSERVACION"

    return None, texto_limpio.strip() if texto_limpio.strip() else "SIN_OBSERVACION"


def resolver_contrato_por_ventana(cursor, placa, fecha):
    """
    Busca el contrato de la bolsa del vehículo cuya ventana [fecha_inicio, fecha_fin_real]
    cubra fecha_factura. Si no cae en ninguna ventana cerrada, usa el contrato abierto activo.
    """
    if not placa or placa == "SIN_PLACA" or not fecha:
        return None

    cursor.execute(
        """
        SELECT c.id_contrato
        FROM public.dim_vehiculos v
        JOIN public.dim_contratos c
          ON UPPER(TRIM(v.bolsa)) = UPPER(TRIM(c.bolsa))
        WHERE UPPER(TRIM(v.placa)) = UPPER(TRIM(%s))
          AND %s::date >= c.fecha_inicio
          AND (c.fecha_fin_real IS NULL OR %s::date <= c.fecha_fin_real)
        ORDER BY c.fecha_inicio DESC
        LIMIT 1;
        """,
        (placa, fecha, fecha)
    )
    fila = cursor.fetchone()
    if fila:
        return fila[0]

    # Respaldo: contrato abierto activo de esa bolsa
    cursor.execute(
        """
        SELECT c.id_contrato
        FROM public.dim_vehiculos v
        JOIN public.dim_contratos c
          ON UPPER(TRIM(v.bolsa)) = UPPER(TRIM(c.bolsa))
        WHERE UPPER(TRIM(v.placa)) = UPPER(TRIM(%s))
          AND c.fecha_fin_real IS NULL
        ORDER BY c.fecha_inicio DESC
        LIMIT 1;
        """,
        (placa,)
    )
    fila_abierta = cursor.fetchone()
    return fila_abierta[0] if fila_abierta else None


def buscar_factura_origen_nc(cursor, num_factura_ref):
    """Busca la factura original que anula o ajusta una Nota de Crédito para heredar placa y contrato."""
    if not num_factura_ref:
        return None, None
    cursor.execute(
        """
        SELECT placa, id_contrato
        FROM public.fact_facturas
        WHERE UPPER(REPLACE(num_factura_estacion, ' ', '')) = %s
          AND tipo_documento = 'Factura'
        LIMIT 1;
        """,
        (num_factura_ref,)
    )
    fila = cursor.fetchone()
    return (fila[0], fila[1]) if fila else (None, None)


def procesar_xml_blindado(ruta_xml, message_id, cursor, conn, placas_autorizadas):
    ns = {
        'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
        'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2'
    }

    requiere_revision = False
    num_factura = f"SIN_NUM_{uuid.uuid4().hex[:5]}"
    num_factura_ref = None
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

        # 1. Identificadores fiscales
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

        # Idempotencia (evitar duplicados)
        cursor.execute(
            "SELECT 1 FROM public.fact_facturas WHERE cufe = %s OR num_factura_estacion = %s",
            (cufe, num_factura)
        )
        if cursor.fetchone():
            return ("SKIP", num_factura, "Duplicado", "")

        # 2. Clasificación de documento y combustible
        es_nota_credito = (
            "CreditNote" in f_root.tag
            or "NOTA CRÉDITO" in texto_xml
            or "NOTA CREDITO" in texto_xml
        )
        tipo_doc = "Nota de Credito" if es_nota_credito else "Factura"
        tipo_combustible = (
            "GASOLINA" if "GASOLINA" in texto_xml
            else "DIESEL" if "DIESEL" in texto_xml
            else "NO_ESPECIFICADO"
        )

        # 3. Protección date_key NOT NULL
        nodo_fecha = f_root.find('./cbc:IssueDate', ns)
        if nodo_fecha is None:
            nodo_fecha = f_root.find('.//cbc:IssueDate', ns)

        if nodo_fecha is not None and nodo_fecha.text:
            fecha = nodo_fecha.text.strip()
            date_key = int(fecha.replace("-", ""))
        else:
            raise ValueError(f"Documento {num_factura} sin nodo cbc:IssueDate (date_key NOT NULL).")

        # 4. Extracción financiera con precisión Decimal
        try:
            total_nodo = f_root.find('.//cac:LegalMonetaryTotal/cbc:PayableAmount', ns)
            if total_nodo is not None and total_nodo.text:
                total = Decimal(total_nodo.text.strip())
            else:
                requiere_revision = True
        except Exception:
            requiere_revision = True

        try:
            v_unit_nodo = f_root.find('.//cac:Price/cbc:PriceAmount', ns)
            if v_unit_nodo is not None and v_unit_nodo.text:
                v_unit = Decimal(v_unit_nodo.text.strip())
        except Exception:
            requiere_revision = True

        try:
            gals_nodo = f_root.find('.//cbc:InvoicedQuantity', ns)
            if gals_nodo is None:
                gals_nodo = f_root.find('.//cbc:CreditedQuantity', ns)
            if gals_nodo is not None and gals_nodo.text:
                gals = Decimal(gals_nodo.text.strip())
            else:
                requiere_revision = True
        except Exception:
            requiere_revision = True

        # Garantizar signo negativo en Notas de Crédito para que descuenten en todas las bolsas
        if tipo_doc != "Factura":
            total = -abs(total)
            gals = -abs(gals)

        # 5. Extracción de placa (Notas primero, luego Descripción de ítem)
        for nota in f_root.findall('.//cbc:Note', ns):
            if nota.text:
                res_placa, res_resto = extraer_placa_y_resto(nota.text, placas_autorizadas)
                if res_placa:
                    placa = res_placa
                    observacion_adicional = res_resto
                    break
                elif observacion_adicional == "SIN_OBSERVACION":
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

        placa = placa.strip().upper()

        # 6. Lógica diferenciada: NOTA DE CRÉDITO vs FACTURA
        if tipo_doc != "Factura":
            # Extraer factura original referenciada en el XML UBL 2.1 DIAN
            ref_nodo = f_root.find('.//cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID', ns)
            if ref_nodo is not None and ref_nodo.text:
                num_factura_ref = ref_nodo.text.strip().replace(" ", "").upper()
                placa_orig, contrato_orig = buscar_factura_origen_nc(cursor, num_factura_ref)
                if placa_orig:
                    placa = placa_orig
                    id_contrato = contrato_orig
                    requiere_revision = False
                    observacion_adicional = (
                        f"NC IMPUTADA A FACTURA {num_factura_ref} ({placa_orig}). "
                        + observacion_adicional
                    )

            # Si no se encontró por referencia pero sí tiene placa real, resolver por ventana de contrato
            if id_contrato is None and placa != "SIN_PLACA":
                id_contrato = resolver_contrato_por_ventana(cursor, placa, fecha)

            # Si sigue SIN_PLACA, NO se fuerza a OFICIALES: queda en NULL para revisión auditada
            if placa == "SIN_PLACA" or id_contrato is None:
                requiere_revision = True
                observacion_adicional = (
                    "PENDIENTE IMPUTACIÓN NC (SIN FACTURA ORIGEN/PLACA). "
                    + observacion_adicional
                )

        else:
            # === CONCILIACIÓN 1:1 (SOLO PARA FACTURAS) ===
            if placa == "SIN_PLACA":
                requiere_revision = True
            else:
                query_matching = """
                    SELECT v.id_vale, v.num_vale_fisico, v.id_contrato, v.galones_autorizados
                    FROM public.fact_vales v
                    LEFT JOIN public.fact_facturas f ON v.id_vale = f.id_vale
                    WHERE UPPER(TRIM(v.placa)) = UPPER(TRIM(%s))
                      AND f.id_vale IS NULL
                      AND ABS(v.fecha_emision - %s::date) <= %s
                    ORDER BY ABS(COALESCE(v.galones_autorizados, 0) - %s) ASC,
                             ABS(v.fecha_emision - %s::date) ASC
                    LIMIT 1;
                """
                cursor.execute(
                    query_matching,
                    (placa, fecha, VENTANA_DIAS_CONCILIACION, gals, fecha)
                )
                match = cursor.fetchone()

                if match:
                    id_vale, num_vale_fisico, id_contrato, gals_aut = match
                    etiqueta_vale = f"CONCILIADO CON VALE #{num_vale_fisico or id_vale}"
                    if gals_aut is not None and gals > Decimal(str(gals_aut)):
                        requiere_revision = True
                        observacion_adicional = (
                            f"ALERTA SOBRECOSTO ({etiqueta_vale}): Cobrados {gals} gal vs "
                            f"Autorizados {gals_aut} gal. {observacion_adicional}"
                        )
                    else:
                        observacion_adicional = f"{etiqueta_vale}. {observacion_adicional}"
                else:
                    requiere_revision = True
                    id_contrato = resolver_contrato_por_ventana(cursor, placa, fecha)
                    observacion_adicional = (
                        f"ALERTA: Vale físico no encontrado (ventana <= {VENTANA_DIAS_CONCILIACION} días). "
                        + observacion_adicional
                    )

        # 7. Inserción con cumplimiento estricto de esquema
        id_f = str(uuid.uuid4())
        cursor.execute(
            """
            INSERT INTO public.fact_facturas (
                id_factura, cufe, message_id, num_factura_estacion, fecha_factura, date_key,
                placa, galones_cobrados, total_cobrado, tipo_documento, valor_unitario,
                tipo_combustible, requiere_revision, obs_adc, id_vale, id_contrato,
                num_factura_referencia
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                id_f,
                cufe[:250] if cufe else None,
                message_id,
                num_factura,
                fecha,
                date_key,
                placa,
                gals,
                total,
                tipo_doc,
                v_unit,
                tipo_combustible,
                requiere_revision,
                observacion_adicional[:500],
                id_vale,
                id_contrato,
                num_factura_ref
            )
        )

        conn.commit()
        if tipo_doc != "Factura":
            estado = "⚠️ NC Pendiente Imputar" if requiere_revision else "✅ NC Imputada"
        else:
            estado = "⚠️ Revisión (Sin Vale/Sobrecosto)" if requiere_revision else "✅ Conciliado"

        return ("OK", num_factura, estado, f"${total:,.2f}")

    except Exception as e:
        conn.rollback()
        return ("ERROR", num_factura, "Fallo Estructural XML o Integridad", str(e))


def procesar_facturas():
    console.print(
        Panel.fit(
            "[bold blue]🤖 ROBOT FINANCIERO CLOUD[/bold blue]",
            subtitle="Conciliación 1:1 + Imputación Multicontrato + Notas de Crédito UBL 2.1"
        )
    )

    if not all([os.getenv('DB_HOST'), os.getenv('EMAIL_PASS')]):
        console.print("[bold red]❌ Error: Faltan credenciales en la configuración (.env / Secrets)[/bold red]")
        sys.exit(1)

    try:
        with psycopg2.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT placa FROM public.dim_vehiculos WHERE placa IS NOT NULL")
                placas_autorizadas = [row[0].strip().upper() for row in cursor.fetchall()]

                mail, ids = conectar_gmail()
                try:
                    if not ids:
                        console.print("[yellow]📭 No hay correos nuevos para procesar en la bandeja.[/yellow]")
                    else:
                        table = Table(title="Procesando documentos DIAN en la nube")
                        table.add_column("Documento", style="cyan")
                        table.add_column("Auditoría", style="bold")
                        table.add_column("Total")

                        for num in track(ids, description="Extrayendo y blindando transacciones..."):
                            _, data = mail.fetch(num, "(BODY.PEEK[])")
                            msg = email.message_from_bytes(data[0][1])

                            asunto_correo = decodificar_texto_mime(msg.get('Subject', '')).upper()
                            remitente_correo = msg.get('From', '').lower()

                            es_remitente_valido = any(
                                r in remitente_correo for r in REMITENTES_AUTORIZADOS if r
                            )
                            if ASUNTO_ESTACION.upper() not in asunto_correo or not es_remitente_valido:
                                continue

                            message_id = msg.get('Message-ID', 'SIN_ID_CORREO')
                            procesados_en_correo = 0
                            hubo_error_en_correo = False

                            try:
                                with tempfile.TemporaryDirectory() as dir_temp:
                                    idx_zip = 0
                                    for part in msg.walk():
                                        nombre_raw = part.get_filename()
                                        nombre_archivo = decodificar_texto_mime(nombre_raw) if nombre_raw else ""

                                        if nombre_archivo.lower().endswith('.zip'):
                                            idx_zip += 1
                                            sub_dir = os.path.join(dir_temp, f"zip_{idx_zip}")
                                            os.makedirs(sub_dir, exist_ok=True)

                                            ruta_zip = sanitizar_ruta(sub_dir, os.path.basename(nombre_archivo))
                                            with open(ruta_zip, 'wb') as f:
                                                f.write(part.get_payload(decode=True))

                                            with zipfile.ZipFile(ruta_zip, 'r') as z:
                                                limite_seguridad = 0
                                                for miembro in z.infolist():
                                                    limite_seguridad += miembro.file_size
                                                    if limite_seguridad > 20 * 1024 * 1024:
                                                        raise ValueError("Posible ZIP Bomb: Tamaño excede 20 MB.")
                                                    sanitizar_ruta(sub_dir, miembro.filename)
                                                    z.extract(miembro, sub_dir)

                                            xmls = glob.glob(os.path.join(sub_dir, '**', '*.xml'), recursive=True)
                                            for xml_doc in xmls:
                                                res = procesar_xml_blindado(
                                                    xml_doc, message_id, cursor, conn, placas_autorizadas
                                                )
                                                if res[0] in ["OK", "ERROR"]:
                                                    color = (
                                                        "[yellow]" if "Revisión" in res[2] or "Pendiente" in res[2]
                                                        else "[red]" if "Fallo" in res[2]
                                                        else "[green]"
                                                    )
                                                    table.add_row(res[1], f"{color}{res[2]}[/]", res[3])
                                                elif res[0] == "SKIP":
                                                    table.add_row(res[1], "[bold cyan]⏭️ Omitido[/bold cyan]", "N/A")

                                                if res[0] == "ERROR":
                                                    hubo_error_en_correo = True
                                                else:
                                                    procesados_en_correo += 1

                            except Exception as e_correo:
                                console.print(f"[bold red]⚠️ Error procesando el correo {message_id}: {e_correo}[/bold red]")
                                hubo_error_en_correo = True
                                continue

                            # Dead-Letter Queue: solo marca \Seen si TODOS los XML del correo quedaron sanos
                            if procesados_en_correo > 0 and not hubo_error_en_correo:
                                mail.store(num, '+FLAGS', '\\Seen')

                        console.print(table)
                        console.print(
                            "\n[bold green]💾 Operación finalizada. Matriz financiera asegurada en PostgreSQL.[/bold green]"
                        )
                finally:
                    mail.logout()

    except psycopg2.Error as e_db:
        console.print(f"[bold red]❌ Error fatal de base de datos: {e_db}[/bold red]")
        sys.exit(1)
    except Exception as e_gral:
        console.print(f"[bold red]❌ Error Crítico del Sistema: {e_gral}[/bold red]")
        sys.exit(1)


if __name__ == "__main__":
    procesar_facturas()