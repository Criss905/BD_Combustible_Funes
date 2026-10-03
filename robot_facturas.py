import os
import imaplib
import email
import zipfile
import glob
import psycopg2
import uuid
import re
import difflib
import sys
from decimal import Decimal
from defusedxml import ElementTree as ET
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import track

# ==========================================
# 1. CONFIGURACIÓN CLOUD NATIVE
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
DIR_TEMP = './Temp_Robot'

def conectar_gmail():
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(EMAIL_USER, EMAIL_PASS)
    mail.select("inbox")
    
    asunto_busqueda = os.getenv('ASUNTO_ESTACION', 'EL PLACER LTDA')
    _, mensajes = mail.search(None, f'(UNSEEN SUBJECT "{asunto_busqueda}")')
    ids = mensajes[0].split() if mensajes[0] else []
    return mail, ids

# ==========================================
# CAMBIO APLICADO: Protección Zip Slip con commonpath
# ==========================================
def sanitizar_ruta(ruta_base, nombre_archivo):
    base_abs = os.path.abspath(ruta_base)
    ruta_absoluta = os.path.abspath(os.path.join(base_abs, nombre_archivo))
    
    # Comprueba que la ruta resultante comparta el mismo ancestro jerárquico real
    if os.path.commonpath([base_abs, ruta_absoluta]) != base_abs:
        raise ValueError(f"Intento de extracción maliciosa (Zip Slip) detectado: {nombre_archivo}")
    return ruta_absoluta

def procesar_xml_blindado(ruta_xml, message_id, cursor, conn, placas_autorizadas):
    ns = {'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
          'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2'}
    
    requiere_revision = False
    num_factura = f"SIN_NUM_{uuid.uuid4().hex[:5]}"
    fecha = None
    cufe = "NO_DISPONIBLE"
    placa = "SIN_PLACA"
    observacion_adicional = "SIN_OBSERVACION"
    gals = None
    total = None
    v_unit = None
    tipo_combustible = "NO_ESPECIFICADO"
    tipo_doc = "Factura"

    try:
        tree = ET.parse(ruta_xml)
        root = tree.getroot()
        
        xml_interno = root.find('.//cac:Attachment/cac:ExternalReference/cbc:Description', ns)
        if xml_interno is None: 
            raise ValueError("El XML no contiene un documento UBL adjunto válido.")
        
        texto_xml = xml_interno.text.upper()
        f_root = ET.fromstring(xml_interno.text)
        
        nodo_id = f_root.find('.//cbc:ID', ns)
        if nodo_id is not None: num_factura = nodo_id.text
        else: requiere_revision = True

        nodo_cufe = f_root.find('.//cbc:UUID', ns)
        if nodo_cufe is not None: cufe = nodo_cufe.text
        else: requiere_revision = True

        cursor.execute("SELECT 1 FROM fact_facturas WHERE num_factura_estacion = %s", (num_factura,))
        if cursor.fetchone(): 
            return ("SKIP", num_factura, "Duplicado", "")

        tipo_combustible = "GASOLINA" if "GASOLINA" in texto_xml else "DIESEL" if "DIESEL" in texto_xml else "NO_ESPECIFICADO"
        tipo_doc = "Nota de Credito" if "NOTA CRÉDITO" in texto_xml or "NOTA CREDITO" in texto_xml else "Factura"
        
        maquinaria_especial = {
            "EXCAVADORA LLANTAS": ["CX130", "CX 130", "LLANTAS", "CX-130", "EXCAVADORA DE LLANTAS CX130B", "RETRO DE LLANTAS CX130B"],
            "EXCAVADORA ORUGAS": ["ORUGAS", "ORUGA", "RETROEXCAVADORA", "RETRO DE ORUGAS", "EXCAVADORA DE ORUGAS"],
            "TRACTOR": ["TRACTOR", "AGROLUX", "FARH", "FAHR", "AGROLUZ"],
            "GUADAÑA": ["GUADAÑA", "PODA", "CESPED", "ROCERIA", "VIVERO"],
            "MOTONIVELADORA": ["MOTONIVELADORA", "MOTO NIVELADORA", "NIVELADORA"],
            "VIBROCOMPACTADOR": ["VIBROCOMPACTADOR", "VIBRO COMPACTADOR", "VIBRO"],
            "NC6574": ["NC6574", "NCG574", "NCG 574"]
        }

        def extraer_placa_y_resto(texto):
            texto_limpio = texto.upper()
            for nombre_oficial, variantes in maquinaria_especial.items():
                for v in variantes:
                    if v in texto_limpio:
                        if nombre_oficial in placas_autorizadas:
                            resto = texto_limpio.replace(v, "").strip()
                            resto = re.sub(r'^[\s\-:]+|[\s\-:]+$', '', resto)
                            return nombre_oficial, resto if resto else "SIN_OBSERVACION"
            
            match = re.search(r'([A-Z]{3}[\s-]?\d{2}[A-Z0-9])', texto_limpio)
            if match:
                placa_extraida = match.group(1).replace("-", "").replace(" ", "")
                coincidencias = difflib.get_close_matches(placa_extraida, placas_autorizadas, n=1, cutoff=0.75)
                
                if coincidencias:
                    placa_oficial = coincidencias[0]
                    resto = texto_limpio.replace(match.group(1), "").strip()
                    resto = re.sub(r'^[\s\-:]+|[\s\-:]+$', '', resto)
                    return placa_oficial, resto if resto else "SIN_OBSERVACION"
                
            return None, texto_limpio.strip() if texto_limpio.strip() else "SIN_OBSERVACION"

        for nota in f_root.findall('.//cbc:Note', ns):
            if nota.text:
                res_placa, res_resto = extraer_placa_y_resto(nota.text)
                if res_placa:
                    placa = res_placa
                    observacion_adicional = res_resto
                    break
                else:
                    observacion_adicional = res_resto
        
        if placa == "SIN_PLACA":
            for desc in f_root.findall('.//cac:Item/cbc:Description', ns):
                if desc.text:
                    res_placa, res_resto = extraer_placa_y_resto(desc.text)
                    if res_placa:
                        placa = res_placa
                        observacion_adicional = res_resto
                        break
                    elif observacion_adicional == "SIN_OBSERVACION":
                        observacion_adicional = res_resto
        
        if placa == "SIN_PLACA" and "SIN_PLACA" not in placas_autorizadas:
            requiere_revision = True

        nodo_fecha = f_root.find('.//cbc:IssueDate', ns)
        if nodo_fecha is not None: fecha = nodo_fecha.text
        else: requiere_revision = True
            
        try:
            total_nodo = f_root.find('.//cac:LegalMonetaryTotal/cbc:PayableAmount', ns)
            total = Decimal(total_nodo.text) if total_nodo is not None else None
            if total is None: requiere_revision = True
        except: requiere_revision = True
            
        try:
            v_unit_nodo = f_root.find('.//cac:Price/cbc:PriceAmount', ns)
            v_unit = Decimal(v_unit_nodo.text) if v_unit_nodo is not None else None
        except: requiere_revision = True
            
        try:
            gals_nodo = f_root.find('.//cbc:InvoicedQuantity', ns)
            if gals_nodo is None: gals_nodo = f_root.find('.//cbc:CreditedQuantity', ns)
            gals = Decimal(gals_nodo.text) if gals_nodo is not None else None
        except: requiere_revision = True

        if tipo_doc == "Nota de Credito":
            if total and total > 0: total *= Decimal('-1')
            if gals and gals > 0: gals *= Decimal('-1')

        id_f = str(uuid.uuid4())
        
        cursor.execute("""
            INSERT INTO fact_facturas 
            (id_factura, cufe, message_id, num_factura_estacion, fecha_factura, placa, galones_cobrados, total_cobrado, tipo_documento, valor_unitario, tipo_combustible, requiere_revision, obs_adc) 
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (id_f, cufe[:250], message_id, num_factura, fecha, placa, gals, total, tipo_doc, v_unit, tipo_combustible, requiere_revision, observacion_adicional[:500]))
        
        conn.commit()
        estado = "⚠️ Revisión (Faltan Datos)" if requiere_revision else "✅ OK"
        return ("OK", num_factura, estado, f"${total:,.2f}" if total else "N/A")

    except Exception as e:
        conn.rollback()
        id_f = str(uuid.uuid4())
        try:
            cursor.execute("""
                INSERT INTO fact_facturas 
                (id_factura, message_id, num_factura_estacion, requiere_revision) 
                VALUES (%s, %s, %s, TRUE)
            """, (id_f, message_id, num_factura))
            conn.commit()
        except:
            conn.rollback()
        return ("ERROR", num_factura, "Fallo Estructural XML", str(e))

def procesar_facturas():
    console.print(Panel.fit("[bold blue]🤖 ROBOT FINANCIERO CLOUD[/bold blue]", subtitle="Fuzzy Matching + Master Data Management"))
    os.makedirs(DIR_TEMP, exist_ok=True)

    if not all([os.getenv('DB_HOST'), os.getenv('EMAIL_PASS')]):
        console.print("[bold red]❌ Error: Faltan credenciales en el archivo .env[/bold red]")
        sys.exit(1)

    try:
        with psycopg2.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT placa FROM dim_vehiculos WHERE placa IS NOT NULL")
                placas_autorizadas = [row[0].upper() for row in cursor.fetchall()]

                mail, ids = conectar_gmail()

                if not ids:
                    console.print("[yellow]📭 No hay correos nuevos para procesar en la bandeja.[/yellow]")
                else:
                    table = Table(title=f"Procesando {len(ids)} documentos en la nube")
                    table.add_column("Factura", style="cyan")
                    table.add_column("Auditoría", style="bold")
                    table.add_column("Total")

                    for num in track(ids, description="Extrayendo y blindando transacciones..."):
                        _, data = mail.fetch(num, "(RFC822)")
                        msg = email.message_from_bytes(data[0][1])
                        message_id = msg.get('Message-ID', 'SIN_ID_CORREO')

                        try:
                            for part in msg.walk():
                                nombre_archivo = part.get_filename()
                                if nombre_archivo and nombre_archivo.endswith('.zip'):
                                    # Sanitizar el nombre del propio archivo ZIP
                                    ruta_zip = sanitizar_ruta(DIR_TEMP, nombre_archivo)
                                    with open(ruta_zip, 'wb') as f:
                                        f.write(part.get_payload(decode=True))

                                    # ==========================================
                                    # CAMBIO APLICADO: Extracción Segura de cada miembro
                                    # ==========================================
                                    with zipfile.ZipFile(ruta_zip, 'r') as z:
                                        for miembro in z.infolist():
                                            # Se valida estrictamente la ruta de extracción de cada archivo interno
                                            sanitizar_ruta(DIR_TEMP, miembro.filename)
                                            z.extract(miembro, DIR_TEMP)
                                    
                                    xmls = glob.glob(os.path.join(DIR_TEMP, '*.xml'))
                                    if xmls:
                                        res = procesar_xml_blindado(xmls[0], message_id, cursor, conn, placas_autorizadas)
                                        if res[0] in ["OK", "ERROR"]:
                                            color = "[yellow]" if "Revisión" in res[2] else "[red]" if "Fallo" in res[2] else "[green]"
                                            table.add_row(res[1], f"{color}{res[2]}[/]", res[3])
                                        elif res[0] == "SKIP":
                                            table.add_row(res[1], "[bold cyan]⏭️ Omitido[/bold cyan]", "N/A")

                                    mail.store(num, '+FLAGS', '\\Seen')

                        except Exception as e_correo:
                            console.print(f"[bold red]⚠️ Error procesando el correo {message_id}: {e_correo}[/bold red]")
                            continue

                        finally:
                            # Limpieza segura
                            for f in os.listdir(DIR_TEMP):
                                ruta_limpieza = os.path.join(DIR_TEMP, f)
                                if os.path.isfile(ruta_limpieza):
                                    os.remove(ruta_limpieza)

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